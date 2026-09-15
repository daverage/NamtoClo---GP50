#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import numpy as np
import soundfile as sf

from build_namtoclo_teacher_dataset import sha256_file
from distiller_v2_dsp import SR, render_full
from distiller_v2_north_star_v2 import _best_lag


V4_METHOD_PREFIX = "north-star-teacher-student-varpro-v4-"


def _db(value: float) -> float:
    return -math.inf if value <= 0.0 else 20.0 * math.log10(value)


def _rms(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    return math.sqrt(float(np.mean(x * x)) + 1e-30) if x.size else 0.0


def _peak(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    return float(np.max(np.abs(x))) if x.size else 0.0


def _crest_db(x: np.ndarray) -> float:
    return _db(_peak(x) / max(_rms(x), 1e-30))


def _esr(pred: np.ndarray, target: np.ndarray) -> float:
    n = min(len(pred), len(target))
    if n <= 0:
        return float("inf")
    p = np.asarray(pred[:n], dtype=np.float64)
    t = np.asarray(target[:n], dtype=np.float64)
    den = float(np.dot(t, t))
    return float(np.dot(p - t, p - t) / den) if den > 1e-20 else float("inf")


def _align(pred: np.ndarray, target: np.ndarray, maxlag: int = 256):
    n = min(len(pred), len(target))
    p = np.asarray(pred[:n], dtype=np.float64)
    t = np.asarray(target[:n], dtype=np.float64)
    lag = int(_best_lag(p, t, maxlag=maxlag))
    if lag >= 0:
        return p[lag:], t[: n - lag], lag, 0
    return p[: n + lag], t[-lag:], lag, -lag


def _spectral_log_rmse_db(pred: np.ndarray, target: np.ndarray) -> float:
    """Gain-matched log-magnitude spectral difference in dB.

    This is diagnostic only. It is deliberately not part of V4 fitting.
    """
    n = min(len(pred), len(target))
    if n < 256:
        return 0.0
    p = np.asarray(pred[:n], dtype=np.float64)
    t = np.asarray(target[:n], dtype=np.float64)
    pr = _rms(p)
    tr = _rms(t)
    if pr <= 1e-15 or tr <= 1e-15:
        return 0.0
    p = p * (tr / pr)
    w = np.hanning(n)
    P = np.abs(np.fft.rfft(p * w))
    T = np.abs(np.fft.rfft(t * w))
    if not np.any(T > 0.0):
        return 0.0
    floor = max(float(np.max(T)) * 1e-4, 1e-12)
    mask = T >= floor
    if not np.any(mask):
        return 0.0
    delta = 20.0 * np.log10(np.maximum(P[mask], 1e-12) / np.maximum(T[mask], 1e-12))
    return float(np.sqrt(np.mean(delta * delta)))


def _aligned_metrics(pred: np.ndarray, target: np.ndarray, maxlag: int = 256) -> dict:
    p, t, lag, target_origin = _align(pred, target, maxlag=maxlag)
    pred_rms = _rms(p)
    target_rms = _rms(t)
    pred_peak = _peak(p)
    target_peak = _peak(t)
    return {
        "lag_samples": lag,
        "lag_ms": 1000.0 * lag / SR,
        "target_origin_samples": target_origin,
        "aligned_samples": int(min(len(p), len(t))),
        "esr": _esr(p, t),
        "pred_rms_dbfs": _db(pred_rms),
        "target_rms_dbfs": _db(target_rms),
        "level_error_db": _db(pred_rms / max(target_rms, 1e-30)),
        "pred_peak_dbfs": _db(pred_peak),
        "target_peak_dbfs": _db(target_peak),
        "peak_error_db": _db(pred_peak / max(target_peak, 1e-30)),
        "pred_crest_db": _crest_db(p),
        "target_crest_db": _crest_db(t),
        "crest_error_db": _crest_db(p) - _crest_db(t),
        "spectral_log_rmse_db": _spectral_log_rmse_db(p, t),
    }


def _window_diagnostics(
    pred: np.ndarray,
    target: np.ndarray,
    window_s: float,
    hop_s: float,
    min_target_rms_dbfs: float,
    top_n: int,
    maxlag: int = 256,
) -> dict:
    p, t, lag, target_origin = _align(pred, target, maxlag=maxlag)
    n = min(len(p), len(t))
    win = max(128, int(round(float(window_s) * SR)))
    hop = max(1, int(round(float(hop_s) * SR)))
    floor_amp = 10.0 ** (float(min_target_rms_dbfs) / 20.0)
    rows = []
    if n >= win:
        for start in range(0, n - win + 1, hop):
            end = start + win
            pp = p[start:end]
            tt = t[start:end]
            target_rms = _rms(tt)
            if target_rms < floor_amp:
                continue
            pred_rms = _rms(pp)
            abs_start = target_origin + start
            rows.append(
                {
                    "start_s": abs_start / float(SR),
                    "end_s": (abs_start + win) / float(SR),
                    "esr": _esr(pp, tt),
                    "level_error_db": _db(pred_rms / max(target_rms, 1e-30)),
                    "target_rms_dbfs": _db(target_rms),
                    "spectral_log_rmse_db": _spectral_log_rmse_db(pp, tt),
                }
            )

    esrs = np.asarray([r["esr"] for r in rows], dtype=np.float64)
    spectra = np.asarray([r["spectral_log_rmse_db"] for r in rows], dtype=np.float64)
    summary = {
        "window_s": float(window_s),
        "hop_s": float(hop_s),
        "min_target_rms_dbfs": float(min_target_rms_dbfs),
        "lag_samples": lag,
        "windows_used": len(rows),
        "median_esr": float(np.median(esrs)) if len(esrs) else None,
        "p90_esr": float(np.quantile(esrs, 0.90)) if len(esrs) else None,
        "max_esr": float(np.max(esrs)) if len(esrs) else None,
        "median_spectral_log_rmse_db": float(np.median(spectra)) if len(spectra) else None,
        "p90_spectral_log_rmse_db": float(np.quantile(spectra, 0.90)) if len(spectra) else None,
        "worst_by_esr": sorted(rows, key=lambda r: r["esr"], reverse=True)[:top_n],
        "worst_by_spectral": sorted(
            rows, key=lambda r: r["spectral_log_rmse_db"], reverse=True
        )[:top_n],
    }
    return summary


def _load_mono(path: Path) -> np.ndarray:
    x, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if int(sr) != SR:
        raise RuntimeError(f"Expected {SR} Hz stimulus cache: {path} is {sr} Hz")
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 2:
        x = x.mean(axis=1)
    if not np.all(np.isfinite(x)):
        raise RuntimeError(f"Non-finite audio: {path}")
    return x


def _level_response(rows: list[dict]) -> dict:
    if not rows:
        return {"anchor_level_db": None, "rows": [], "rms_response_rmse_db": None}
    anchor = min(rows, key=lambda r: (abs(float(r["level_db"])), -float(r["level_db"])))
    ar_t = float(anchor["target_rms_dbfs"])
    ar_p = float(anchor["pred_rms_dbfs"])
    ap_t = float(anchor["target_peak_dbfs"])
    ap_p = float(anchor["pred_peak_dbfs"])
    out = []
    errors = []
    for row in sorted(rows, key=lambda r: float(r["level_db"]), reverse=True):
        nam_rms_delta = float(row["target_rms_dbfs"]) - ar_t
        clo_rms_delta = float(row["pred_rms_dbfs"]) - ar_p
        nam_peak_delta = float(row["target_peak_dbfs"]) - ap_t
        clo_peak_delta = float(row["pred_peak_dbfs"]) - ap_p
        err = clo_rms_delta - nam_rms_delta
        if row is not anchor:
            errors.append(err)
        out.append(
            {
                "level_db": float(row["level_db"]),
                "nam_rms_delta_db": nam_rms_delta,
                "clo_rms_delta_db": clo_rms_delta,
                "rms_response_error_db": err,
                "nam_peak_delta_db": nam_peak_delta,
                "clo_peak_delta_db": clo_peak_delta,
                "peak_response_error_db": clo_peak_delta - nam_peak_delta,
            }
        )
    rmse = math.sqrt(float(np.mean(np.square(errors)))) if errors else 0.0
    return {
        "anchor_level_db": float(anchor["level_db"]),
        "rms_response_rmse_db": rmse,
        "rows": out,
    }


def _find_reports(root: Path, model_regex: str | None):
    rx = re.compile(model_regex, re.I) if model_regex else None
    found = []
    for path in sorted(root.rglob("report.json")):
        try:
            report = json.loads(path.read_text())
        except Exception:
            continue
        if not str(report.get("method", "")).startswith(V4_METHOD_PREFIX):
            continue
        name = str(report.get("model_name", path.parent.name))
        if rx and not rx.search(name):
            continue
        found.append((path, report))
    return found


def analyze_report(
    report_path: Path,
    report: dict,
    window_s: float,
    hop_s: float,
    min_target_rms_dbfs: float,
    top_n: int,
    verify_hashes: bool,
) -> dict:
    a = np.asarray(report["A128"], dtype=np.float64)
    pk = np.asarray(report["pk"], dtype=np.float64)
    b = np.asarray(report["B512_device"], dtype=np.float64)
    if len(a) != 128 or len(pk) != 4 or len(b) != 512:
        raise RuntimeError(f"Invalid V4 coefficient sizes in {report_path}")

    variants = list(report.get("stimulus", {}).get("level_variants", []))
    if not variants:
        raise RuntimeError(f"No stimulus.level_variants in {report_path}")

    level_rows = []
    global_windows = []
    for variant in sorted(variants, key=lambda r: float(r["level_db"]), reverse=True):
        inp = Path(variant["student_input"]).expanduser()
        target_path = Path(variant["student_target"]).expanduser()
        if not inp.is_file() or not target_path.is_file():
            raise FileNotFoundError(
                f"Missing cached V4 stimulus pair for {report.get('model_name')}: "
                f"{inp} / {target_path}"
            )
        if verify_hashes:
            expected = variant.get("student_input_sha256")
            if expected and sha256_file(inp) != expected:
                raise RuntimeError(f"Stimulus input SHA mismatch: {inp}")
            expected = variant.get("student_target_sha256")
            if expected and sha256_file(target_path) != expected:
                raise RuntimeError(f"Stimulus target SHA mismatch: {target_path}")

        x = _load_mono(inp)
        target = _load_mono(target_path)
        n = min(len(x), len(target))
        x = x[:n]
        target = target[:n]
        pred = render_full(x, a, pk, b)

        metrics = _aligned_metrics(pred, target)
        windows = _window_diagnostics(
            pred,
            target,
            window_s=window_s,
            hop_s=hop_s,
            min_target_rms_dbfs=min_target_rms_dbfs,
            top_n=top_n,
        )
        row = {
            "level_db": float(variant["level_db"]),
            "task_id": str(variant.get("task_id", "")),
            **metrics,
            "window_summary": windows,
        }
        level_rows.append(row)
        for item in windows["worst_by_esr"]:
            global_windows.append({"level_db": row["level_db"], **item})

    response = _level_response(level_rows)
    mean_level_esr = float(np.mean([row["esr"] for row in level_rows]))
    worst_global = sorted(global_windows, key=lambda r: r["esr"], reverse=True)[:top_n]

    return {
        "diagnostic_version": 1,
        "diagnostic_only": True,
        "note": (
            "This analysis does not alter or select coefficients. It renders the already-frozen "
            "V4 CLO against its cached T3K stimulus targets to locate residual error."
        ),
        "report_path": str(report_path),
        "model_name": report.get("model_name"),
        "model_key": report.get("model_key"),
        "model_id": report.get("model_id"),
        "stimulus_sha256": report.get("stimulus", {}).get("stimulus_sha256"),
        "pk": [float(v) for v in pk],
        "optimizer_fit_evidence_esr": float(
            report.get("optimizer_fit_evidence_metrics", {}).get("esr", float("nan"))
        ),
        "post_output_gain_calibration_db": float(
            report.get("post_output_gain_calibration_db", 0.0)
        ),
        "device_mean_per_level_esr": mean_level_esr,
        "level_response": response,
        "levels": level_rows,
        "worst_windows_across_levels": worst_global,
    }


def _fmt(value, digits=4):
    if value is None or not math.isfinite(float(value)):
        return "n/a"
    return f"{float(value):.{digits}f}"


def _print_result(result: dict) -> None:
    print(f"\n=== V4 STIMULUS DIAGNOSTICS: {result['model_name']} ===")
    print(
        "level   ESR      levelErr  spectral  lag(ms)  win-p90-ESR\n"
        "-----   -------  --------  --------  -------  -----------"
    )
    for row in sorted(result["levels"], key=lambda r: r["level_db"], reverse=True):
        w = row["window_summary"]
        print(
            f"{row['level_db']:+5.0f}   {_fmt(row['esr']):>7}  "
            f"{row['level_error_db']:+8.2f}  {row['spectral_log_rmse_db']:8.2f}  "
            f"{row['lag_ms']:+7.3f}  {_fmt(w['p90_esr']):>11}"
        )
    response = result["level_response"]
    print(
        f"level-response RMSE vs {response['anchor_level_db']:+g} dB anchor: "
        f"{response['rms_response_rmse_db']:.3f} dB"
    )
    print(
        f"optimizer evidence ESR={result['optimizer_fit_evidence_esr']:.4f}; "
        f"final-device mean per-level ESR={result['device_mean_per_level_esr']:.4f}; "
        f"post gain={result['post_output_gain_calibration_db']:+.2f} dB"
    )
    print("worst active windows by ESR:")
    for row in result["worst_windows_across_levels"]:
        print(
            f"  L{row['level_db']:+g} {row['start_s']:.3f}-{row['end_s']:.3f}s "
            f"ESR={row['esr']:.3f} level={row['level_error_db']:+.2f}dB "
            f"spectral={row['spectral_log_rmse_db']:.2f}dB"
        )


def main():
    p = argparse.ArgumentParser(
        description=(
            "Diagnostic-only analysis of already-trained North Star v4 stimulus-only CLOs. "
            "Reports per-level and short-time T3K residual error without retraining or guitar use."
        )
    )
    p.add_argument("--v4-root", default="~/NamtoCloNorthStarV4_StimulusOnly")
    p.add_argument("--model-regex")
    p.add_argument("--window-ms", type=float, default=500.0)
    p.add_argument("--hop-ms", type=float, default=250.0)
    p.add_argument("--min-target-rms-dbfs", type=float, default=-80.0)
    p.add_argument("--top-windows", type=int, default=8)
    p.add_argument("--verify-hashes", action="store_true")
    p.add_argument(
        "--summary-json",
        help="Optional aggregate JSON path. Per-model stimulus_diagnostics.json files are always written.",
    )
    args = p.parse_args()

    if args.window_ms <= 0 or args.hop_ms <= 0:
        raise SystemExit("--window-ms and --hop-ms must be > 0")
    if args.top_windows < 1:
        raise SystemExit("--top-windows must be >= 1")

    root = Path(args.v4_root).expanduser()
    reports = _find_reports(root, args.model_regex)
    if not reports:
        raise SystemExit("No North Star v4 report.json files matched")

    results = []
    for report_path, report in reports:
        result = analyze_report(
            report_path,
            report,
            window_s=args.window_ms / 1000.0,
            hop_s=args.hop_ms / 1000.0,
            min_target_rms_dbfs=args.min_target_rms_dbfs,
            top_n=args.top_windows,
            verify_hashes=args.verify_hashes,
        )
        out = report_path.parent / "stimulus_diagnostics.json"
        out.write_text(json.dumps(result, indent=2, allow_nan=False))
        _print_result(result)
        print("diagnostics:", out)
        results.append(result)

    summary = {
        "diagnostic_version": 1,
        "diagnostic_only": True,
        "v4_root": str(root),
        "models": [
            {
                "model_name": r["model_name"],
                "model_key": r["model_key"],
                "model_id": r["model_id"],
                "optimizer_fit_evidence_esr": r["optimizer_fit_evidence_esr"],
                "device_mean_per_level_esr": r["device_mean_per_level_esr"],
                "level_response_rmse_db": r["level_response"]["rms_response_rmse_db"],
                "pk": r["pk"],
            }
            for r in results
        ],
    }
    summary_path = (
        Path(args.summary_json).expanduser()
        if args.summary_json
        else root / "stimulus_diagnostics_summary.json"
    )
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2, allow_nan=False))
    print("\nsummary:", summary_path)


if __name__ == "__main__":
    main()
