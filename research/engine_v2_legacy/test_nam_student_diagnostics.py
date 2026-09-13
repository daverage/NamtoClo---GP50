#!/usr/bin/env python3
from __future__ import annotations

import numpy as np

from analyze_nam_student_diagnostics import (
    _envelope_diagnostics,
    _role_context,
    _signal_stats,
    _static_diagnostics,
)


def main() -> None:
    for role in ("fit", "selection", "benchmark"):
        context = _role_context(role)
        assert context["title"]
        assert context["material_label"]
        assert context["influence_note"]
    assert _role_context("fit")["benchmark_only"] is False
    assert _role_context("selection")["benchmark_only"] is False
    assert _role_context("benchmark")["benchmark_only"] is True
    try:
        _role_context("not-a-role")
    except ValueError:
        pass
    else:
        raise AssertionError("invalid role should fail")

    rng = np.random.default_rng(260911)
    target = rng.standard_normal(44100) * 0.05
    pred = target * 0.5

    target_stats = _signal_stats(target)
    pred_stats = _signal_stats(pred)
    assert abs((pred_stats["rms_dbfs"] - target_stats["rms_dbfs"]) + 6.0206) < 1e-3
    assert abs((pred_stats["peak_dbfs"] - target_stats["peak_dbfs"]) + 6.0206) < 1e-3
    assert abs((pred_stats["p999_dbfs"] - target_stats["p999_dbfs"]) + 6.0206) < 1e-3
    assert abs(pred_stats["crest_factor_db"] - target_stats["crest_factor_db"]) < 1e-10

    static = _static_diagnostics(pred, target)
    assert abs(static["rms_error_db"] + 6.0206) < 1e-3
    assert abs(static["peak_error_db"] + 6.0206) < 1e-3
    assert abs(static["p999_error_db"] + 6.0206) < 1e-3
    assert abs(static["crest_factor_delta_db"]) < 1e-10

    envelope, frames = _envelope_diagnostics(pred, target)
    assert envelope["active_frames"] > 0
    assert abs(envelope["mean_error_db"] + 6.0206) < 1e-3
    assert abs(envelope["rmse_db"] - 6.0206) < 1e-3
    assert envelope["gain_matched_rmse_db"] < 1e-10
    assert envelope["gain_matched_p95_abs_error_db"] < 1e-10
    assert envelope["correlation"] is not None
    assert envelope["correlation"] > 0.999999999
    assert frames
    assert max(
        abs(row["gain_matched_error_db"])
        for row in frames
        if row["active"]
    ) < 1e-10

    print("NAM-centered diagnostics self-tests passed")


if __name__ == "__main__":
    main()
