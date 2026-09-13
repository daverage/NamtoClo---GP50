#!/usr/bin/env python3
from __future__ import annotations

import tempfile
from pathlib import Path

import numpy as np

from distiller_v2_data import Pair
from distiller_v2_dsp import POST, PRE, controls_to_a, render_full, write_clo
from evaluate_north_star_vs_original import (
    BANDS,
    _discover_clo,
    _spectral_views,
    _tail_indices,
    _tail_metrics,
    read_compact_clo,
    render_compact_clo,
    select_shared_real_material,
)


def _pair(task, model_key, source, start=0.0, role="benchmark", model_id=2):
    return Pair(
        task_id=task,
        model_key=model_key,
        model_name=model_key,
        tone_id=1,
        model_id=model_id,
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
    # from the coefficients actually quantized into the CLO file.
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
        trusted = render_full(x, clo.a, clo.pk, clo.b)
        parsed = render_compact_clo(x, clo)
        assert np.max(np.abs(trusted - parsed)) < 1e-12

    # Current NSv2 names must be discoverable by model ID even when the file is
    # copied outside its normal per-model output directory. Legacy names stay
    # supported for reproducibility of the first v2 runs.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        pair = _pair("naming", "tone1_model403887_deadbeef", "common", model_id=403887)
        current = root / "NSV2__403887__JCM800_G5.clo"
        write_clo(current, a, pk, b)
        assert _discover_clo(root, pair, "north_star") == current
        current.unlink()
        legacy_dir = root / "tone1_model403887_deadbeef__JCM800_G5"
        legacy_dir.mkdir()
        legacy = legacy_dir / "distilled.clo"
        write_clo(legacy, a, pk, b)
        assert _discover_clo(root, pair, "north_star") == legacy

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

    # Disjoint tail frames are never concatenated for spectral analysis. An
    # identical teacher/student tail must therefore remain exactly zero-error,
    # rather than acquiring broadband energy from artificial stitch boundaries.
    rng = np.random.default_rng(456)
    frames = []
    for amp in (0.10, 0.10, 0.10, 0.010, 0.006, 0.003, 0.0015, 0.0, 0.0):
        frames.append(rng.standard_normal(frame) * amp)
    x = np.concatenate(frames)
    target = 0.63 * x
    tail = _tail_metrics(x, target, target.copy())
    assert tail is not None
    assert tail["seconds"] >= 0.25
    assert abs(tail["esr"]) < 1e-15
    assert abs(tail["gain_matched_esr"]) < 1e-15
    assert tail["gain_matched_spectral_rmse_db"] < 1e-10
    confident_tail = [r for r in tail["curve"] if r["confident"]]
    assert confident_tail
    assert max(abs(r["gain_matched_error_db"]) for r in confident_tail) < 1e-10

    print("three-way evaluator self-tests passed")


if __name__ == "__main__":
    main()
