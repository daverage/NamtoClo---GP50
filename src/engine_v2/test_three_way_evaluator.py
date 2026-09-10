#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from distiller_v2_data import Pair
from distiller_v2_dsp import POST, PRE, controls_to_a, render_full, write_clo
from evaluate_north_star_vs_original import (
    BANDS,
    _spectral_views,
    _tail_indices,
    read_compact_clo,
    render_compact_clo,
    select_shared_real_material,
)


def _pair(task, model_key, source, start=0.0, role="benchmark"):
    return Pair(
        task_id=task,
        model_key=model_key,
        model_name=model_key,
        tone_id=1,
        model_id=2,
        nam_split="development",
        role=role,
        dataset="guitarset",
        synthetic=False,
        input_path="/tmp/in.wav",
        target_path="/tmp/out.wav",
        duration_s=4.0,
        input_id=task,
        source_path=f"/tmp/{source}.wav",
        source_sha256=f"sha-{source}",
        start_s=start,
        level_offset_db=0.0,
    )


def main():
    # Compact parser/renderer must reproduce the trusted device-style renderer
    # for a CLO written by the clean-sheet serializer.
    a = controls_to_a(np.zeros(24))
    pk = np.array([0.08, 0.11, 4.0, 5.0])
    b = np.zeros(512)
    b[0] = 0.37
    b[4] = -0.05
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "test.clo"
        write_clo(path, a, pk, b)
        clo = read_compact_clo(path)
        assert np.allclose(clo.pre, PRE)
        assert np.allclose(clo.post, POST)
        assert np.allclose(clo.a, a, atol=1e-7)
        assert np.allclose(clo.pk, pk, atol=1e-7)
        assert np.allclose(clo.b, b, atol=1e-7)
        rng = np.random.default_rng(260910)
        x = rng.standard_normal(8192) * 0.05
        trusted = render_full(x, a, pk, b)
        parsed = render_compact_clo(x, clo)
        assert np.max(np.abs(trusted - parsed)) < 2e-7

    # Shared material selection must use the same underlying performance for
    # every model even though model-specific teacher task IDs differ.
    groups = {
        "m1": [
            _pair("m1-a", "m1", "common-a", 1.0),
            _pair("m1-b", "m1", "common-b", 2.0),
            _pair("m1-only", "m1", "only-m1", 3.0),
        ],
        "m2": [
            _pair("m2-a", "m2", "common-a", 1.0),
            _pair("m2-b", "m2", "common-b", 2.0),
            _pair("m2-only", "m2", "only-m2", 3.0),
        ],
        "m3": [
            _pair("m3-a", "m3", "common-a", 1.0),
            _pair("m3-b", "m3", "common-b", 2.0),
        ],
    }
    keys, chosen = select_shared_real_material(
        groups,
        ["m1", "m2", "m3"],
        count=2,
        seed=3,
    )
    assert len(keys) == 2
    for index, key in enumerate(keys):
        assert all(chosen[m][index].source_sha256 == key[1] for m in chosen)

    # Full-spectrum analysis spans bass through upper-harmonic breathing room.
    # A pure global -6.02 dB gain error must vanish after gain matching.
    rng = np.random.default_rng(123)
    target = rng.standard_normal(131072) * 0.01
    pred = target * 0.5
    spec = _spectral_views(pred, target)
    assert len(spec["curve"]) == 144
    assert spec["curve"][0]["freq_hz"] >= 20.0
    assert spec["curve"][-1]["freq_hz"] <= 20000.0
    assert len(spec["bands"]) == len(BANDS)
    confident = [row for row in spec["curve"] if row["confident"]]
    assert confident
    assert (
        abs(np.mean([row["absolute_error_db"] for row in confident]) + 6.0206)
        < 0.05
    )
    assert max(abs(row["gain_matched_error_db"]) for row in confident) < 1e-8

    # Tail selection identifies low-level decay/playing, but not the loud
    # plateau or numerical silence.
    frame = 4096
    x = np.concatenate(
        [
            np.ones(frame * 2) * 0.1,
            np.ones(frame * 2) * 0.01,
            np.ones(frame * 2) * 0.001,
            np.zeros(frame * 2),
        ]
    )
    tails = _tail_indices(x, frame=frame)
    assert tails
    assert all(a >= frame * 2 and b <= frame * 6 for a, b in tails)

    print("three-way evaluator self-tests passed")


if __name__ == "__main__":
    main()
