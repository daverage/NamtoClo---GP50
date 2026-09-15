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
  - the exact CLO byte layout the shipped engine writes, decoded by
    clo_reader.py (a port of src/core/clo_refiner.cpp's parseModel).

CORRECTNESS NOTE (why both sides are scored from the actual final CLO file):
train_namtoclo_north_star_v42.py's report.json records
`optimizer_fit_evidence_metrics` = the fit computed on the *uncalibrated* B,
before `_calibrate_stimulus_post_gain` rescales B into the B that is actually
written to the CLO (`write_clo(clo, a, best.pk, b_device)`). Since ESR is not
scale-invariant, that stored number can disagree with how the real, final CLO
file actually scores. So this script never reads
`optimizer_fit_evidence_metrics` -- it decodes and renders
`report["clo_path"]` (EngineV2's own final output file) exactly the same way
it decodes and renders the shipped engine's output, through the same
`_score_clo` function, and scores both from that rendering.

IMPORTANT LIMITATION: this only measures fit to a raw-NAM teacher on one
synthetic sweep stimulus. It is NOT a hardware-validated result and does not
by itself answer "is EngineV2 better" -- see the verdict field this script
writes, and ENGINE_V2_RESEARCH_NORTH_STAR.md for the full evidence bar
(real GP-50 listening, held-out guitar) that a real answer requires.

This script is read-only comparison tooling: it never feeds any result back
into EngineV2 coefficients, weights, training, or fitting in any way.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from clo_reader import read_clo
from distiller_v2_dsp import render_full_with
from distiller_v2_fit import _map
from distiller_v2_north_star_v2 import evidence_esr
from distiller_v2_north_star_v4 import StimulusFitEvidence, EvidenceWindow

# Bump this whenever the scoring path here (render/score function, evidence
# reconstruction, etc.) changes in a way that could change the numbers -- it
# is folded into the baseline cache key so old cache entries are invalidated
# automatically rather than silently reused with a stale scorer.
SCORER_VERSION = 1

# Winner is decided by lower ESR. Below this relative gap the two sides are
# considered a tie rather than crowning a winner on noise.
TIE_RELATIVE_EPSILON = 1e-6

DEFAULT_BASELINE_CACHE_DIRNAME = "_baseline_cache"


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def _score_clo(clo_path: Path, inputs: list[np.ndarray], targets: list[np.ndarray], evidence) -> float:
    """Decode a compact VTSI CLO and score it against evidence.

    This is the SINGLE render/score code path used for both the released
    (shipped C++ engine) CLO and the EngineV2 CLO -- deliberately, so neither
    side can drift from the other. Rendering across the (independent) 5
    stimulus levels is parallelized with distiller_v2_fit._map (the existing
    nogil-njit threaded helper); `_map` wraps `ThreadPoolExecutor.map`, which
    preserves input order in its results regardless of which worker finishes
    first, so the returned per-level predictions line up with `evidence`'s
    window/level order deterministically.
    """
    model = read_clo(clo_path)
    pk = np.array([model.pp, model.pn, model.kp, model.kn], dtype=np.float64)
    preds = _map(
        lambda x: render_full_with(x, model.pre, model.a, pk, model.post, model.b),
        inputs,
    )
    return evidence_esr(preds, targets, evidence)


def _run_namtoclo_convert(namtoclo_bin: Path, nam_path: Path, out_dir: Path) -> tuple[Path, list[str]]:
    argv = [
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
    proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"namtoclo convert failed (exit {proc.returncode}) for {nam_path}\n"
            f"command: {' '.join(argv)}\nstdout:\n{proc.stdout[-4000:]}\n"
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
    return path, argv


# ---------------------------------------------------------------------------
# Baseline (released-engine) cache
# ---------------------------------------------------------------------------


def _argv_identity(nam_path: Path) -> list[str]:
    """The part of the conversion argv that is meaningful to cache identity.

    The namtoclo binary path/hash and the ephemeral --output tmp directory
    are tracked separately (binary via its own SHA256, output dir is never
    stable across runs), so they're excluded here to avoid spurious cache
    misses; everything that actually controls what gets produced is here.
    """
    return ["convert", str(nam_path), "--tone-match", "--reference", "auto", "--json"]


def _cache_identity(
    nam_sha256: str,
    namtoclo_bin_sha256: str,
    argv_identity: list[str],
    stimulus_sha256: str,
    levels_db: tuple[float, ...],
) -> dict:
    return {
        "scorer_version": SCORER_VERSION,
        "nam_sha256": nam_sha256,
        "namtoclo_bin_sha256": namtoclo_bin_sha256,
        "argv": list(argv_identity),
        "stimulus_sha256": stimulus_sha256,
        "levels_db": [float(x) for x in levels_db],
    }


def _cache_key(identity: dict) -> str:
    blob = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(blob.encode("utf-8"))


def _cache_paths(cache_root: Path, key: str) -> tuple[Path, Path]:
    return cache_root / f"{key}.json", cache_root / f"{key}.clo"


def get_baseline_result(
    *,
    nam_path: Path,
    namtoclo_bin: Path,
    namtoclo_bin_sha256: str,
    stimulus_sha256: str,
    levels_db: tuple[float, ...],
    inputs: list[np.ndarray],
    targets: list[np.ndarray],
    evidence,
    cache_root: Path,
    use_cache: bool,
    force: bool,
) -> dict:
    """Return {esr, clo_path, clo_sha256, provenance, cache_hit}.

    On a cache hit (identity match, cache disk state intact), skips both
    `namtoclo convert` and the render/score pass entirely by reusing the
    stored ESR and the cached CLO file. On a miss (or when caching is
    disabled/forced-refreshed), runs the conversion, renders+scores through
    `_score_clo` (the same function used for the EngineV2 side), and writes a
    fresh cache entry (overwriting any stale one) unless `--no-baseline-cache`
    was given.
    """
    nam_sha256 = _sha256_file(nam_path)
    argv_identity = _argv_identity(nam_path)
    identity = _cache_identity(nam_sha256, namtoclo_bin_sha256, argv_identity, stimulus_sha256, levels_db)
    key = _cache_key(identity)
    entry_path, clo_cache_path = _cache_paths(cache_root, key)

    if use_cache and not force and entry_path.is_file() and clo_cache_path.is_file():
        try:
            entry = json.loads(entry_path.read_text())
        except Exception:
            entry = None
        if entry is not None and entry.get("identity") == identity:
            # Identity matches exactly (including nam/bin SHAs, argv,
            # stimulus SHA, levels_db, and scorer_version) -- trust the
            # cached ESR and CLO without re-running or re-rendering.
            return {
                "esr": float(entry["esr"]),
                "clo_path": clo_cache_path,
                "clo_sha256": entry["clo_sha256"],
                "provenance": entry["provenance"],
                "cache_hit": True,
            }

    # Miss (or cache disabled/forced): run the real conversion.
    with tempfile.TemporaryDirectory(prefix="namtoclo_baseline_") as tmp:
        clo_path, exact_argv = _run_namtoclo_convert(namtoclo_bin, nam_path, Path(tmp))
        clo_sha256 = _sha256_file(clo_path)
        esr = _score_clo(clo_path, inputs, targets, evidence)

        cache_root.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(clo_path, clo_cache_path)

    provenance = {
        "namtoclo_bin_path": str(namtoclo_bin.resolve()),
        "namtoclo_bin_sha256": namtoclo_bin_sha256,
        "nam_path": str(nam_path),
        "nam_sha256": nam_sha256,
        "stimulus_sha256": stimulus_sha256,
        "levels_db": [float(x) for x in levels_db],
        "argv": exact_argv,
        "clo_sha256": clo_sha256,
    }

    if use_cache:
        entry = {
            "identity": identity,
            "esr": float(esr),
            "clo_sha256": clo_sha256,
            "provenance": provenance,
        }
        entry_path.write_text(json.dumps(entry, indent=2))

    return {
        "esr": float(esr),
        "clo_path": clo_cache_path,
        "clo_sha256": clo_sha256,
        "provenance": provenance,
        "cache_hit": False,
    }


# ---------------------------------------------------------------------------
# Scoring math (pure, standalone -- kept separate from I/O for testability)
# ---------------------------------------------------------------------------


def _compute_winner(released_engine_esr: float, engine_v2_esr: float) -> tuple[float, float, str]:
    """Return (esr_delta, relative_improvement, winner).

    Sign convention: esr_delta = released - engine_v2, so a POSITIVE
    esr_delta means EngineV2 has the lower (better) ESR. relative_improvement
    follows the same convention: positive means EngineV2 improved on the
    released engine. Winner is decided by lower ESR, with ties (relative gap
    below TIE_RELATIVE_EPSILON) reported as "tie" rather than picking a side
    on noise.
    """
    esr_delta = released_engine_esr - engine_v2_esr
    if released_engine_esr != 0:
        relative_improvement = (released_engine_esr - engine_v2_esr) / released_engine_esr
    else:
        relative_improvement = float("nan")

    denom = max(abs(released_engine_esr), abs(engine_v2_esr), 1e-30)
    rel_diff = abs(released_engine_esr - engine_v2_esr) / denom
    if rel_diff < TIE_RELATIVE_EPSILON:
        winner = "tie"
    elif engine_v2_esr < released_engine_esr:
        winner = "engine_v2"
    else:
        winner = "released"
    return esr_delta, relative_improvement, winner


def _build_summary(results: list[dict], failures: list[dict], namtoclo_bin_path: str, namtoclo_bin_sha256: str) -> dict:
    improvements = [r["relative_improvement"] for r in results if np.isfinite(r["relative_improvement"])]
    return {
        "num_models_compared": len(results),
        "num_models_attempted": len(results) + len(failures),
        "engine_v2_wins": sum(1 for r in results if r["winner"] == "engine_v2"),
        "released_wins": sum(1 for r in results if r["winner"] == "released"),
        "ties": sum(1 for r in results if r["winner"] == "tie"),
        "mean_relative_improvement": statistics.fmean(improvements) if improvements else None,
        "median_relative_improvement": statistics.median(improvements) if improvements else None,
        "namtoclo_bin_path": namtoclo_bin_path,
        "namtoclo_bin_sha256": namtoclo_bin_sha256,
        "results": results,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Per-model comparison
# ---------------------------------------------------------------------------


def compare_one(
    report_path: Path,
    namtoclo_bin: Path,
    namtoclo_bin_sha256: str,
    nam_root: Path | None,
    cache_root: Path,
    use_cache: bool,
    force_baseline: bool,
    progress_prefix: str = "",
) -> dict:
    print(f"{progress_prefix}", flush=True, end="")
    report = json.loads(report_path.read_text())
    model_name = report.get("model_name", report_path.parent.name)
    print(f"{model_name}", flush=True)

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
    nam_sha256 = _sha256_file(nam_path)

    engine_v2_clo_path_str = report.get("clo_path")
    if not engine_v2_clo_path_str:
        raise RuntimeError(
            f"report.json has no 'clo_path' field -- can't score EngineV2's actual "
            f"final CLO output: {report_path}"
        )
    engine_v2_clo_path = Path(engine_v2_clo_path_str)
    if not engine_v2_clo_path.is_file():
        raise RuntimeError(
            f"report.json's clo_path does not exist: {engine_v2_clo_path} "
            f"(from {report_path})"
        )

    level_variants = _load_level_variants(report)
    stimulus_sha256 = report.get("stimulus", {}).get("stimulus_sha256", "")
    inputs, targets, evidence = _build_evidence_and_audio(level_variants, stimulus_sha256)
    levels_db = tuple(float(lv["level_db"]) for lv in level_variants)

    print("  scoring EngineV2's final CLO...", flush=True)
    engine_v2_clo_sha256 = _sha256_file(engine_v2_clo_path)
    engine_v2_esr = _score_clo(engine_v2_clo_path, inputs, targets, evidence)

    identity = _cache_identity(nam_sha256, namtoclo_bin_sha256, _argv_identity(nam_path), stimulus_sha256, levels_db)
    key = _cache_key(identity)
    entry_path, _ = _cache_paths(cache_root, key)
    would_hit = use_cache and not force_baseline and entry_path.is_file()
    if would_hit:
        print("  baseline cache: HIT", flush=True)
    else:
        print("  baseline cache: MISS", flush=True)
        print("  running released converter...", flush=True)
        print("  rendering 5 stimulus levels...", flush=True)

    baseline = get_baseline_result(
        nam_path=nam_path,
        namtoclo_bin=namtoclo_bin,
        namtoclo_bin_sha256=namtoclo_bin_sha256,
        stimulus_sha256=stimulus_sha256,
        levels_db=levels_db,
        inputs=inputs,
        targets=targets,
        evidence=evidence,
        cache_root=cache_root,
        use_cache=use_cache,
        force=force_baseline,
    )
    released_engine_esr = baseline["esr"]

    esr_delta, relative_improvement, winner = _compute_winner(released_engine_esr, engine_v2_esr)

    direction = "lower" if engine_v2_esr < released_engine_esr else "higher or equal"
    pct = abs(relative_improvement) * 100.0 if np.isfinite(relative_improvement) else float("nan")
    verdict = (
        f"EngineV2 ESR ({engine_v2_esr:.6g}), rendered from its actual final CLO "
        f"file ({engine_v2_clo_path.name}), is {direction} than the shipped C++ "
        f"engine's ESR ({released_engine_esr:.6g}, also rendered from its actual "
        f"final CLO file) against the same NAM-teacher evidence -- a {pct:.3g}% "
        "relative difference on this one stimulus-fit-to-raw-NAM-teacher metric "
        "only. This is NOT a hardware-validated result and does NOT by itself "
        "resolve whether EngineV2 is actually better: see "
        "ENGINE_V2_RESEARCH_NORTH_STAR.md 'Existing converter baseline' for the full "
        "required evidence (real GP-50 listening, held-out guitar) before treating "
        "this as a resolved comparison."
    )

    result = {
        "model_name": model_name,
        "model_key": report.get("model_key"),
        "nam_path": str(nam_path),
        "nam_sha256": nam_sha256,
        "report_path": str(report_path),
        "engine_v2_report_path": str(report_path),
        "engine_v2_clo_path": str(engine_v2_clo_path),
        "engine_v2_clo_sha256": engine_v2_clo_sha256,
        "released_clo_path": str(baseline["clo_path"]),
        "released_clo_sha256": baseline["clo_sha256"],
        "namtoclo_bin_path": str(namtoclo_bin.resolve()),
        "namtoclo_bin_sha256": namtoclo_bin_sha256,
        "stimulus_sha256": stimulus_sha256,
        "levels_db": list(levels_db),
        "released_engine_esr": released_engine_esr,
        "engine_v2_esr": float(engine_v2_esr),
        "esr_delta": esr_delta,
        "relative_improvement": relative_improvement,
        "winner": winner,
        "baseline_cache_hit": baseline["cache_hit"],
        "baseline_provenance": baseline["provenance"],
        "verdict": verdict,
        # Kept for backwards compatibility with earlier consumers of this
        # script's output; prefer released_engine_esr/engine_v2_esr above.
        "shipped_engine_esr": released_engine_esr,
    }
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=None, help="Regex to select which report.json(s) to compare (matches model_name or output subdirectory name).")
    p.add_argument("--nam-root", default=None, help="Fallback search root for the .nam file if report.json's recorded nam_path no longer exists.")
    p.add_argument("--output-root", required=True, help="An EngineV2 output directory: either one model's directory (containing report.json directly) or a parent containing several model subdirectories.")
    p.add_argument("--namtoclo-bin", default="build-macos/namtoclo", help="Path to the built namtoclo CLI.")
    p.add_argument("--baseline-cache-root", default=None, help=f"Directory for the persistent released-engine baseline cache (default: <output-root>/{DEFAULT_BASELINE_CACHE_DIRNAME}).")
    p.add_argument("--no-baseline-cache", action="store_true", help="Never read or write the baseline cache; always recompute the released-engine result.")
    p.add_argument("--force-baseline", action="store_true", help="Ignore any existing baseline cache hit and recompute the released-engine result, then refresh the cache.")
    p.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a human summary line.")
    args = p.parse_args()

    output_root = Path(args.output_root).expanduser()
    if not output_root.exists():
        raise SystemExit(f"--output-root does not exist: {output_root}")
    namtoclo_bin = Path(args.namtoclo_bin).expanduser()
    if not namtoclo_bin.is_file():
        raise SystemExit(f"--namtoclo-bin does not exist: {namtoclo_bin}")
    nam_root = Path(args.nam_root).expanduser() if args.nam_root else None

    cache_root = (
        Path(args.baseline_cache_root).expanduser()
        if args.baseline_cache_root
        else output_root / DEFAULT_BASELINE_CACHE_DIRNAME
    )
    use_cache = not args.no_baseline_cache

    namtoclo_bin_sha256 = _sha256_file(namtoclo_bin)

    reports = _find_reports(output_root, args.model)
    if not reports:
        raise SystemExit(
            f"No report.json found under {output_root}"
            + (f" matching --model {args.model!r}" if args.model else "")
        )

    results = []
    failures = []
    n = len(reports)
    for i, report_path in enumerate(reports, start=1):
        try:
            result = compare_one(
                report_path,
                namtoclo_bin,
                namtoclo_bin_sha256,
                nam_root,
                cache_root,
                use_cache,
                args.force_baseline,
                progress_prefix=f"[{i}/{n}] ",
            )
        except Exception as exc:  # noqa: BLE001 -- one model's failure must not abort the run
            failures.append({"report_path": str(report_path), "error": str(exc)})
            print(f"  FAILED: {exc}", flush=True)
            continue

        out_path = report_path.parent / "baseline_report.json"
        out_path.write_text(json.dumps(result, indent=2))
        result["baseline_report_path"] = str(out_path)
        results.append(result)

        if args.json:
            print(json.dumps(result))
        else:
            print(f"  released: {result['released_engine_esr']:.6g}", flush=True)
            print(f"  EngineV2: {result['engine_v2_esr']:.6g}", flush=True)
            pct = result["relative_improvement"] * 100.0 if np.isfinite(result["relative_improvement"]) else float("nan")
            print(f"  EngineV2 improvement: {pct:.3g}%", flush=True)
            print(f"  winner: {result['winner']}", flush=True)
            print(f"  baseline cache: {'HIT' if result['baseline_cache_hit'] else 'MISS'}", flush=True)
            print(f"  -> {out_path}", flush=True)

    # Always write the aggregate summary (even for a single-model run) so
    # downstream tooling has one stable path to look at; per-model detail
    # still lives in each model's own baseline_report.json.
    summary = _build_summary(results, failures, str(namtoclo_bin.resolve()), namtoclo_bin_sha256)
    summary_path = output_root / "baseline_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    if not args.json:
        print(
            f"\nSummary: {summary['num_models_compared']}/{summary['num_models_attempted']} compared, "
            f"engine_v2 wins={summary['engine_v2_wins']} released wins={summary['released_wins']} "
            f"ties={summary['ties']} failures={len(failures)} -> {summary_path}",
            flush=True,
        )

    return 0 if not failures or results else 1


if __name__ == "__main__":
    sys.exit(main())
