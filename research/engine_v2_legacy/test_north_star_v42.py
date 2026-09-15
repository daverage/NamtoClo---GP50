#!/usr/bin/env python3
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

import distiller_v2_north_star_v42 as v42
import train_namtoclo_north_star_v42 as trainer
from clo_reader import read_clo
from distiller_v2_dsp import A_TAPS, B_TAPS, POST, write_clo
from distiller_v2_fit import Candidate, Metrics


def _metrics(esr: float) -> Metrics:
    return Metrics(esr, esr, 0.0, 0.0, 0.0)


def _candidate(ctrl=0.0, pk=None, esr=1.0) -> Candidate:
    if pk is None:
        pk = [0.1, 0.1, 1.0, 1.0]
    return Candidate(
        np.asarray([ctrl], dtype=np.float64),
        np.asarray(pk, dtype=np.float64),
        np.zeros(1, dtype=np.float64),
        _metrics(esr),
        _metrics(float("inf")),
    )


def test_a_line_search_brackets_and_refines_near_target():
    original = v42._evaluate_fit
    target = 0.15

    def fake_eval(ctrl, pk, *_args):
        esr = float((ctrl[0] - target) ** 2)
        return Candidate(ctrl.copy(), pk.copy(), np.zeros(1), _metrics(esr), _metrics(float("inf")))

    try:
        v42._evaluate_fit = fake_eval
        start = _candidate(esr=target**2)
        out, moved, evaluations, capped = v42._line_search_a_coordinate(
            start,
            0,
            None,
            None,
            None,
            max_line_steps=20,
        )
    finally:
        v42._evaluate_fit = original

    assert abs(float(out.controls_db[0]) - target) < 1e-12
    assert moved == 3
    assert evaluations >= 4
    assert not capped


def test_a_line_search_accelerates_far_coordinate():
    original = v42._evaluate_fit
    target_steps = 32
    target = target_steps * v42.FINE_A_STEP_DB

    def fake_eval(ctrl, pk, *_args):
        esr = float((ctrl[0] - target) ** 2)
        return Candidate(ctrl.copy(), pk.copy(), np.zeros(1), _metrics(esr), _metrics(float("inf")))

    try:
        v42._evaluate_fit = fake_eval
        start = _candidate(esr=target**2)
        out, moved, evaluations, capped = v42._line_search_a_coordinate(
            start,
            0,
            None,
            None,
            None,
            max_line_steps=96,
        )
    finally:
        v42._evaluate_fit = original

    assert abs(float(out.controls_db[0]) - target) < 1e-12
    assert moved == target_steps
    # The old walk needed ~target_steps sequential improving evaluations.
    assert evaluations < target_steps
    assert not capped


def test_a_line_search_reports_safety_cap():
    original = v42._evaluate_fit

    def fake_eval(ctrl, pk, *_args):
        esr = float((ctrl[0] - 10.0) ** 2)
        return Candidate(ctrl.copy(), pk.copy(), np.zeros(1), _metrics(esr), _metrics(float("inf")))

    try:
        v42._evaluate_fit = fake_eval
        start = _candidate(esr=100.0)
        out, moved, _evaluations, capped = v42._line_search_a_coordinate(
            start,
            0,
            None,
            None,
            None,
            max_line_steps=2,
        )
    finally:
        v42._evaluate_fit = original

    assert abs(float(out.controls_db[0]) - 0.10) < 1e-12
    assert moved == 2
    assert capped


def test_pk_line_search_is_multiplicative_and_converges():
    original = v42._evaluate_pk
    target = math.exp(3.0 * v42.FINE_PK_LOG_STEP)

    def fake_eval(_aouts, ctrl, pk, *_args):
        esr = float(math.log(pk[2] / target) ** 2)
        return Candidate(ctrl.copy(), pk.copy(), np.zeros(1), _metrics(esr), _metrics(float("inf")))

    try:
        v42._evaluate_pk = fake_eval
        start = _candidate(esr=math.log(1.0 / target) ** 2)
        out, moved, evaluations, capped = v42._line_search_pk_coordinate(
            start,
            2,
            None,
            None,
            None,
            None,
            max_line_steps=20,
        )
    finally:
        v42._evaluate_pk = original

    assert abs(math.log(float(out.pk[2]) / target)) < 1e-12
    assert moved == 3
    assert evaluations >= 4
    assert not capped


def test_stop_rules():
    assert v42._polish_stop_reason(1.0, 1.0, 0, 1e-5) == "no_moves"
    assert (
        v42._polish_stop_reason(1.0, 1.0 - 1e-7, 1, 1e-5)
        == "negligible_fit_improvement"
    )
    assert v42._polish_stop_reason(1.0, 0.99, 1, 1e-5) is None

    # The production default stops once a complete cycle contributes <=0.1%
    # relative FIT-ESR improvement, while materially larger descent continues.
    assert (
        v42._polish_stop_reason(1.0, 0.9995, 1)
        == "negligible_fit_improvement"
    )
    assert v42._polish_stop_reason(1.0, 0.998, 1) is None


