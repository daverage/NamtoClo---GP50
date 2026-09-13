#!/usr/bin/env python3

from distiller_v2_north_star_v3 import A_STEP_FLOOR_DB, PK_LOG_STEP_FLOOR
from distiller_v2_north_star_v41 import (
    DEFAULT_POLISH_CYCLES,
    DEFAULT_POLISH_REL_TOL,
    FINE_A_STEP_DB,
    FINE_PK_LOG_STEP,
    _polish_stop_reason,
)


def main():
    # V4.1 must polish at the exact established V3/V4 floor resolution rather
    # than silently introducing a new search scale.
    assert FINE_A_STEP_DB == float(A_STEP_FLOOR_DB) == 0.05
    assert FINE_PK_LOG_STEP == float(PK_LOG_STEP_FLOOR) == 0.02
    assert DEFAULT_POLISH_CYCLES >= 1
    assert 0.0 < DEFAULT_POLISH_REL_TOL < 1.0

    # A cycle with no accepted A or P/K move is converged regardless of the
    # numerical tolerance.
    assert _polish_stop_reason(0.2, 0.2, 0, 0) == "no_moves"

    # A moved coordinate can still be treated as negligible when the total FIT
    # improvement is below the scale-relative convergence threshold.
    assert (
        _polish_stop_reason(0.2, 0.2 - 1.0e-8, 1, 0, 1.0e-5)
        == "negligible_fit_improvement"
    )

    # Material improvement must continue polishing.
    assert _polish_stop_reason(0.2, 0.19, 2, 1, 1.0e-5) is None

    print("north-star v4.1 convergence self-tests passed")


if __name__ == "__main__":
    main()
