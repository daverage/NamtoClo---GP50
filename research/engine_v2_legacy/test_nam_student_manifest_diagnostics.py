#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from analyze_nam_student_manifest_diagnostics import (
    _discover_report,
    _manifest_rows,
    _pairs_from_manifest,
    _verify_report_clo,
)
from distiller_v2_dsp import controls_to_a, write_clo


def _pair(task_id: str, synthetic: bool, model_id: int = 403888):
    return SimpleNamespace(
        task_id=task_id,
        synthetic=synthetic,
        model_id=model_id,
        model_key="tone37987_model403888_test",
        model_name="test model",
    )


def main() -> None:
    report = {
        "model_id": 403888,
        "model_key": "tone37987_model403888_test",
        "model_name": "test model",
        "material": {
            "fit": [
                {"task_id": "probe-a", "synthetic": True},
                {"task_id": "real-a", "synthetic": False},
                {"task_id": "probe-b", "synthetic": True},
                {"task_id": "real-b", "synthetic": False},
            ],
            "selection": [
                {"task_id": "sel-a", "synthetic": False},
                {"task_id": "sel-b", "synthetic": False},
            ],
            "benchmark": [{"task_id": "bench-a", "synthetic": False}],
        },
    }
    pairs = {
        task: _pair(task, task.startswith("probe"))
        for task in (
            "probe-a",
            "real-a",
            "probe-b",
            "real-b",
            "sel-a",
            "sel-b",
            "bench-a",
        )
    }

    assert [r["task_id"] for r in _manifest_rows(report, "fit-real")] == [
        "real-a",
        "real-b",
    ]
    assert [r["task_id"] for r in _manifest_rows(report, "fit-synthetic")] == [
        "probe-a",
        "probe-b",
    ]
    assert [p.task_id for p in _pairs_from_manifest(report, "selection", pairs)] == [
        "sel-a",
        "sel-b",
    ]

    try:
        _pairs_from_manifest(report, "benchmark", {})
    except RuntimeError as exc:
        assert "absent from the teacher dataset" in str(exc)
    else:
        raise AssertionError("missing manifest task did not fail")

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        model_dir = root / "frozen-model"
        model_dir.mkdir()
        report_path = model_dir / "report.json"
        report_path.write_text(json.dumps(report))
        found_path, found_report = _discover_report(root, pairs["real-a"])
        assert found_path == report_path.resolve()
        assert found_report["model_id"] == 403888

    a = controls_to_a(np.zeros(24, dtype=np.float64))
    pk = np.array([0.1, 0.12, 4.0, 5.0], dtype=np.float64)
    b = np.zeros(512, dtype=np.float64)
    b[0] = 0.5
    b[7] = -0.03
    parity_report = {
        "A128": a.tolist(),
        "pk": pk.tolist(),
        "B512_device": b.tolist(),
    }
    with tempfile.TemporaryDirectory() as td:
        clo_path = Path(td) / "frozen.clo"
        write_clo(clo_path, a, pk, b)
        _verify_report_clo(parity_report, clo_path)

        bad = dict(parity_report)
        bad_b = b.copy()
        bad_b[0] += 0.01
        bad["B512_device"] = bad_b.tolist()
        try:
            _verify_report_clo(bad, clo_path)
        except RuntimeError as exc:
            assert "report/CLO mismatch" in str(exc)
        else:
            raise AssertionError("report/CLO mismatch did not fail")

    print("exact frozen-manifest diagnostics self-tests passed")


if __name__ == "__main__":
    main()
