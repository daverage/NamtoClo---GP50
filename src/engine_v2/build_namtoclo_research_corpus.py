#!/usr/bin/env python3
"""
build_namtoclo_research_corpus.py

Build the open, reproducible source corpus for the clean-sheet NAM -> GP50 CLO
distillation research programme.

Default "full" profile:
  1. Guitar-TECHS (Zenodo 14963133)       - primary real electric-guitar DI corpus
  2. EGFxSet (Zenodo 7044411)             - Strat / pickup / note diversity
  3. GuitarSet mono pickup mix + JAMS     - independent held-out human benchmark
  4. FreePats FSBS Electric Guitar Direct - CC0 raw electric guitar samples
  5. Emilyguitar                           - CC0 direct electric-guitar samples
  6. Shinyguitar                           - CC0 pickup samples
  7. Black & Green Guitars                 - CC0 electric-guitar sample libraries
  8. NamtoClo synthetic identification probes generated locally

The script deliberately does NOT download MUSDB/Slakh/etc by default. They are
useful music corpora, but they add much less raw pickup/system-identification
information than the sources above.

Dependencies:
    python3 -m pip install requests numpy py7zr

Usage:
    python3 build_namtoclo_research_corpus.py ~/NamtoCloCorpus

Useful options:
    --profile full        Download the full research set (default)
    --profile core        Prefer likely dry/DI files from the large Zenodo records
    --no-extract          Leave archives compressed
    --delete-archives     Delete archives after successful extraction
    --skip guitar-techs   Skip one or more datasets
    --dry-run             Show what would be downloaded without downloading it
    --list-zenodo         Print the files exposed by the Zenodo records and exit

Downloads are resumable where the remote server supports HTTP Range.
A provenance manifest and SHA-256 hashes are written beside the corpus.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import shutil
import struct
import sys
import tarfile
import time
import urllib.parse
import wave
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:
    raise SystemExit(
        "Missing dependency 'requests'. Run:\n"
        "  python3 -m pip install requests numpy py7zr"
    )

try:
    import numpy as np
except ImportError:
    raise SystemExit(
        "Missing dependency 'numpy'. Run:\n"
        "  python3 -m pip install requests numpy py7zr"
    )


USER_AGENT = "NamtoClo-CorpusBuilder/1.0 (+https://github.com/daverage/NamtoClo---GP50)"
CHUNK = 4 * 1024 * 1024
TIMEOUT = (20, 120)

# Dataset licences verified for this corpus design.
# The script also captures the live Zenodo metadata so the exact record used
# remains auditable.
DATASETS = {
    "guitar-techs": {
        "kind": "zenodo",
        "record": 14963133,
        "expected_license": "cc-by-4.0",
        "role": "primary_real_di",
        "notes": "3 electric guitarists, multi-perspective recordings including DI.",
    },
    "egfxset": {
        "kind": "zenodo",
        "record": 7044411,
        "expected_license": "cc-by-4.0",
        "role": "pickup_and_note_diversity",
        "notes": "Stratocaster notes across five pickup configurations; clean + effected material.",
    },
    "guitarset": {
        "kind": "direct",
        "role": "held_out_human_benchmark",
        "license": "CC-BY-4.0",
        "notes": "Only JAMS annotations and mono pickup mix are downloaded by default.",
        "files": [
            {
                "name": "annotation.zip",
                "url": "https://zenodo.org/record/3371780/files/annotation.zip?download=1",
                "md5": "b39b78e63d3446f2e54ddb7a54df9b10",
            },
            {
                "name": "audio_mono-pickup_mix.zip",
                "url": "https://zenodo.org/record/3371780/files/audio_mono-pickup_mix.zip?download=1",
                "md5": "aecce79f425a44e2055e46f680e10f6a",
            },
        ],
    },
    "freepats": {
        "kind": "direct",
        "role": "controlled_cc0_samples",
        "license": "CC0-1.0",
        "notes": "FSBS Electric Guitar Direct, raw bridge-pickup recordings.",
        "files": [
            {
                "name": "EGuitarFSBS-direct-SFZ+FLAC-20220911.7z",
                "url": "https://github.com/freepats/electric-guitar-FSBS-direct/releases/download/2022-09-11/EGuitarFSBS-direct-SFZ%2BFLAC-20220911.7z",
            }
        ],
    },
    "emilyguitar": {
        "kind": "github_zip",
        "role": "controlled_cc0_samples",
        "license": "CC0-1.0",
        "repo": "sfzinstruments/karoryfer.emilyguitar",
        "branch": "master",
        "notes": "Epiphone direct samples, multiple velocity layers / round robins / chords.",
    },
    "shinyguitar": {
        "kind": "github_zip",
        "role": "controlled_cc0_samples",
        "license": "CC0-1.0",
        "repo": "sfzinstruments/karoryfer.shinyguitar",
        "branch": "master",
        "notes": "Archtop guitar; use pickup/direct material for this project.",
    },
    "black-green": {
        "kind": "github_zip",
        "role": "controlled_cc0_samples",
        "license": "CC0-1.0",
        "repo": "sfzinstruments/karoryfer.black-and-green-guitars",
        "branch": "main",
        "notes": "Two hollowbody electric-guitar sample libraries.",
    },
}


@dataclass
class AcquiredFile:
    dataset: str
    source_url: str
    local_path: str
    size_bytes: int
    sha256: str
    upstream_checksum: Optional[str] = None


def session() -> requests.Session:
    """Requests session hardened for transient Zenodo/GitHub failures."""
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


HTTP = session()


def human_bytes(n: int) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{n} B"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def request_json(url: str) -> dict:
    try:
        r = HTTP.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        status = getattr(e.response, "status_code", "unknown")
        raise RuntimeError(
            f"Metadata request failed after retries: HTTP {status}\n{url}\n"
            "This is usually an upstream service error. The corpus builder is "
            "resumable, so it is safe to rerun later."
        ) from e
    except requests.RequestException as e:
        raise RuntimeError(
            f"Metadata request failed after retries:\n{url}\n{e}"
        ) from e


def download(
    url: str,
    dest: Path,
    *,
    expected_md5: Optional[str] = None,
    dry_run: bool = False,
) -> Optional[AcquiredFile]:
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        print(f"[dry-run] {url}\n          -> {dest}")
        return None

    # Existing complete file: verify checksum if known, otherwise keep it.
    if dest.exists() and dest.stat().st_size > 0:
        if expected_md5:
            got = md5_file(dest)
            if got.lower() == expected_md5.lower():
                print(f"[ok] {dest.name} already present ({human_bytes(dest.stat().st_size)})")
                return AcquiredFile("", url, str(dest), dest.stat().st_size, sha256_file(dest), f"md5:{got}")
            print(f"[warn] Existing {dest.name} failed MD5; re-downloading.")
            dest.unlink()
        else:
            print(f"[ok] {dest.name} already present ({human_bytes(dest.stat().st_size)})")
            return AcquiredFile("", url, str(dest), dest.stat().st_size, sha256_file(dest))

    part = dest.with_suffix(dest.suffix + ".part")
    start = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={start}-"} if start else {}

    with HTTP.get(url, headers=headers, stream=True, timeout=TIMEOUT, allow_redirects=True) as r:
        # Server ignored Range: start over rather than append duplicate bytes.
        if start and r.status_code == 200:
            start = 0
            part.unlink(missing_ok=True)
        r.raise_for_status()

        content_length = int(r.headers.get("Content-Length", "0") or 0)
        total = start + content_length if content_length else 0
        mode = "ab" if start else "wb"
        done = start
        last_print = 0.0

        print(f"[download] {dest.name}" + (f" ({human_bytes(total)})" if total else ""))
        with part.open(mode) as f:
            for chunk in r.iter_content(chunk_size=CHUNK):
                if not chunk:
                    continue
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - last_print > 1.0:
                    if total:
                        pct = done * 100.0 / total
                        print(f"\r  {pct:6.2f}%  {human_bytes(done)} / {human_bytes(total)}", end="", flush=True)
                    else:
                        print(f"\r  {human_bytes(done)}", end="", flush=True)
                    last_print = now
        print()

    part.replace(dest)

    if expected_md5:
        got_md5 = md5_file(dest)
        if got_md5.lower() != expected_md5.lower():
            raise RuntimeError(
                f"MD5 mismatch for {dest}\nexpected {expected_md5}\ngot      {got_md5}"
            )
        upstream = f"md5:{got_md5}"
    else:
        upstream = None

    return AcquiredFile("", url, str(dest), dest.stat().st_size, sha256_file(dest), upstream)


def safe_extract_zip(archive: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve()
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            target = (out_dir / member.filename).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"Unsafe zip member: {member.filename}")
        z.extractall(out_dir)


def safe_extract_tar(archive: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    root = out_dir.resolve()
    with tarfile.open(archive) as t:
        for member in t.getmembers():
            target = (out_dir / member.name).resolve()
            if root not in target.parents and target != root:
                raise RuntimeError(f"Unsafe tar member: {member.name}")
        t.extractall(out_dir)


def extract_archive(archive: Path, out_dir: Path) -> bool:
    """Return True if archive was extracted."""
    name = archive.name.lower()
    print(f"[extract] {archive.name} -> {out_dir}")

    if name.endswith(".zip"):
        safe_extract_zip(archive, out_dir)
        return True

    if name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
        safe_extract_tar(archive, out_dir)
        return True

    if name.endswith(".7z"):
        try:
            import py7zr
        except ImportError:
            print(
                "[warn] py7zr is not installed, so the .7z archive was left compressed.\n"
                "       Run: python3 -m pip install py7zr"
            )
            return False
        out_dir.mkdir(parents=True, exist_ok=True)
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(path=out_dir)
        return True

    return False


def zenodo_metadata(record_id: int) -> dict:
    return request_json(f"https://zenodo.org/api/records/{record_id}")


def zenodo_license_text(record: dict) -> str:
    md = record.get("metadata", {})
    bits = []

    lic = md.get("license")
    if isinstance(lic, dict):
        bits.extend(str(x) for x in (lic.get("id"), lic.get("title")) if x)
    elif lic:
        bits.append(str(lic))

    for right in md.get("rights", []) or []:
        if isinstance(right, dict):
            bits.extend(str(x) for x in (right.get("id"), right.get("title"), right.get("description")) if x)
        else:
            bits.append(str(right))
    return " | ".join(bits)


def zenodo_files(record: dict) -> list[dict]:
    return list(record.get("files", []) or [])


def zenodo_file_url(record_id: int, entry: dict) -> str:
    links = entry.get("links", {}) or {}
    for key in ("content", "download", "self"):
        if links.get(key):
            return links[key]
    key = entry["key"]
    return f"https://zenodo.org/records/{record_id}/files/{urllib.parse.quote(key)}?download=1"


def choose_zenodo_files(dataset: str, files: list[dict], profile: str) -> list[dict]:
    if profile == "full":
        return files

    # "core" is intended as a smaller bring-up corpus. If the upstream naming
    # does not expose dry/DI semantics reliably, we fall back to all files so
    # the script never silently omits the useful audio.
    if dataset == "guitar-techs":
        rx = re.compile(r"(?:^|[_\-. ])(di|direct|input|metadata|readme|license)(?:[_\-. ]|$)", re.I)
    elif dataset == "egfxset":
        rx = re.compile(r"(clean|dry|metadata|csv|readme|license)", re.I)
    else:
        return files

    selected = [f for f in files if rx.search(f.get("key", ""))]
    if not selected:
        print(f"[warn] Could not identify a safe '{profile}' subset for {dataset}; using all record files.")
        return files
    return selected


def acquire_zenodo(
    dataset: str,
    cfg: dict,
    root: Path,
    profile: str,
    extract: bool,
    delete_archives: bool,
    dry_run: bool,
) -> tuple[list[AcquiredFile], dict]:
    rid = cfg["record"]
    print(f"\n=== {dataset}: Zenodo record {rid} ===")
    record = zenodo_metadata(rid)

    live_license = zenodo_license_text(record)
    expected = cfg["expected_license"].lower()
    if live_license:
        print(f"[license] live metadata: {live_license}")
        if expected not in live_license.lower().replace("_", "-"):
            print(
                f"[warn] Expected {cfg['expected_license']} but live metadata did not contain that exact token.\n"
                "       The record is still captured in provenance; review before redistributing derived media."
            )
    else:
        print("[warn] Zenodo API did not expose a machine-readable licence string.")

    files = choose_zenodo_files(dataset, zenodo_files(record), profile)
    total = sum(int(f.get("size", 0) or 0) for f in files)
    print(f"[plan] {len(files)} file(s), approximately {human_bytes(total)}")

    acquired: list[AcquiredFile] = []
    raw = root / "sources" / dataset / "raw"
    extracted = root / "sources" / dataset / "extracted"

    for entry in files:
        key = entry["key"]
        checksum = entry.get("checksum")
        md5 = None
        if isinstance(checksum, str) and checksum.lower().startswith("md5:"):
            md5 = checksum.split(":", 1)[1]

        item = download(
            zenodo_file_url(rid, entry),
            raw / key,
            expected_md5=md5,
            dry_run=dry_run,
        )
        if item:
            item.dataset = dataset
            item.upstream_checksum = checksum or item.upstream_checksum
            acquired.append(item)

        path = raw / key
        if extract and not dry_run and path.exists():
            dest = extracted / re.sub(r"\.(zip|7z|tar|tgz|gz|bz2|xz)$", "", path.name, flags=re.I)
            ok = extract_archive(path, dest)
            if ok and delete_archives:
                path.unlink()

    provenance = {
        "dataset": dataset,
        "role": cfg["role"],
        "expected_license": cfg["expected_license"],
        "live_license_metadata": live_license,
        "zenodo_record": rid,
        "concept_doi": record.get("conceptdoi"),
        "doi": record.get("doi"),
        "title": record.get("metadata", {}).get("title"),
        "creators": record.get("metadata", {}).get("creators"),
        "notes": cfg.get("notes"),
        "profile": profile,
        "selected_files": [f.get("key") for f in files],
    }
    return acquired, provenance


def acquire_direct(
    dataset: str,
    cfg: dict,
    root: Path,
    extract: bool,
    delete_archives: bool,
    dry_run: bool,
) -> tuple[list[AcquiredFile], dict]:
    print(f"\n=== {dataset} ===")
    print(f"[license] {cfg['license']}")
    acquired: list[AcquiredFile] = []
    raw = root / "sources" / dataset / "raw"
    extracted = root / "sources" / dataset / "extracted"

    for f in cfg["files"]:
        path = raw / f["name"]
        item = download(f["url"], path, expected_md5=f.get("md5"), dry_run=dry_run)
        if item:
            item.dataset = dataset
            acquired.append(item)

        if extract and not dry_run and path.exists():
            dest = extracted / path.name
            # nicer output directory name
            for ext in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".7z", ".tgz", ".tar"):
                if dest.name.lower().endswith(ext):
                    dest = dest.with_name(dest.name[:-len(ext)])
                    break
            ok = extract_archive(path, dest)
            if ok and delete_archives:
                path.unlink()

    provenance = {
        "dataset": dataset,
        "role": cfg["role"],
        "license": cfg["license"],
        "notes": cfg.get("notes"),
        "source_files": cfg["files"],
    }
    return acquired, provenance


def acquire_github_zip(
    dataset: str,
    cfg: dict,
    root: Path,
    extract: bool,
    delete_archives: bool,
    dry_run: bool,
) -> tuple[list[AcquiredFile], dict]:
    repo = cfg["repo"]
    branch = cfg["branch"]
    url = f"https://github.com/{repo}/archive/refs/heads/{branch}.zip"
    print(f"\n=== {dataset}: {repo}@{branch} ===")
    print(f"[license] {cfg['license']}")

    raw = root / "sources" / dataset / "raw"
    archive = raw / f"{repo.split('/')[-1]}-{branch}.zip"
    item = download(url, archive, dry_run=dry_run)
    acquired: list[AcquiredFile] = []
    if item:
        item.dataset = dataset
        acquired.append(item)

    if extract and not dry_run and archive.exists():
        out = root / "sources" / dataset / "extracted"
        ok = extract_archive(archive, out)
        if ok and delete_archives:
            archive.unlink()

    provenance = {
        "dataset": dataset,
        "role": cfg["role"],
        "license": cfg["license"],
        "repo": repo,
        "branch": branch,
        "archive_url": url,
        "notes": cfg.get("notes"),
    }
    return acquired, provenance


# -------------------------- synthetic probes -------------------------- #

SR = 44100


def dbamp(db: float) -> float:
    return 10.0 ** (db / 20.0)


def write_pcm32_wav(path: Path, x: np.ndarray, sr: int = SR) -> None:
    """Write mono signed 32-bit PCM WAV, clipping only at full scale."""
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
    if p == 0:
        return np.asarray(x, dtype=np.float64)
    return np.asarray(x, dtype=np.float64) / p * dbamp(peak_db)


def log_chirp(duration: float, f0: float, f1: float, sr: int = SR) -> np.ndarray:
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    ratio = f1 / f0
    k = math.log(ratio) / duration
    phase = 2.0 * math.pi * f0 * (np.exp(k * t) - 1.0) / k
    return np.sin(phase)


def multisine(duration: float, freqs: np.ndarray, seed: int, sr: int = SR) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(duration * sr)
    t = np.arange(n, dtype=np.float64) / sr
    phases = rng.uniform(0.0, 2.0 * math.pi, len(freqs))
    y = np.zeros(n, dtype=np.float64)
    for f, ph in zip(freqs, phases):
        y += np.sin(2.0 * math.pi * float(f) * t + ph)
    return y / max(float(np.max(np.abs(y))), 1e-15)


def generate_probes(root: Path) -> list[dict]:
    out = root / "synthetic_probes"
    out.mkdir(parents=True, exist_ok=True)
    made = []

    def emit(name: str, x: np.ndarray, description: str):
        path = out / name
        write_pcm32_wav(path, x)
        made.append({
            "file": str(path.relative_to(root)),
            "sample_rate": SR,
            "format": "mono PCM32 WAV",
            "description": description,
            "sha256": sha256_file(path),
        })

    # Silence / reset
    emit("00_silence.wav", np.zeros(2 * SR), "Two seconds silence for reset/noise-floor checks.")

    # Positive/negative impulses at two levels
    for idx, level in ((1, -36), (2, -12)):
        x = np.zeros(2 * SR)
        x[SR // 2] = dbamp(level)
        emit(f"{idx:02d}_impulse_{level}dBFS.wav", x, f"Positive impulse at {level} dBFS.")

    # Polarity pulses
    x = np.zeros(6 * SR)
    events = [
        (0.75, +dbamp(-30)), (1.25, -dbamp(-30)),
        (2.00, +dbamp(-18)), (2.50, -dbamp(-18)),
        (3.25, +dbamp(-9)),  (3.75, -dbamp(-9)),
        (4.50, +dbamp(-3)),  (5.00, -dbamp(-3)),
    ]
    width = int(0.004 * SR)
    win = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(width) / max(width - 1, 1))
    for t, amp in events:
        i = int(t * SR)
        x[i:i + width] += amp * win
    emit("03_polarity_pulses.wav", x, "Matched positive/negative transient probes at several levels.")

    # Log sweeps
    for idx, level in ((4, -36), (5, -18), (6, -6)):
        sw = fade_edges(log_chirp(8.0, 20.0, 20000.0), 0.05)
        sw = normalise_peak(sw, level)
        x = np.concatenate((np.zeros(int(0.5 * SR)), sw, np.zeros(int(0.5 * SR))))
        emit(f"{idx:02d}_log_sweep_{level}dBFS.wav", x, f"20 Hz to 20 kHz logarithmic sweep at {level} dBFS peak.")

    freqs = np.geomspace(70.0, 12000.0, 23)

    # Multisine ladder
    base = multisine(1.0, freqs, seed=7001)
    levels = [-36, -30, -24, -18, -12, -9, -6, -3]
    parts = [np.zeros(int(0.5 * SR))]
    for level in levels:
        parts.append(normalise_peak(base, level))
        parts.append(np.zeros(int(0.25 * SR)))
    emit("07_multisine_level_ladder.wav", np.concatenate(parts),
         "Identical 23-tone multisine at -36,-30,-24,-18,-12,-9,-6,-3 dBFS peak.")

    # Multisine smooth drive ramp
    base = multisine(12.0, freqs, seed=8001)
    half = len(base) // 2
    db_curve = np.concatenate((
        np.linspace(-36.0, -3.0, half, endpoint=False),
        np.linspace(-3.0, -36.0, len(base) - half),
    ))
    emit("08_multisine_drive_ramp.wav", base * np.power(10.0, db_curve / 20.0),
         "23-tone multisine driven smoothly -36 -> -3 -> -36 dBFS.")

    # 1 kHz level ladder
    levels_1k = [-42, -36, -30, -24, -18, -12, -9, -6, -3, -1]
    parts = [np.zeros(int(0.5 * SR))]
    for level in levels_1k:
        t = np.arange(int(0.65 * SR)) / SR
        s = fade_edges(np.sin(2 * np.pi * 1000.0 * t) * dbamp(level))
        parts += [s, np.zeros(int(0.15 * SR))]
    emit("09_1kHz_level_ladder.wav", np.concatenate(parts),
         "1 kHz sine ladder for transfer/compression/harmonic-growth measurement.")

    # Frequency x level matrix
    matrix_freqs = [100, 400, 1000, 2500, 5000]
    matrix_levels = [-30, -18, -9, -3]
    parts = [np.zeros(int(0.5 * SR))]
    for f in matrix_freqs:
        for level in matrix_levels:
            t = np.arange(int(0.45 * SR)) / SR
            s = fade_edges(np.sin(2 * np.pi * f * t) * dbamp(level), 0.008)
            parts += [s, np.zeros(int(0.10 * SR))]
        parts.append(np.zeros(int(0.2 * SR)))
    emit("10_frequency_level_matrix.wav", np.concatenate(parts),
         "100/400/1000/2500/5000 Hz tones at -30/-18/-9/-3 dBFS.")

    # Two-tone IMD
    pairs = [(100, 1000), (250, 2000), (500, 4000), (1000, 5000)]
    imd_levels = [-24, -9, -3]
    parts = [np.zeros(int(0.5 * SR))]
    for f1, f2 in pairs:
        for level in imd_levels:
            t = np.arange(int(0.9 * SR)) / SR
            s = np.sin(2 * np.pi * f1 * t) + np.sin(2 * np.pi * f2 * t + 0.37)
            s = fade_edges(normalise_peak(s, level))
            parts += [s, np.zeros(int(0.2 * SR))]
    emit("11_two_tone_IMD_matrix.wav", np.concatenate(parts),
         "Two-tone intermodulation probes at multiple drive levels.")

    # Deterministic broadband noise ladder
    rng = np.random.default_rng(260909)
    noise = rng.standard_normal(SR)
    noise /= max(float(np.max(np.abs(noise))), 1e-15)
    parts = [np.zeros(int(0.5 * SR))]
    for level in [-36, -24, -18, -12, -6, -3]:
        parts += [noise * dbamp(level), np.zeros(int(0.25 * SR))]
    emit("12_broadband_noise_level_ladder.wav", np.concatenate(parts),
         "Identical deterministic broadband-noise segment repeated at six levels.")

    # Transient tone bursts
    parts = [np.zeros(int(0.5 * SR))]
    for f in [80, 160, 500, 1500, 4000]:
        for level in [-18, -6, -3]:
            n = int(0.18 * SR)
            t = np.arange(n) / SR
            env = np.sin(np.pi * np.arange(n) / max(n - 1, 1)) ** 2
            s = normalise_peak(np.sin(2 * np.pi * f * t) * env, level)
            parts += [s, np.zeros(int(0.22 * SR))]
    emit("13_tone_burst_transients.wav", np.concatenate(parts),
         "Short 80 Hz-4 kHz tone bursts at three levels for transient behaviour.")

    # Sparse pick-like broadband transients. This is deliberately artificial:
    # useful for system identification, not a replacement for real guitar DI.
    rng = np.random.default_rng(14001)
    x = np.zeros(int(10.0 * SR), dtype=np.float64)
    event_times = [0.6, 1.2, 1.8, 2.5, 3.2, 4.1, 5.0, 6.1, 7.2, 8.4]
    event_levels = [-24, -18, -12, -9, -6, -3, -12, -6, -18, -3]
    for j, (tm, level) in enumerate(zip(event_times, event_levels)):
        n = int(0.35 * SR)
        z = rng.standard_normal(n)
        alpha = math.exp(-2.0 * math.pi * (1800.0 + 250.0 * j) / SR)
        filt = np.empty(n, dtype=np.float64)
        state = 0.0
        for k in range(n):
            state = (1.0 - alpha) * z[k] + alpha * state
            filt[k] = state
        env = np.exp(-np.arange(n) / (0.055 * SR))
        burst = normalise_peak(filt * env, level)
        i = int(tm * SR)
        x[i:i+n] += burst
    emit("14_pick_like_transient_probe.wav", x,
         "Sparse decaying broadband transients at varied levels; synthetic diagnostic only.")

    return made


def list_zenodo_records() -> None:
    for name in ("guitar-techs", "egfxset"):
        cfg = DATASETS[name]
        record = zenodo_metadata(cfg["record"])
        print(f"\n{name} / Zenodo {cfg['record']}")
        print("title:", record.get("metadata", {}).get("title"))
        print("licence:", zenodo_license_text(record))
        for f in zenodo_files(record):
            print(f"  {human_bytes(int(f.get('size', 0) or 0)):>10}  {f.get('key')}")


def make_plan(root: Path, provenance: list[dict], probes: list[dict]) -> None:
    plan = """# NamtoClo clean-sheet research corpus

