#!/usr/bin/env python3
from __future__ import annotations

import os
import sys


def _early_thread_cap(argv):
    for i, arg in enumerate(argv):
        if arg.startswith("--threads="):
            try:
                return max(1, int(arg.split("=", 1)[1]))
            except ValueError:
                return None
        if arg == "--threads" and i + 1 < len(argv):
            try:
                return max(1, int(argv[i + 1]))
            except ValueError:
                return None
    return None


_THREAD_CAP = _early_thread_cap(sys.argv[1:])
if _THREAD_CAP:
    for _var in (
        "VECLIB_MAXIMUM_THREADS",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "NUMBA_NUM_THREADS",
    ):
        os.environ[_var] = str(_THREAD_CAP)

import argparse
import re
import time
from pathlib import Path

from build_namtoclo_teacher_dataset import (
    DEFAULT_NAMCORE_REF,
    discover_nams,
    ensure_namcore_renderer,
    model_key as teacher_model_key,
    sha256_file,
)
from distiller_v2_data import group_models, load_audio, load_pairs
from distiller_v2_dsp import controls_to_a, warm, write_clo
from distiller_v2_fit import distortion_excess_report, level_response_report, score
from distiller_v2_north_star import select_north_star_material
from distiller_v2_north_star_v3 import (
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MULTISTARTS,
    MAX_MULTISTARTS,
)
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
from train_namtoclo_north_star import _preview_real, dump, safe
from train_namtoclo_north_star_v4 import (
    _calibrate_stimulus_post_gain,
    _find_v3_report,
    _pair_row,
    _v3_scores,
)


METHOD = "north-star-teacher-student-varpro-v4.2-t3k-multilevel-bracket-refine"


def _compatible_v41_warm_start(root, pair, stimulus_manifest, levels_db, controls):
    """Return a compatible V4.1 A/P-K restart, never its stored B/score."""
    found = _find_v3_report(Path(root).expanduser(), pair)
    if found is None:
        return None
    path, report = found
    if "v4.1" not in str(report.get("method", "")).lower():
        return None

    stimulus = report.get("stimulus") or {}
    if stimulus.get("stimulus_sha256") != stimulus_manifest.get("stimulus_sha256"):
        return None
    try:
        old_levels = tuple(float(v) for v in stimulus.get("levels_db", []))
        new_levels = tuple(float(v) for v in levels_db)
    except Exception:
        return None
    if old_levels != new_levels:
        return None

    controls_db = report.get("controls_db")
    pk = report.get("pk")
    if not isinstance(controls_db, list) or len(controls_db) != int(controls):
        return None
    if not isinstance(pk, list) or len(pk) != 4:
        return None

    return {
        "report_path": str(path),
        "controls_db": controls_db,
        "pk": pk,
        "reported_fit_evidence_esr": (
            (report.get("optimizer_fit_evidence_metrics") or {}).get("esr")
        ),
    }


