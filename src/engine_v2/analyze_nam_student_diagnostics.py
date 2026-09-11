#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import re
from pathlib import Path

import numpy as np

from distiller_v2_data import group_models, load_pairs, read_pair
from distiller_v2_dsp import SR, warm
from evaluate_north_star_vs_original import (
    BANDS,
    _aligned,
    _discover_clo,
    _dump,
    _mean,
    _rms,
    _safe,
    _spectral_views,
    _tail_metrics,
    _write_csv,
    read_compact_clo,
    render_compact_clo,
    select_shared_real_material,
)


ENVELOPE_FRAME = 1024
ENVELOPE_HOP = 256
ENVELOPE_RELATIVE_FLOOR_DB = 50.0
ENVELOPE_ABSOLUTE_FLOOR_DBFS = -85.0


def _dbfs(value: float) -> float:
    return 20.0 * math.log10(max(float(value), 1e-15))


def _signal_stats(x: np.ndarray) -> dict:
    x = np.asarray(x, dtype=np.float64)
    absolute = np.abs(x)
    rms_dbfs = _dbfs(_rms(x))
    peak_dbfs = _dbfs(float(np.max(absolute))) if len(x) else -300.0
    p999_dbfs = (
        _dbfs(float(np.quantile(absolute, 0.999))) if len(x) else -300.0
    )
    return {
        "rms_dbfs": rms_dbfs,
        "peak_dbfs": peak_dbfs,
        "p999_dbfs": p999_dbfs,
        "crest_factor_db": peak_dbfs - rms_dbfs,
    }


def _static_diagnostics(pred: np.ndarray, target: np.ndarray) -> dict:
    p, t, lag = _aligned(pred, target)
    nam = _signal_stats(t)
    student = _signal_stats(p)
    return {
        "lag_samples": int(lag),
        "nam_rms_dbfs": nam["rms_dbfs"],
        "student_rms_dbfs": student["rms_dbfs"],
        "rms_error_db": student["rms_dbfs"] - nam["rms_dbfs"],
        "nam_peak_dbfs": nam["peak_dbfs"],
        "student_peak_dbfs": student["peak_dbfs"],
        "peak_error_db": student["peak_dbfs"] - nam["peak_dbfs"],
        "nam_p999_dbfs": nam["p999_dbfs"],
        "student_p999_dbfs": student["p999_dbfs"],
        "p999_error_db": student["p999_dbfs"] - nam["p999_dbfs"],
        "nam_crest_factor_db": nam["crest_factor_db"],
        "student_crest_factor_db": student["crest_factor_db"],
        "crest_factor_delta_db": (
            student["crest_factor_db"] - nam["crest_factor_db"]
        ),
    }