This directory is a **source corpus**, not the final runtime conversion corpus.

## Intended roles

- **Guitar-TECHS**: primary real electric-guitar DI / human-performance material.
  Split by guitarist/guitar before model research so a player/guitar is never
  present in both fit and final benchmark sets.
- **EGFxSet**: controlled Stratocaster note/pickup diversity. Prefer its clean
  recordings when constructing NAM inputs.
- **GuitarSet mono pickup mix**: independent held-out human-playing benchmark.
  Do not optimise converter hyperparameters against the final sealed subset.
- **FreePats / Emilyguitar / Shinyguitar / Black & Green**: CC0 controlled
  source material for deterministic generated riffs, velocity/pickup/instrument
  generalisation, and future compact runtime-corpus design.
- **synthetic_probes**: system-identification stimuli. These are intended for
  fitting/diagnostics rather than perceptual evaluation.

## Recommended research split

Do not make random *clip-level* train/test splits from the same performance.
Prefer source/player/guitar separation:

1. FIT: two Guitar-TECHS players + controlled CC0 material + synthetic probes.
2. SELECTION: remaining sections/guitar conditions not present in FIT.
3. BENCHMARK: sealed GuitarSet pickup-mix subset + one held-out Guitar-TECHS
   player/guitar + selected EGFxSet pickup conditions never used for tuning.

