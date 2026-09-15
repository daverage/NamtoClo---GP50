#!/usr/bin/env python3
"""
build_namtoclo_nam_corpus.py

Build the NAM-model side of the clean-sheet NAM -> GP50 CLO research corpus.

It downloads a curated set of modern NAM A2 models from TONE3000 using the
official API, separates them into development / selection / sealed groups,
records provenance/licensing/model metadata, and optionally clones the public
legacy A1 GPL corpus used for architecture/generalisation testing.

IMPORTANT
---------
TONE3000 files often use the T3K licence. This script is for LOCAL research.
It does not grant permission to redistribute downloaded .nam files. Keep the
generated Tone3000 directories out of git unless the individual tone licence
explicitly permits redistribution.

Authentication
--------------
The current TONE3000 API accepts Bearer authentication. For a local research
script, provide ONE of:

    export TONE3000_SECRET_KEY='t3k_cs_...'
or
    export TONE3000_ACCESS_TOKEN='...'

Never put a secret key into this file or commit it to a repository.

Dependencies
------------
    python3 -m pip install requests

Usage
-----
    python3 build_namtoclo_nam_corpus.py ~/NamtoCloNAMCorpus

Useful options
--------------
    --plan                 Show the curated plan without downloading
    --metadata-only        Fetch TONE3000 metadata but not model files
    --skip-legacy          Do not clone the legacy GPL A1 corpus
    --open-licenses-only   Skip Tone3000 tones whose tone-level licence is
                           not CC0/CC-BY/CC-BY-SA
    --no-sealed-download   Fetch metadata for sealed models but do not download
                           their .nam files (useful for a genuinely sealed test)
    --extra-tone ID        Add all A2 models from an additional TONE3000 tone
                           (may be supplied multiple times)

The curated plan intentionally uses model-name selectors inside large packs.
If an upstream creator renames/removes a model, the script records the mismatch
and refuses to silently substitute a different model.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import requests
except ImportError:
    raise SystemExit("Missing dependency 'requests'. Run: python3 -m pip install requests")

API = "https://www.tone3000.com/api/v1"
USER_AGENT = "NamtoClo-NAMCorpusBuilder/1.0 (+https://github.com/daverage/NamtoClo---GP50)"
TIMEOUT = (20, 120)
CHUNK = 2 * 1024 * 1024
OPEN_LICENSES = {"cco", "cc0", "cc-by", "cc-by-sa"}

# ---------------------------------------------------------------------------
# Curated modern A2 research corpus
#
# 'patterns' are regular expressions applied to the official API model name.
# Empty/omitted patterns means all A2 models in that tone.
# ---------------------------------------------------------------------------

CURATED = [
    # DEVELOPMENT ------------------------------------------------------------
    {
        "split": "development",
        "tone_id": 10912,
        "label": "Roland JC-120B",
        "class": "solid_state_clean_full_rig",
        "why": "Near-linear clean baseline; same amp with bright/mic variants.",
        "patterns": [
            r"Bright On, SM57$",
            r"Bright Off, SM57$",
        ],
    },
    {
        "split": "development",
        "tone_id": 755,
        "label": "Fender Deluxe Reverb",
        "class": "tube_clean_to_breakup_head",
        "why": "Controlled same-amp progression at volume 3, 5 and 10.",
        "patterns": [],  # exactly three models; keep all
    },
    {
        "split": "development",
        "tone_id": 42319,
        "label": "65 Fender Princeton SM57 progression",
        "class": "tube_clean_eob_crunch_full_rig",
        "why": "Same combo/mic, volume 3/5/7: clean -> edge -> crunch.",
        "patterns": [
            r"Princeton Clean 3 SM57$",
            r"Princeton EOB Vol 5 SM57$",
            r"Princeton Crunch Vol 7 SM57$",
        ],
    },
    {
        "split": "development",
        "tone_id": 89722,
        "label": "Vox AC30 CC2",
        "class": "british_clean_to_hot_head",
        "why": "Controlled CLEAN/EDGE/CRUNCH/HOT progression.",
        "patterns": [
            r"AC30_CLEAN$",
            r"AC30_EDGE$",
            r"AC30_CRUNCH$",
            r"AC30_HOT$",
        ],
    },
    {
        "split": "development",
        "tone_id": 29285,
        "label": "Dumble Steel String Singer",
        "class": "boutique_clean_to_drive_head",
        "why": "Clean plus two drive levels from one amp family.",
        "patterns": [],  # clean + drive1 + drive2
    },
    {
        "split": "development",
        "tone_id": 37987,
        "label": "Marshall JCM800 2203",
        "class": "classic_crunch_gain_progression_head",
        "why": "Same head/settings except gain 3/5/7/10.",
        "patterns": [],  # exactly four gain stages
    },
    {
        "split": "development",
        "tone_id": 50546,
        "label": "Soldano SLO-30",
        "class": "modern_crunch_high_gain_head",
        "why": "Calibrated crunch and lead models from the same head.",
        "patterns": [],  # 2 models
    },
    {
        "split": "development",
        "tone_id": 57410,
        "label": "Mesa Mark V calibrated progression",
        "class": "modern_multi_mode_high_gain_head",
        "why": "Calibrated 18 dBu clean and representative unboosted/boosted high-gain modes.",
        "patterns": [
            r"\(Ch 1 Clean\).*CLN",
            r"\(Ch 1 Fat\).*CLN",
            r"\(Ch 3 MK-IIC\+ 808\).*HG",
            r"\(Ch 3 MK-IV\) HG bal",
            r"\(Ch 3 MK-IV\) HG scoop",
            r"\(Ch 3 Ext\) HG bal",
            r"Black Metal I",
            r"Black Metal II",
        ],
    },
    {
        "split": "development",
        "tone_id": 36551,
        "label": "ProCo RAT controlled slice",
        "class": "pedal_static_nonlinearity",
        "why": "Controlled 7x7 grid; take drive progression at fixed filter plus filter extremes.",
        "patterns": [
            r"Distortion 1 Filter 4$",
            r"Distortion 3 Filter 4$",
            r"Distortion 5 Filter 4$",
            r"Distortion 7 Filter 4$",
            r"Distortion 5 Filter 1$",
            r"Distortion 5 Filter 7$",
        ],
    },
    {
        "split": "development",
        "tone_id": 63491,
        "label": "Big Muff",
        "class": "fuzz_pedal",
        "why": "Different nonlinear behaviour from normal amp clipping.",
        "patterns": [],  # BigMuff + BigMuffHigh
    },
    {
        "split": "development",
        "tone_id": 84925,
        "label": "Ampeg SVT-CL gain stages",
        "class": "bass_amp_gain_progression",
        "why": "Wide-band bass test; low/mid/high gain plus B7K-driven chain.",
        "patterns": [
            r"Hi IN - Gain 1\s*$",
            r"Hi IN - Gain 5\s*$",
            r"Hi IN - Gain 9\s*$",
            r"AmpegSVT - B7K\s*$",
        ],
    },
    {
        "split": "development",
        "tone_id": 62048,
        "label": "Tone3000 Full Rig Run Down",
        "class": "full_rig_cross_section",
        "why": "Tone3000's own A2 test pack: clean, high gain, boosted high gain and bass full rigs.",
        "patterns": [],  # 6 models
    },

    # SELECTION --------------------------------------------------------------
    {
        "split": "selection",
        "tone_id": 59309,
        "label": "Mesa Mark V / Lonestar",
        "class": "unseen_mesa_head",
        "why": "Independent creator and capture chain; rhythm/lead/pushed behaviour.",
        "patterns": [],  # 3
    },
    {
        "split": "selection",
        "tone_id": 32868,
        "label": "Peavey 5150 + Mesa full rig",
        "class": "boosted_unboosted_high_gain_full_rig",
        "why": "Same full rig with no boost, MXR and Maxon variants/mics.",
        "patterns": [],  # 5
    },
    {
        "split": "selection",
        "tone_id": 64090,
        "label": "Dumble SSS full rig",
        "class": "boutique_full_rig",
        "why": "Cabbed/full-rig counterpart to head-only Dumble behaviour.",
        "patterns": [],  # 2
    },

    # SEALED -----------------------------------------------------------------
    # These are intentionally not needed to develop the optimiser. Use
    # --no-sealed-download if you want to preserve the strongest possible seal.
    {
        "split": "sealed",
        "tone_id": 32152,
        "label": "Peavey 5150 Captured",
        "class": "unseen_high_gain_full_rig",
        "why": "Independent 5150 capture with substantially harder A2-Lite behaviour.",
        "patterns": [],
    },
    {
        "split": "sealed",
        "tone_id": 70286,
        "label": "Mesa Mark V MK-IV scoop alternate calibration",
        "class": "unseen_calibration_same_family",
        "why": "14.2 dBu alternate calibration/creator state; tests calibration robustness.",
        "patterns": [],  # 4
    },
    {
        "split": "sealed",
        "tone_id": 43985,
        "label": "Pathological Soldano teacher",
        "class": "poor_teacher_stability",
        "why": "One deliberately high-error A2 teacher to test optimiser stability, not tone quality.",
        "patterns": [
            r"Piss1s SOldano 3$",
        ],
    },
]

LEGACY_REPO = "https://github.com/pelennor2170/NAM_models.git"


@dataclass
class Downloaded:
    split: str
    tone_id: int
    tone_title: str
    tone_license: str
    model_id: int
    model_name: str
    architecture: str
    size: str
    path: str
    sha256: str
    embedded_metadata: dict[str, Any]


def safe_name(s: str, limit: int = 140) -> str:
    s = re.sub(r"[^\w .()+\-[\]]+", "_", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip(" ._")
    return (s or "model")[:limit]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def auth_token() -> str:
    token = os.environ.get("TONE3000_SECRET_KEY") or os.environ.get("TONE3000_ACCESS_TOKEN")
    if not token:
        raise SystemExit(
            "No TONE3000 credential found.\n\n"
            "Set one of:\n"
            "  export TONE3000_SECRET_KEY='t3k_cs_...'\n"
            "  export TONE3000_ACCESS_TOKEN='...'\n\n"
            "Create API credentials in your own TONE3000 account settings.\n"
            "Do not paste a secret into this script."
        )
    return token.strip()


class T3K:
    def __init__(self, token: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        })

    def get(self, path_or_url: str, params: Optional[dict] = None, stream: bool = False):
        url = path_or_url if path_or_url.startswith("http") else API + path_or_url
        r = self.s.get(url, params=params, timeout=TIMEOUT, stream=stream, allow_redirects=True)
        if r.status_code == 429:
            retry = int(r.headers.get("Retry-After", "5") or 5)
            print(f"[rate limit] sleeping {retry}s")
            time.sleep(retry)
            r = self.s.get(url, params=params, timeout=TIMEOUT, stream=stream, allow_redirects=True)
        if r.status_code == 401:
            raise RuntimeError("TONE3000 authentication failed (401). Check your Bearer credential.")
        if r.status_code == 403:
            raise RuntimeError(
                f"TONE3000 returned 403 for {url}. Your API integration/account may not have "
                "access to this endpoint under the current API terms."
            )
        r.raise_for_status()
        return r

    def tone(self, tone_id: int) -> dict:
        return self.get(f"/tones/{tone_id}", params={"architecture": "2"}).json()

    def models(self, tone_id: int) -> list[dict]:
        out = []
        page = 1
        while True:
            js = self.get("/models", params={
                "tone_id": tone_id,
                "architecture": "2",
                "page": page,
                "page_size": 300,
            }).json()
            out.extend(js.get("data", []))
            pages = int(js.get("total_pages", 1) or 1)
            if page >= pages:
                return out
            page += 1

    def download_model(self, model: dict, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  [ok] {dest.name}")
            return
        part = dest.with_suffix(dest.suffix + ".part")
        # model_url requires the same bearer token according to current API docs.
        with self.get(model["model_url"], stream=True) as r:
            total = int(r.headers.get("Content-Length", "0") or 0)
            done = 0
            print(f"  [download] {dest.name}", end="", flush=True)
            with part.open("wb") as f:
                for chunk in r.iter_content(CHUNK):
                    if not chunk:
                        continue
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r  [download] {dest.name}: {done*100/total:5.1f}%", end="", flush=True)
            print()
        part.replace(dest)


def _normalise_model_name(name: str) -> str:
    """Normalise harmless upstream naming differences before selector matching."""
    s = unicodedata.normalize("NFKC", str(name))
    s = (
        s.replace("\u2010", "-")
         .replace("\u2011", "-")
         .replace("\u2012", "-")
         .replace("\u2013", "-")
         .replace("\u2014", "-")
         .replace("\u2212", "-")
         .replace("\u00a0", " ")
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


def model_matches(name: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    normalised = _normalise_model_name(name)
    return any(re.search(p, normalised, flags=re.I) for p in patterns)


INTERESTING_META = re.compile(
    r"(architecture|version|input.*level|output.*level|calib|esr|epoch|sample.*rate|"
    r"receptive|loud|gain|metadata|training)", re.I
)


def extract_embedded_metadata(path: Path) -> dict[str, Any]:
    """Best-effort extraction of compact useful metadata from JSON .nam files."""
    try:
        raw = path.read_text(encoding="utf-8")
        obj = json.loads(raw)
    except Exception:
        return {}

    found: dict[str, Any] = {}

    def walk(x: Any, prefix: str = ""):
        if len(found) > 100:
            return
        if isinstance(x, dict):
            for k, v in x.items():
                key = f"{prefix}.{k}" if prefix else str(k)
                if INTERESTING_META.search(str(k)):
                    # Avoid embedding huge weight arrays/objects in our manifest.
                    if isinstance(v, (str, int, float, bool)) or v is None:
                        found[key] = v
                    elif isinstance(v, list) and len(v) <= 16 and all(
                        isinstance(i, (str, int, float, bool)) or i is None for i in v
                    ):
                        found[key] = v
                if isinstance(v, dict):
                    walk(v, key)

    walk(obj)
    return found


def choose_models(models: list[dict], patterns: list[str], strict: bool = True) -> list[dict]:
    selected = [m for m in models if model_matches(str(m.get("name", "")), patterns)]
    if patterns and strict:
        missed = [
            p for p in patterns
            if not any(
                re.search(p, _normalise_model_name(str(m.get("name", ""))), re.I)
                for m in models
            )
        ]
        if missed:
            available = "\n".join(f"    - {m.get('name')}" for m in models)
            raise RuntimeError(
                "One or more curated model selectors no longer match upstream:\n"
                + "\n".join(f"  {p}" for p in missed)
                + "\nAvailable A2 models are:\n" + available
            )
    return selected


def write_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding="utf-8")


def clone_legacy(root: Path):
    dest = root / "legacy_a1_gpl" / "NAM_models"
    if dest.exists():
        print(f"\n[legacy] already present: {dest}")
        return
    if shutil.which("git") is None:
        print("[legacy] git not found; skipping legacy corpus clone.")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"\n[legacy] cloning {LEGACY_REPO}")
    subprocess.run(["git", "clone", "--depth", "1", LEGACY_REPO, str(dest)], check=True)


def plan_rows(entries: list[dict]):
    for e in entries:
        pats = e.get("patterns") or ["<all A2 models>"]
        print(f"{e['split']:11}  tone {e['tone_id']:>5}  {e['label']}")
        print(f"             class: {e['class']}")
        print(f"             why:   {e['why']}")
        print(f"             pick:  {', '.join(pats)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("destination", nargs="?", default="./NamtoCloNAMCorpus")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--metadata-only", action="store_true")
    ap.add_argument("--skip-legacy", action="store_true")
    ap.add_argument("--open-licenses-only", action="store_true")
    ap.add_argument("--no-sealed-download", action="store_true")
    ap.add_argument("--extra-tone", action="append", type=int, default=[])
    args = ap.parse_args()

    entries = list(CURATED)
    for tid in args.extra_tone:
        entries.append({
            "split": "selection",
            "tone_id": tid,
            "label": f"Extra tone {tid}",
            "class": "user_extra",
            "why": "User-supplied additional A2 tone.",
            "patterns": [],
        })

    if args.plan:
        plan_rows(entries)
        return 0

    root = Path(args.destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    client = T3K(auth_token())
    provenance = []
    downloaded: list[Downloaded] = []
    missing_or_skipped = []

    print("NamtoClo NAM research corpus builder")
    print("destination:", root)
    print("TONE3000 files are local research inputs; redistribution depends on each tone's licence.")

    for e in entries:
        tid = e["tone_id"]
        print(f"\n=== [{e['split']}] TONE3000 tone {tid}: {e['label']} ===")
        tone = client.tone(tid)
        tone_license = str(tone.get("license") or "").lower()
        tone_title = str(tone.get("title") or e["label"])
        print(f"[tone] {tone_title}")
        print(f"[licence] {tone_license or 'unknown'}")

        prov = {
            **e,
            "tone_title_live": tone_title,
            "tone_license": tone_license,
            "tone_url": tone.get("url"),
            "creator": (tone.get("user") or {}).get("username"),
            "gear": tone.get("gear"),
            "makes": tone.get("makes"),
            "tags": tone.get("tags"),
            "published_at": tone.get("published_at"),
            "a2_models_count": tone.get("a2_models_count"),
        }
        provenance.append(prov)

        if args.open_licenses_only and tone_license not in OPEN_LICENSES:
            print("[skip] non-open tone-level licence")
            missing_or_skipped.append({"tone_id": tid, "reason": f"licence:{tone_license}"})
            continue

        models = client.models(tid)
        selected = choose_models(models, e.get("patterns", []), strict=True)
        print(f"[models] selected {len(selected)} of {len(models)} A2 models")

        # Always capture metadata, even if sealed files themselves are withheld.
        write_json(root / "metadata" / e["split"] / f"tone_{tid}.json", {
            "curation": e,
            "tone": tone,
            "selected_models": selected,
        })

        withhold_files = args.metadata_only or (e["split"] == "sealed" and args.no_sealed_download)
        if withhold_files:
            print("[metadata only] model files not downloaded for this entry")
            continue

        for m in selected:
            model_id = int(m["id"])
            name = str(m["name"])
            suffix = ".nam"
            filename = f"{model_id}__{safe_name(name)}{suffix}"
            dest = root / e["split"] / f"tone_{tid}__{safe_name(tone_title)}" / filename
            client.download_model(m, dest)
            meta = extract_embedded_metadata(dest)
            downloaded.append(Downloaded(
                split=e["split"],
                tone_id=tid,
                tone_title=tone_title,
                tone_license=tone_license,
                model_id=model_id,
                model_name=name,
                architecture=str(m.get("architecture_version") or ""),
                size=str(m.get("size") or ""),
                path=str(dest.relative_to(root)),
                sha256=sha256_file(dest),
                embedded_metadata=meta,
            ))

    if not args.skip_legacy:
        clone_legacy(root)

    write_json(root / "curation_plan.json", entries)
    write_json(root / "provenance.json", provenance)
    write_json(root / "download_manifest.json", [asdict(x) for x in downloaded])
    write_json(root / "skipped.json", missing_or_skipped)

    readme = """# NamtoClo NAM research corpus

