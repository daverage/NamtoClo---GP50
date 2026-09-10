#!/usr/bin/env python3
from types import SimpleNamespace

import numpy as np

from distiller_v2_data import Pair
from distiller_v2_north_star_v2 import (
    _prepare_evidence,
    build_fit_evidence,
    evidence_esr,
    solve_b_balanced,
)


def _pair(task, basename, synthetic=True):
    return Pair(
        task_id=task,
        model_key="m",
        model_name="model",
        tone_id=1,
        model_id=2,
        nam_split="development",
        role="fit",
        dataset="synthetic_probes" if synthetic else "guitarjam",
        synthetic=synthetic,
        input_path=f"/tmp/{basename}",
        target_path=f"/tmp/{basename}.target.wav",
        duration_s=1.0,
        input_id=task,
        source_path=f"/tmp/{basename}",
        source_sha256=f"sha-{task}",
        start_s=0.0,
        level_offset_db=0.0,
    )


def _tone_ladder_audio():
    sr = 44100
    n = int(round((0.5 + 8 * 1.25) * sr))
    x = np.zeros(n)
    y = np.zeros(n)
    levels = (-36, -30, -24, -18, -12, -9, -6, -3)
    rng = np.random.default_rng(260910)
    for i, level in enumerate(levels):
        start = int(round((0.50 + i * 1.25) * sr))
        end = int(round((0.50 + i * 1.25 + 1.05) * sr))
        amp = 10.0 ** (level / 20.0)
        signal = rng.standard_normal(end - start) * amp
        x[start:end] = signal
        y[start:end] = 0.37 * signal
    return x, y


def main():
    sr = 44100
    probe_durations = {
        "04_log_sweep_-36dBFS.wav": 9.0,
        "07_multisine_level_ladder.wav": 10.5,
        "09_1kHz_level_ladder.wav": 8.5,
        "10_frequency_level_matrix.wav": 11.5,
        "11_two_tone_IMD_matrix.wav": 13.7,
        "13_tone_burst_transients.wav": 6.5,
    }

    audio = []
    for i, (basename, duration) in enumerate(probe_durations.items()):
        n = int(round(duration * sr))
        x = np.ones(n) * 0.01
        y = np.ones(n) * 0.005
        audio.append((x, y, _pair(f"probe-{i}", basename)))

    for i in range(6):
        n = sr
        x = np.ones(n) * 0.02
        y = np.ones(n) * 0.01
        audio.append((x, y, _pair(f"real-{i}", f"real-{i}.wav", synthetic=False)))

    evidence = build_fit_evidence(audio)
    assert evidence.source_units == 12, evidence.source_units
    assert evidence.virtual_examples == 72, evidence.virtual_examples

    group_weights = {}
    for windows in evidence.windows:
        for window in windows:
            group_weights[window.group] = group_weights.get(window.group, 0.0) + window.weight

    assert len(group_weights) == 12, group_weights
    expected = 1.0 / 12.0
    for group, weight in group_weights.items():
        assert abs(weight - expected) < 1e-12, (group, weight)

    # A relative error on the -36 dB multisine window must have the same
    # influence as the same relative error on the -3 dB window.
    x, target = _tone_ladder_audio()
    pair = _pair("ladder", "07_multisine_level_ladder.wav")
    ladder_evidence = build_fit_evidence([(x, target, pair)])

    quiet_bad = target.copy()
    quiet_start = int(round(0.50 * sr))
    quiet_end = int(round((0.50 + 1.05) * sr))
    quiet_bad[quiet_start:quiet_end] *= 2.0

    loud_bad = target.copy()
    loud_start = int(round((0.50 + 7 * 1.25) * sr))
    loud_end = int(round((0.50 + 7 * 1.25 + 1.05) * sr))
    loud_bad[loud_start:loud_end] *= 2.0

    quiet_esr = evidence_esr([quiet_bad], [target], ladder_evidence)
    loud_esr = evidence_esr([loud_bad], [target], ladder_evidence)
    assert abs(quiet_esr - loud_esr) < 1e-10, (quiet_esr, loud_esr)
    assert abs(quiet_esr - 0.125) < 1e-8, quiet_esr

    # The balanced analytic B solve should recover a common linear gain across
    # the same -36 ... -3 dB ladder despite the 33 dB energy spread. Broadband
    # deterministic content is used here so the 512-tap solve is actually
    # identifiable across frequency rather than testing only DC.
    nfft = 1 << (len(x) + 512 - 1).bit_length()
    ws = SimpleNamespace(
        targets=[target],
        lengths=[len(target)],
        nfft=nfft,
    )
    prepared = _prepare_evidence(ws, ladder_evidence)
    b, metrics = solve_b_balanced([x], ws, ladder_evidence, prepared)
    assert abs(b[0] - 0.37) < 0.02, b[0]
    assert metrics.esr < 2e-3, metrics.esr

    print("north-star v2 self-tests passed")


if __name__ == "__main__":
    main()
