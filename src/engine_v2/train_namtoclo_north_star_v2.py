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

from distiller_v2_data import (
    flatten_groups,
    group_models,
    level_sweep_groups,
    load_audio,
    load_pairs,
)
from distiller_v2_dsp import controls_to_a, warm, write_clo
from distiller_v2_fit import distortion_excess_report, level_response_report
from distiller_v2_north_star import NORTH_STAR_PROBES, select_north_star_material
from distiller_v2_north_star_v2 import build_fit_evidence, distill_north_star_v2
from train_namtoclo_north_star import (
    _calibrate_post_gain,
    _group_task_ids,
    _metrics_for_roles,
    _preview_real,
    _print_distortion_report,
    _print_level_report,
    dump,
    safe,
)


METHOD = "north-star-teacher-student-varpro-v2-segmented-level-balanced"


def run_model(pairs, out, args):
    name = pairs[0].model_name
    print(f"\n=== NORTH STAR v2: {name} ===")
    material = select_north_star_material(
        pairs,
        fit_real=args.fit_real,
        selection_real=args.selection_real,
        benchmark_real=args.benchmark_real,
        seed=args.seed,
    )

    print("FIT synthetic source probes:")
    for p in material.probes:
        print(" ", Path(p.source_path).name)
    print("FIT real:")
    for p in material.fit_real:
        print(f"  {p.dataset}: {Path(p.source_path).name} @ {p.start_s:.2f}s")
    print("SELECTION real:")
    for p in material.selection_real:
        print(f"  {p.dataset}: {Path(p.source_path).name} @ {p.start_s:.2f}s")
    print("BENCHMARK real:")
    for p in material.benchmark_real:
        print(f"  {p.dataset}: {Path(p.source_path).name} @ {p.start_s:.2f}s")

    fit = load_audio(material.fit)
    selection = load_audio(material.selection)
    benchmark = load_audio(material.benchmark)
    evidence = build_fit_evidence(fit)

    probe_window_counts = {}
    for (_, _, pair), windows in zip(fit, evidence.windows):
        if bool(getattr(pair, "synthetic", False)):
            probe_window_counts[Path(pair.source_path).name] = len(windows)

    print(
        f"source material fit={len(fit)} ({len(material.probes)} synthetic probe families + "
        f"{len(material.fit_real)} real) selection={len(selection)} real "
        f"benchmark={len(benchmark)} real"
    )
    print(
        f"v2 FIT evidence={evidence.virtual_examples} normalized windows; "
        "each probe family total weight = one real FIT clip"
    )
    for basename, count in probe_window_counts.items():
        print(f"  evidence {basename}: {count} window{'s' if count != 1 else ''}")
    print(
        "objective=level-balanced direct aligned ESR; "
        "B=segmented/level-balanced shared analytic solve; "
        "level/distortion penalties=OFF"
    )

    # Matched real-guitar sweeps remain diagnostics only, exactly as in v1.
    lfg = level_sweep_groups(pairs, "fit", args.level_groups, args.seed ^ 0x1E7E1)
    lsg = level_sweep_groups(pairs, "selection", args.level_groups, args.seed ^ 0x51E71)
    lbg = level_sweep_groups(pairs, "benchmark", args.level_groups, args.seed ^ 0xB3E71)

    started = time.monotonic()
    best = distill_north_star_v2(
        fit,
        selection,
        controls=args.a_controls,
        rounds=args.rounds,
        pk_passes=args.pk_passes,
        pause_ms=args.yield_ms,
        fit_evidence=evidence,
    )
    a = controls_to_a(best.controls_db)
    print("final P/K:", " ".join(f"{v:.6g}" for v in best.pk))

    b_uncalibrated = best.b.copy()
    pre_metrics = _metrics_for_roles(
        fit, selection, benchmark, a, best.pk, b_uncalibrated
    )

    if args.no_level_calibration:
        b_device = b_uncalibrated.copy()
        gain_db = 0.0
        cal_info = {
            "disabled": True,
            "stage": "B512 post-P/K output",
            "source": "North Star real FIT material only",
        }
    else:
        b_device, gain_db, cal_before, cal_after, cal_info = _calibrate_post_gain(
            fit,
            a,
            best.pk,
            b_uncalibrated,
            peak_margin_db=args.peak_margin_db,
        )
        limited = " PEAK-LIMITED" if cal_info.get("limited_by_peak_safety") else ""
        print(
            f"POST output gain calibration (real FIT only): "
            f"requested {cal_info['requested_rms_gain_db']:+.2f} dB, "
            f"cap {cal_info['peak_safe_cap_db']:+.2f} dB, "
            f"applied {gain_db:+.2f} dB{limited}"
        )
        print(
            f"  calibration level {cal_before.signed_level_db:+.2f}"
            f"->{cal_after.signed_level_db:+.2f} dB"
        )

    final_metrics = _metrics_for_roles(
        fit, selection, benchmark, a, best.pk, b_device
    )

    level_audio = {
        "fit": load_audio(flatten_groups(lfg)),
        "selection": load_audio(flatten_groups(lsg)),
        "benchmark": load_audio(flatten_groups(lbg)),
    }
    level_reports = {
        role: level_response_report(audio, a, best.pk, b_device)
        for role, audio in level_audio.items()
    }
    distortion_reports = {
        role: distortion_excess_report(audio, a, best.pk, b_device)
        for role, audio in level_audio.items()
    }
    for role in ("fit", "selection", "benchmark"):
        _print_level_report(role, level_reports[role])
        _print_distortion_report(role, distortion_reports[role])

    directory = out / safe(pairs[0].model_key + "__" + name)
    directory.mkdir(parents=True, exist_ok=True)
    clo = directory / "distilled.clo"
    write_clo(clo, a, best.pk, b_device)

    previews = {
        role: _preview_real(role, audio, directory, a, best.pk, b_device)
        for role, audio in (
            ("fit", fit),
            ("selection", selection),
            ("benchmark", benchmark),
        )
    }

    report = {
        "method": METHOD,
        "north_star_document": "ENGINE_V2_RESEARCH_NORTH_STAR.md",
        "model_name": name,
        "model_key": pairs[0].model_key,
        "tone_id": pairs[0].tone_id,
        "model_id": pairs[0].model_id,
        "optimization": {
            "fit_objective": (
                "direct aligned ESR over level-balanced virtual evidence windows"
            ),
            "selection_gate": (
                "mean aligned ESR across disjoint real SELECTION examples"
            ),
            "b_solve": (
                "one shared segmented/level-balanced analytic B512 solve "
                "per A/P-K candidate"
            ),
            "synthetic_vs_real_balance": (
                "six probe-family units + six real-clip units by default"
            ),
            "a_and_pk_joint": True,
            "pk_passes": args.pk_passes,
            "level_response_weight": 0.0,
            "distortion_excess_weight": 0.0,
            "isolated_pk_identification": False,
            "named_amp_rules": False,
        },
        "material": material.manifest(),
        "fit_evidence": evidence.manifest(fit),
        "A128": a,
        "pk": best.pk,
        "B512_uncalibrated": b_uncalibrated,
        "B512_device": b_device,
        "optimizer_fit_evidence_metrics": best.fit,
        "optimizer_selection_metrics": best.selection,
        "whole_source_pre_calibration_metrics": pre_metrics,
        "post_output_gain_calibration_db": gain_db,
        "output_gain_calibration": cal_info,
        "whole_source_fit_metrics": final_metrics["fit"],
        "selection_metrics": final_metrics["selection"],
        "benchmark_metrics": final_metrics["benchmark"],
        "diagnostics_only": {
            "level_response": level_reports,
            "distortion_excess": distortion_reports,
            "level_sweep_task_ids": {
                "fit": _group_task_ids(lfg),
                "selection": _group_task_ids(lsg),
                "benchmark": _group_task_ids(lbg),
            },
        },
        "cpu_controls": {"threads": args.threads, "yield_ms": args.yield_ms},
        "elapsed_seconds": time.monotonic() - started,
        "clo_path": str(clo),
        "preview_tasks": previews,
    }
    dump(directory / "report.json", report)

    bm = final_metrics["benchmark"]
    print(
        f"benchmark: ESR={bm.esr:.4f} composite(diag)={bm.composite:.4f} "
        f"level={bm.signed_level_db:+.2f} dB"
    )
    print("CLO:", clo)
    return report


