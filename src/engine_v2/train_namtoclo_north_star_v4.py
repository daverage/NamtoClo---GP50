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
import json
import re
import time
from pathlib import Path

import numpy as np

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
from distiller_v2_north_star_v4 import (
    build_stimulus_fit_evidence,
    distill_north_star_v4,
)
from north_star_v4_stimulus import (
    DEFAULT_STIMULUS_LEVELS_DB,
    DEFAULT_STIMULUS_URL,
    ensure_stimulus,
    parse_levels,
    prepare_multilevel_teacher_audio,
)
from train_namtoclo_north_star import (
    _peak_safe_gain_cap,
    _preview_real,
    dump,
    safe,
)


METHOD = "north-star-teacher-student-varpro-v4-t3k-multilevel-stimulus-only"


def _pair_row(p):
    return {
        "task_id": p.task_id,
        "dataset": p.dataset,
        "source": Path(p.source_path).name,
        "start_s": float(p.start_s),
        "duration_s": float(p.duration_s),
        "level_offset_db": float(p.level_offset_db),
    }


def _calibrate_stimulus_post_gain(
    fit_audio,
    a,
    pk,
    b,
    peak_margin_db=0.25,
    max_abs_db=12.0,
):
    """Post-P/K output calibration using stimulus FIT evidence only."""
    if not fit_audio:
        return b.copy(), 0.0, None, None, {"disabled": True, "reason": "no stimulus FIT audio"}
    before = score(fit_audio, a, pk, b)
    requested_db = float(np.clip(-before.signed_level_db, -max_abs_db, max_abs_db))
    cap_db, details = _peak_safe_gain_cap(fit_audio, a, pk, b, peak_margin_db)
    if requested_db > 0.0:
        gain_db = float(np.clip(min(requested_db, max(0.0, cap_db)), 0.0, max_abs_db))
    else:
        gain_db = requested_db
    scaled = b * (10.0 ** (gain_db / 20.0))
    after = score(fit_audio, a, pk, scaled)
    return scaled, gain_db, before, after, {
        "requested_rms_gain_db": requested_db,
        "peak_safe_cap_db": cap_db,
        "peak_margin_db": peak_margin_db,
        "applied_gain_db": gain_db,
        "stage": "B512 post-P/K output",
        "source": "North Star v4 canonical stimulus FIT levels only",
        "limited_by_peak_safety": bool(
            requested_db > 0.0 and gain_db < requested_db - 1e-9
        ),
        "clips": details,
    }


def _find_v3_report(root: Path, pair):
    root = Path(root).expanduser()
    if not root.exists():
        return None
    candidates = []
    for path in root.rglob("report.json"):
        try:
            report = json.loads(path.read_text())
        except Exception:
            continue
        if pair.model_id is not None and report.get("model_id") is not None:
            try:
                if int(report.get("model_id")) != int(pair.model_id):
                    continue
            except Exception:
                continue
        elif report.get("model_key") != pair.model_key:
            continue
        candidates.append((path, report))
    exact = [x for x in candidates if x[1].get("model_key") == pair.model_key]
    if exact:
        candidates = exact
    return candidates[0] if len(candidates) == 1 else None


def _v3_scores(v3_root: Path, pair, comparison_audio):
    found = _find_v3_report(v3_root, pair)
    if found is None:
        return None
    path, report = found
    try:
        a = np.asarray(report["A128"], dtype=np.float64)
        pk = np.asarray(report["pk"], dtype=np.float64)
        b = np.asarray(report["B512_device"], dtype=np.float64)
    except Exception:
        return None
    return {
        "report_path": str(path),
        **{
            role: score(audio, a, pk, b)
            for role, audio in comparison_audio.items()
            if audio
        },
    }


