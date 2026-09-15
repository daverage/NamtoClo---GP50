#!/usr/bin/env python3
"""
build_namtoclo_teacher_dataset.py

Create the cached NAM-teacher dataset used to develop the clean-sheet
NAM -> GP50 CLO distiller.

This tool crosses:
    1. the open guitar/system-identification corpus created by
       build_namtoclo_research_corpus.py
with
    2. the curated NAM corpus created by
       build_namtoclo_nam_corpus.py

and produces reproducible:
    input -> NAM -> target
pairs using NeuralAmpModelerCore's official offline `render` tool.

Design goals
------------
- Exact playback DSP: preferred renderer is NeuralAmpModelerCore, not a Python
  approximation of A2.
- A2 Full by default (`--slim 1.0` for SlimmableContainer models).
- Deterministic source selection, clipping, level variants and sample-rate
  conversion.
- Raw teacher renders retained at the NAM's native/assumed sample rate.
- Student-ready input/target copies at 44.1 kHz retained without output
  normalisation.
- Every source/model/renderer/output hashed.
- SQLite run journal makes the job safely resumable.
- Development / selection / benchmark roles are preserved.
- Sealed NAMs are NOT rendered unless explicitly requested.

Dependencies
------------
    python3 -m pip install numpy scipy soundfile

System build tools for the preferred NAMCore renderer:
    git, cmake, and a C++ compiler (Xcode command-line tools on macOS)

Typical first run
-----------------
    python3 build_namtoclo_teacher_dataset.py \
        --guitar-corpus ~/NamtoCloResearchCorpus \
        --nam-corpus ~/NamtoCloNAMCorpus \
        --output ~/NamtoCloTeacherDataset \
        --build-namcore \
        --profile full

Inspect only:
    ... --plan-only

Resume:
    Run the same command again. Completed task IDs are skipped.

After the algorithm is frozen:
    rerun with --include-sealed to populate the final NAM generalisation test.

IMPORTANT
---------
This dataset may contain outputs derived from locally downloaded TONE3000 NAMs.
Do not assume that having generated output audio grants permission to redistribute
the original `.nam` files. Preserve the NAM corpus provenance/licence records.
"""

from __future__ import annotations

import argparse
import concurrent.futures as futures
import csv
import dataclasses
from dataclasses import dataclass, asdict
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any, Iterable, Optional, Sequence

try:
    import numpy as np
except ImportError:
    raise SystemExit("Missing numpy. Run: python3 -m pip install numpy scipy soundfile")

try:
    import soundfile as sf
except ImportError:
    raise SystemExit("Missing soundfile. Run: python3 -m pip install numpy scipy soundfile")

try:
    from scipy.signal import resample_poly
except ImportError:
    raise SystemExit("Missing scipy. Run: python3 -m pip install numpy scipy soundfile")


VERSION = 1
DEFAULT_NAMCORE_REF = "v0.5.3"
NAMCORE_REPO = "https://github.com/sdatkinson/NeuralAmpModelerCore.git"
DEFAULT_RENDER_RATE = 48_000
DEFAULT_STUDENT_RATE = 44_100
BASE_GUITAR_PEAK_DBFS = -9.0

PROFILE_SECONDS = {
    # Real guitar/source material before level augmentation.
    "smoke": 2 * 60,
    "standard": 15 * 60,
    "full": 30 * 60,
    # Use every eligible source. This can become enormous.
    "exhaustive": None,
}

PROFILE_LEVEL_FRACTION = {
    "smoke": 0.0,
    "standard": 0.20,
    "full": 0.25,
    "exhaustive": 0.33,
}

ROLE_FRACTIONS = {
    "fit": 0.50,
    "selection": 0.25,
    "benchmark": 0.25,
}

LEVEL_OFFSETS_DB = (-12.0, -6.0, 0.0, 6.0)

AUDIO_EXTS = {".wav", ".flac", ".aif", ".aiff", ".ogg", ".caf"}

# The source corpus builder creates these names.
DATASET_ALIASES = {
    "guitar-techs": "guitar-techs",
    "egfxset": "egfxset",
    "guitarset": "guitarset",
    "freepats": "freepats",
    "emilyguitar": "emilyguitar",
    "shinyguitar": "shinyguitar",
    "black-green": "black-green",
    "synthetic_probes": "synthetic_probes",
}

SYNTHETIC_ROLES = {
    # Diagnostics are allowed in fitting but flagged separately in metadata.
    "00": "fit",
    "01": "fit",
    "02": "fit",
    "03": "benchmark",
    "04": "fit",
    "05": "selection",
    "06": "benchmark",
    "07": "fit",
    "08": "selection",
    "09": "fit",
    "10": "fit",
    "11": "selection",
    "12": "fit",
    "13": "selection",
    "14": "benchmark",
}


@dataclass(frozen=True)
class SourceAudio:
    dataset: str
    path: str
    role: str
    frames: int
    sample_rate: int
    channels: int
    duration_s: float


@dataclass(frozen=True)
class Segment:
    dataset: str
    source_path: str
    role: str
    start_s: float
    duration_s: float
    synthetic: bool = False


@dataclass(frozen=True)
class PreparedInput:
    input_id: str
    dataset: str
    role: str
    source_path: str
    source_sha256: str
    source_sample_rate: int
    start_s: float
    duration_s: float
    target_sample_rate: int
    level_offset_db: float
    base_peak_dbfs: Optional[float]
    peak: float
    rms: float
    path: str
    sha256: str
    synthetic: bool


@dataclass(frozen=True)
class NamModel:
    split: str
    path: str
    sha256: str
    model_name: str
    tone_id: Optional[int]
    model_id: Optional[int]
    architecture: str
    file_version: Optional[str]
    expected_sample_rate: int
    slimmable: bool
    licence: Optional[str]