def main():
    p = argparse.ArgumentParser(
        description=(
            "EngineV2 North Star v2. Uses the same canonical source material as v1, "
            "but balances the logical level/frequency/transient windows inside the "
            "synthetic probes so quiet operating points contribute directly."
        )
    )
    p.add_argument("--teacher-root", default="~/NamtoCloTeacherDataset")
    p.add_argument("--output", default="~/NamtoCloNorthStarV2")
    p.add_argument("--model-regex")
    p.add_argument("--list-models", action="store_true")
    p.add_argument("--fit-real", type=int, default=6)
    p.add_argument("--selection-real", type=int, default=4)
    p.add_argument("--benchmark-real", type=int, default=3)
    p.add_argument("--a-controls", type=int, default=24)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--pk-passes", type=int, default=3)
    p.add_argument("--seed", type=int, default=260910)
    p.add_argument(
        "--threads",
        type=int,
        default=0,
        help="Cap native math-library worker threads; 0 keeps library defaults.",
    )
    p.add_argument(
        "--yield-ms",
        type=float,
        default=0.0,
        help="Sleep this many ms after each candidate to reduce sustained CPU load.",
    )
    p.add_argument(
        "--level-groups",
        type=int,
        default=3,
        help="Matched real-guitar sweep groups per role for diagnostics only.",
    )
    p.add_argument(
        "--no-level-calibration",
        action="store_true",
        help="Disable final fit-only post-P/K B512 loudness correction.",
    )
    p.add_argument(
        "--peak-margin-db",
        type=float,
        default=0.25,
        help="Allowed CLO overshoot above FIT NAM peak/p99.9 in post-output calibration.",
    )
    args = p.parse_args()

    for name in ("fit_real", "selection_real", "benchmark_real"):
        if getattr(args, name) < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be >= 1")
    if args.a_controls < 1:
        raise SystemExit("--a-controls must be >= 1")
    if args.rounds < 1:
        raise SystemExit("--rounds must be >= 1")
    if args.pk_passes < 1:
        raise SystemExit("--pk-passes must be >= 1")
    if args.threads < 0:
        raise SystemExit("--threads must be >= 0")
    if args.yield_ms < 0:
        raise SystemExit("--yield-ms must be >= 0")
    if args.level_groups < 0:
        raise SystemExit("--level-groups must be >= 0")
    if args.peak_margin_db < 0:
        raise SystemExit("--peak-margin-db must be >= 0")

    root = Path(args.teacher_root).expanduser()
    out = Path(args.output).expanduser()
    groups = group_models(load_pairs(root))

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
    print("NORTH STAR v2 canonical probes:")
    for label, basename in NORTH_STAR_PROBES:
        print(f"  {label}: {basename}")
    print("selected:", *[groups[k][0].model_name for k in keys], sep="\n  ")
    if args.threads:
        print(f"native math thread cap: {args.threads}")
    if args.yield_ms:
        print(f"CPU yield: {args.yield_ms:g} ms/candidate")
    print("warming JIT...")
    warm()

    results = []
    failures = []
    for key in keys:
        try:
            results.append(run_model(groups[key], out, args))
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
                "fit_real": args.fit_real,
                "selection_real": args.selection_real,
                "benchmark_real": args.benchmark_real,
                "a_controls": args.a_controls,
                "rounds": args.rounds,
                "pk_passes": args.pk_passes,
                "level_response_weight": 0.0,
                "distortion_excess_weight": 0.0,
                "diagnostic_level_groups": args.level_groups,
                "threads": args.threads,
                "yield_ms": args.yield_ms,
            },
        },
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