def run_model(pairs, nam, renderer, renderer_sha, renderer_commit, stimulus_path, out, args):
    name = pairs[0].model_name
    print(f"\n=== NORTH STAR v4 STIMULUS ONLY: {name} ===")

    # Reuse the exact deterministic v3 real-guitar partitions only as a fair
    # post-fit comparison. None of this material is passed to the optimizer or
    # output-gain calibration.
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

    fit, stimulus_manifest = prepare_multilevel_teacher_audio(
        nam=nam,
        model_pair=pairs[0],
        renderer=renderer,
        renderer_sha256=renderer_sha,
        renderer_commit=renderer_commit,
        stimulus_path=stimulus_path,
        levels_db=args.stimulus_levels_db,
        cache_root=out / "_v4_teacher_cache",
        slim_value=args.slim,
    )
    evidence = build_stimulus_fit_evidence(
        fit, stimulus_sha256=stimulus_manifest["stimulus_sha256"]
    )

    print(
        f"v4 FIT evidence={evidence.virtual_examples} complete waveform pairs; "
        "one equal-weight unit per global input level"
    )
    print(
        "objective=direct aligned ESR across whole T3K stimulus level variants; "
        "B=target-energy-normalized shared analytic B512; guitar objective terms=NONE"
    )
    print(
        f"search=deterministic multistart ({args.multistarts}) + coarse-to-fine "
        f"FIT-only trajectories (max {args.rounds} rounds); FIT chooses checkpoints"
    )

    started = time.monotonic()
    search_info = {}
    best = distill_north_star_v4(
        fit,
        controls=args.a_controls,
        rounds=args.rounds,
        pk_passes=args.pk_passes,
        multistarts=args.multistarts,
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
            f"POST output gain calibration (stimulus FIT only): "
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

    v3 = _v3_scores(Path(args.v3_root).expanduser(), pairs[0], comparison_audio)
    for role in ("v3_fit_real", "v3_selection_real", "v3_benchmark_real"):
        vm = guitar_metrics[role]
        if v3 and role in v3:
            print(
                f"guitar comparison {role}: V4 ESR={vm.esr:.4f} "
                f"level={vm.signed_level_db:+.2f} dB | "
                f"V3 ESR={v3[role].esr:.4f} level={v3[role].signed_level_db:+.2f} dB"
            )
        else:
            print(
                f"guitar comparison {role}: V4 ESR={vm.esr:.4f} "
                f"level={vm.signed_level_db:+.2f} dB"
            )

    directory = out / safe(pairs[0].model_key + "__" + name)
    directory.mkdir(parents=True, exist_ok=True)
    clo = directory / f"NSV4__{pairs[0].model_id}__{safe(name)}.clo"
    write_clo(clo, a, best.pk, b_device)

    previews = {
        role: _preview_real(role, audio, directory, a, best.pk, b_device)
        for role, audio in comparison_audio.items()
    }

    report = {
        "method": METHOD,
        "north_star_document": "ENGINE_V2_RESEARCH_NORTH_STAR.md",
        "model_name": name,
        "model_key": pairs[0].model_key,
        "tone_id": pairs[0].tone_id,
        "model_id": pairs[0].model_id,
        "optimization": {
            "fit_source": "TONE3000 T3K-sweep-v3.wav rendered independently at multiple global input levels",
            "fit_objective": "direct aligned waveform ESR; one equal-weight target-energy-normalized whole-stimulus unit per level",
            "candidate_choice": "FIT stimulus evidence only",
            "guitar_used_for_training": False,
            "guitar_used_for_candidate_choice": False,
            "guitar_used_for_output_calibration": False,
            "b_solve": "one shared target-energy-normalized analytic B512 per A/P-K candidate",
            "a_and_pk_joint": True,
            "pk_passes": args.pk_passes,
            "multistarts": args.multistarts,
            "max_rounds": args.rounds,
            "search": search_info,
            "level_response_weight": 0.0,
            "distortion_excess_weight": 0.0,
            "isolated_pk_identification": False,
            "named_amp_rules": False,
        },
        "stimulus": stimulus_manifest,
        "fit_evidence": evidence.manifest(fit),
        "guitar_comparison_only": {
            "note": (
                "These real-guitar clips are evaluated only after V4 coefficients are frozen. "
                "They do not affect A/P-K/B or output calibration. Existing V3 partitions are "
                "reused solely for paired development comparison; they are not a fresh held-out claim."
            ),
            "material": {
                role: [_pair_row(p) for p in ps]
                for role, ps in comparison_pairs.items()
            },
            "v4_metrics": guitar_metrics,
            "v3_reference": v3,
        },
        "A128": a,
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

    print(
        f"stimulus: evidence ESR={best.fit.esr:.4f} whole-source ESR={stimulus_whole_metrics.esr:.4f}"
    )
    print("CLO:", clo)
    return report


def main():
    p = argparse.ArgumentParser(
        description=(
            "EngineV2 North Star v4 experiment: train the GP50 student only from the "
            "official TONE3000 T3K sweep rendered through the NAM at multiple input "
            "levels. Real guitar is comparison-only and never influences coefficients."
        )
    )
    p.add_argument("--teacher-root", default="~/NamtoCloTeacherDataset")
    p.add_argument("--nam-root", default="~/NamtoCloNAMCorpus")
    p.add_argument("--output", default="~/NamtoCloNorthStarV4_StimulusOnly")
    p.add_argument("--model-regex")
    p.add_argument("--list-models", action="store_true")
    p.add_argument("--stimulus", default=DEFAULT_STIMULUS_URL)
    p.add_argument(
        "--stimulus-levels",
        default=",".join(f"{v:g}" for v in DEFAULT_STIMULUS_LEVELS_DB),
        help="Comma-separated global dB offsets applied to the unnormalised T3K stimulus.",
    )
    p.add_argument("--compare-fit-real", type=int, default=6)
    p.add_argument("--compare-selection-real", type=int, default=4)
    p.add_argument("--compare-benchmark-real", type=int, default=3)
    p.add_argument("--v3-root", default="~/NamtoCloNorthStarV3_SearchRobust")
    p.add_argument("--a-controls", type=int, default=24)
    p.add_argument("--rounds", type=int, default=DEFAULT_MAX_ROUNDS)
    p.add_argument("--pk-passes", type=int, default=3)
    p.add_argument("--multistarts", type=int, default=DEFAULT_MULTISTARTS)
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
    print("v4 stimulus:", stimulus_path)
    print("v4 stimulus sha256:", sha256_file(stimulus_path))

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
                    groups[key], nam_by_key[key], renderer, renderer_sha,
                    renderer_commit, stimulus_path, out, args
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
                "a_controls": args.a_controls,
                "rounds": args.rounds,
                "pk_passes": args.pk_passes,
                "multistarts": args.multistarts,
                "guitar_used_for_training": False,
                "v3_root": str(Path(args.v3_root).expanduser()),
                "threads": args.threads,
                "yield_ms": args.yield_ms,
            },
        },
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
