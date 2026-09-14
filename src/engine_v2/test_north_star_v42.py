#!/usr/bin/env python3
from __future__ import annotations

import math

import numpy as np

import distiller_v2_north_star_v42 as v42
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


def main():
    test_a_line_search_brackets_and_refines_near_target()
    test_a_line_search_accelerates_far_coordinate()
    test_a_line_search_reports_safety_cap()
    test_pk_line_search_is_multiplicative_and_converges()
    test_stop_rules()
    test_constants_preserve_v4_fine_resolution()
    print("north-star v4.2 accelerated convergence self-tests passed")


if __name__ == "__main__":
    main()