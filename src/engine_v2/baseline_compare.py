#!/usr/bin/env python3
"""Compare the shipped C++ engine against an EngineV2 result, apples-to-apples.

Background (see ENGINE_V2_RESEARCH_NORTH_STAR.md, "Existing converter
baseline" and the central-hypothesis section): EngineV2's trainers
(train_namtoclo_north_star_v42.py etc.) report their own ESR-to-NAM-teacher
score in report.json, but nothing in this repo compared that number to the
*shipped* C++ engine's (src/core/native_converter.cpp + clo_refiner.cpp,
exposed via the `namtoclo` CLI) ESR against the exact same teacher evidence.
This script builds that missing three-way comparison point:

    NAM teacher  vs.  shipped C++ engine  vs.  EngineV2 candidate

for one model, reusing:
  - the exact stimulus/teacher-render cache EngineV2's report.json already
    points at (student_44100 input/target WAVs for each of the 5 levels), so
    there is no re-derivation that could subtly diverge from what EngineV2
    actually trained/scored against;
  - the exact scoring function EngineV2 uses, `evidence_esr` from
    distiller_v2_north_star_v2.py (imported, not reimplemented);
  - the exact CLO byte layout the shipped engine writes, decoded by the new
    clo_reader.py (a port of src/core/clo_refiner.cpp's parseModel).

IMPORTANT LIMITATION: this only measures fit to a raw-NAM teacher on one
synthetic sweep stimulus. It is NOT a hardware-validated result and does not
by itself answer "is EngineV2 better" -- see the verdict field this script
writes, and ENGINE_V2_RESEARCH_NORTH_STAR.md for the full evidence bar
(real GP-50 listening, held-out guitar) that a real answer requires.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import soundfile as sf

from clo_reader import read_clo
from distiller_v2_dsp import render_full_with
from distiller_v2_north_star_v2 import evidence_esr
from distiller_v2_north_star_v4 import StimulusFitEvidence, EvidenceWindow


def _find_reports(output_root: Path, model_regex_pat) -> list[Path]:
    import re

    rx = re.compile(model_regex_pat, re.I) if model_regex_pat else None
    candidates: list[Path] = []
    if (output_root / "report.json").is_file():
        candidates.append(output_root / "report.json")
    else:
        candidates.extend(sorted(output_root.glob("*/report.json")))
        candidates.extend(sorted(output_root.glob("**/report.json")))
    # de-dup while preserving order
    seen = set()
    uniq = []
    for c in candidates:
        rc = c.resolve()
        if rc not in seen:
            seen.add(rc)
            uniq.append(c)
    if rx is not None:
        out = []
        for c in uniq:
            try:
                report = json.loads(c.read_text())
            except Exception:
                continue
            name = str(report.get("model_name", "")) + " " + c.parent.name
            if rx.search(name):
                out.append(c)
        return out
    return uniq


def _load_level_variants(report: dict) -> list[dict]:
    stim = report.get("stimulus") or {}
    variants = stim.get("level_variants")
    if not variants:
        raise RuntimeError(
            "report.json has no stimulus.level_variants -- can't reconstruct the "
            "exact teacher evidence EngineV2 used (need student_input/student_target "
            "WAV paths per level)."
        )
    return variants


def _build_evidence_and_audio(level_variants: list[dict], stimulus_sha256: str):
    """Reconstruct the same StimulusFitEvidence layout build_stimulus_fit_evidence
    produces (distiller_v2_north_star_v4.py) directly from the cached level WAVs
    the EngineV2 report.json already recorded -- same weighting (one equal-weight
    whole-clip window per level), same stimulus/levels, so this is the identical
    evidence EngineV2 scored against, not a re-derivation.
    """
    inputs: list[np.ndarray] = []
    targets: list[np.ndarray] = []
    levels: list[float] = []
    windows_rows: list[tuple[EvidenceWindow, ...]] = []
    n_levels = len(level_variants)
    source_weight = 1.0 / float(n_levels)

    for lv in level_variants:
        level_db = float(lv["level_db"])
        x, sx = sf.read(str(lv["student_input"]), dtype="float32", always_2d=False)
        y, sy = sf.read(str(lv["student_target"]), dtype="float32", always_2d=False)
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if x.ndim > 1:
            x = x[:, 0]
        if y.ndim > 1:
            y = y[:, 0]
        n = min(len(x), len(y))
        x, y = x[:n], y[:n]
        inputs.append(x)
        targets.append(y)
        levels.append(level_db)
        windows_rows.append(
            (
                EvidenceWindow(
                    group=f"stimulus_level:{level_db:+.3f}dB",
                    label=f"whole_stimulus_{level_db:+.3f}dB",
                    start=0,
                    end=n,
                    weight=source_weight,
                ),
            )
        )

    evidence = StimulusFitEvidence(
        windows=tuple(windows_rows),
        virtual_examples=n_levels,
        source_units=n_levels,
        stimulus_sha256=str(stimulus_sha256),
        levels_db=tuple(levels),
    )
    return inputs, targets, evidence


def _run_namtoclo_convert(namtoclo_bin: Path, nam_path: Path, out_dir: Path) -> Path:
    cmd = [
        str(namtoclo_bin),
        "convert",
        str(nam_path),
        "--output",
        str(out_dir),
        "--tone-match",
        "--reference",
        "auto",
        "--json",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"namtoclo convert failed (exit {proc.returncode}) for {nam_path}\n"
            f"command: {' '.join(cmd)}\nstdout:\n{proc.stdout[-4000:]}\n"
            f"stderr:\n{proc.stderr[-4000:]}"
        )
    complete = None
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("event") == "complete":
            complete = obj
    if complete is None:
        raise RuntimeError(
            f"namtoclo convert produced no 'complete' JSON event for {nam_path}\n"
            f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
        )
    path_str = complete.get("output_gp5gp50")
    if not path_str:
        raise RuntimeError(
            f"namtoclo convert 'complete' event has no output_gp5gp50 field: {complete}"
        )
    path = Path(path_str)
    if not path.is_file():
        raise RuntimeError(f"namtoclo reported output_gp5gp50={path} but it does not exist")
    return path


def compare_one(report_path: Path, namtoclo_bin: Path, nam_root: Path | None) -> dict:
    report = json.loads(report_path.read_text())
    model_name = report.get("model_name", report_path.parent.name)

    nam_path = Path(report.get("stimulus", {}).get("nam_path", ""))
    if not nam_path.is_file():
        if nam_root is None:
            raise RuntimeError(
                f"NAM file from report.json does not exist ({nam_path}) and no "
                "--nam-root was given to search for a replacement."
            )
        matches = list(Path(nam_root).expanduser().rglob(nam_path.name))
        if not matches:
            raise RuntimeError(
                f"NAM file from report.json does not exist ({nam_path}), and no file "
                f"named {nam_path.name!r} was found under --nam-root {nam_root}."
            )
        nam_path = matches[0]

    engine_v2_esr = (report.get("optimizer_fit_evidence_metrics") or {}).get("esr")
    if engine_v2_esr is None:
        raise RuntimeError(
            f"report.json has no optimizer_fit_evidence_metrics.esr: {report_path}"
        )

    level_variants = _load_level_variants(report)
    stimulus_sha256 = report.get("stimulus", {}).get("stimulus_sha256", "")
    inputs, targets, evidence = _build_evidence_and_audio(level_variants, stimulus_sha256)

    with tempfile.TemporaryDirectory(prefix="namtoclo_baseline_") as tmp:
        clo_path = _run_namtoclo_convert(namtoclo_bin, nam_path, Path(tmp))
        model = read_clo(clo_path)

    preds = [
        render_full_with(x, model.pre, model.a, np.array([model.pp, model.pn, model.kp, model.kn]), model.post, model.b)
        for x in inputs
    ]
    shipped_engine_esr = evidence_esr(preds, targets, evidence)

    if shipped_engine_esr > 0:
        relative_improvement = (shipped_engine_esr - float(engine_v2_esr)) / shipped_engine_esr
    else:
        relative_improvement = float("nan")

    direction = "lower" if engine_v2_esr < shipped_engine_esr else "higher or equal"
    pct = abs(relative_improvement) * 100.0 if np.isfinite(relative_improvement) else float("nan")
    verdict = (
        f"EngineV2 ESR ({engine_v2_esr:.6g}) is {direction} than the shipped C++ engine's "
        f"ESR ({shipped_engine_esr:.6g}) against the same NAM-teacher evidence -- "
        f"a {pct:.3g}% relative difference on this one stimulus-fit-to-raw-NAM-teacher "
        "metric only. This is NOT a hardware-validated result and does NOT by itself "
        "resolve whether EngineV2 is actually better: see "
        "ENGINE_V2_RESEARCH_NORTH_STAR.md 'Existing converter baseline' for the full "
        "required evidence (real GP-50 listening, held-out guitar) before treating "
        "this as a resolved comparison."
    )

    return {
        "model_name": model_name,
        "model_key": report.get("model_key"),
        "nam_path": str(nam_path),
        "report_path": str(report_path),
        "shipped_engine_esr": shipped_engine_esr,
        "engine_v2_esr": float(engine_v2_esr),
        "relative_improvement": relative_improvement,
        "stimulus_sha256": stimulus_sha256,
        "levels_db": [float(lv["level_db"]) for lv in level_variants],
        "verdict": verdict,
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=None, help="Regex to select which report.json(s) to compare (matches model_name or output subdirectory name).")
    p.add_argument("--nam-root", default=None, help="Fallback search root for the .nam file if report.json's recorded nam_path no longer exists.")
    p.add_argument("--output-root", required=True, help="An EngineV2 output directory: either one model's directory (containing report.json directly) or a parent containing several model subdirectories.")
    p.add_argument("--namtoclo-bin", default="build-macos/namtoclo", help="Path to the built namtoclo CLI.")
    p.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a human summary line.")
    args = p.parse_args()

    output_root = Path(args.output_root).expanduser()
    if not output_root.exists():
        raise SystemExit(f"--output-root does not exist: {output_root}")
    namtoclo_bin = Path(args.namtoclo_bin).expanduser()
    if not namtoclo_bin.is_file():
        raise SystemExit(f"--namtoclo-bin does not exist: {namtoclo_bin}")
    nam_root = Path(args.nam_root).expanduser() if args.nam_root else None

    reports = _find_reports(output_root, args.model)
    if not reports:
        raise SystemExit(
            f"No report.json found under {output_root}"
            + (f" matching --model {args.model!r}" if args.model else "")
        )

    results = []
    for report_path in reports:
        result = compare_one(report_path, namtoclo_bin, nam_root)
        out_path = report_path.parent / "baseline_report.json"
        out_path.write_text(json.dumps(result, indent=2))
        result["baseline_report_path"] = str(out_path)
        results.append(result)

        if args.json:
            print(json.dumps(result))
        else:
            print(
                f"{result['model_name']}: shipped_engine_esr={result['shipped_engine_esr']:.6g} "
                f"engine_v2_esr={result['engine_v2_esr']:.6g} "
                f"relative_improvement={result['relative_improvement']:.4g} "
                f"-> {out_path}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