This corpus is for development of the clean-sheet NAM -> GP50 CLO distiller.

## Splits
- `development/`: may be used for optimiser/loss/probe design.
- `selection/`: choose algorithms/hyperparameters, but do not fit individual
  development decisions to these models.
- `sealed/`: final modern-A2 generalisation test. Prefer generating this only
  after the algorithm is frozen; use `--no-sealed-download` during development.
- `legacy_a1_gpl/`: older WaveNet-era NAM collection for architecture/generalisation.

## Critical licence rule
TONE3000's common `t3k` licence permits use of downloaded model files but does
not permit republishing/distributing those data files without creator permission.
Do not commit those `.nam` files into NamtoClo. Keep tone/model IDs and provenance
in reproducible manifests instead.

## Experimental rule
When producing cached teacher audio, preserve:
1. input WAV hash,
2. NAM file SHA-256,
3. NAM embedded calibration/input-level metadata where present,
4. NAMCore version,
5. render sample rate,
6. level offset applied,
7. latency/alignment treatment.

This allows every teacher render to be reproduced exactly.
"""
    (root / "README.md").write_text(readme, encoding="utf-8")

    counts = {}
    for d in downloaded:
        counts[d.split] = counts.get(d.split, 0) + 1

    print("\n=== complete ===")
    print("Downloaded models:", len(downloaded))
    for split in ("development", "selection", "sealed"):
        print(f"  {split:11}: {counts.get(split, 0)}")
    print("Manifests written to:", root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
