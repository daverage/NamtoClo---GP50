#!/usr/bin/env python3

import numpy as np

from analyze_north_star_v4_stimulus_diagnostics import (
    _aligned_metrics,
    _level_response,
    _window_diagnostics,
)
from distiller_v2_dsp import SR


def main():
    # Alignment diagnostics should remove a small pure delay and recover an
    # essentially exact waveform match.
    n = SR * 2
    rng = np.random.default_rng(260913)
    target = rng.standard_normal(n) * 0.01
    delay = 37
    pred = np.concatenate([np.zeros(delay), target[:-delay]])
    m = _aligned_metrics(pred, target, maxlag=128)
    assert abs(m["lag_samples"] - delay) <= 1, m
    assert m["esr"] < 1e-10, m

    # Relative level-response diagnostics are anchored at the level nearest
    # 0 dB and measure CLO-vs-NAM compression/cleanup error independently of a
    # constant output-level offset.
    rows = [
        {
            "level_db": 0.0,
            "target_rms_dbfs": -12.0,
            "pred_rms_dbfs": -13.0,
            "target_peak_dbfs": -6.0,
            "pred_peak_dbfs": -7.0,
        },
        {
            "level_db": -6.0,
            "target_rms_dbfs": -17.0,
            "pred_rms_dbfs": -18.5,
            "target_peak_dbfs": -11.0,
            "pred_peak_dbfs": -12.5,
        },
        {
            "level_db": -12.0,
            "target_rms_dbfs": -22.0,
            "pred_rms_dbfs": -24.0,
            "target_peak_dbfs": -16.0,
            "pred_peak_dbfs": -18.0,
        },
    ]
    response = _level_response(rows)
    assert response["anchor_level_db"] == 0.0
    by_level = {r["level_db"]: r for r in response["rows"]}
    assert abs(by_level[-6.0]["rms_response_error_db"] + 0.5) < 1e-12
    assert abs(by_level[-12.0]["rms_response_error_db"] + 1.0) < 1e-12

    # Short-time diagnostics should localise an injected mismatch rather than
    # merely reporting one whole-stimulus number.
    t = np.arange(SR * 2, dtype=np.float64) / SR
    target = 0.1 * np.sin(2.0 * np.pi * 440.0 * t)
    pred = target.copy()
    pred[int(0.75 * SR) : int(1.25 * SR)] *= 0.2
    windows = _window_diagnostics(
        pred,
        target,
        window_s=0.25,
        hop_s=0.125,
        min_target_rms_dbfs=-80.0,
        top_n=4,
        maxlag=64,
    )
    assert windows["windows_used"] > 0
    worst = windows["worst_by_esr"][0]
    assert worst["start_s"] < 1.25 and worst["end_s"] > 0.75, worst
    assert worst["esr"] > 0.1, worst

    print("north-star v4 stimulus diagnostics self-tests passed")


if __name__ == "__main__":
    main()