def test_constants_preserve_v4_fine_resolution():
    assert v42.FINE_A_STEP_DB == 0.05
    assert v42.FINE_PK_LOG_STEP == 0.02
    assert v42.DEFAULT_POLISH_REL_TOL == 1.0e-3
    assert v42.DEFAULT_MAX_LINE_STEPS >= 1


def _fake_report(method: str):
    return {
        "method": method,
        "stimulus": {
            "stimulus_sha256": "stim-sha",
            "levels_db": [0.0, -6.0, -12.0, -18.0, -24.0],
        },
        "controls_db": [0.0, 1.0],
        "pk": [0.1, 0.1, 1.0, 1.0],
        "optimizer_fit_evidence_metrics": {"esr": 0.25},
    }


def test_v4_report_is_valid_optimization_warm_start():
    original = trainer._find_v3_report
    report = _fake_report("north-star-teacher-student-varpro-v4-t3k-multilevel-stimulus-only")
    try:
        trainer._find_v3_report = lambda root, pair: (Path(root) / "report.json", report)
        warm = trainer._compatible_v4_family_warm_start(
            "/v4",
            object(),
            {"stimulus_sha256": "stim-sha"},
            (0.0, -6.0, -12.0, -18.0, -24.0),
            2,
            "V4",
        )
        wrong_family = trainer._compatible_v4_family_warm_start(
            "/v4",
            object(),
            {"stimulus_sha256": "stim-sha"},
            (0.0, -6.0, -12.0, -18.0, -24.0),
            2,
            "V4.1",
        )
    finally:
        trainer._find_v3_report = original

    assert warm is not None
    assert warm["source_family"] == "V4"
    assert warm["controls_db"] == [0.0, 1.0]
    assert warm["pk"] == [0.1, 0.1, 1.0, 1.0]
    assert wrong_family is None


def test_warm_start_priority_prefers_newest_family():
    original = trainer._compatible_v4_family_warm_start
    calls = []

    def fake_compatible(root, pair, manifest, levels, controls, family):
        calls.append((family, str(root)))
        if family == "V4":
            return {"source_family": family}
        return None

    try:
        trainer._compatible_v4_family_warm_start = fake_compatible
        warm = trainer._best_compatible_warm_start(
            "/out",
            "/v41",
            "/v4",
            object(),
            {"stimulus_sha256": "stim-sha"},
            (0.0, -6.0, -12.0, -18.0, -24.0),
            24,
        )
    finally:
        trainer._compatible_v4_family_warm_start = original

    assert warm == {"source_family": "V4"}
    assert [family for family, _ in calls] == ["V4.2", "V4.1", "V4"]


def main():
    test_a_line_search_brackets_and_refines_near_target()
    test_a_line_search_accelerates_far_coordinate()
    test_a_line_search_reports_safety_cap()
    test_pk_line_search_is_multiplicative_and_converges()
    test_stop_rules()
    test_constants_preserve_v4_fine_resolution()
    test_v4_report_is_valid_optimization_warm_start()
    test_warm_start_priority_prefers_newest_family()
    print("north-star v4.2 accelerated convergence self-tests passed")


def test_clo_reader_round_trips_against_write_clo(tmp_path=None):
    """clo_reader.read_clo must agree with this project's own write_clo.

    Uses distiller_v2_dsp.write_clo (this project's own byte-level writer) as
    independent ground truth for clo_reader.py's offset reading, rather than
    trusting a re-read of the C++ parseModel offsets alone -- see
    src/engine_v2/clo_reader.py's module docstring for the C++ cross-reference.
    """
    rng = np.random.default_rng(1234)
    a = rng.normal(size=A_TAPS)
    pk = np.array([0.123, 0.234, 1.11, 0.98])
    b = rng.normal(size=B_TAPS)

    path = Path("/tmp") / "namtoclo_clo_reader_roundtrip_test.clo"
    write_clo(path, a, pk, b)
    try:
        model = read_clo(path)
        assert np.allclose(model.pre, [1.0, 0.0, 0.0, 0.0, 0.0])
        assert np.allclose(model.post, POST)
        assert np.allclose([model.pp, model.pn, model.kp, model.kn], pk, atol=1e-6)
        assert np.allclose(model.a, a, atol=1e-6)
        assert np.allclose(model.b, b, atol=1e-6)
    finally:
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    test_clo_reader_round_trips_against_write_clo()
    main()
