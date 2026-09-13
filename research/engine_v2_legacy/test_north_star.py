#!/usr/bin/env python3
from types import SimpleNamespace

import numpy as np

from distiller_v2_data import Pair
from distiller_v2_north_star import (
    NORTH_STAR_PROBES,
    _solve_b_normalized,
    select_north_star_material,
)


def _pair(task, role, dataset, synthetic=False, source=None, level=0.0, start=0.0):
    return Pair(
        task_id=task,
        model_key="m",
        model_name="model",
        tone_id=1,
        model_id=2,
        nam_split="development",
        role=role,
        dataset=dataset,
        synthetic=synthetic,
        input_path="in.wav",
        target_path="out.wav",
        duration_s=1.0,
        input_id=task,
        source_path=source or f"/tmp/{task}.wav",
        source_sha256=f"sha-{source or task}",
        start_s=start,
        level_offset_db=level,
    )


def main():
    pairs = []
    # All six canonical probes must be pulled into FIT regardless of their
    # historical teacher-dataset role tags.
    for i, (_, basename) in enumerate(NORTH_STAR_PROBES):
        role = "selection" if i in (4, 5) else "fit"
        pairs.append(
            _pair(
                f"probe{i}",
                role,
                "synthetic_probes",
                synthetic=True,
                source=f"/tmp/{basename}",
            )
        )

    # Real tasks include matched level variants. Canonical selection must use
    # one base/nearest-to-zero task per source segment rather than multiplying
    # the real set with level augmentation.
    for role, dataset, count in (
        ("fit", "guitarjam", 4),
        ("fit", "freepats", 4),
        ("selection", "egfxset", 5),
        ("benchmark", "guitarset", 4),
    ):
        for i in range(count):
            source = f"/tmp/{dataset}_{i}.wav"
            pairs += [
                _pair(f"{role}-{i}-m6", role, dataset, source=source, level=-6.0, start=i),
                _pair(f"{role}-{i}-z0", role, dataset, source=source, level=0.0, start=i),
                _pair(f"{role}-{i}-p6", role, dataset, source=source, level=6.0, start=i),
            ]

    # The wider research corpus intentionally contains bass and sample-library
    # artifacts. They must never consume a canonical real-guitar slot.
    pairs += [
        _pair("bass-fit", "fit", "growlybass", source="/tmp/a2_staccato_rr2.wav"),
        _pair("bass-fit-2", "fit", "black-blue-basses", source="/tmp/bass_note.wav"),
        _pair("release-name", "fit", "black-green", source="/tmp/release_b5_rr4.wav"),
        _pair("release-dir", "fit", "black-green", source="/tmp/Samples/green/rel/e4_rr1.wav"),
        _pair("noise-name", "fit", "shinyguitar", source="/tmp/string_noise_rr1.wav"),
        _pair("silence-name", "fit", "emilyguitar", source="/tmp/silence.wav"),
    ]

    material = select_north_star_material(
        pairs, fit_real=6, selection_real=4, benchmark_real=3, seed=123
    )
    assert len(material.probes) == 6, len(material.probes)
    assert len(material.fit) == 12, len(material.fit)
    assert len(material.fit_real) == 6
    assert len(material.selection) == 4
    assert len(material.benchmark) == 3
    assert all(not p.synthetic for p in material.selection)
    assert all(not p.synthetic for p in material.benchmark)
    assert all(abs(p.level_offset_db) < 1e-12 for p in material.fit_real)
    assert all(abs(p.level_offset_db) < 1e-12 for p in material.selection_real)
    assert all(abs(p.level_offset_db) < 1e-12 for p in material.benchmark_real)
    # Dataset round-robin should preserve diversity in the fit set.
    assert {p.dataset for p in material.fit_real} == {"guitarjam", "freepats"}
    selected_tasks = {p.task_id for p in material.fit_real}
    assert "bass-fit" not in selected_tasks
    assert "bass-fit-2" not in selected_tasks
    assert "release-name" not in selected_tasks
    assert "release-dir" not in selected_tasks
    assert "noise-name" not in selected_tasks
    assert "silence-name" not in selected_tasks

    # Normalized shared-B solve: recover the same one-tap gain from examples
    # whose target amplitudes differ by two orders of magnitude.
    n = 4096
    rng = np.random.default_rng(260910)
    x1 = rng.standard_normal(n)
    x2 = rng.standard_normal(n) * 0.01
    gain = 0.37
    t1 = gain * x1
    t2 = gain * x2
    nfft = 1 << (n + 512 - 1).bit_length()
    ws = SimpleNamespace(
        targets=[t1, t2],
        target_ffts=[np.fft.rfft(t1, nfft), np.fft.rfft(t2, nfft)],
        lengths=[n, n],
        nfft=nfft,
    )
    b, metrics = _solve_b_normalized([x1, x2], ws)
    assert abs(b[0] - gain) < 0.01, b[0]
    assert metrics.esr < 1e-3, metrics.esr

    print("north-star self-tests passed")


if __name__ == "__main__":
    main()