@dataclass(frozen=True)
class RenderTask:
    task_id: str
    nam: NamModel
    inp: PreparedInput
    renderer_path: str
    renderer_sha256: str
    renderer_commit: str
    slim_value: Optional[float]
    student_rate: int
    raw_output: str
    student_input: str
    student_target: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def short_hash(text: str, n: int = 16) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def json_dump(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def jsonl_write(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def dbamp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def amp_db(x: float) -> float:
    return -math.inf if x <= 0 else 20.0 * math.log10(x)


def human_time(seconds: float) -> str:
    if not math.isfinite(seconds):
        return "unknown"
    hours, rem = divmod(int(round(seconds)), 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {mins:02d}m {secs:02d}s"
    return f"{mins}m {secs:02d}s"


def human_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.1f} {unit}"
        x /= 1024
    return str(n)


def git_output(args: Sequence[str], cwd: Optional[Path] = None) -> str:
    try:
        return subprocess.check_output(
            list(args), cwd=str(cwd) if cwd else None, text=True, stderr=subprocess.STDOUT
        ).strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# NAMCore renderer
# ---------------------------------------------------------------------------

def find_renderer(build_root: Path) -> Optional[Path]:
    names = {"render", "render.exe"}
    candidates = []
    if build_root.exists():
        for p in build_root.rglob("*"):
            if p.is_file() and p.name in names:
                candidates.append(p)
    if not candidates:
        return None
    # Prefer paths containing tools and Release over incidental copies.
    candidates.sort(key=lambda p: (
        "tools" not in str(p).lower(),
        "release" not in str(p).lower(),
        len(str(p))
    ))
    return candidates[0]


def ensure_namcore_renderer(
    output_root: Path,
    explicit_renderer: Optional[str],
    build_namcore: bool,
    namcore_source: Optional[str],
    namcore_ref: str,
    build_jobs: int,
) -> tuple[Path, str]:
    if explicit_renderer:
        p = Path(explicit_renderer).expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"--renderer does not exist: {p}")
        commit = "external"
        return p, commit

    if namcore_source:
        source = Path(namcore_source).expanduser().resolve()
    else:
        source = output_root / "_tools" / "NeuralAmpModelerCore"

    build = source / "build-namtoclo"

    existing = find_renderer(build)
    if existing and not build_namcore:
        commit = git_output(["git", "rev-parse", "HEAD"], cwd=source) or "unknown"
        return existing.resolve(), commit

    if not build_namcore and not existing:
        raise RuntimeError(
            "No NAMCore renderer found.\n"
            "Either pass --renderer /path/to/render, --namcore-source PATH with an existing "
            "build-namtoclo build, or add --build-namcore."
        )

    if shutil.which("git") is None or shutil.which("cmake") is None:
        raise RuntimeError("--build-namcore requires both git and cmake in PATH.")

    source.parent.mkdir(parents=True, exist_ok=True)

    if not source.exists():
        print(f"[NAMCore] cloning {NAMCORE_REPO} @ {namcore_ref}")
        subprocess.run(
            [
                "git", "clone", "--recursive",
                "--branch", namcore_ref,
                "--depth", "1",
                NAMCORE_REPO, str(source),
            ],
            check=True,
        )
    else:
        print(f"[NAMCore] using existing source: {source}")
        if namcore_source is None:
            # This is the checkout owned by this builder, so keep it pinned to
            # the requested ref. Never mutate an explicitly supplied checkout.
            subprocess.run(
                ["git", "fetch", "--tags", "--depth", "1", "origin", namcore_ref],
                cwd=source, check=True
            )
            subprocess.run(
                ["git", "checkout", "--detach", namcore_ref],
                cwd=source, check=True
            )
        subprocess.run(
            ["git", "submodule", "update", "--init", "--recursive"],
            cwd=source, check=True
        )

    commit = git_output(["git", "rev-parse", "HEAD"], cwd=source) or "unknown"
    print(f"[NAMCore] commit {commit}")

    subprocess.run(
        [
            "cmake", "-S", str(source), "-B", str(build),
            "-DCMAKE_BUILD_TYPE=Release",
        ],
        check=True,
    )
    subprocess.run(
        [
            "cmake", "--build", str(build),
            "--config", "Release",
            "--target", "render",
            "--parallel", str(max(1, build_jobs)),
        ],
        check=True,
    )

    renderer = find_renderer(build)
    if renderer is None:
        raise RuntimeError(f"NAMCore built but the render executable was not found below {build}")
    return renderer.resolve(), commit


# ---------------------------------------------------------------------------
# NAM discovery
# ---------------------------------------------------------------------------

def read_nam_header(path: Path) -> tuple[str, Optional[str], int, bool]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise RuntimeError(f"Cannot parse NAM JSON {path}: {e}") from e

    architecture = str(obj.get("architecture", "unknown"))
    version = obj.get("version")
    reported = obj.get("sample_rate")

    if reported is None and architecture == "SlimmableContainer":
        try:
            subs = obj["config"]["submodels"]
            for sub in subs:
                m = sub.get("model", {})
                if m.get("sample_rate") is not None:
                    reported = m["sample_rate"]
                    break
        except Exception:
            pass

    # The official NAM plugin assumes old files without a sample rate are 48 kHz.
    rate = int(round(float(reported))) if reported not in (None, -1, -1.0) else DEFAULT_RENDER_RATE
    return architecture, str(version) if version is not None else None, rate, architecture == "SlimmableContainer"


def discover_nams(
    root: Path,
    include_sealed: bool,
    legacy_limit: int,
    seed: int,
) -> list[NamModel]:
    manifest_path = root / "download_manifest.json"
    manifest = []
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = []

    rows_by_path: dict[str, dict] = {}
    for row in manifest if isinstance(manifest, list) else []:
        p = row.get("path")
        if p:
            rows_by_path[str(p)] = row

    found: list[NamModel] = []

    for split in ("development", "selection", "sealed"):
        if split == "sealed" and not include_sealed:
            continue
        d = root / split
        if not d.exists():
            continue
        for path in sorted(d.rglob("*.nam")):
            rel = str(path.relative_to(root))
            row = rows_by_path.get(rel, {})
            architecture, version, rate, slimmable = read_nam_header(path)
            found.append(NamModel(
                split=split,
                path=str(path.resolve()),
                sha256=sha256_file(path),
                model_name=str(row.get("model_name") or path.stem),
                tone_id=int(row["tone_id"]) if row.get("tone_id") is not None else None,
                model_id=int(row["model_id"]) if row.get("model_id") is not None else None,
                architecture=architecture,
                file_version=version,
                expected_sample_rate=rate,
                slimmable=slimmable,
                licence=row.get("tone_license"),
            ))

    if legacy_limit > 0:
        legacy_root = root / "legacy_a1_gpl"
        legacy = sorted(legacy_root.rglob("*.nam")) if legacy_root.exists() else []
        rng = random.Random(seed ^ 0xA11)
        rng.shuffle(legacy)
        for path in legacy[:legacy_limit]:
            try:
                architecture, version, rate, slimmable = read_nam_header(path)
            except Exception as e:
                print(f"[warn] skipping legacy NAM {path}: {e}")
                continue
            found.append(NamModel(
                split="legacy",
                path=str(path.resolve()),
                sha256=sha256_file(path),
                model_name=path.stem,
                tone_id=None,
                model_id=None,
                architecture=architecture,
                file_version=version,
                expected_sample_rate=rate,
                slimmable=slimmable,
                licence="GPL-3.0 (repository declaration)",
            ))

    if not found:
        raise RuntimeError(f"No NAM files found under {root}")
    return found


# ---------------------------------------------------------------------------
# Guitar/source discovery
# ---------------------------------------------------------------------------

def dataset_from_relative(rel: str) -> str:
    parts = Path(rel).parts
    if parts and parts[0] == "sources" and len(parts) > 1:
        return DATASET_ALIASES.get(parts[1], parts[1])
    if parts and parts[0] == "synthetic_probes":
        return "synthetic_probes"
    return parts[0] if parts else "unknown"


def synthetic_role(path: Path) -> str:
    m = re.match(r"(\d\d)", path.name)
    return SYNTHETIC_ROLES.get(m.group(1), "fit") if m else "fit"


def source_role(dataset: str, path: Path) -> str:
    s = str(path).replace("\\", "/").lower()
    if dataset == "synthetic_probes":
        return synthetic_role(path)

    if dataset == "guitar-techs":
        # Deliberate player split:
        # P1 = fitting, P2 = selection, P3 = unseen real-performance benchmark.
        if re.search(r"(^|[/_.-])p1([/_.-]|$)", s):
            return "fit"
        if re.search(r"(^|[/_.-])p2([/_.-]|$)", s):
            return "selection"
        if re.search(r"(^|[/_.-])p3([/_.-]|$)", s):
            return "benchmark"
        # Archives often retain P1/P2/P3 in parent names, but if not:
        return "selection"

    if dataset == "guitarset":
        return "benchmark"

    if dataset == "egfxset":
        # Pickup/note diversity is useful as a separate selection domain rather
        # than letting it dominate the fit set.
        return "selection"

    # CC0 sample libraries are controlled fitting material.
    if dataset in {"freepats", "emilyguitar", "shinyguitar", "black-green"}:
        return "fit"

    return "fit"


def is_eligible_path(dataset: str, path: Path, all_guitartechs: bool) -> bool:
    s = str(path).replace("\\", "/").lower()

    if path.suffix.lower() not in AUDIO_EXTS:
        return False

    if dataset == "egfxset":
        # The source downloader may contain every effected pack. Teacher INPUTS
        # must be the clean source tones.
        return bool(re.search(r"(^|/)clean([/_\-.]|$)", s))

    if dataset == "guitarset":
        return "mono-pickup" in s or "pickup_mix" in s or "pickup-mix" in s

    if dataset == "guitar-techs" and not all_guitartechs:
        # Only direct/pickup perspective. We explicitly avoid mic/ego/exo audio.
        if re.search(r"(^|[/_.-])(di|direct)([/_.-]|$)", s):
            return True
        return False

    if dataset in {"emilyguitar", "shinyguitar", "black-green"}:
        # These repositories also contain release/noise samples. They are useful
        # instrument assets, but poor primary NAM excitation material.
        if re.search(r"(^|/)(noise|noises|release|releases)(/|$)", s):
            return False

    if dataset == "shinyguitar":
        # Prefer pickup/direct files if naming exposes it.
        if any(tok in s for tok in ("mic", "microphone")) and not any(tok in s for tok in ("pickup", "direct", "di")):
            return False

    return True


def discover_audio(
    root: Path,
    all_guitartechs: bool,
) -> list[SourceAudio]:
    inventory = root / "audio_inventory.csv"
    raw_paths: list[tuple[str, Path]] = []

    if inventory.exists():
        with inventory.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                rel = row.get("relative_path")
                dataset = row.get("dataset") or (dataset_from_relative(rel) if rel else "unknown")
                if rel:
                    p = root / rel
                    if p.exists():
                        raw_paths.append((dataset, p))

    # Synthetic probe paths may not have made an inventory generated before them.
    synth = root / "synthetic_probes"
    if synth.exists():
        for p in synth.rglob("*.wav"):
            raw_paths.append(("synthetic_probes", p))

    # Fallback / recovery if inventory is absent.
    if not raw_paths:
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                rel = str(p.relative_to(root))
                raw_paths.append((dataset_from_relative(rel), p))

    # Deduplicate.
    unique: dict[str, tuple[str, Path]] = {}
    for dataset, p in raw_paths:
        unique[str(p.resolve())] = (dataset, p.resolve())

    candidates: list[SourceAudio] = []
    guitartechs_total = 0
    guitartechs_di = 0

    for dataset, p in unique.values():
        if dataset == "guitar-techs":
            guitartechs_total += 1
            if is_eligible_path(dataset, p, False):
                guitartechs_di += 1

        if not is_eligible_path(dataset, p, all_guitartechs):
            continue
        try:
            info = sf.info(str(p))
        except Exception as e:
            print(f"[warn] cannot inspect {p}: {e}")
            continue
        if info.frames <= 0 or info.samplerate <= 0:
            continue
        candidates.append(SourceAudio(
            dataset=dataset,
            path=str(p),
            role=source_role(dataset, p),
            frames=int(info.frames),
            sample_rate=int(info.samplerate),
            channels=int(info.channels),
            duration_s=float(info.frames) / float(info.samplerate),
        ))

    if guitartechs_total and not all_guitartechs and guitartechs_di == 0:
        raise RuntimeError(
            "Guitar-TECHS was found, but no filenames/directories exposed an unambiguous "
            "DI/direct token. The teacher builder refuses to silently use amp/mic/ego/exo "
            "audio as NAM input. Inspect the extracted layout, then either rename/map the DI "
            "directory or rerun with --allow-unfiltered-guitartechs only if you have manually "
            "confirmed that the files are DI."
        )

    if not candidates:
        raise RuntimeError(f"No eligible audio found below {root}")
    return candidates


def deterministic_key(seed: int, *parts: str) -> str:
    return hashlib.sha256(("|".join([str(seed), *parts])).encode()).hexdigest()


def segments_for_source(src: SourceAudio, max_clip_s: float, seed: int) -> list[Segment]:
    synthetic = src.dataset == "synthetic_probes"
    if synthetic:
        return [Segment(
            dataset=src.dataset,
            source_path=src.path,
            role=src.role,
            start_s=0.0,
            duration_s=src.duration_s,
            synthetic=True,
        )]

    if src.duration_s <= max_clip_s + 0.001:
        return [Segment(
            dataset=src.dataset,
            source_path=src.path,
            role=src.role,
            start_s=0.0,
            duration_s=src.duration_s,
            synthetic=False,
        )]

    # Cover a long performance with deterministic, evenly distributed windows.
    count = max(1, int(math.ceil(src.duration_s / max_clip_s)))
    max_start = max(0.0, src.duration_s - max_clip_s)
    starts = np.linspace(0.0, max_start, count)
    return [
        Segment(
            dataset=src.dataset,
            source_path=src.path,
            role=src.role,
            start_s=float(start),
            duration_s=min(max_clip_s, src.duration_s - float(start)),
            synthetic=False,
        )
        for start in starts
    ]


def choose_segments(
    sources: list[SourceAudio],
    profile: str,
    max_clip_s: float,
    seed: int,
) -> list[Segment]:
    synthetic: list[Segment] = []
    real_by_role: dict[str, list[Segment]] = {"fit": [], "selection": [], "benchmark": []}

    for src in sources:
        for seg in segments_for_source(src, max_clip_s, seed):
            if seg.synthetic:
                synthetic.append(seg)
            else:
                real_by_role.setdefault(seg.role, []).append(seg)

    target = PROFILE_SECONDS[profile]
    chosen_real: list[Segment] = []

    for role, segs in real_by_role.items():
        segs = list(segs)
        segs.sort(key=lambda s: deterministic_key(
            seed, role, s.dataset, s.source_path, f"{s.start_s:.6f}"
        ))
        if target is None:
            chosen_real.extend(segs)
            continue

        quota = target * ROLE_FRACTIONS.get(role, 0.0)
        used = 0.0
        for seg in segs:
            if used >= quota and chosen_real:
                break
            chosen_real.append(seg)
            used += seg.duration_s

    return sorted(
        synthetic + chosen_real,
        key=lambda s: (s.role, s.synthetic, s.dataset, s.source_path, s.start_s)
    )


# ---------------------------------------------------------------------------
# Audio preparation
# ---------------------------------------------------------------------------

def read_segment(seg: Segment) -> tuple[np.ndarray, int, int]:
    info = sf.info(seg.source_path)
    sr = int(info.samplerate)
    start = int(round(seg.start_s * sr))
    frames = int(round(seg.duration_s * sr))
    data, got_sr = sf.read(
        seg.source_path,
        start=start,
        frames=frames,
        dtype="float32",
        always_2d=True,
    )
    if int(got_sr) != sr:
        raise RuntimeError(f"Unexpected sample-rate mismatch reading {seg.source_path}")
    original_channels = data.shape[1]
    if original_channels == 1:
        mono = data[:, 0]
    else:
        mono = np.mean(data, axis=1, dtype=np.float64).astype(np.float32)
    return mono.astype(np.float64), sr, original_channels


def resample_audio(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return np.asarray(x, dtype=np.float64)
    g = math.gcd(int(src_sr), int(dst_sr))
    up = dst_sr // g
    down = src_sr // g
    return resample_poly(np.asarray(x, dtype=np.float64), up, down).astype(np.float64)


def write_float_wav(path: Path, x: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # IEEE float32 preserves values above 1.0 if a future experiment chooses them.
    sf.write(str(path), np.asarray(x, dtype=np.float32), sr, subtype="FLOAT")


def peak_rms(x: np.ndarray) -> tuple[float, float]:
    if x.size == 0:
        return 0.0, 0.0
    peak = float(np.max(np.abs(x)))
    rms = float(np.sqrt(np.mean(np.square(x), dtype=np.float64)))
    return peak, rms


def segment_key(seg: Segment) -> str:
    return short_hash(
        f"{seg.dataset}|{Path(seg.source_path).resolve()}|{seg.start_s:.9f}|"
        f"{seg.duration_s:.9f}|{seg.role}|{int(seg.synthetic)}",
        20,
    )


def level_variant_for_segment(seg: Segment, profile: str, seed: int) -> tuple[float, ...]:
    if seg.synthetic:
        return (0.0,)
    fraction = PROFILE_LEVEL_FRACTION[profile]
    marker = int(deterministic_key(seed ^ 0x1EAE1, segment_key(seg))[:8], 16) / 0xFFFFFFFF
    if marker < fraction:
        return LEVEL_OFFSETS_DB
    return (0.0,)


def prepare_one_input(
    seg: Segment,
    target_sr: int,
    level_db: float,
    out_root: Path,
    source_hash_cache: dict[str, str],
) -> Optional[PreparedInput]:
    source = Path(seg.source_path)
    source_sha = source_hash_cache.get(str(source))
    if source_sha is None:
        source_sha = sha256_file(source)
        source_hash_cache[str(source)] = source_sha

    x, src_sr, _channels = read_segment(seg)
    if x.size == 0:
        return None

    if not np.all(np.isfinite(x)):
        raise RuntimeError(f"Non-finite source samples in {source}")

    base_peak = None
    if not seg.synthetic:
        peak, _ = peak_rms(x)
        if peak < 1e-7:
            return None
        base_peak = BASE_GUITAR_PEAK_DBFS
        x = x * (dbamp(BASE_GUITAR_PEAK_DBFS) / peak)
        x = x * dbamp(level_db)
    else:
        # Synthetic probes encode their own absolute level structure.
        level_db = 0.0

    x = resample_audio(x, src_sr, target_sr)

    p, r = peak_rms(x)
    ident_payload = {
        "segment": asdict(seg),
        "source_sha256": source_sha,
        "source_sr": src_sr,
        "target_sr": target_sr,
        "level_db": level_db,
        "base_peak_dbfs": base_peak,
        "builder_version": VERSION,
    }
    input_id = sha256_bytes(
        json.dumps(ident_payload, sort_keys=True, separators=(",", ":")).encode()
    )[:24]

    role_dir = out_root / "prepared_inputs" / f"sr{target_sr}" / seg.role
    suffix = f"L{level_db:+05.1f}dB" if not seg.synthetic else "absolute"
    dest = role_dir / f"{input_id}__{seg.dataset}__{suffix}.wav"

    if not dest.exists():
        write_float_wav(dest, x, target_sr)

    return PreparedInput(
        input_id=input_id,
        dataset=seg.dataset,
        role=seg.role,
        source_path=str(source.resolve()),
        source_sha256=source_sha,
        source_sample_rate=src_sr,
        start_s=seg.start_s,
        duration_s=float(len(x)) / target_sr,
        target_sample_rate=target_sr,
        level_offset_db=float(level_db),
        base_peak_dbfs=base_peak,
        peak=p,
        rms=r,
        path=str(dest.resolve()),
        sha256=sha256_file(dest),
        synthetic=seg.synthetic,
    )


def prepare_inputs(
    segments: list[Segment],
    needed_rates: Sequence[int],
    profile: str,
    seed: int,
    out_root: Path,
) -> list[PreparedInput]:
    source_hash_cache: dict[str, str] = {}
    prepared: list[PreparedInput] = []
    total = len(segments)
    for i, seg in enumerate(segments, 1):
        levels = level_variant_for_segment(seg, profile, seed)
        print(
            f"\r[prepare] {i}/{total} {seg.dataset} {Path(seg.source_path).name[:45]:45}",
            end="", flush=True
        )
        for sr in needed_rates:
            for level in levels:
                p = prepare_one_input(seg, sr, level, out_root, source_hash_cache)
                if p is not None:
                    prepared.append(p)
    print()
    # De-duplicate by exact prepared-input identity.
    dedup = {p.input_id: p for p in prepared}
    return list(dedup.values())


# ---------------------------------------------------------------------------
# Task planning / rendering
# ---------------------------------------------------------------------------

def model_key(m: NamModel) -> str:
    return (
        f"tone{m.tone_id}_model{m.model_id}_{m.sha256[:10]}"
        if m.tone_id is not None
        else f"{short_hash(m.model_name, 10)}_{m.sha256[:10]}"
    )


def make_task(
    nam: NamModel,
    inp: PreparedInput,
    renderer: Path,
    renderer_sha: str,
    renderer_commit: str,
    student_rate: int,
    slim_value: float,
    out_root: Path,
) -> RenderTask:
    actual_slim = float(slim_value) if nam.slimmable else None
    payload = {
        "nam_sha": nam.sha256,
        "input_sha": inp.sha256,
        "renderer_sha": renderer_sha,
        "renderer_commit": renderer_commit,
        "slim": actual_slim,
        "student_rate": student_rate,
        "builder_version": VERSION,
    }
    task_id = sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    )[:32]

    mk = model_key(nam)
    raw = (
        out_root / "teacher_raw" / nam.split / mk /
        f"{inp.input_id}__{task_id[:8]}.wav"
    )
    sin = (
        out_root / "student_44100" / "inputs" /
        f"sr{inp.target_sample_rate}" / f"{inp.input_id}.wav"
    )
    sout = (
        out_root / "student_44100" / "targets" / nam.split / mk /
        f"{inp.input_id}__{task_id[:8]}.wav"
    )
    return RenderTask(
        task_id=task_id,
        nam=nam,
        inp=inp,
        renderer_path=str(renderer),
        renderer_sha256=renderer_sha,
        renderer_commit=renderer_commit,
        slim_value=actual_slim,
        student_rate=student_rate,
        raw_output=str(raw.resolve()),
        student_input=str(sin.resolve()),
        student_target=str(sout.resolve()),
    )


def build_tasks(
    nams: list[NamModel],
    prepared: list[PreparedInput],
    renderer: Path,
    renderer_sha: str,
    renderer_commit: str,
    student_rate: int,
    slim_value: float,
    out_root: Path,
) -> list[RenderTask]:
    by_rate: dict[int, list[PreparedInput]] = {}
    for p in prepared:
        by_rate.setdefault(p.target_sample_rate, []).append(p)

    tasks = []
    for nam in nams:
        inputs = by_rate.get(nam.expected_sample_rate, [])
        if not inputs:
            raise RuntimeError(
                f"No prepared inputs at {nam.expected_sample_rate} Hz for {nam.path}"
            )
        for inp in inputs:
            tasks.append(make_task(
                nam, inp, renderer, renderer_sha, renderer_commit,
                student_rate, slim_value, out_root
            ))
    return tasks


def sqlite_connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS renders (
            task_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            finished_at REAL,
            elapsed_s REAL,
            raw_output TEXT,
            raw_sha256 TEXT,
            student_target TEXT,
            student_target_sha256 TEXT,
            error TEXT,
            stats_json TEXT
        )
    """)
    db.commit()
    return db


def completed_task_ids(db: sqlite3.Connection) -> set[str]:
    return {
        row[0] for row in db.execute(
            "SELECT task_id FROM renders WHERE status='complete'"
        )
    }


def ensure_student_input(task: RenderTask) -> str:
    dest = Path(task.student_input)
    if dest.exists():
        return sha256_file(dest)

    x, sr = sf.read(task.inp.path, dtype="float32", always_2d=False)
    if np.ndim(x) > 1:
        x = np.mean(x, axis=1)
    y = resample_audio(np.asarray(x, dtype=np.float64), int(sr), task.student_rate)

    # Many NAM render tasks share this same input. Write to a unique temporary
    # file then atomically publish it, so parallel workers cannot leave a
    # half-written shared WAV.
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(
        dest.stem + f".tmp-{os.getpid()}-{short_hash(task.task_id, 8)}" + dest.suffix
    )
    write_float_wav(tmp, y, task.student_rate)
    try:
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return sha256_file(dest)


def render_worker(task: RenderTask) -> dict:
    started = time.monotonic()
    raw = Path(task.raw_output)
    target = Path(task.student_target)
    raw.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    cmd = [task.renderer_path]
    if task.slim_value is not None:
        cmd += ["--slim", f"{task.slim_value:.6f}"]
    cmd += [task.nam.path, task.inp.path, str(raw)]

    try:
        # Exact Core render.
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"NAMCore render failed ({proc.returncode})\n"
                f"command: {cmd}\n"
                f"stdout:\n{proc.stdout[-4000:]}\n"
                f"stderr:\n{proc.stderr[-4000:]}"
            )

        inp_info = sf.info(task.inp.path)
        out_info = sf.info(str(raw))
        if out_info.frames != inp_info.frames:
            raise RuntimeError(
                f"Render length mismatch: input={inp_info.frames}, output={out_info.frames}"
            )
        if int(out_info.samplerate) != int(inp_info.samplerate):
            raise RuntimeError(
                f"Render sample-rate mismatch: input={inp_info.samplerate}, "
                f"output={out_info.samplerate}"
            )

        y, sr = sf.read(str(raw), dtype="float32", always_2d=False)
        y = np.asarray(y, dtype=np.float64)
        if y.ndim > 1:
            y = y[:, 0]
        if not np.all(np.isfinite(y)):
            raise RuntimeError("Teacher render contains NaN/Inf")

        raw_peak, raw_rms = peak_rms(y)
        y_student = resample_audio(y, int(sr), task.student_rate)
        write_float_wav(target, y_student, task.student_rate)
        student_peak, student_rms = peak_rms(y_student)

        student_input_sha = ensure_student_input(task)

        return {
            "task_id": task.task_id,
            "status": "complete",
            "elapsed_s": time.monotonic() - started,
            "raw_output": str(raw),
            "raw_sha256": sha256_file(raw),
            "student_target": str(target),
            "student_target_sha256": sha256_file(target),
            "student_input_sha256": student_input_sha,
            "error": None,
            "stats": {
                "input_peak": task.inp.peak,
                "input_rms": task.inp.rms,
                "teacher_raw_peak": raw_peak,
                "teacher_raw_rms": raw_rms,
                "teacher_student_peak": student_peak,
                "teacher_student_rms": student_rms,
                "raw_sample_rate": int(sr),
                "student_sample_rate": task.student_rate,
                "frames_raw": int(len(y)),
                "frames_student": int(len(y_student)),
            },
        }

    except Exception as e:
        return {
            "task_id": task.task_id,
            "status": "failed",
            "elapsed_s": time.monotonic() - started,
            "raw_output": str(raw),
            "raw_sha256": None,
            "student_target": str(target),
            "student_target_sha256": None,
            "student_input_sha256": None,
            "error": str(e),
            "stats": {},
        }


def db_record(db: sqlite3.Connection, result: dict) -> None:
    db.execute(
        """
        INSERT INTO renders(
            task_id,status,finished_at,elapsed_s,raw_output,raw_sha256,
            student_target,student_target_sha256,error,stats_json
        ) VALUES(?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(task_id) DO UPDATE SET
            status=excluded.status,
            finished_at=excluded.finished_at,
            elapsed_s=excluded.elapsed_s,
            raw_output=excluded.raw_output,
            raw_sha256=excluded.raw_sha256,
            student_target=excluded.student_target,
            student_target_sha256=excluded.student_target_sha256,
            error=excluded.error,
            stats_json=excluded.stats_json
        """,
        (
            result["task_id"],
            result["status"],
            time.time(),
            result["elapsed_s"],
            result.get("raw_output"),
            result.get("raw_sha256"),
            result.get("student_target"),
            result.get("student_target_sha256"),
            result.get("error"),
            json.dumps(result.get("stats", {}), sort_keys=True),
        ),
    )
    db.commit()


def run_tasks(tasks: list[RenderTask], db_path: Path, workers: int) -> None:
    db = sqlite_connect(db_path)
    complete = completed_task_ids(db)

    pending = [t for t in tasks if t.task_id not in complete]
    if not pending:
        print("[render] all tasks already complete")
        db.close()
        return

    print(f"[render] {len(pending)} pending / {len(tasks)} total; workers={workers}")
    ok = 0
    failed = 0
    started = time.monotonic()

    try:
        with futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(render_worker, t): t for t in pending}
            for n, fut in enumerate(futures.as_completed(futs), 1):
                result = fut.result()
                db_record(db, result)
                if result["status"] == "complete":
                    ok += 1
                else:
                    failed += 1
                    task = futs[fut]
                    print(
                        f"\n[FAILED] {task.nam.model_name} / {task.inp.input_id}\n"
                        f"{result['error'][:2000]}"
                    )
                elapsed = time.monotonic() - started
                rate = n / elapsed if elapsed > 0 else 0.0
                remain = (len(pending) - n) / rate if rate > 0 else math.inf
                print(
                    f"\r[render] {n}/{len(pending)}  ok={ok} fail={failed}  "
                    f"ETA {human_time(remain):>12}",
                    end="", flush=True
                )
        print()
    except KeyboardInterrupt:
        print("\n[interrupt] completed renders are journalled; rerun the same command to resume.")
        raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reports / validation
# ---------------------------------------------------------------------------

def task_audio_seconds(tasks: list[RenderTask]) -> float:
    return sum(t.inp.duration_s for t in tasks)


def print_plan(
    sources: list[SourceAudio],
    segments: list[Segment],
    prepared: list[PreparedInput],
    nams: list[NamModel],
    tasks: list[RenderTask],
) -> None:
    print("\n=== RESEARCH PLAN ===")

    src_by_dataset: dict[str, int] = {}
    for s in sources:
        src_by_dataset[s.dataset] = src_by_dataset.get(s.dataset, 0) + 1
    print("Eligible source files:")
    for k, v in sorted(src_by_dataset.items()):
        print(f"  {k:20} {v:6}")

    seg_secs: dict[str, float] = {}
    for s in segments:
        seg_secs[s.role] = seg_secs.get(s.role, 0.0) + s.duration_s
    print("\nSelected base material:")
    for role in ("fit", "selection", "benchmark"):
        print(f"  {role:10} {human_time(seg_secs.get(role, 0.0)):>12}")

    prep_secs: dict[str, float] = {}
    for p in prepared:
        # Prepared has duplicates per target rate; report only one representative
        # rate to avoid multiplying the human-readable duration.
        if p.target_sample_rate == min(x.target_sample_rate for x in prepared):
            prep_secs[p.role] = prep_secs.get(p.role, 0.0) + p.duration_s
    print("\nAfter deterministic level augmentation (one render-rate view):")
    for role in ("fit", "selection", "benchmark"):
        print(f"  {role:10} {human_time(prep_secs.get(role, 0.0)):>12}")

    nam_by_split: dict[str, int] = {}
    for n in nams:
        nam_by_split[n.split] = nam_by_split.get(n.split, 0) + 1
    print("\nNAMs:")
    for split, count in sorted(nam_by_split.items()):
        print(f"  {split:12} {count:4}")

    print(f"\nRender tasks:       {len(tasks):,}")
    print(f"Teacher audio:      {human_time(task_audio_seconds(tasks))}")
    print("Student rate:       44.1 kHz")
    print("A2 teacher mode:    Full (Slim=1.0) unless overridden")
    print("State isolation:    one NAMCore process/reset per clip")
    print("Output normalising: NONE")


def validate_outputs(root: Path, db_path: Path) -> None:
    db = sqlite_connect(db_path)
    rows = list(db.execute(
        "SELECT task_id,status,raw_output,raw_sha256,student_target,"
        "student_target_sha256,error FROM renders"
    ))
    bad = []
    for task_id, status, raw, raw_sha, target, target_sha, error in rows:
        if status != "complete":
            bad.append((task_id, status, error or ""))
            continue
        for p, expected in ((raw, raw_sha), (target, target_sha)):
            path = Path(p)
            if not path.exists():
                bad.append((task_id, "missing", p))
                continue
            got = sha256_file(path)
            if got != expected:
                bad.append((task_id, "hash-mismatch", p))
    db.close()

    report = {
        "checked": len(rows),
        "bad": [{"task_id": a, "status": b, "detail": c} for a, b, c in bad],
    }
    json_dump(root / "validation_report.json", report)

    if bad:
        print(f"[validate] {len(bad)} problem(s); see validation_report.json")
    else:
        print(f"[validate] {len(rows)} render(s) verified")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--guitar-corpus", required=True)
    ap.add_argument("--nam-corpus", required=True)
    ap.add_argument("--output", required=True)

    ap.add_argument("--profile", choices=tuple(PROFILE_SECONDS), default="full")
    ap.add_argument("--seed", type=int, default=260909)
    ap.add_argument("--max-clip-seconds", type=float, default=20.0)

    ap.add_argument("--renderer", help="Path to prebuilt NeuralAmpModelerCore render executable.")
    ap.add_argument("--namcore-source", help="Existing NeuralAmpModelerCore checkout.")
    ap.add_argument("--build-namcore", action="store_true")
    ap.add_argument("--namcore-ref", default=DEFAULT_NAMCORE_REF)
    ap.add_argument("--build-jobs", type=int, default=max(1, min(8, os.cpu_count() or 4)))

    ap.add_argument("--workers", type=int, default=max(1, min(4, (os.cpu_count() or 4) // 2)))
    ap.add_argument("--student-rate", type=int, default=DEFAULT_STUDENT_RATE)
    ap.add_argument("--slim", type=float, default=1.0,
                    help="A2 SlimmableContainer size; 1.0 = full capacity, 0.0 = smallest.")
    ap.add_argument("--include-sealed", action="store_true")
    ap.add_argument("--legacy-limit", type=int, default=0,
                    help="Also render a deterministic sample of legacy GPL NAMs.")
    ap.add_argument("--allow-unfiltered-guitartechs", action="store_true",
                    help="Only use after manually confirming Guitar-TECHS extracted files are DI.")

    ap.add_argument("--plan-only", action="store_true")
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--validate-only", action="store_true")
    ap.add_argument("--limit-tasks", type=int, default=0,
                    help="Debug/smoke limit after deterministic task ordering.")
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    if not (0.0 <= args.slim <= 1.0):
        raise SystemExit("--slim must be between 0.0 and 1.0")
    if args.max_clip_seconds <= 0:
        raise SystemExit("--max-clip-seconds must be > 0")

    guitar_root = Path(args.guitar_corpus).expanduser().resolve()
    nam_root = Path(args.nam_corpus).expanduser().resolve()
    out_root = Path(args.output).expanduser().resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    db_path = out_root / "render_journal.sqlite3"

    if args.validate_only:
        validate_outputs(out_root, db_path)
        return 0

    renderer, renderer_commit = ensure_namcore_renderer(
        out_root,
        args.renderer,
        args.build_namcore,
        args.namcore_source,
        args.namcore_ref,
        args.build_jobs,
    )
    renderer_sha = sha256_file(renderer)

    print("NamtoClo teacher dataset builder")
    print("guitar corpus:", guitar_root)
    print("NAM corpus:   ", nam_root)
    print("output:       ", out_root)
    print("renderer:     ", renderer)
    print("NAMCore:      ", renderer_commit)
    print("renderer SHA: ", renderer_sha[:16])
    print("profile:      ", args.profile)

    nams = discover_nams(
        nam_root,
        include_sealed=args.include_sealed,
        legacy_limit=args.legacy_limit,
        seed=args.seed,
    )

    sources = discover_audio(
        guitar_root,
        all_guitartechs=args.allow_unfiltered_guitartechs,
    )
    segments = choose_segments(
        sources,
        profile=args.profile,
        max_clip_s=args.max_clip_seconds,
        seed=args.seed,
    )

    needed_rates = sorted({n.expected_sample_rate for n in nams})
    print("[prepare] NAM render rates:", ", ".join(str(x) for x in needed_rates))

    prepared = prepare_inputs(
        segments,
        needed_rates=needed_rates,
        profile=args.profile,
        seed=args.seed,
        out_root=out_root,
    )

    jsonl_write(out_root / "prepared_inputs.jsonl", [asdict(p) for p in prepared])
    jsonl_write(out_root / "nam_models.jsonl", [asdict(n) for n in nams])

    tasks = build_tasks(
        nams,
        prepared,
        renderer=renderer,
        renderer_sha=renderer_sha,
        renderer_commit=renderer_commit,
        student_rate=args.student_rate,
        slim_value=args.slim,
        out_root=out_root,
    )

    tasks.sort(key=lambda t: (
        t.nam.split,
        model_key(t.nam),
        t.inp.role,
        t.inp.input_id,
        t.task_id,
    ))
    if args.limit_tasks > 0:
        tasks = tasks[:args.limit_tasks]

    jsonl_write(
        out_root / "tasks.jsonl",
        [{
            "task_id": t.task_id,
            "nam": asdict(t.nam),
            "input": asdict(t.inp),
            "renderer_path": t.renderer_path,
            "renderer_sha256": t.renderer_sha256,
            "renderer_commit": t.renderer_commit,
            "slim_value": t.slim_value,
            "student_rate": t.student_rate,
            "raw_output": t.raw_output,
            "student_input": t.student_input,
            "student_target": t.student_target,
        } for t in tasks]
    )

    run_manifest = {
        "schema_version": VERSION,
        "created_at_unix": time.time(),
        "profile": args.profile,
        "seed": args.seed,
        "base_guitar_peak_dbfs": BASE_GUITAR_PEAK_DBFS,
        "level_offsets_db": LEVEL_OFFSETS_DB,
        "level_variant_fraction": PROFILE_LEVEL_FRACTION[args.profile],
        "max_clip_seconds": args.max_clip_seconds,
        "renderer": str(renderer),
        "renderer_sha256": renderer_sha,
        "namcore_commit": renderer_commit,
        "namcore_ref_requested": args.namcore_ref,
        "a2_slim_value": args.slim,
        "student_rate": args.student_rate,
        "include_sealed": args.include_sealed,
        "legacy_limit": args.legacy_limit,
        "nam_count": len(nams),
        "prepared_input_count": len(prepared),
        "task_count": len(tasks),
        "teacher_audio_seconds": task_audio_seconds(tasks),
        "critical_notes": [
            "Raw teacher output is not peak/RMS/loudness normalised.",
            "A2 SlimmableContainer uses --slim 1.0 by default (full capacity).",
            "Old NAMs without explicit sample_rate are assumed 48000 Hz, matching the official plugin fallback.",
            "Each clip is rendered in its own NAMCore process/reset to avoid state leakage between examples.",
            "44.1 kHz student inputs and targets are deterministic polyphase-resampled derivatives.",
        ],
    }
    json_dump(out_root / "run_manifest.json", run_manifest)

    print_plan(sources, segments, prepared, nams, tasks)

    if args.plan_only:
        print("\n[plan-only] No NAM renders were executed.")
        return 0
    if args.prepare_only:
        print("\n[prepare-only] Inputs/tasks were prepared; no NAM renders were executed.")
        return 0

    run_tasks(tasks, db_path, workers=max(1, args.workers))
    validate_outputs(out_root, db_path)

    print("\nTeacher dataset complete.")
    print("Use student_44100/inputs + student_44100/targets for CLO research.")
    print("Keep teacher_raw/ as the exact NAMCore render reference.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
