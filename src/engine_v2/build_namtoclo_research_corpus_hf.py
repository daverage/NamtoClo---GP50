#!/usr/bin/env python3
"""
build_namtoclo_research_corpus_hf.py

HF-first source-corpus builder for the clean-sheet NAM -> GP50 CLO research project.

Default corpus
--------------
Hugging Face:
  - Julian-br/GuitarJam      : real clean electric-guitar DI, 44.1 kHz, CC0
  - taohu/egfxset            : EGFxSet, 44.1 kHz, CC BY 4.0
                               (only Clean rows are materialised)
  - taohu/guitarset          : GuitarSet, 44.1 kHz, CC BY 4.0
                               (only mono pickup mix is materialised)

GitHub / public releases:
  - FreePats FSBS Electric Guitar Direct, CC0
  - Karoryfer Emilyguitar, CC0
  - Karoryfer Shinyguitar, CC0
  - Karoryfer Black & Green Guitars, CC0
  - Karoryfer Growlybass, CC0
  - Karoryfer Black And Blue Basses, CC0

Local:
  - NamtoClo synthetic system-identification probes

Why this version exists
-----------------------
The earlier builder depended on Zenodo for Guitar-TECHS / EGFxSet / GuitarSet.
This version makes Zenodo optional. It is designed to be sufficient for the
main NAM->CLO research run even if Zenodo is unavailable.

Dependencies
------------
    python3 -m pip install huggingface_hub pyarrow requests numpy soundfile py7zr

Usage
-----
    python3 build_namtoclo_research_corpus_hf.py ~/NamtoCloResearchCorpus

Useful options
--------------
    --plan              Show the acquisition plan only
    --keep-containers   Keep downloaded Parquet/archive containers after
                        successful materialisation/extraction
    --skip NAME         Skip one dataset (repeatable)
    --with-guitar-techs Try to add Guitar-TECHS from Zenodo, but do not fail
                        the whole build if Zenodo is unavailable
    --no-probes         Do not generate the synthetic probe pack

This builder is resumable: existing extracted/materialised datasets are skipped.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import tarfile
import time
import wave
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import numpy as np
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    import requests
    import soundfile as sf
    from huggingface_hub import snapshot_download
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as e:
    raise SystemExit(
        f"Missing dependency: {e}\n\n"
        "Install with:\n"
        "  python3 -m pip install huggingface_hub pyarrow requests numpy soundfile py7zr"
    )

USER_AGENT = "NamtoClo-HF-CorpusBuilder/1.0 (+https://github.com/daverage/NamtoClo---GP50)"
CHUNK = 4 * 1024 * 1024
TIMEOUT = (20, 180)
SR = 44100

DATASETS = {
    "guitarjam": {
        "kind": "hf_direct",
        "repo": "Julian-br/GuitarJam",
        "license": "CC0-1.0",
        "role": "primary_real_di",
        "approx_download": "796 MB",
        "notes": "580 ~15s clean DI improvisations, Fender Strat, 44.1 kHz.",
        "allow_patterns": ["guitar_jam/**", "README.md", ".gitattributes"],
    },
    "egfxset": {
        "kind": "hf_parquet",
        "repo": "taohu/egfxset",
        "license": "CC-BY-4.0",
        "role": "pickup_and_note_diversity",
        "approx_download": "6.47 GB",
        "notes": "8,970 44.1 kHz examples; materialise only Clean rows.",
        "audio_column": None,       # auto-detect
        "filter_value": "Clean",    # auto-detect field containing this exact value
        "output_subdir": "Clean",
    },
    "guitarset": {
        "kind": "hf_parquet",
        "repo": "taohu/guitarset",
        "license": "CC-BY-4.0",
        "role": "held_out_human_benchmark",
        "approx_download": "1.86 GB",
        "notes": "360 real performances; materialise only audio_mix (mono pickup mix).",
        "audio_column": "audio_mix",
        "filter_value": None,
        "output_subdir": "pickup_mix",
    },
    "freepats": {
        "kind": "archive",
        "license": "CC0-1.0",
        "role": "controlled_cc0_samples",
        "approx_download": "~74 MB",
        "notes": "FSBS direct electric guitar.",
        "url": "https://github.com/freepats/electric-guitar-FSBS-direct/releases/download/2022-09-11/EGuitarFSBS-direct-SFZ%2BFLAC-20220911.7z",
        "filename": "EGuitarFSBS-direct-SFZ+FLAC-20220911.7z",
    },
    "emilyguitar": {
        "kind": "github_branch_zip",
        "license": "CC0-1.0",
        "role": "controlled_cc0_samples",
        "approx_download": "~100 MB",
        "notes": "Epiphone direct samples, velocity layers and round robins.",
        "repo": "sfzinstruments/karoryfer.emilyguitar",
        "branch": "master",
    },
    "shinyguitar": {
        "kind": "github_branch_zip",
        "license": "CC0-1.0",
        "role": "controlled_cc0_samples",
        "approx_download": "~350 MB",
        "notes": "Archtop guitar; direct magnetic-pickup material available.",
        "repo": "sfzinstruments/karoryfer.shinyguitar",
        "branch": "master",
    },
    "black-green": {
        "kind": "github_branch_zip",
        "license": "CC0-1.0",
        "role": "controlled_cc0_samples",
        "approx_download": "~450 MB",
        "notes": "Two hollowbody electric guitars.",
        "repo": "sfzinstruments/karoryfer.black-and-green-guitars",
        "branch": "main",
    },
    "growlybass": {
        "kind": "archive",
        "license": "CC0-1.0",
        "role": "controlled_bass_di",
        "approx_download": "160 MB",
        "notes": "Squier Jazz Bass, 44.1 kHz.",
        "url": "https://github.com/sfzinstruments/karoryfer.growlybass/releases/download/v1.002/Karoryfer.Growlybass.v1.002.zip",
        "filename": "Karoryfer.Growlybass.v1.002.zip",
    },
    "black-blue-basses": {
        "kind": "archive",
        "license": "CC0-1.0",
        "role": "controlled_bass_di",
        "approx_download": "961 MB",
        "notes": "Two five-string bass guitars, 44.1 kHz.",
        "url": "https://github.com/sfzinstruments/karoryfer.black-and-blue-basses/releases/download/v1.001/Black_And_Blue_Basses_1001.zip",
        "filename": "Black_And_Blue_Basses_1001.zip",
    },
}

ALL_NAMES = sorted(DATASETS)


@dataclass
class SourceRecord:
    dataset: str
    source: str
    local_path: str
    sha256: str
    size_bytes: int
    license: str
    role: str
    notes: str


def retry_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    retry = Retry(
        total=8,
        connect=8,
        read=8,
        status=8,
        backoff_factor=2.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=8)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


HTTP = retry_session()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def human_bytes(n: int) -> str:
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024 or unit == "TiB":
            return f"{x:.1f} {unit}"
        x /= 1024
    return str(n)


def safe_name(value: Any, limit: int = 120) -> str:
    s = str(value or "item")
    s = re.sub(r"[^\w .()+\-[\]]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip(" ._")
    return (s or "item")[:limit]


def atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        print(f"[ok] {dest.name} already present ({human_bytes(dest.stat().st_size)})")
        return dest

    part = dest.with_suffix(dest.suffix + ".part")
    start = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={start}-"} if start else {}

    with HTTP.get(url, headers=headers, stream=True, timeout=TIMEOUT, allow_redirects=True) as r:
        if start and r.status_code == 200:
            start = 0
            part.unlink(missing_ok=True)
        r.raise_for_status()
        length = int(r.headers.get("Content-Length", "0") or 0)
        total = start + length if length else 0
        mode = "ab" if start else "wb"
        done = start
        last = 0.0
        print(f"[download] {dest.name}" + (f" ({human_bytes(total)})" if total else ""))
        with part.open(mode) as f:
            for block in r.iter_content(CHUNK):
                if not block:
                    continue
                f.write(block)
                done += len(block)
                now = time.monotonic()
                if now - last > 1.0:
                    if total:
                        print(f"\r  {done*100/total:6.2f}%  {human_bytes(done)} / {human_bytes(total)}", end="", flush=True)
                    else:
                        print(f"\r  {human_bytes(done)}", end="", flush=True)
                    last = now
        print()
    part.replace(dest)
    return dest


def safe_extract_zip(archive: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    root = out.resolve()
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            target = (out / member.filename).resolve()
            if target != root and root not in target.parents:
                raise RuntimeError(f"Unsafe zip member: {member.filename}")
        z.extractall(out)


def extract_archive(archive: Path, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    print(f"[extract] {archive.name}")

    if name.endswith(".zip"):
        safe_extract_zip(archive, out)
        return

    if name.endswith(".7z"):
        try:
            import py7zr
        except ImportError:
            raise RuntimeError("Install py7zr: python3 -m pip install py7zr")
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(path=out)
        return

    if name.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
        root = out.resolve()
        with tarfile.open(archive) as t:
            for member in t.getmembers():
                target = (out / member.name).resolve()
                if target != root and root not in target.parents:
                    raise RuntimeError(f"Unsafe tar member: {member.name}")
            t.extractall(out)
        return

    raise RuntimeError(f"Unsupported archive: {archive}")


def completion_marker(out: Path) -> Path:
    return out / ".materialised-ok.json"


def is_complete(out: Path) -> bool:
    return completion_marker(out).exists()


def mark_complete(out: Path, payload: dict) -> None:
    atomic_json(completion_marker(out), payload)


def hf_snapshot(repo: str, dest: Path, allow_patterns: Optional[list[str]] = None) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    print(f"[Hugging Face] {repo}")
    path = snapshot_download(
        repo_id=repo,
        repo_type="dataset",
        local_dir=str(dest),
        allow_patterns=allow_patterns,
    )
    return Path(path)


def find_parquets(root: Path) -> list[Path]:
    return sorted(root.rglob("*.parquet"))


def arrow_audio_columns(schema: pa.Schema) -> list[str]:
    out = []
    for field in schema:
        t = field.type
        if pa.types.is_struct(t):
            child_names = {x.name for x in t}
            if "bytes" in child_names or "path" in child_names:
                out.append(field.name)
    return out


def discover_clean_filter(parquets: list[Path], desired_value: str) -> str:
    """Find a scalar string column containing exact value 'Clean'."""
    wanted = desired_value.casefold()
    candidate_names: set[str] = set()

    for p in parquets:
        pf = pq.ParquetFile(p)
        for f in pf.schema_arrow:
            if pa.types.is_string(f.type) or pa.types.is_large_string(f.type):
                candidate_names.add(f.name)

    preferred = ["effect", "fx", "effect_name", "category", "label", "processing"]
    ordered = [x for x in preferred if x in candidate_names] + sorted(candidate_names - set(preferred))

    for col in ordered:
        for p in parquets:
            pf = pq.ParquetFile(p)
            if col not in pf.schema_arrow.names:
                continue
            for rg in range(min(pf.num_row_groups, 4)):
                table = pf.read_row_group(rg, columns=[col])
                vals = table[col].to_pylist()
                if any(isinstance(v, str) and v.casefold() == wanted for v in vals):
                    return col
    raise RuntimeError(
        f"Could not find a string column containing exact value {desired_value!r}. "
        f"String columns inspected: {ordered}"
    )


def extension_from_audio(audio: dict) -> str:
    path = str(audio.get("path") or "")
    ext = Path(path).suffix.lower()
    if ext in {".wav", ".flac", ".aif", ".aiff", ".ogg"}:
        return ext

    data = audio.get("bytes")
    if isinstance(data, (bytes, bytearray)):
        if data[:4] == b"RIFF":
            return ".wav"
        if data[:4] == b"fLaC":
            return ".flac"
        if data[:4] == b"FORM":
            return ".aiff"
        if data[:4] == b"OggS":
            return ".ogg"
    return ".wav"


def materialise_audio_value(audio: dict, repo_root: Path, dest: Path) -> None:
    data = audio.get("bytes")
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, bytearray):
        data = bytes(data)

    dest.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(data, bytes) and data:
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        return

    original = audio.get("path")
    if original:
        p = Path(original)
        candidates = [p, repo_root / original]
        for c in candidates:
            if c.exists() and c.is_file():
                shutil.copy2(c, dest)
                return

    raise RuntimeError("Audio struct had neither embedded bytes nor a resolvable path.")


def metadata_identifier(row: dict, index: int) -> str:
    for key in ("track_id", "id", "name", "filename", "file", "path"):
        v = row.get(key)
        if v not in (None, "") and not isinstance(v, dict):
            return safe_name(v)
    return f"{index:06d}"


def materialise_hf_parquet(
    dataset: str,
    cfg: dict,
    raw_dir: Path,
    out_dir: Path,
) -> dict:
    if is_complete(out_dir):
        marker = json.loads(completion_marker(out_dir).read_text(encoding="utf-8"))
        print(f"[ok] {dataset} already materialised ({marker.get('written', '?')} files)")
        return marker

    parquets = find_parquets(raw_dir)
    if not parquets:
        raise RuntimeError(f"No Parquet files found for {dataset} under {raw_dir}")

    first_schema = pq.ParquetFile(parquets[0]).schema_arrow
    audio_col = cfg.get("audio_column")
    if audio_col:
        if audio_col not in first_schema.names:
            raise RuntimeError(
                f"{dataset}: expected audio column {audio_col!r}; "
                f"available columns: {first_schema.names}"
            )
    else:
        audio_cols = arrow_audio_columns(first_schema)
        if not audio_cols:
            raise RuntimeError(f"{dataset}: no Hugging Face audio struct column detected.")
        audio_col = "audio" if "audio" in audio_cols else audio_cols[0]

    filter_col = None
    filter_value = cfg.get("filter_value")
    if filter_value:
        filter_col = discover_clean_filter(parquets, filter_value)
        print(f"[{dataset}] filtering {filter_col} == {filter_value!r}")

    out_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = out_dir / "materialised_metadata.jsonl"
    written = 0
    skipped = 0

    with metadata_path.open("w", encoding="utf-8") as meta_f:
        for parquet in parquets:
            pf = pq.ParquetFile(parquet)
            print(f"[{dataset}] {parquet.name}: {pf.num_row_groups} row group(s)")
            for rg in range(pf.num_row_groups):
                table = pf.read_row_group(rg)

                if filter_col:
                    values = table[filter_col]
                    # Case-insensitive filter to be robust to "clean"/"Clean".
                    mask_py = [
                        isinstance(v, str) and v.casefold() == str(filter_value).casefold()
                        for v in values.to_pylist()
                    ]
                    if not any(mask_py):
                        skipped += table.num_rows
                        continue
                    table = table.filter(pa.array(mask_py))

                for row in table.to_pylist():
                    audio = row.get(audio_col)
                    if not isinstance(audio, dict):
                        raise RuntimeError(
                            f"{dataset}: row {written} column {audio_col!r} was not an audio struct."
                        )

                    ident = metadata_identifier(row, written)
                    ext = extension_from_audio(audio)

                    # Preserve useful grouping in directory names when present.
                    player = row.get("player_id")
                    pickup = (
                        row.get("pickup")
                        or row.get("pickup_position")
                        or row.get("pickup_config")
                        or row.get("pickup_configuration")
                    )
                    sub = out_dir
                    if player not in (None, ""):
                        sub = sub / f"player_{safe_name(player, 32)}"
                    if pickup not in (None, ""):
                        sub = sub / f"pickup_{safe_name(pickup, 48)}"

                    dest = sub / f"{written:06d}__{ident}{ext}"
                    if not dest.exists():
                        materialise_audio_value(audio, raw_dir, dest)

                    safe_meta = {}
                    for k, v in row.items():
                        if k == audio_col:
                            continue
                        if isinstance(v, (str, int, float, bool)) or v is None:
                            safe_meta[k] = v
                    safe_meta["materialised_path"] = str(dest.relative_to(out_dir))
                    meta_f.write(json.dumps(safe_meta, ensure_ascii=False) + "\n")
                    written += 1

    marker = {
        "dataset": dataset,
        "repo": cfg["repo"],
        "audio_column": audio_col,
        "filter_column": filter_col,
        "filter_value": filter_value,
        "written": written,
        "skipped": skipped,
        "completed_at": time.time(),
    }
    mark_complete(out_dir, marker)
    return marker


def acquire_hf_direct(name: str, cfg: dict, root: Path) -> list[SourceRecord]:
    dest = root / "sources" / name / "extracted"
    if is_complete(dest):
        print(f"[ok] {name} already downloaded")
    else:
        hf_snapshot(cfg["repo"], dest, cfg.get("allow_patterns"))
        mark_complete(dest, {
            "dataset": name,
            "repo": cfg["repo"],
            "completed_at": time.time(),
        })

    records = []
    for p in sorted(dest.rglob("*")):
        if p.is_file() and p.name != ".materialised-ok.json":
            records.append(SourceRecord(
                dataset=name,
                source=f"https://huggingface.co/datasets/{cfg['repo']}",
                local_path=str(p),
                sha256=sha256_file(p),
                size_bytes=p.stat().st_size,
                license=cfg["license"],
                role=cfg["role"],
                notes=cfg["notes"],
            ))
    return records


def acquire_hf_parquet(name: str, cfg: dict, root: Path, keep_containers: bool) -> list[SourceRecord]:
    base = root / "sources" / name
    raw = base / "_hf_container"
    extracted = base / "extracted" / cfg.get("output_subdir", "audio")

    if not is_complete(extracted):
        hf_snapshot(cfg["repo"], raw, ["data/*.parquet", "data/**/*.parquet", "README.md"])
        materialise_hf_parquet(name, cfg, raw, extracted)

    records = []
    for p in sorted(extracted.rglob("*")):
        if p.is_file() and p.name not in {".materialised-ok.json", "materialised_metadata.jsonl"}:
            records.append(SourceRecord(
                dataset=name,
                source=f"https://huggingface.co/datasets/{cfg['repo']}",
                local_path=str(p),
                sha256=sha256_file(p),
                size_bytes=p.stat().st_size,
                license=cfg["license"],
                role=cfg["role"],
                notes=cfg["notes"],
            ))

    if not keep_containers and is_complete(extracted) and raw.exists():
        print(f"[cleanup] removing {name} Parquet container after successful materialisation")
        shutil.rmtree(raw)

    return records


def acquire_archive(name: str, cfg: dict, root: Path, keep_containers: bool) -> list[SourceRecord]:
    base = root / "sources" / name
    raw = base / "_archive" / cfg["filename"]
    extracted = base / "extracted"

    if not is_complete(extracted):
        archive = download(cfg["url"], raw)
        extract_archive(archive, extracted)
        mark_complete(extracted, {
            "dataset": name,
            "url": cfg["url"],
            "archive_sha256": sha256_file(archive),
            "completed_at": time.time(),
        })

    records = []
    for p in sorted(extracted.rglob("*")):
        if p.is_file() and p.name != ".materialised-ok.json":
            records.append(SourceRecord(
                dataset=name,
                source=cfg["url"],
                local_path=str(p),
                sha256=sha256_file(p),
                size_bytes=p.stat().st_size,
                license=cfg["license"],
                role=cfg["role"],
                notes=cfg["notes"],
            ))

    if not keep_containers and is_complete(extracted) and raw.parent.exists():
        print(f"[cleanup] removing {name} archive after successful extraction")
        shutil.rmtree(raw.parent)

    return records


def acquire_github_branch(name: str, cfg: dict, root: Path, keep_containers: bool) -> list[SourceRecord]:
    repo = cfg["repo"]
    branch = cfg["branch"]
    url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
    local_cfg = dict(cfg)
    local_cfg.update({
        "kind": "archive",
        "url": url,
        "filename": f"{repo.split('/')[-1]}-{branch}.zip",
    })
    return acquire_archive(name, local_cfg, root, keep_containers)


# -------------------------- synthetic probes -------------------------- #

def dbamp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def write_pcm32_wav(path: Path, x: np.ndarray, sr: int = SR) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    y = np.asarray(x, dtype=np.float64)
    y = np.clip(y, -1.0, 1.0 - (1.0 / 2147483648.0))
    pcm = np.round(y * 2147483647.0).astype("<i4")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(4)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def fade_edges(x: np.ndarray, seconds: float = 0.01, sr: int = SR) -> np.ndarray:
    x = np.array(x, dtype=np.float64, copy=True)
    n = min(int(seconds * sr), len(x) // 2)
    if n > 1:
        f = np.linspace(0.0, 1.0, n)
        x[:n] *= f
        x[-n:] *= f[::-1]
    return x


def normalise_peak(x: np.ndarray, peak_db: float) -> np.ndarray:
    p = float(np.max(np.abs(x))) if len(x) else 0.0
    return np.asarray(x, dtype=np.float64) if p == 0 else np.asarray(x) / p * dbamp(peak_db)


def log_chirp(duration: float, f0: float, f1: float, sr: int = SR) -> np.ndarray:
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    k = math.log(f1 / f0) / duration
    phase = 2.0 * math.pi * f0 * (np.exp(k * t) - 1.0) / k
    return np.sin(phase)


def multisine(duration: float, freqs: np.ndarray, seed: int, sr: int = SR) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    phases = rng.uniform(0.0, 2.0 * math.pi, len(freqs))
    y = np.zeros(n)
    for f, ph in zip(freqs, phases):
        y += np.sin(2 * np.pi * float(f) * t + ph)
    return y / max(float(np.max(np.abs(y))), 1e-15)


def generate_probes(root: Path) -> list[dict]:
    out = root / "synthetic_probes"
    out.mkdir(parents=True, exist_ok=True)
    made = []

    def emit(name: str, x: np.ndarray, description: str):
        p = out / name
        write_pcm32_wav(p, x)
        made.append({
            "file": str(p.relative_to(root)),
            "description": description,
            "sample_rate": SR,
            "sha256": sha256_file(p),
        })

    emit("00_silence.wav", np.zeros(2 * SR), "Silence/reset probe.")

    for idx, level in ((1, -36), (2, -12)):
        x = np.zeros(2 * SR)
        x[SR // 2] = dbamp(level)
        emit(f"{idx:02d}_impulse_{level}dBFS.wav", x, f"Impulse at {level} dBFS.")

    x = np.zeros(6 * SR)
    width = int(0.004 * SR)
    win = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(width) / max(width - 1, 1))
    events = [
        (0.75, +dbamp(-30)), (1.25, -dbamp(-30)),
        (2.00, +dbamp(-18)), (2.50, -dbamp(-18)),
        (3.25, +dbamp(-9)),  (3.75, -dbamp(-9)),
        (4.50, +dbamp(-3)),  (5.00, -dbamp(-3)),
    ]
    for t, amp in events:
        i = int(t * SR)
        x[i:i+width] += amp * win
    emit("03_polarity_pulses.wav", x, "Matched positive/negative pulses.")

    for idx, level in ((4, -36), (5, -18), (6, -6)):
        sw = normalise_peak(fade_edges(log_chirp(8.0, 20.0, 20000.0), 0.05), level)
        emit(f"{idx:02d}_log_sweep_{level}dBFS.wav",
             np.concatenate([np.zeros(int(.5*SR)), sw, np.zeros(int(.5*SR))]),
             f"20 Hz-20 kHz log sweep at {level} dBFS.")

    freqs = np.geomspace(70.0, 12000.0, 23)
    base = multisine(1.0, freqs, 7001)
    parts = [np.zeros(int(.5*SR))]
    for level in [-36,-30,-24,-18,-12,-9,-6,-3]:
        parts += [normalise_peak(base, level), np.zeros(int(.25*SR))]
    emit("07_multisine_level_ladder.wav", np.concatenate(parts), "23-tone level ladder.")

    base = multisine(12.0, freqs, 8001)
    half = len(base)//2
    dbcurve = np.concatenate([
        np.linspace(-36, -3, half, endpoint=False),
        np.linspace(-3, -36, len(base)-half),
    ])
    emit("08_multisine_drive_ramp.wav", base * np.power(10.0, dbcurve/20.0),
         "Multisine -36 -> -3 -> -36 dBFS.")

    parts = [np.zeros(int(.5*SR))]
    for level in [-42,-36,-30,-24,-18,-12,-9,-6,-3,-1]:
        t = np.arange(int(.65*SR))/SR
        parts += [fade_edges(np.sin(2*np.pi*1000*t)*dbamp(level)), np.zeros(int(.15*SR))]
    emit("09_1kHz_level_ladder.wav", np.concatenate(parts), "1 kHz transfer/compression ladder.")

    parts = [np.zeros(int(.5*SR))]
    for f in [100,400,1000,2500,5000]:
        for level in [-30,-18,-9,-3]:
            t = np.arange(int(.45*SR))/SR
            parts += [fade_edges(np.sin(2*np.pi*f*t)*dbamp(level), .008), np.zeros(int(.10*SR))]
    emit("10_frequency_level_matrix.wav", np.concatenate(parts), "Frequency x level matrix.")

    parts = [np.zeros(int(.5*SR))]
    for f1,f2 in [(100,1000),(250,2000),(500,4000),(1000,5000)]:
        for level in [-24,-9,-3]:
            t = np.arange(int(.9*SR))/SR
            s = np.sin(2*np.pi*f1*t) + np.sin(2*np.pi*f2*t + .37)
            parts += [fade_edges(normalise_peak(s, level)), np.zeros(int(.2*SR))]
    emit("11_two_tone_IMD_matrix.wav", np.concatenate(parts), "Two-tone IMD matrix.")

    rng = np.random.default_rng(260909)
    noise = rng.standard_normal(SR)
    noise /= max(float(np.max(np.abs(noise))), 1e-15)
    parts = [np.zeros(int(.5*SR))]
    for level in [-36,-24,-18,-12,-6,-3]:
        parts += [noise*dbamp(level), np.zeros(int(.25*SR))]
    emit("12_broadband_noise_level_ladder.wav", np.concatenate(parts), "Deterministic broadband-noise ladder.")

    parts = [np.zeros(int(.5*SR))]
    for f in [80,160,500,1500,4000]:
        for level in [-18,-6,-3]:
            n = int(.18*SR)
            t = np.arange(n)/SR
            env = np.sin(np.pi*np.arange(n)/max(n-1,1))**2
            parts += [normalise_peak(np.sin(2*np.pi*f*t)*env, level), np.zeros(int(.22*SR))]
    emit("13_tone_burst_transients.wav", np.concatenate(parts), "Tone-burst transient probe.")

    rng = np.random.default_rng(140014)
    parts = [np.zeros(int(.5*SR))]
    for level in [-24,-12,-6,-3]:
        for f0 in [90,180,440,900,1800]:
            n = int(.35*SR)
            noise = rng.standard_normal(n)
            t = np.arange(n)/SR
            env = np.exp(-t*16.0)
            carrier = np.sin(2*np.pi*f0*t)
            sig = (0.6*carrier + 0.4*noise) * env
            parts += [normalise_peak(sig, level), np.zeros(int(.15*SR))]
    emit("14_pick_like_transient_probe.wav", np.concatenate(parts), "Synthetic pick-like decaying transients.")

    return made


def inventory_audio(root: Path) -> dict:
    exts = {".wav", ".flac", ".aif", ".aiff", ".ogg"}
    rows = []
    totals: dict[str, dict[str, float]] = {}

    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        rel = p.relative_to(root)
        parts = rel.parts
        dataset = parts[1] if len(parts) > 1 and parts[0] == "sources" else parts[0]
        try:
            info = sf.info(str(p))
            duration = float(info.frames) / float(info.samplerate) if info.samplerate else 0.0
            rows.append([
                dataset, str(rel), p.suffix.lower(), p.stat().st_size,
                int(info.samplerate), int(info.channels), int(info.frames), duration
            ])
            t = totals.setdefault(dataset, {"files": 0, "bytes": 0, "seconds": 0.0})
            t["files"] += 1
            t["bytes"] += p.stat().st_size
            t["seconds"] += duration
        except Exception as e:
            print(f"[warn] audio inventory could not inspect {p}: {e}")

    with (root / "audio_inventory.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["dataset","relative_path","extension","size_bytes","sample_rate","channels","frames","duration_s"])
        w.writerows(rows)

    atomic_json(root / "audio_inventory_summary.json", totals)
    return totals


def try_guitar_techs(root: Path, keep_containers: bool) -> list[SourceRecord]:
    """Optional/non-blocking Zenodo acquisition for Guitar-TECHS."""
    record_id = 14963133
    api = f"https://zenodo.org/api/records/{record_id}"
    name = "guitar-techs"
    base = root / "sources" / name
    records: list[SourceRecord] = []

    try:
        print("\n=== optional Guitar-TECHS / Zenodo ===")
        r = HTTP.get(api, timeout=TIMEOUT)
        r.raise_for_status()
        record = r.json()
        files = record.get("files", [])
        if not files:
            raise RuntimeError("Zenodo returned no files.")

        for item in files:
            key = item["key"]
            url = (item.get("links") or {}).get("content") or (item.get("links") or {}).get("download")
            if not url:
                continue
            archive = base / "_archive" / key
            extracted = base / "extracted" / safe_name(Path(key).stem)
            if not is_complete(extracted):
                p = download(url, archive)
                extract_archive(p, extracted)
                mark_complete(extracted, {"source": api, "archive": key, "sha256": sha256_file(p)})
            if not keep_containers and archive.exists():
                archive.unlink(missing_ok=True)

        for p in (base / "extracted").rglob("*"):
            if p.is_file() and p.name != ".materialised-ok.json":
                records.append(SourceRecord(
                    dataset=name,
                    source=api,
                    local_path=str(p),
                    sha256=sha256_file(p),
                    size_bytes=p.stat().st_size,
                    license="CC-BY-4.0",
                    role="optional_real_di_validation",
                    notes="Optional full Guitar-TECHS mirror from Zenodo.",
                ))
    except Exception as e:
        print(f"[optional] Guitar-TECHS unavailable; continuing without it:\n  {e}")

    return records


def print_plan() -> None:
    print("NamtoClo HF-first corpus acquisition plan\n")
    for name, cfg in DATASETS.items():
        print(f"{name:20} {cfg['approx_download']:>10}  {cfg['license']:12}  {cfg['role']}")
        print(f"{'':20} {'':>10}  {cfg['notes']}")
    print("\nExpected network transfer: roughly 11-12 GB.")
    print("Recommended free disk before starting: at least 25 GB.")
    print("Container Parquet/archives are removed after successful extraction by default.")
    print("Guitar-TECHS is optional and not included in that estimate.")


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("destination", nargs="?", default="./NamtoCloResearchCorpus")
    p.add_argument("--plan", action="store_true")
    p.add_argument("--skip", action="append", default=[], choices=ALL_NAMES)
    p.add_argument("--keep-containers", action="store_true")
    p.add_argument("--with-guitar-techs", action="store_true")
    p.add_argument("--no-probes", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.plan:
        print_plan()
        return 0

    root = Path(args.destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    print("NamtoClo HF-first research corpus builder")
    print("destination:", root)
    print("keep containers:", args.keep_containers)
    if args.skip:
        print("skipping:", ", ".join(args.skip))

    all_records: list[SourceRecord] = []
    provenance = []

    for name, cfg in DATASETS.items():
        if name in args.skip:
            continue

        print(f"\n=== {name} ===")
        print(f"role: {cfg['role']} | licence: {cfg['license']} | expected download: {cfg['approx_download']}")

        if cfg["kind"] == "hf_direct":
            records = acquire_hf_direct(name, cfg, root)
        elif cfg["kind"] == "hf_parquet":
            records = acquire_hf_parquet(name, cfg, root, args.keep_containers)
        elif cfg["kind"] == "archive":
            records = acquire_archive(name, cfg, root, args.keep_containers)
        elif cfg["kind"] == "github_branch_zip":
            records = acquire_github_branch(name, cfg, root, args.keep_containers)
        else:
            raise RuntimeError(f"Unknown dataset kind: {cfg['kind']}")

        all_records.extend(records)
        provenance.append({
            "dataset": name,
            **cfg,
            "materialised_file_count": len(records),
            "materialised_bytes": sum(r.size_bytes for r in records),
        })

    if args.with_guitar_techs:
        gt = try_guitar_techs(root, args.keep_containers)
        all_records.extend(gt)

    probes = []
    if not args.no_probes:
        print("\n=== synthetic probes ===")
        probes = generate_probes(root)
        print(f"[ok] generated {len(probes)} probes")

    # Compact source manifest: one row per source file.
    manifest_path = root / "source_manifest.jsonl"
    with manifest_path.open("w", encoding="utf-8") as f:
        for row in all_records:
            f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")

    atomic_json(root / "provenance.json", {
        "schema_version": 2,
        "builder": "HF-first",
        "sources": provenance,
        "synthetic_probes": probes,
        "guitar_techs_requested": args.with_guitar_techs,
    })

    totals = inventory_audio(root)

    print("\n=== complete ===")
    for dataset, info in sorted(totals.items()):
        print(
            f"{dataset:20} {int(info['files']):6d} files  "
            f"{human_bytes(int(info['bytes'])):>10}  "
            f"{info['seconds']/3600:7.2f} h"
        )
    print("\nWrote:")
    print(" ", root / "audio_inventory.csv")
    print(" ", root / "audio_inventory_summary.json")
    print(" ", root / "source_manifest.jsonl")
    print(" ", root / "provenance.json")
    print("\nNext: run build_namtoclo_teacher_dataset.py against this corpus.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