def run_model(pairs, nam, renderer, renderer_sha, renderer_commit, stimulus_path, out, args):
    name = pairs[0].model_name
    print(f"\n=== NORTH STAR v4.2 STIMULUS + ACCELERATED CONVERGENCE: {name} ===")

    material = select_north_star_material(
        pairs,
        fit_real=args.compare_fit_real,
        selection_real=args.compare_selection_real,
        benchmark_real=args.compare_benchmark_real,
        seed=args.seed,
    )
    comparison_pairs = {
        "v3_fit_real": list(material.fit_real),
        "v3_selection_real": list(material.selection_real),
        "v3_benchmark_real": list(material.benchmark_real),
    }
    comparison_audio = {role: load_audio(ps) for role, ps in comparison_pairs.items()}

    print("TRAINING: TONE3000 canonical stimulus only")
    print("  stimulus:", stimulus_path)
    print("  levels:", ", ".join(f"{v:+g} dB" for v in args.stimulus_levels_db))
    print("GUITAR COMPARISON ONLY (never used for coefficients/candidate choice):")
    for role, ps in comparison_pairs.items():
        print(f"  {role}: {len(ps)} real clips")

    if args.stimulus_cache_root:
        cache_root = Path(args.stimulus_cache_root).expanduser()
    else:
        cache_root = out / "_v42_teacher_cache"
    print("stimulus teacher cache:", cache_root)

    # Uses latest EngineV2 stimulus preparation, including parallel cache misses.
    fit, stimulus_manifest = prepare_multilevel_teacher_audio(
        nam=nam,
        model_pair=pairs[0],
        renderer=renderer,
        renderer_sha256=renderer_sha,
        renderer_commit=renderer_commit,
        stimulus_path=stimulus_path,
        levels_db=args.stimulus_levels_db,
        cache_root=cache_root,
        slim_value=args.slim,
    )
    evidence = build_stimulus_fit_evidence(
        fit, stimulus_sha256=stimulus_manifest["stimulus_sha256"]
    )

    print(
        f"v4.2 FIT evidence={evidence.virtual_examples} complete waveform pairs; "
        "one equal-weight unit per global input level"
    )
    print(
        "objective=EXACT V4 direct aligned ESR across whole T3K level variants; "
        "B=target-energy-normalized shared analytic B512; guitar objective terms=NONE"
    )

    warm_start = None
    if not args.no_v41_warm_start:
        warm_start = _compatible_v41_warm_start(
            args.v41_root,
            pairs[0],
            stimulus_manifest,
            args.stimulus_levels_db,
            args.a_controls,
        )
    if warm_start:
        print("search=WARM START from compatible V4.1 A/P-K, re-evaluate on current FIT and re-solve B")
        print("  warm-start report:", warm_start["report_path"])
        if warm_start.get("reported_fit_evidence_esr") is not None:
            print(
                f"  V4.1 reported FIT ESR: {float(warm_start['reported_fit_evidence_esr']):.6g}"
            )
    else:
        print(
            f"search=no compatible V4.1 warm start; fall back to exact V4 multistart "
            f"({args.multistarts}) x <= {args.rounds} rounds"
        )
    print(
        f"polish=bracket/refine on unchanged fine grid A={FINE_A_STEP_DB:g}dB "
        f"P/K={FINE_PK_LOG_STEP:g}; max-line-distance={args.max_line_steps}, "
        f"<= {args.polish_cycles} cycles"
    )
    print("evaluation path=latest EngineV2 threaded clip evaluation + latest stimulus cache/render path")

    started = time.monotonic()
    search_info = {}
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
        warm_start_controls_db=(warm_start or {}).get("controls_db"),
        warm_start_pk=(warm_start or {}).get("pk"),
        warm_start_source=(warm_start or {}).get("report_path"),
    )
    a = controls_to_a(best.controls_db)
    print("final P/K:", " ".join(f"{v:.6g}" for v in best.pk))

    # Keep V4/V4.1 output calibration unchanged; calibration remains a separate
    # experiment so this run isolates optimizer convergence/runtime mechanics.
    b_uncalibrated = best.b.copy()
    if args.no_level_calibration:
        b_device = b_uncalibrated.copy()
        gain_db = 0.0
        cal_info = {
            "disabled": True,
            "stage": "B512 post-P/K output",
            "source": "disabled by CLI",
        }
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
            f"requested {cal_info['requested_rms_gain_db']:+.2f} dB, "
            f"cap {cal_info['peak_safe_cap_db']:+.2f} dB, "
            f"applied {gain_db:+.2f} dB{limited}"
        )
        print(
            f"  calibration level {cal_before.signed_level_db:+.2f}"
            f"->{cal_after.signed_level_db:+.2f} dB"
        )

    stimulus_whole_metrics = score(fit, a, best.pk, b_device)
    stimulus_level_response = level_response_report(fit, a, best.pk, b_device)
    stimulus_distortion = distortion_excess_report(fit, a, best.pk, b_device)

    guitar_metrics = {
        role: score(audio, a, best.pk, b_device)
        for role, audio in comparison_audio.items()
    }
    all_guitar = []
    for audio in comparison_audio.values():
        all_guitar.extend(audio)
    guitar_metrics["combined"] = score(all_guitar, a, best.pk, b_device)

    v41_ref = _v3_scores(Path(args.v41_root).expanduser(), pairs[0], comparison_audio)
    v4_ref = _v3_scores(Path(args.v4_root).expanduser(), pairs[0], comparison_audio)
    v3_ref = _v3_scores(Path(args.v3_root).expanduser(), pairs[0], comparison_audio)
    for role in ("v3_fit_real", "v3_selection_real", "v3_benchmark_real"):
        vm = guitar_metrics[role]
        bits = [f"V4.2 ESR={vm.esr:.4f} level={vm.signed_level_db:+.2f} dB"]
        if v41_ref and role in v41_ref:
            bits.append(
                f"V4.1 ESR={v41_ref[role].esr:.4f} level={v41_ref[role].signed_level_db:+.2f} dB"
            )
        if v4_ref and role in v4_ref:
            bits.append(
                f"V4 ESR={v4_ref[role].esr:.4f} level={v4_ref[role].signed_level_db:+.2f} dB"
            )
        if v3_ref and role in v3_ref:
            bits.append(
                f"V3 ESR={v3_ref[role].esr:.4f} level={v3_ref[role].signed_level_db:+.2f} dB"
            )
        print(f"guitar comparison {role}: " + " | ".join(bits))

    directory = out / safe(pairs[0].model_key + "__" + name)
    directory.mkdir(parents=True, exist_ok=True)
    clo = directory / f"NSV42__{pairs[0].model_id}__{safe(name)}.clo"
    write_clo(clo, a, best.pk, b_device)

    previews = {
        role: _preview_real(role, audio, directory, a, best.pk, b_device)
        for role, audio in comparison_audio.items()
    }

    report = {
        "method": METHOD,
        "north_star_document": "ENGINE_V2_RESEARCH_NORTH_STAR.md",
        "experiment_document": "NORTH_STAR_V4_2_LINE_CONVERGENCE_EXPERIMENT.md",
        "model_name": name,
        "model_key": pairs[0].model_key,
        "tone_id": pairs[0].tone_id,
        "model_id": pairs[0].model_id,
        "optimization": {
            "fit_source": "TONE3000 T3K-sweep-v3.wav rendered independently at multiple global input levels",
            "fit_objective": "EXACT V4 direct aligned waveform ESR; one equal-weight target-energy-normalized whole-stimulus unit per level",
            "candidate_choice": "FIT stimulus evidence only",
            "v42_only_change": (
                "reuse a compatible V4.1 A/P-K state as an optimization restart when available, "
                "re-evaluate it on current FIT evidence/re-solve B, then use exponential bracket "
                "plus discrete fine-grid refinement instead of walking one fine step at a time"
            ),
            "guitar_used_for_training": False,
            "guitar_used_for_candidate_choice": False,
            "guitar_used_for_output_calibration": False,
            "b_solve": "one shared target-energy-normalized analytic B512 per A/P-K candidate",
            "a_and_pk_joint": True,
            "pk_passes": args.pk_passes,
            "multistarts": args.multistarts,
            "max_rounds": args.rounds,
            "polish_cycles": args.polish_cycles,
            "polish_relative_tolerance": args.polish_rel_tol,
            "max_line_steps": args.max_line_steps,
            "fine_a_step_db": FINE_A_STEP_DB,
            "fine_pk_log_step": FINE_PK_LOG_STEP,
            "search": search_info,
            "level_response_weight": 0.0,
            "distortion_excess_weight": 0.0,
            "isolated_pk_identification": False,
            "named_amp_rules": False,
        },
        "runtime_efficiency": {
            "uses_current_threaded_clip_map": True,
            "uses_current_parallel_multilevel_teacher_render": True,
            "uses_compatible_v41_warm_start": bool(warm_start),
            "warm_start_report": (warm_start or {}).get("report_path"),
            "stimulus_cache_root": str(cache_root),
            "note": "Runtime/search mechanics only; no change to V4 fitting evidence or objective.",
        },
        "stimulus": stimulus_manifest,
        "fit_evidence": evidence.manifest(fit),
        "guitar_comparison_only": {
            "note": (
                "These real-guitar clips are evaluated only after V4.2 coefficients are frozen. "
                "They do not affect A/P-K/B or output calibration. Existing development partitions "
                "are reused for paired comparison and are not fresh held-out evidence."
            ),
            "material": {
                role: [_pair_row(p) for p in ps]
                for role, ps in comparison_pairs.items()
            },
            "v42_metrics": guitar_metrics,
            "v41_reference": v41_ref,
            "v4_reference": v4_ref,
            "v3_reference": v3_ref,
        },
        "A128": a,
        "controls_db": best.controls_db,
        "pk": best.pk,
        "B512_uncalibrated": b_uncalibrated,
        "B512_device": b_device,
        "optimizer_fit_evidence_metrics": best.fit,
        "stimulus_whole_source_metrics": stimulus_whole_metrics,
        "post_output_gain_calibration_db": gain_db,
        "output_gain_calibration": cal_info,
        "diagnostics_only": {
            "stimulus_level_response": stimulus_level_response,
            "stimulus_distortion_excess": stimulus_distortion,
        },
        "cpu_controls": {"threads": args.threads, "yield_ms": args.yield_ms},
        "elapsed_seconds": time.monotonic() - started,
        "clo_path": str(clo),
        "preview_tasks": previews,
    }
    dump(directory / "report.json", report)

    selected = search_info.get("selected", {})
    if selected:
        print(
            f"convergence: start ESR={selected.get('starting_fit_evidence_esr', float('nan')):.6g} "
            f"-> V4.2 ESR={selected.get('v42_fit_evidence_esr', float('nan')):.6g} "
            f"rel-improve={selected.get('relative_improvement', 0.0):.3g}"
        )
    line = search_info.get("line_polish", {})
    if line:
        print(
            f"line-polish stop={line.get('stop_reason')} cycles={line.get('cycles_executed')}"
        )
    print(
        f"stimulus: evidence ESR={best.fit.esr:.4f} whole-source ESR={stimulus_whole_metrics.esr:.4f}"
    )
    print("CLO:", clo)
    return report


