#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from distiller_v2_dsp import controls_to_a
import train_namtoclo_north_star_v42_fast as fast


def test_a128_projection_preserves_a_response():
    controls = np.linspace(-5.0, 4.0, 24, dtype=np.float64)
    a = controls_to_a(controls)
    projected, diagnostic = fast.controls_from_a128(a, 24)
    assert len(projected) == 24
    assert diagnostic["response_rmse_db_30_20k"] < 0.25
    assert diagnostic["response_max_abs_db_30_20k"] < 1.0


def test_historical_v4_report_becomes_compatible_restart():
    controls = np.linspace(-4.0, 3.0, 24, dtype=np.float64)
    a = controls_to_a(controls)
    pair = SimpleNamespace(model_id=12345, model_key="fixture-model")
    stimulus_sha = "fixture-sha"
    levels = [0.0, -6.0, -12.0, -18.0, -24.0]

    report = {
        "method": "north-star-teacher-student-varpro-v4-t3k-multilevel-stimulus-only",
        "model_id": 12345,
        "model_key": "fixture-model",
        "stimulus": {
            "stimulus_sha256": stimulus_sha,
            "levels_db": levels,
        },
        "A128": a.tolist(),
        "pk": [0.05, 0.05, 20.0, 20.0],
        "optimizer_fit_evidence_metrics": {"esr": 0.75},
    }

    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "model" / "report.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(report))
        warm = fast._historical_v4_compatible_warm_start(
            td,
            pair,
            {"stimulus_sha256": stimulus_sha},
            levels,
            24,
        )

    assert warm is not None
    assert warm["source_family"] == "V4"
    assert warm["controls_source"] == "projected_from_historical_A128"
    assert len(warm["controls_db"]) == 24
    assert warm["pk"] == report["pk"]


def test_historical_v4_restart_rejects_wrong_stimulus():
    pair = SimpleNamespace(model_id=12345, model_key="fixture-model")
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "model" / "report.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "method": "north-star-teacher-student-varpro-v4-t3k-multilevel-stimulus-only",
            "model_id": 12345,
            "model_key": "fixture-model",
            "stimulus": {"stimulus_sha256": "old", "levels_db": [0, -6, -12, -18, -24]},
            "A128": controls_to_a(np.zeros(24)).tolist(),
            "pk": [0.1, 0.1, 1.0, 1.0],
        }))
        warm = fast._historical_v4_compatible_warm_start(
            td,
            pair,
            {"stimulus_sha256": "new"},
            [0, -6, -12, -18, -24],
            24,
        )
    assert warm is None


def main():
    test_a128_projection_preserves_a_response()
    test_historical_v4_report_becomes_compatible_restart()
    test_historical_v4_restart_rejects_wrong_stimulus()
    print("north-star v4.2 fast warm-start self-tests passed")


if __name__ == "__main__":
    main()
