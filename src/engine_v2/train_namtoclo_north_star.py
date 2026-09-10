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
import math
import re
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import soundfile as sf

from distiller_v2_data import (
    flatten_groups,
    group_models,
    level_sweep_groups,
    load_audio,
    load_pairs,
)
from distiller_v2_dsp import SR, controls_to_a, render_full, warm, write_clo
from distiller_v2_fit import distortion_excess_report, level_response_report, score
from distiller_v2_north_star import (
    NORTH_STAR_PROBES,
    distill_north_star,
    select_north_star_material,
)


def safe(s):
    return re.sub(r"[^A-Za-z0-9._()+-]+", "_", s).strip("_")[:110]


def dump(path, obj):
    def cv(x):
        if isinstance(x, np.ndarray):
            return x.tolist()
        if hasattr(x, "__dataclass_fields__"):
            return asdict(x)
        if isinstance(x, np.generic):
            return x.item()
        raise TypeError(type(x).__name__)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=cv))


def _metrics_for_roles(fit, selection, benchmark, a, pk, b):
    return {
        "fit": score(fit, a, pk, b) if fit else None,
        "selection": score(selection, a, pk, b) if selection else None,
        "benchmark": score(benchmark, a, pk, b) if benchmark else None,
    }


def _db_ratio(num, den):
    return 20.0 * math.log10(max(float(num), 1e-15) / max(float(den), 1e-15))


def _real_audio(audio):
    return [z for z in audio if not bool(getattr(z[2], "synthetic", False))]


def _peak_safe_gain_cap(audio, a, pk, b, margin_db=0.25):
    caps = []
    details = []
    for x, target, pair in audio:
        pred = render_full(x, a, pk, b)
        n = min(len(pred), len(target))
        pred = np.asarray(pred[:n])
        target = np.asarray(target[:n])
        if n == 0:
            continue
        pa = np.abs(pred)
        ta = np.abs(target)
        p_peak = float(np.max(pa))
        t_peak = float(np.max(ta))
        p_q = float(np.quantile(pa, 0.999))
        t_q = float(np.quantile(ta, 0.999))
        peak_cap = _db_ratio(t_peak, p_peak)
        q_cap = _db_ratio(t_q, p_q)
        cap = min(peak_cap, q_cap) + float(margin_db)
        caps.append(cap)
        details.append(
            {
                "task_id": getattr(pair, "task_id", ""),
                "peak_cap_db": peak_cap,
                "p999_cap_db": q_cap,
                "cap_with_margin_db": cap,
                "pred_peak": p_peak,
                "target_peak": t_peak,
                "pred_p999": p_q,
                "target_p999": t_q,
            }
        )
    return (min(caps) if caps else float("inf")), details


def _calibrate_post_gain(fit_audio, a, pk, b, peak_margin_db=0.25, max_abs_db=12.0):
    """Fit-only loudness correction through B512, strictly after P/K."""
    cal_audio = _real_audio(fit_audio)
    if not cal_audio:
        return b.copy(), 0.0, None, None, {"disabled": True, "reason": "no real FIT audio"}

    before = score(cal_audio, a, pk, b)
    requested_db = float(np.clip(-before.signed_level_db, -max_abs_db, max_abs_db))
    cap_db, details = _peak_safe_gain_cap(cal_audio, a, pk, b, peak_margin_db)
    if requested_db > 0.0:
        gain_db = float(np.clip(min(requested_db, max(0.0, cap_db)), 0.0, max_abs_db))
    else:
        gain_db = requested_db
    scaled = b * (10.0 ** (gain_db / 20.0))
    after = score(cal_audio, a, pk, scaled)
    return scaled, gain_db, before, after, {
        "requested_rms_gain_db": requested_db,
        "peak_safe_cap_db": cap_db,
        "peak_margin_db": peak_margin_db,
        "applied_gain_db": gain_db,
        "stage": "B512 post-P/K output",
        "source": "North Star real FIT material only",
        "limited_by_peak_safety": bool(
            requested_db > 0.0 and gain_db < requested_db - 1e-9
        ),
        "clips": details,
    }


def _group_task_ids(groups):
    return [[p.task_id for p in g] for g in groups]


def _print_level_report(label, report):
    if not report or not report.get("groups"):
        print(f"level response {label}: no matched sweep available")
        return
    print(
        f"level response {label}: RMSE={report['rmse_db']:.3f} dB "
        f"max={report['max_abs_db']:.3f} dB "
        f"({report['groups']} group, {report['points']} comparisons) DIAGNOSTIC ONLY"
    )