def main():
    p = argparse.ArgumentParser(
        description=(
            "EngineV2 North Star v4.2: V4 stimulus-only objective with accelerated "
            "bracket/refine fine-grid convergence. Uses compatible V4.1 A/P-K as a "
            "warm start when available. Real guitar remains comparison-only."
        )
    )
    p.add_argument("--teacher-root", default="~/NamtoCloTeacherDataset")
    p.add_argument("--nam-root", default="~/NamtoCloNAMCorpus")
    p.add_argument("--output", default="~/NamtoCloNorthStarV4_2_LineConverged")
    p.add_argument("--model-regex")
    p.add_argument("--list-models", action="store_true")
    p.add_argument("--stimulus", default=DEFAULT_STIMULUS_URL)
    p.add_argument(
        "--stimulus-levels",
        default=",".join(f"{v:g}" for v in DEFAULT_STIMULUS_LEVELS_DB),
        help="Comma-separated global dB offsets applied to the unnormalised T3K stimulus.",
    )
    p.add_argument(
        "--stimulus-cache-root",
        help=(
            "Optional reusable V4-family teacher cache. Point this at an existing "
            "V4.1 _v41_teacher_cache to avoid re-rendering matching NAM/stimulus levels."
        ),
    )
    p.add_argument("--compare-fit-real", type=int, default=6)
    p.add_argument("--compare-selection-real", type=int, default=4)
    p.add_argument("--compare-benchmark-real", type=int, default=3)
    p.add_argument("--v41-root", default="~/NamtoCloNorthStarV4_1_Converged")
    p.add_argument(
        "--no-v41-warm-start",
        action="store_true",
        help="Ignore compatible V4.1 report and rerun the exact V4 multistart basin search.",
    )
    p.add_argument("--v4-root", default="~/NamtoCloNorthStarV4_StimulusOnly")
    p.add_argument("--v3-root", default="~/NamtoCloNorthStarV3_SearchRobust")
    p.add_argument("--a-controls", type=int, default=24)
    p.add_argument("--rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    p.add_argument("--pk-passes", type=int, default=3)
    p.add_argument("--multistarts", type=int, default=DEFAULT_MULTISTARTS)
    p.add_argument("--polish-cycles", type=int, default=DEFAULT_POLISH_CYCLES)
    p.add_argument("--polish-rel-tol", type=float, default=DEFAULT_POLISH_REL_TOL)
    p.add_argument("--max-line-steps", type=int, default=DEFAULT_MAX_LINE_STEPS)
    p.add_argument("--seed", type=int, default=260910)
    p.add_argument("--threads", type=int, default=0)
    p.add_argument("--yield-ms", type=float, default=0.0)
    p.add_argument("--no-level-calibration", action="store_true")
    p.add_argument("--peak-margin-db", type=float, default=0.25)
    p.add_argument("--renderer")
    p.add_argument("--namcore-source")
    p.add_argument("--namcore-ref", default=DEFAULT_NAMCORE_REF)
    p.add_argument("--build-namcore", action="store_true")
    p.add_argument("--build-jobs", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    p.add_argument("--slim", type=float, default=1.0)
    args = p.parse_args()

    try:
        args.stimulus_levels_db = parse_levels(args.stimulus_levels)
    except ValueError as exc:
        raise SystemExit(f"Invalid --stimulus-levels: {exc}")

    for name in ("compare_fit_real", "compare_selection_real", "compare_benchmark_real"):
        if getattr(args, name) < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be >= 1")
    if args.a_controls < 1 or args.rounds < 1 or args.pk_passes < 1:
        raise SystemExit("--a-controls, --rounds and --pk-passes must be >= 1")
    if not 1 <= args.multistarts <= MAX_MULTISTARTS:
        raise SystemExit(f"--multistarts must be in [1, {MAX_MULTISTARTS}]")
    if args.polish_cycles < 1 or args.max_line_steps < 1:
        raise SystemExit("--polish-cycles and --max-line-steps must be >= 1")
    if args.polish_rel_tol < 0.0:
        raise SystemExit("--polish-rel-tol must be >= 0")
    if args.threads < 0 or args.yield_ms < 0 or args.peak_margin_db < 0:
        raise SystemExit("thread/yield/peak-margin values must be non-negative")
    if args.build_jobs < 1:
        raise SystemExit("--build-jobs must be >= 1")
    if not 0.0 < args.slim <= 1.0:
        raise SystemExit("--slim must be in (0, 1]")

    teacher_root = Path(args.teacher_root).expanduser()
    nam_root = Path(args.nam_root).expanduser()
    out = Path(args.output).expanduser()
    groups = group_models(load_pairs(teacher_root))

    if args.list_models:
        for key in sorted(groups):
            print(groups[key][0].nam_split, groups[key][0].model_name, key)
        return 0

    if not args.model_regex:
        raise SystemExit("Choose --model-regex REGEX or --list-models")
    rx = re.compile(args.model_regex, re.I)
    keys = [k for k in sorted(groups) if rx.search(groups[k][0].model_name)]
    if not keys:
        raise SystemExit("No models matched")

    out.mkdir(parents=True, exist_ok=True)
    stimulus_path = ensure_stimulus(args.stimulus, out / "_stimulus")
    print("v4.2 stimulus:", stimulus_path)
    print("v4.2 stimulus sha256:", sha256_file(stimulus_path))

    nams = discover_nams(
        nam_root,
        include_sealed=False,
        legacy_limit=0,
        seed=args.seed,
    )
    nam_by_key = {teacher_model_key(n): n for n in nams}
    missing = [k for k in keys if k not in nam_by_key]
    if missing:
        names = "\n  ".join(groups[k][0].model_name for k in missing)
        raise SystemExit(
            "Selected teacher models could not be matched to local NAM files under "
            f"{nam_root}:\n  {names}"
        )

    renderer, renderer_commit = ensure_namcore_renderer(
        teacher_root,
        explicit_renderer=args.renderer,
        build_namcore=args.build_namcore,
        namcore_source=args.namcore_source,
        namcore_ref=args.namcore_ref,
        build_jobs=args.build_jobs,
    )
    renderer_sha = sha256_file(renderer)
    print("NAMCore renderer:", renderer)
    print("NAMCore commit:", renderer_commit)
    print("selected:", *[groups[k][0].model_name for k in keys], sep="\n  ")
    if args.threads:
        print(f"native math thread cap: {args.threads}")
    print("warming JIT...")
    warm()

    results = []
    failures = []
    for key in keys:
        try:
            results.append(
                run_model(
                    groups[key],
                    nam_by_key[key],
                    renderer,
                    renderer_sha,
                    renderer_commit,
                    stimulus_path,
                    out,
                    args,
                )
            )
        except Exception as exc:
            print("FAILED", groups[key][0].model_name, exc)
            failures.append({"key": key, "error": str(exc)})

    dump(
        out / "summary.json",
        {
            "method": METHOD,
            "results": results,
            "failures": failures,
            "configuration": {
                "stimulus": str(stimulus_path),
                "stimulus_sha256": sha256_file(stimulus_path),
                "stimulus_levels_db": list(args.stimulus_levels_db),
                "stimulus_cache_root": args.stimulus_cache_root,
                "a_controls": args.a_controls,
                "rounds": args.rounds,
                "pk_passes": args.pk_passes,
                "multistarts": args.multistarts,
                "polish_cycles": args.polish_cycles,
                "polish_relative_tolerance": args.polish_rel_tol,
                "max_line_steps": args.max_line_steps,
                "fine_a_step_db": FINE_A_STEP_DB,
                "fine_pk_log_step": FINE_PK_LOG_STEP,
                "guitar_used_for_training": False,
                "v41_root": str(Path(args.v41_root).expanduser()),
                "v41_warm_start_enabled": not args.no_v41_warm_start,
                "v4_root": str(Path(args.v4_root).expanduser()),
                "v3_root": str(Path(args.v3_root).expanduser()),
                "threads": args.threads,
                "yield_ms": args.yield_ms,
            },
        },
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