The large corpus exists to discover the best compact ~30-60 second runtime
probe corpus and objective function. It is not intended to be processed by
every user's NAM in the final application.

## Licensing

See provenance.json. CC BY material requires attribution. CC0 material is
included specifically to provide a source pool with minimal redistribution
friction. Do not discard upstream licence/readme files from extracted archives.
"""
    (root / "CORPUS_PLAN.md").write_text(plan, encoding="utf-8")
    (root / "provenance.json").write_text(
        json.dumps({"sources": provenance, "synthetic_probes": probes}, indent=2),
        encoding="utf-8",
    )


def inventory_audio(root: Path) -> None:
    exts = {".wav", ".flac", ".aif", ".aiff", ".ogg"}
    rows = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            try:
                rel = p.relative_to(root)
                dataset = rel.parts[1] if len(rel.parts) > 2 and rel.parts[0] == "sources" else rel.parts[0]
                rows.append((dataset, str(rel), p.suffix.lower(), p.stat().st_size))
            except OSError:
                pass

    with (root / "audio_inventory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["dataset", "relative_path", "extension", "size_bytes"])
        w.writerows(rows)
    print(f"\n[inventory] {len(rows)} audio file(s) indexed in audio_inventory.csv")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("destination", nargs="?", default="./NamtoCloResearchCorpus")
    p.add_argument("--profile", choices=("full", "core"), default="full")
    p.add_argument("--skip", action="append", default=[], choices=sorted(DATASETS))
    p.add_argument("--no-extract", action="store_true")
    p.add_argument("--delete-archives", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--list-zenodo", action="store_true",
                   help="Print live Zenodo record file lists and exit.")
    p.add_argument("--no-probes", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if args.list_zenodo:
        list_zenodo_records()
        return 0

    root = Path(args.destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    print("NamtoClo research corpus builder")
    print("destination:", root)
    print("profile:", args.profile)
    print("extract:", not args.no_extract)
    if args.skip:
        print("skipping:", ", ".join(args.skip))

    provenance = []
    acquired_all: list[AcquiredFile] = []

    for name, cfg in DATASETS.items():
        if name in args.skip:
            continue

        if cfg["kind"] == "zenodo":
            acquired, prov = acquire_zenodo(
                name, cfg, root, args.profile,
                not args.no_extract, args.delete_archives, args.dry_run,
            )
        elif cfg["kind"] == "direct":
            acquired, prov = acquire_direct(
                name, cfg, root,
                not args.no_extract, args.delete_archives, args.dry_run,
            )
        elif cfg["kind"] == "github_zip":
            acquired, prov = acquire_github_zip(
                name, cfg, root,
                not args.no_extract, args.delete_archives, args.dry_run,
            )
        else:
            raise RuntimeError(f"Unknown dataset type: {cfg['kind']}")

        acquired_all.extend(acquired)
        provenance.append(prov)

    probes = []
    if not args.no_probes and not args.dry_run:
        print("\n=== generating NamtoClo identification probes ===")
        probes = generate_probes(root)
        print(f"[ok] generated {len(probes)} probe WAV files")

    if not args.dry_run:
        manifest = {
            "builder_version": 1,
            "profile": args.profile,
            "acquired_files": [asdict(x) for x in acquired_all],
        }
        (root / "download_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        make_plan(root, provenance, probes)
        inventory_audio(root)

    print("\nDone.")
    if args.dry_run:
        print("Dry run only: no files were downloaded.")
    else:
        print("Next step: build player/guitar-disjoint FIT / SELECTION / BENCHMARK manifests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
