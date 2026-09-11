#!/usr/bin/env python3

import numpy as np

from distiller_v2_fit import Candidate, Metrics
from distiller_v2_north_star_v3 import (
    DEFAULT_MULTISTARTS,
    MAX_MULTISTARTS,
    _Checkpoint,
    _pk_seed_bank,
    _search_steps,
    _select_best_checkpoint,
)


def _metrics(esr):
    return Metrics(esr, esr, 0.0, 0.0, 0.0)


def _candidate(fit_esr, selection_esr):
    return Candidate(
        controls_db=np.zeros(2),
        pk=np.array([0.1, 0.1, 1.0, 1.0]),
        b=np.zeros(512),
        fit=_metrics(fit_esr),
        selection=_metrics(selection_esr),
    )


def main():
    # Regression for the v2 four-round truncation: six requested rounds must
    # actually produce six coarse-to-fine search steps.
    six = _search_steps(6)
    assert len(six) == 6, six
    assert all(six[i + 1][0] <= six[i][0] for i in range(len(six) - 1))
    assert all(six[i + 1][1] <= six[i][1] for i in range(len(six) - 1))

    # Default search reaches the explicit general floors without an unbounded
    # tail of duplicate floor-sized rounds.
    default_steps = _search_steps(8)
    assert 6 <= len(default_steps) <= 8, default_steps
    assert abs(default_steps[-1][0] - 0.05) < 1e-12, default_steps[-1]
    assert abs(default_steps[-1][1] - 0.02) < 1e-12, default_steps[-1]

    # Multistart is deterministic and preserves the historical seed first.
    bank = _pk_seed_bank(DEFAULT_MULTISTARTS)
    assert len(bank) == DEFAULT_MULTISTARTS
    assert bank[0][0] == "legacy"
    assert np.allclose(bank[0][1], [0.1, 0.1, 1.0, 1.0])
    assert not np.allclose(bank[1][1], bank[2][1])
    for _, pk in _pk_seed_bank(MAX_MULTISTARTS):
        assert np.all((pk[:2] >= 0.01) & (pk[:2] <= 2.0)), pk
        assert np.all((pk[2:] >= 0.05) & (pk[2:] <= 80.0)), pk

    # Selection chooses among retained checkpoints. It may legitimately pick an
    # earlier checkpoint even when a later FIT-only checkpoint has lower FIT ESR.
    early = _Checkpoint(0, "legacy", 1, 1.5, 0.2, _candidate(0.30, 0.10))
    later = _Checkpoint(0, "legacy", 2, 0.75, 0.1, _candidate(0.20, 0.12))
    chosen = _select_best_checkpoint([later, early])
    assert chosen is early, (chosen.round_index, chosen.candidate.selection.esr)

    # Across starts, selection remains the primary chooser with FIT only as a
    # deterministic tie-breaker.
    tie_a = _Checkpoint(0, "legacy", 3, 0.3, 0.05, _candidate(0.20, 0.08))
    tie_b = _Checkpoint(1, "hard_symmetric", 3, 0.3, 0.05, _candidate(0.15, 0.08))
    chosen = _select_best_checkpoint([tie_a, tie_b])
    assert chosen is tie_b

    print("north-star v3 search self-tests passed")


if __name__ == "__main__":
    main()