def _frame_rms_db(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray(x, dtype=np.float64)
    if len(x) < ENVELOPE_FRAME:
        return np.array([0]), np.array([_dbfs(_rms(x))])
    starts = list(range(0, len(x) - ENVELOPE_FRAME + 1, ENVELOPE_HOP))
    final_start = len(x) - ENVELOPE_FRAME
    if not starts or starts[-1] != final_start:
        starts.append(final_start)
    db = [
        _dbfs(_rms(x[start : start + ENVELOPE_FRAME]))
        for start in starts
    ]
    return np.asarray(starts), np.asarray(db, dtype=np.float64)


def _envelope_diagnostics(
    pred: np.ndarray,
    target: np.ndarray,
) -> tuple[dict, list[dict]]:
    p, t, lag = _aligned(pred, target)
    starts_t, nam_db = _frame_rms_db(t)
    starts_p, student_db = _frame_rms_db(p)
    n = min(len(nam_db), len(student_db))
    starts = starts_t[:n]
    nam_db = nam_db[:n]
    student_db = student_db[:n]

    active_floor = max(
        float(np.max(nam_db)) - ENVELOPE_RELATIVE_FLOOR_DB,
        ENVELOPE_ABSOLUTE_FLOOR_DBFS,
    )
    active = nam_db >= active_floor
    if not np.any(active):
        active = np.ones(n, dtype=bool)

    error = student_db - nam_db
    gain_match_db = _dbfs(_rms(t) / max(_rms(p), 1e-15))
    gm_error = student_db + gain_match_db - nam_db
    active_error = error[active]
    active_gm_error = gm_error[active]
    active_nam = nam_db[active]
    active_student = student_db[active]
    corr = None
    if (
        len(active_nam) >= 2
        and np.std(active_nam) > 1e-12
        and np.std(active_student) > 1e-12
    ):
        corr = float(np.corrcoef(active_nam, active_student)[0, 1])

    summary = {
        "lag_samples": int(lag),
        "active_frames": int(np.sum(active)),
        "active_floor_dbfs": float(active_floor),
        "mean_error_db": float(np.mean(active_error)),
        "rmse_db": math.sqrt(float(np.mean(active_error**2))),
        "p95_abs_error_db": float(
            np.quantile(np.abs(active_error), 0.95)
        ),
        "gain_match_db": float(gain_match_db),
        "gain_matched_rmse_db": math.sqrt(
            float(np.mean(active_gm_error**2))
        ),
        "gain_matched_p95_abs_error_db": float(
            np.quantile(np.abs(active_gm_error), 0.95)
        ),
        "correlation": corr,
    }
    rows = [
        {
            "frame": i,
            "time_s": float(starts[i]) / SR,
            "nam_rms_dbfs": float(nam_db[i]),
            "student_rms_dbfs": float(student_db[i]),
            "error_db": float(error[i]),
            "gain_matched_error_db": float(gm_error[i]),
            "active": bool(active[i]),
        }
        for i in range(n)
    ]
    return summary, rows


def _system_diagnostics(x, target, pred):
    static = _static_diagnostics(pred, target)
    envelope, envelope_rows = _envelope_diagnostics(pred, target)
    spectrum = _spectral_views(pred, target)
    tail = _tail_metrics(x, target, pred)
    clip = {
        **static,
        "envelope_mean_error_db": envelope["mean_error_db"],
        "envelope_rmse_db": envelope["rmse_db"],
        "envelope_p95_abs_error_db": envelope["p95_abs_error_db"],
        "gain_matched_envelope_rmse_db": envelope[
            "gain_matched_rmse_db"
        ],
        "gain_matched_envelope_p95_abs_error_db": envelope[
            "gain_matched_p95_abs_error_db"
        ],
        "envelope_correlation": envelope["correlation"],
        "tail_level_error_db": tail["level_error_db"] if tail else None,
        "tail_gain_matched_esr": tail["gain_matched_esr"] if tail else None,
        "tail_spectral_rmse_db": (
            tail["gain_matched_spectral_rmse_db"] if tail else None
        ),
        "tail_seconds": tail["seconds"] if tail else None,
    }
    return clip, envelope_rows, spectrum["bands"]


def _aggregate_system(rows: list[dict], prefix: str) -> dict:
    keys = (
        "rms_error_db",
        "peak_error_db",
        "p999_error_db",
        "crest_factor_delta_db",
        "envelope_mean_error_db",
        "envelope_rmse_db",
        "envelope_p95_abs_error_db",
        "gain_matched_envelope_rmse_db",
        "gain_matched_envelope_p95_abs_error_db",
        "envelope_correlation",
        "tail_level_error_db",
        "tail_gain_matched_esr",
        "tail_spectral_rmse_db",
    )
    return {f"mean_{key}": _mean(rows, f"{prefix}_{key}") for key in keys}


def _aggregate_bands(rows: list[dict], system: str) -> list[dict]:
    out = []
    for name, lo, hi in BANDS:
        matching = [
            r for r in rows if r["system"] == system and r["band"] == name
        ]
        confident = [r for r in matching if r["confident"]]
        source = confident or matching
        if not source:
            continue
        out.append(
            {
                "system": system,
                "band": name,
                "lo_hz": lo,
                "hi_hz": hi,
                "absolute_error_db": float(
                    np.mean([r["absolute_error_db"] for r in source])
                ),
                "gain_matched_error_db": float(
                    np.mean([r["gain_matched_error_db"] for r in source])
                ),
                "confident_clips": len(confident),
                "clips": len(matching),
            }
        )
    return out


def evaluate_model(
    pairs,
    north_path: Path,
    directory: Path,
    original_path: Path | None = None,
) -> dict:
    north = read_compact_clo(north_path)
    original = read_compact_clo(original_path) if original_path else None
    directory.mkdir(parents=True, exist_ok=True)

    clip_rows = []
    envelope_rows = []
    band_rows = []

    for clip_index, pair in enumerate(pairs):
        x, target, _ = read_pair(pair)
        systems = {
            "north": ("north_star_v2", render_compact_clo(x, north)),
        }
        if original is not None:
            systems["baseline"] = (
                "original_baseline",
                render_compact_clo(x, original),
            )

        row = {
            "clip": clip_index,
            "task_id": pair.task_id,
            "dataset": pair.dataset,
            "source": Path(pair.source_path).name,
            "start_s": pair.start_s,
            "duration_s": pair.duration_s,
        }
        for prefix, (system_name, pred) in systems.items():
            diag, env, bands = _system_diagnostics(x, target, pred)
            row.update({f"{prefix}_{key}": value for key, value in diag.items()})
            envelope_rows.extend(
                {
                    "clip": clip_index,
                    "task_id": pair.task_id,
                    "source": Path(pair.source_path).name,
                    "system": system_name,
                    **frame,
                }
                for frame in env
            )
            band_rows.extend(
                {
                    "clip": clip_index,
                    "task_id": pair.task_id,
                    "source": Path(pair.source_path).name,
                    "system": system_name,
                    "band": band["band"],
                    "lo_hz": band["lo_hz"],
                    "hi_hz": band["hi_hz"],
                    "absolute_error_db": band["absolute_error_db"],
                    "gain_matched_error_db": band["gain_matched_error_db"],
                    "teacher_relative_power_db": band[
                        "teacher_relative_power_db"
                    ],
                    "confident": band["confident"],
                }
                for band in bands
            )
        clip_rows.append(row)

    band_summary = _aggregate_bands(band_rows, "north_star_v2")
    if original is not None:
        band_summary.extend(_aggregate_bands(band_rows, "original_baseline"))

    summary = {
        "model_name": pairs[0].model_name,
        "model_key": pairs[0].model_key,
        "model_id": pairs[0].model_id,
        "target": "NAM teacher",
        "north_star_v2_clo": str(north_path),
        "original_baseline_clo": str(original_path) if original_path else None,
        "north_star_v2": _aggregate_system(clip_rows, "north"),
        "original_baseline": (
            _aggregate_system(clip_rows, "baseline")
            if original is not None
            else None
        ),
        "bands": band_summary,
        "analysis": {
            "benchmark_only": True,
            "nam_is_behavioral_authority": True,
            "original_converter_is_baseline_only": True,
            "envelope_frame_samples": ENVELOPE_FRAME,
            "envelope_hop_samples": ENVELOPE_HOP,
            "envelope_active_definition": (
                "NAM short-time RMS frames within 50 dB of the clip's loudest "
                "NAM frame and no lower than -85 dBFS"
            ),
            "diagnostics_do_not_change_coefficients": True,
        },
    }
    _dump(directory / "summary.json", summary)
    _write_csv(directory / "per_clip.csv", clip_rows)
    _write_csv(directory / "envelope_frames.csv", envelope_rows)
    _write_csv(directory / "bands_per_clip.csv", band_rows)
    _write_csv(directory / "bands_summary.csv", band_summary)
    return summary


def _fmt(value, width=8):
    return (
        f"{value:{width}.2f}"
        if value is not None and np.isfinite(value)
        else f"{'n/a':>{width}}"
    )


def _print_summary(summary: dict) -> None:
    north = summary["north_star_v2"]
    baseline = summary["original_baseline"]
    print(f"\n=== {summary['model_name']} ===")
    print(
        "NAM-CENTERED DIAGNOSTICS                    NSv2"
        + ("    baseline" if baseline else "")
    )
    metrics = (
        ("RMS error", "mean_rms_error_db", "dB"),
        ("peak error", "mean_peak_error_db", "dB"),
        ("p99.9 error", "mean_p999_error_db", "dB"),
        ("crest-factor delta", "mean_crest_factor_delta_db", "dB"),
        ("envelope signed error", "mean_envelope_mean_error_db", "dB"),
        ("envelope RMSE", "mean_envelope_rmse_db", "dB"),
        ("gain-matched envelope RMSE", "mean_gain_matched_envelope_rmse_db", "dB"),
        (
            "gain-matched envelope p95 |error|",
            "mean_gain_matched_envelope_p95_abs_error_db",
            "dB",
        ),
        ("envelope correlation", "mean_envelope_correlation", ""),
        ("tail level error", "mean_tail_level_error_db", "dB"),
        ("tail gain-matched ESR", "mean_tail_gain_matched_esr", ""),
        ("tail spectral RMSE", "mean_tail_spectral_rmse_db", "dB"),
    )
    for label, key, unit in metrics:
        line = f"{label:36s}{_fmt(north.get(key))}"
        if baseline:
            line += f"  {_fmt(baseline.get(key))}"
        print(line + (f" {unit}" if unit else ""))

    print("NSv2 broad-band error vs NAM (+ = more energy, - = less):")
    for row in summary["bands"]:
        if row["system"] != "north_star_v2" or row["confident_clips"] <= 0:
            continue
        print(
            f"  {row['lo_hz']:5.0f}-{row['hi_hz']:5.0f} Hz  "
            f"absolute {row['absolute_error_db']:+6.2f} dB  "
            f"gain-matched {row['gain_matched_error_db']:+6.2f} dB  "
            f"{row['band']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "NAM-centered held-out diagnostics for frozen North Star v2. "
            "NAM is the target; original NamToClo is optional baseline only."
        )
    )
    parser.add_argument("--teacher-root", default="~/NamtoCloTeacherDataset")
    parser.add_argument("--north-star-root", required=True)
    parser.add_argument("--original-root")
    parser.add_argument("--output", default="~/NamtoCloNAMCenteredDiagnostics")
    parser.add_argument("--model-regex", required=True)
    parser.add_argument("--benchmark-count", type=int, default=3)
    parser.add_argument("--seed", type=int, default=260910)
    args = parser.parse_args()
    if args.benchmark_count < 1:
        raise SystemExit("--benchmark-count must be >= 1")

    groups = group_models(load_pairs(Path(args.teacher_root).expanduser()))
    rx = re.compile(args.model_regex, re.I)
    keys = [k for k in sorted(groups) if rx.search(groups[k][0].model_name)]
    if not keys:
        raise SystemExit("No teacher models matched --model-regex")

    shared_keys, selected = select_shared_real_material(
        groups,
        keys,
        role="benchmark",
        count=args.benchmark_count,
        seed=args.seed,
    )
    print("NAM-CENTERED HELD-OUT DIAGNOSTICS")
    print("NAM teacher is the behavioral authority.")
    print("Frozen NSv2 is the student under test.")
    if args.original_root:
        print("Original NamToClo is reported only as a baseline.")
    print("No coefficients are changed; benchmark remains evaluation-only.")
    print("selected models:", *[groups[k][0].model_name for k in keys], sep="\n  ")
    print("shared held-out performances:")
    for index in range(len(shared_keys)):
        pair = selected[keys[0]][index]
        print(
            f"  {pair.dataset}: {Path(pair.source_path).name} @ "
            f"{pair.start_s:.2f}s ({pair.duration_s:.2f}s)"
        )
    print("warming GP50 renderer...")
    warm()

    output = Path(args.output).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for key in keys:
        p0 = groups[key][0]
        north = _discover_clo(Path(args.north_star_root), p0, "north_star")
        original = (
            _discover_clo(Path(args.original_root), p0, "original")
            if args.original_root
            else None
        )
        directory = output / _safe(p0.model_key + "__" + p0.model_name)
        summary = evaluate_model(
            selected[key],
            north,
            directory,
            original_path=original,
        )
        results.append(summary)
        _print_summary(summary)

    root_summary = {
        "method": "nam-centered-heldout-perceptual-diagnostics-v1",
        "north_star_document": "ENGINE_V2_RESEARCH_NORTH_STAR.md",
        "benchmark_only": True,
        "nam_is_behavioral_authority": True,
        "original_converter_is_baseline_only": True,
        "same_underlying_performances_across_models": True,
        "shared_performance_keys": [list(key) for key in shared_keys],
        "results": results,
    }
    _dump(output / "summary.json", root_summary)
    print("\nWrote:", output / "summary.json")
    print("NAM remains the target; diagnostics did not change the fitter.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
