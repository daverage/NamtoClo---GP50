#!/usr/bin/env python3
"""Run the V4.2 pipeline (exact same search/objective/DSP as
train_namtoclo_north_star_v42.py) against a single arbitrary .nam file that
is NOT part of the pre-built ~/NamtoCloTeacherDataset corpus.

train_namtoclo_north_star_v42.py's --model-regex path requires the model to
already exist in a built teacher dataset (for model grouping and for the
guitar comparison-only material). For a one-off NAM dropped in, e.g.,
~/Downloads, that infrastructure is unnecessary: the actual FIT stimulus
(TONE3000 T3K-sweep-v3.wav rendered through the NAM at 5 levels) can be
built directly from the .nam file with north_star_v4_stimulus.py. This
script does exactly that, then calls the identical distill_north_star_v42
search used by the batch trainer -- no shortcuts on the objective, DSP,
stimulus, or search mechanics.

The only thing this script cannot do (by design, matching the project's
"guitar is comparison-only" rule) is print a guitar A/B comparison, since
that requires the pre-built teacher dataset's real DI clips for this model,
which don't exist for an ad-hoc NAM. Coefficients/candidate choice never
depended on guitar material anyway (see ENGINE_V2_RESEARCH_NORTH_STAR.md).

Usage:
    python3 -u convert_one_nam_v42.py --nam /path/to/model.nam \
        --output ~/NamtoCloNorthStarV4_2_Adhoc \
        [--renderer /path/to/NeuralAmpModelerCore/build-namtoclo/tools/render]

Output: <output>/<model_key>__<slug>/report.json + NSV42__<slug>.clo, in the
same schema baseline_compare.py already expects (model_name, model_key,
stimulus.nam_path/stimulus_sha256/level_variants, clo_path,
optimizer_fit_evidence_metrics.esr) so it can be scored against the shipped
engine the same way as any batch-trained model.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path
from types import SimpleNamespace

from build_namtoclo_teacher_dataset import (
    DEFAULT_NAMCORE_REF,
    NamModel,
    ensure_namcore_renderer,
    read_nam_header,
    sha256_file,
)
from distiller_v2_dsp import controls_to_a, warm, write_clo
from distiller_v2_north_star_v3 import DEFAULT_MAX_ROUNDS, DEFAULT_MULTISTARTS
from distiller_v2_north_star_v4 import build_stimulus_fit_evidence
from distiller_v2_north_star_v42 import (
    DEFAULT_MAX_LINE_STEPS,
    DEFAULT_POLISH_CYCLES,
    DEFAULT_POLISH_REL_TOL,
    FINE_A_STEP_DB,
    FINE_PK_LOG_STEP,
    distill_north_star_v42,
)
from north_star_v4_stimulus import (
    DEFAULT_STIMULUS_LEVELS_DB,
    DEFAULT_STIMULUS_URL,
    ensure_stimulus,
    parse_levels,
    prepare_multilevel_teacher_audio,
)
from train_namtoclo_north_star import dump
from train_namtoclo_north_star_v4 import _calibrate_stimulus_post_gain

METHOD = "north-star-teacher-student-varpro-v4.2-t3k-multilevel-bracket-refine"


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:120]


def _load_nam_model(nam_path: Path) -> NamModel:
    architecture, version, rate, slimmable = read_nam_header(nam_path)
    return NamModel(
        split="adhoc",
        path=str(nam_path.resolve()),
        sha256=sha256_file(nam_path),
        model_name=nam_path.stem,
        tone_id=None,
        model_id=None,
        architecture=architecture,
        file_version=version,
        expected_sample_rate=rate,
        slimmable=slimmable,
        licence=None,
    )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--nam", required=True, help="Path to the .nam file to convert.")
    p.add_argument("--output", default="~/NamtoCloNorthStarV4_2_Adhoc")
    p.add_argument("--stimulus", default=DEFAULT_STIMULUS_URL)
    p.add_argument("--stimulus-cache-root")
    p.add_argument(
        "--stimulus-levels-db",
        type=parse_levels,
        default=DEFAULT_STIMULUS_LEVELS_DB,
        help="Comma-separated global dB offsets applied to the unnormalised T3K stimulus.",
    )
    p.add_argument("--a-controls", type=int, default=24)
    p.add_argument("--rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    p.add_argument("--pk-passes", type=int, default=3)
    p.add_argument("--multistarts", type=int, default=DEFAULT_MULTISTARTS)
    p.add_argument("--polish-cycles", type=int, default=DEFAULT_POLISH_CYCLES)
    p.add_argument("--polish-rel-tol", type=float, default=DEFAULT_POLISH_REL_TOL)
    p.add_argument("--max-line-steps", type=int, default=DEFAULT_MAX_LINE_STEPS)
    p.add_argument("--yield-ms", type=float, default=0.0)
    p.add_argument("--no-level-calibration", action="store_true")
    p.add_argument("--peak-margin-db", type=float, default=0.25)
    p.add_argument("--renderer")
    p.add_argument("--namcore-source")
    p.add_argument("--namcore-ref", default=DEFAULT_NAMCORE_REF)
    p.add_argument("--build-namcore", action="store_true")
    p.add_argument("--build-jobs", type=int, default=4)
    p.add_argument("--slim", type=float, default=1.0)
    args = p.parse_args()

    nam_path = Path(args.nam).expanduser().resolve()
    if not nam_path.is_file():
        raise SystemExit(f"--nam does not exist: {nam_path}")
    if not 0.0 < args.slim <= 1.0:
        raise SystemExit("--slim must be in (0, 1]")

    out_root = Path(args.output).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    stimulus_path = ensure_stimulus(args.stimulus, out_root / "_stimulus")
    print("v4.2 stimulus:", stimulus_path)
    print("v4.2 stimulus sha256:", sha256_file(stimulus_path))

    nam = _load_nam_model(nam_path)
    print("NAM:", nam.path)
    print("NAM sha256:", nam.sha256)
    print("NAM architecture:", nam.architecture, "sample_rate:", nam.expected_sample_rate)

    renderer, renderer_commit = ensure_namcore_renderer(
        out_root,
        explicit_renderer=args.renderer,
        build_namcore=args.build_namcore,
        namcore_source=args.namcore_source,
        namcore_ref=args.namcore_ref,
        build_jobs=args.build_jobs,
    )
    renderer_sha = sha256_file(renderer)
    print("NAMCore renderer:", renderer)
    print("NAMCore commit:", renderer_commit)

    model_key = f"adhoc_{nam.sha256[:16]}"
    slug = _slug(nam.model_name)
    directory = out_root / f"{model_key}__{slug}"
    directory.mkdir(parents=True, exist_ok=True)

    model_pair = SimpleNamespace(
        model_key=model_key,
        model_name=nam.model_name,
        tone_id=None,
        model_id=None,
        nam_split="adhoc",
    )

    print(f"\n=== NORTH STAR v4.2 STIMULUS + ACCELERATED CONVERGENCE (ad-hoc): {nam.model_name} ===")
    print("TRAINING: TONE3000 canonical stimulus only")
    print("  stimulus:", stimulus_path)
    print("  levels:", ", ".join(f"{v:+g} dB" for v in args.stimulus_levels_db))
    print("NOTE: no pre-built teacher dataset entry for this NAM -- guitar")
    print("      comparison-only material is skipped (never affects fitting).")

    if args.stimulus_cache_root:
        cache_root = Path(args.stimulus_cache_root).expanduser()
    else:
        cache_root = out_root / "_v42_adhoc_teacher_cache"
    print("stimulus teacher cache:", cache_root)

    fit, stimulus_manifest = prepare_multilevel_teacher_audio(
        nam=nam,
        model_pair=model_pair,
        renderer=renderer,
        renderer_sha256=renderer_sha,
        renderer_commit=renderer_commit,
        stimulus_path=stimulus_path,
        levels_db=args.stimulus_levels_db,
        cache_root=cache_root,
        slim_value=args.slim,
    )
    evidence = build_stimulus_fit_evidence(fit, stimulus_sha256=stimulus_manifest["stimulus_sha256"])

    print(
        f"v4.2 FIT evidence={evidence.virtual_examples} complete waveform pairs; "
        "one equal-weight unit per global input level"
    )
    print(
        "objective=EXACT V4 direct aligned ESR across whole T3K level variants; "
        "B=target-energy-normalized shared analytic B512; guitar objective terms=NONE"
    )
    print(
        f"search=no compatible V4-family warm start (ad-hoc NAM); "
        f"exact V4 multistart ({args.multistarts}) x <= {args.rounds} rounds"
    )
    print(
        f"polish=bracket/refine on unchanged fine grid A={FINE_A_STEP_DB:g}dB "
        f"P/K={FINE_PK_LOG_STEP:g}; max-line-distance={args.max_line_steps}, "
        f"<= {args.polish_cycles} cycles"
    )
    print("evaluation path=latest EngineV2 threaded clip evaluation + latest stimulus cache/render path")

    print("warming JIT...")
    warm()

    started = time.monotonic()
    search_info: dict = {}
    best = distill_north_star_v42(
        fit,
        controls=args.a_controls,
        rounds=args.rounds,
        pk_passes=args.pk_passes,
        multistarts=args.multistarts,
        polish_cycles=args.polish_cycles,
        polish_relative_tolerance=args.polish_rel_tol,
        max_line_steps=args.max_line_steps,
        pause_ms=args.yield_ms,
        fit_evidence=evidence,
        search_report=search_info,
    )
    a = controls_to_a(best.controls_db)
    print("final P/K:", " ".join(f"{v:.6g}" for v in best.pk))

    b_uncalibrated = best.b.copy()
    if args.no_level_calibration:
        b_device = b_uncalibrated.copy()
        gain_db = 0.0
        cal_info = {"disabled": True, "stage": "B512 post-P/K output", "source": "disabled by CLI"}
    else:
        b_device, gain_db, cal_before, cal_after, cal_info = _calibrate_stimulus_post_gain(
            fit,
            a,
            best.pk,
            b_uncalibrated,
            peak_margin_db=args.peak_margin_db,
        )
        limited = " PEAK-LIMITED" if cal_info.get("limited_by_peak_safety") else ""
        print(
            f"POST output gain calibration (UNCHANGED V4 rule): "
            f"requested {gain_db:+.2f} dB{limited}"
        )

    clo_path = directory / f"NSV42__adhoc__{slug}.clo"
    write_clo(clo_path, a, best.pk, b_device)
    print("CLO:", clo_path)

    elapsed = time.monotonic() - started
    selected = search_info.get("selected", {})
    if selected:
        print(
            f"convergence: start ESR={selected.get('starting_fit_evidence_esr', float('nan')):.6g} "
            f"-> V4.2 ESR={selected.get('v42_fit_evidence_esr', float('nan')):.6g} "
            f"rel-improve={selected.get('relative_improvement', 0.0):.3g}"
        )
    line = search_info.get("line_polish", {})
    if line:
        print(f"line-polish stop={line.get('stop_reason')} cycles={line.get('cycles_executed')}")

    report = {
        "method": METHOD,
        "model_name": nam.model_name,
        "model_key": model_key,
        "adhoc": True,
        "renderer": str(renderer),
        "renderer_sha256": renderer_sha,
        "renderer_commit": renderer_commit,
        "stimulus": stimulus_manifest,
        "a_controls": args.a_controls,
        "controls_db": list(best.controls_db),
        "pk": list(best.pk),
        "B512_uncalibrated": b_uncalibrated,
        "B512_device": b_device,
        "optimizer_fit_evidence_metrics": best.fit,
        "post_output_gain_calibration_db": gain_db,
        "output_gain_calibration": cal_info,
        "search_info": search_info,
        "elapsed_seconds": elapsed,
        "clo_path": str(clo_path),
    }
    dump(directory / "report.json", report)
    print("report:", directory / "report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