def _print_distortion_report(label, report):
    if not report or not report.get("points"):
        print(f"distortion excess {label}: no matched sweep available")
        return
    print(
        f"distortion excess {label}: RMSE={report['rmse_excess_db']:.2f} dB "
        f"max={report['max_excess_db']:.2f} dB "
        f"({report['points']} points) DIAGNOSTIC ONLY"
    )


def _preview_real(role, audio, directory, a, pk, b):
    real = _real_audio(audio)
    if not real:
        return None
    x, target, pair = real[0]
    pred = render_full(x, a, pk, b)
    for tag, z in (("input", x), ("nam", target), ("clo", pred)):
        sf.write(
            directory / f"preview_{role}_{tag}.wav",
            np.asarray(z, np.float32),
            SR,
            subtype="FLOAT",
        )
    return pair.task_id


def run_model(pairs, out, args):
    name = pairs[0].model_name
    print(f"\n=== NORTH STAR: {name} ===")
    material = select_north_star_material(
        pairs,
        fit_real=args.fit_real,
        selection_real=args.selection_real,
        benchmark_real=args.benchmark_real,
        seed=args.seed,
    )

    print("FIT synthetic:")
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

    # These matched-level sweeps are deliberately NOT passed to the optimiser.
    # They are loaded after coefficient search only as diagnostic evidence.
    lfg = level_sweep_groups(pairs, "fit", args.level_groups, args.seed ^ 0x1E7E1)
    lsg = level_sweep_groups(pairs, "selection", args.level_groups, args.seed ^ 0x51E71)
    lbg = level_sweep_groups(pairs, "benchmark", args.level_groups, args.seed ^ 0xB3E71)

    print(
        f"material examples fit={len(fit)} ({len(material.probes)} synthetic + "
        f"{len(material.fit_real)} real) selection={len(selection)} real "
        f"benchmark={len(benchmark)} real"
    )
    print(
        "objective=mean aligned ESR; B=target-energy-normalized shared analytic solve; "
        "level/distortion penalties=OFF"
    )

    started = time.monotonic()
    best = distill_north_star(
        fit,
        selection,
        controls=args.a_controls,
        rounds=args.rounds,
        pk_passes=args.pk_passes,
        pause_ms=args.yield_ms,
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

    # Diagnostics only: coefficient search and post-gain are already frozen.
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

    d = out / safe(pairs[0].model_key + "__" + name)
    d.mkdir(parents=True, exist_ok=True)
    clo = d / "distilled.clo"
    write_clo(clo, a, best.pk, b_device)

    previews = {
        role: _preview_real(role, audio, d, a, best.pk, b_device)
        for role, audio in (
            ("fit", fit),
            ("selection", selection),
            ("benchmark", benchmark),
        )
    }

    report = {
        "method": "north-star-teacher-student-varpro-v1",
        "north_star_document": "ENGINE_V2_RESEARCH_NORTH_STAR.md",
        "model_name": name,
        "model_key": pairs[0].model_key,
        "tone_id": pairs[0].tone_id,
        "model_id": pairs[0].model_id,
        "optimization": {
            "fit_objective": "mean aligned ESR across canonical FIT examples",
            "selection_gate": "mean aligned ESR across disjoint real SELECTION examples",
            "b_solve": "one shared target-energy-normalized analytic B512 solve per A/P-K candidate",
            "a_and_pk_joint": True,
            "pk_passes": args.pk_passes,
            "level_response_weight": 0.0,
            "distortion_excess_weight": 0.0,
            "isolated_pk_identification": False,
            "named_amp_rules": False,
        },
        "material": material.manifest(),
        "A128": a,
        "pk": best.pk,
        "B512_uncalibrated": b_uncalibrated,
        "B512_device": b_device,
        "optimizer_fit_metrics": best.fit,
        "optimizer_selection_metrics": best.selection,
        "pre_calibration_metrics": pre_metrics,
        "post_output_gain_calibration_db": gain_db,
        "output_gain_calibration": cal_info,
        "fit_metrics": final_metrics["fit"],
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
    dump(d / "report.json", report)

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
            "Canonical EngineV2 North Star teacher/student experiment. "
            "Fits six system-identification probes plus a small varied real-DI set; "
            "selection and benchmark are real-DI only."
        )
    )
    p.add_argument("--teacher-root", default="~/NamtoCloTeacherDataset")
    p.add_argument("--output", default="~/NamtoCloNorthStar")
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
    print("NORTH STAR canonical probes:")
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
        except Exception as e:
            print("FAILED", groups[key][0].model_name, e)
            failures.append({"key": key, "error": str(e)})

    dump(
        out / "summary.json",
        {
            "method": "north-star-teacher-student-varpro-v1",
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
