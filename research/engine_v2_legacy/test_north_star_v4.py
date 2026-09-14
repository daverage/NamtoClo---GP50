#!/usr/bin/env python3

import numpy as np

from distiller_v2_data import Pair
from distiller_v2_fit import Candidate, Metrics
from distiller_v2_north_star_v4 import (
    _FitCheckpoint,
    _select_best_fit_checkpoint,
    build_stimulus_fit_evidence,
)
from north_star_v4_stimulus import parse_levels


def _metrics(esr):
    return Metrics(esr, esr, 0.0, 0.0, 0.0)


def _candidate(esr):
    return Candidate(
        controls_db=np.zeros(2),
        pk=np.array([0.1, 0.1, 1.0, 1.0]),
        b=np.zeros(512),
        fit=_metrics(esr),
        selection=_metrics(float("inf")),
    )


def _pair(level):
    return Pair(
        task_id=f"stim-{level}",
        model_key="m",
        model_name="anonymous",
        tone_id=1,
        model_id=2,
        nam_split="development",
        role="fit",
        dataset="tone3000_t3k_sweep_v3",
        synthetic=True,
        input_path="/tmp/T3K-sweep-v3.wav",
        target_path="/tmp/target.wav",
        duration_s=1.0,
        input_id=f"L{level}",
        source_path="/tmp/T3K-sweep-v3.wav",
        source_sha256="abc",
        start_s=0.0,
        level_offset_db=float(level),
    )


def main():
    levels = parse_levels("0,-6,-12,-18,-24,-12")
    assert levels == (0.0, -6.0, -12.0, -18.0, -24.0), levels

    # Every independently rendered global input level is one equal-weight whole
    # stimulus unit. Quiet variants cannot disappear merely because they have
    # less absolute energy; the existing B preparation additionally normalizes
    # each target window by target energy.
    audio = []
    for i, level in enumerate(levels):
        x = np.ones(1000, dtype=np.float64) * (0.5 ** i)
        y = x * 0.25
        audio.append((x, y, _pair(level)))
    evidence = build_stimulus_fit_evidence(audio, stimulus_sha256="abc")
    assert evidence.source_units == len(levels)
    assert evidence.virtual_examples == len(levels)
    for windows in evidence.windows:
        assert len(windows) == 1
        assert windows[0].start == 0
        assert windows[0].end == 1000
        assert abs(windows[0].weight - 1.0 / len(levels)) < 1e-12

    # V4 checkpoint/start choice is FIT-only: there is no guitar selection score
    # available to influence the trained CLO.
    a = _FitCheckpoint(0, "legacy", 3, 0.75, 0.16, _candidate(0.20))
    b = _FitCheckpoint(1, "hard", 2, 1.5, 0.27, _candidate(0.15))
    chosen = _select_best_fit_checkpoint([a, b])
    assert chosen is b

    print("north-star v4 stimulus-only self-tests passed")


if __name__ == "__main__":
    main()
