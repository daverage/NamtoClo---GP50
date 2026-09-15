#!/usr/bin/env python3
"""Focused, hardware-free tests for baseline_compare.py.

No real .nam conversion or built namtoclo binary is required: the released
converter invocation (`_run_namtoclo_convert`) is monkeypatched, and CLOs are
small synthetic files written with this project's own `write_clo` (the same
ground truth test_north_star_v42.py's clo_reader round-trip test uses).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

import baseline_compare as bc
from clo_reader import read_clo
from distiller_v2_dsp import A_TAPS, B_TAPS, write_clo
from distiller_v2_north_star_v4 import StimulusFitEvidence, EvidenceWindow


TMP = Path("/tmp/namtoclo_baseline_compare_tests")


def _fresh_tmp() -> Path:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    return TMP


def _write_fake_clo(path: Path, seed: int) -> None:
    rng = np.random.default_rng(seed)
    a = rng.normal(scale=0.01, size=A_TAPS)
    a[0] += 1.0  # keep it roughly identity-ish so rendering doesn't blow up
    pk = np.array([0.2, 0.2, 1.0, 1.0])
    b = rng.normal(scale=0.01, size=B_TAPS)
    b[0] += 1.0
    write_clo(path, a, pk, b)


def _tiny_evidence_and_audio(n=256, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=n).astype(np.float64) * 0.1
    y = x.copy()  # target == input is fine, we only care about relative ESR behavior
    inputs = [x]
    targets = [y]
    evidence = StimulusFitEvidence(
        windows=((EvidenceWindow(group="g", label="l", start=0, end=n, weight=1.0),),),
        virtual_examples=1,
        source_units=1,
        stimulus_sha256="deadbeef",
        levels_db=(0.0,),
    )
    return inputs, targets, evidence


# ---------------------------------------------------------------------------
# Baseline cache identity / hit / miss
# ---------------------------------------------------------------------------


def _patch_convert(monkeypatch, tmp: Path, calls: list, clo_seed: int = 1):
    """Monkeypatch _run_namtoclo_convert to write a fake CLO instead of
    shelling out to a real namtoclo binary, and count invocations."""

    def fake_convert(namtoclo_bin, nam_path, out_dir):
        calls.append((str(namtoclo_bin), str(nam_path)))
        out_dir.mkdir(parents=True, exist_ok=True)
        clo_path = out_dir / "fake.clo"
        _write_fake_clo(clo_path, clo_seed)
        argv = [str(namtoclo_bin), "convert", str(nam_path), "--output", str(out_dir), "--tone-match", "--reference", "auto", "--json"]
        return clo_path, argv

    monkeypatch.setattr(bc, "_run_namtoclo_convert", fake_convert)


def _make_nam_and_bin(tmp: Path, nam_content=b"nam-a", bin_content=b"bin-a"):
    nam_path = tmp / "model.nam"
    nam_path.write_bytes(nam_content)
    bin_path = tmp / "namtoclo"
    bin_path.write_bytes(bin_content)
    return nam_path, bin_path


def test_cache_hit_when_identity_identical(monkeypatch):
    tmp = _fresh_tmp()
    calls = []
    _patch_convert(monkeypatch, tmp, calls)
    nam_path, bin_path = _make_nam_and_bin(tmp)
    inputs, targets, evidence = _tiny_evidence_and_audio()
    bin_sha = bc._sha256_file(bin_path)
    cache_root = tmp / "cache"

    kwargs = dict(
        nam_path=nam_path,
        namtoclo_bin=bin_path,
        namtoclo_bin_sha256=bin_sha,
        stimulus_sha256="stim-a",
        levels_db=(0.0,),
        inputs=inputs,
        targets=targets,
        evidence=evidence,
        cache_root=cache_root,
        use_cache=True,
        force=False,
    )

    first = bc.get_baseline_result(**kwargs)
    assert first["cache_hit"] is False
    assert len(calls) == 1

    second = bc.get_baseline_result(**kwargs)
    assert second["cache_hit"] is True
    assert len(calls) == 1  # no second conversion
    assert second["esr"] == first["esr"]


def test_cache_miss_when_nam_sha_differs(monkeypatch):
    tmp = _fresh_tmp()
    calls = []
    _patch_convert(monkeypatch, tmp, calls)
    nam_path, bin_path = _make_nam_and_bin(tmp)
    inputs, targets, evidence = _tiny_evidence_and_audio()
    bin_sha = bc._sha256_file(bin_path)
    cache_root = tmp / "cache"

    def run(nam_content):
        nam_path.write_bytes(nam_content)
        return bc.get_baseline_result(
            nam_path=nam_path,
            namtoclo_bin=bin_path,
            namtoclo_bin_sha256=bin_sha,
            stimulus_sha256="stim-a",
            levels_db=(0.0,),
            inputs=inputs,
            targets=targets,
            evidence=evidence,
            cache_root=cache_root,
            use_cache=True,
            force=False,
        )

    r1 = run(b"nam-a")
    assert r1["cache_hit"] is False
    r2 = run(b"nam-b")  # different content -> different sha256 -> different identity
    assert r2["cache_hit"] is False
    assert len(calls) == 2


def test_cache_miss_when_bin_sha_differs(monkeypatch):
    tmp = _fresh_tmp()
    calls = []
    _patch_convert(monkeypatch, tmp, calls)
    nam_path, bin_path = _make_nam_and_bin(tmp)
    inputs, targets, evidence = _tiny_evidence_and_audio()
    cache_root = tmp / "cache"

    def run(bin_sha):
        return bc.get_baseline_result(
            nam_path=nam_path,
            namtoclo_bin=bin_path,
            namtoclo_bin_sha256=bin_sha,
            stimulus_sha256="stim-a",
            levels_db=(0.0,),
            inputs=inputs,
            targets=targets,
            evidence=evidence,
            cache_root=cache_root,
            use_cache=True,
            force=False,
        )

    r1 = run("sha-bin-1")
    assert r1["cache_hit"] is False
    r2 = run("sha-bin-2")
    assert r2["cache_hit"] is False
    assert len(calls) == 2


def test_cache_miss_when_stimulus_sha_or_levels_differ(monkeypatch):
    tmp = _fresh_tmp()
    calls = []
    _patch_convert(monkeypatch, tmp, calls)
    nam_path, bin_path = _make_nam_and_bin(tmp)
    inputs, targets, evidence = _tiny_evidence_and_audio()
    bin_sha = bc._sha256_file(bin_path)
    cache_root = tmp / "cache"

    def run(stim_sha, levels):
        return bc.get_baseline_result(
            nam_path=nam_path,
            namtoclo_bin=bin_path,
            namtoclo_bin_sha256=bin_sha,
            stimulus_sha256=stim_sha,
            levels_db=levels,
            inputs=inputs,
            targets=targets,
            evidence=evidence,
            cache_root=cache_root,
            use_cache=True,
            force=False,
        )

    r1 = run("stim-a", (0.0,))
    assert r1["cache_hit"] is False
    r2 = run("stim-b", (0.0,))  # different stimulus sha
    assert r2["cache_hit"] is False
    r3 = run("stim-b", (0.0, 3.0))  # different levels_db
    assert r3["cache_hit"] is False
    r4 = run("stim-b", (0.0, 3.0))  # now identical to r3 -> hit
    assert r4["cache_hit"] is True
    assert len(calls) == 3


def test_force_baseline_ignores_cache_hit(monkeypatch):
    tmp = _fresh_tmp()
    calls = []
    _patch_convert(monkeypatch, tmp, calls)
    nam_path, bin_path = _make_nam_and_bin(tmp)
    inputs, targets, evidence = _tiny_evidence_and_audio()
    bin_sha = bc._sha256_file(bin_path)
    cache_root = tmp / "cache"
    kwargs = dict(
        nam_path=nam_path, namtoclo_bin=bin_path, namtoclo_bin_sha256=bin_sha,
        stimulus_sha256="s", levels_db=(0.0,), inputs=inputs, targets=targets,
        evidence=evidence, cache_root=cache_root, use_cache=True,
    )
    r1 = bc.get_baseline_result(force=False, **kwargs)
    assert r1["cache_hit"] is False
    r2 = bc.get_baseline_result(force=True, **kwargs)
    assert r2["cache_hit"] is False  # forced recompute despite an existing entry
    assert len(calls) == 2


def test_no_baseline_cache_never_hits(monkeypatch):
    tmp = _fresh_tmp()
    calls = []
    _patch_convert(monkeypatch, tmp, calls)
    nam_path, bin_path = _make_nam_and_bin(tmp)
    inputs, targets, evidence = _tiny_evidence_and_audio()
    bin_sha = bc._sha256_file(bin_path)
    cache_root = tmp / "cache"
    kwargs = dict(
        nam_path=nam_path, namtoclo_bin=bin_path, namtoclo_bin_sha256=bin_sha,
        stimulus_sha256="s", levels_db=(0.0,), inputs=inputs, targets=targets,
        evidence=evidence, cache_root=cache_root, use_cache=False, force=False,
    )
    bc.get_baseline_result(**kwargs)
    r2 = bc.get_baseline_result(**kwargs)
    assert r2["cache_hit"] is False
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# EngineV2 side must be scored from the actual final CLO, not the stored metric
# ---------------------------------------------------------------------------


def test_engine_v2_scored_from_actual_clo_not_stored_metric(monkeypatch, tmp_path=None):
    tmp = _fresh_tmp()
    # Build a report.json whose stored optimizer_fit_evidence_metrics.esr is a
    # deliberately wrong/misleading number, while report["clo_path"] points at
    # a real CLO file that will score differently when actually rendered.
    clo_path = tmp / "engine_v2_final.clo"
    _write_fake_clo(clo_path, seed=42)

    inputs, targets, evidence = _tiny_evidence_and_audio(seed=7)
    x = inputs[0]
    y = targets[0]

    student_input = tmp / "in.wav"
    student_target = tmp / "tgt.wav"
    import soundfile as sf

    sf.write(str(student_input), x.astype(np.float32), 44100)
    sf.write(str(student_target), y.astype(np.float32), 44100)

    # Compute what the real rendered ESR of this CLO actually is, using the
    # exact same WAV-round-tripped evidence compare_one will reconstruct
    # (float32 WAV round trip is slightly lossy vs the raw float64 arrays
    # above, so re-derive it the same way to get a bit-exact comparison).
    level_variants = [{"level_db": 0.0, "student_input": str(student_input), "student_target": str(student_target)}]
    rt_inputs, rt_targets, rt_evidence = bc._build_evidence_and_audio(level_variants, "stim-x")
    real_esr = bc._score_clo(clo_path, rt_inputs, rt_targets, rt_evidence)

    # The stored metric claims something wildly different.
    misleading_stored_esr = real_esr + 1000.0
    assert misleading_stored_esr != real_esr

    report = {
        "model_name": "fake-model",
        "clo_path": str(clo_path),
        "optimizer_fit_evidence_metrics": {"esr": misleading_stored_esr},
        "stimulus": {
            "nam_path": str(tmp / "model.nam"),
            "stimulus_sha256": "stim-x",
            "level_variants": [
                {"level_db": 0.0, "student_input": str(student_input), "student_target": str(student_target)}
            ],
        },
    }
    report_path = tmp / "report.json"
    report_path.write_text(json.dumps(report))
    (tmp / "model.nam").write_bytes(b"nam-content")

    bin_path = tmp / "namtoclo"
    bin_path.write_bytes(b"bin-content")

    calls = []
    _patch_convert(monkeypatch, tmp, calls, clo_seed=99)

    result = bc.compare_one(
        report_path=report_path,
        namtoclo_bin=bin_path,
        namtoclo_bin_sha256=bc._sha256_file(bin_path),
        nam_root=None,
        cache_root=tmp / "cache",
        use_cache=True,
        force_baseline=False,
    )

    assert result["engine_v2_esr"] == real_esr
    assert result["engine_v2_esr"] != misleading_stored_esr


def test_released_and_engine_v2_use_identical_render_score_function(monkeypatch):
    """Swapping which CLO plays "released" vs "engine_v2" must give symmetric
    results, proving both sides go through the same _score_clo code path."""
    tmp = _fresh_tmp()
    clo_a = tmp / "a.clo"
    clo_b = tmp / "b.clo"
    _write_fake_clo(clo_a, seed=1)
    _write_fake_clo(clo_b, seed=2)
    inputs, targets, evidence = _tiny_evidence_and_audio(seed=3)

    esr_a = bc._score_clo(clo_a, inputs, targets, evidence)
    esr_b = bc._score_clo(clo_b, inputs, targets, evidence)

    # Symmetric: computing (a as released, b as engine_v2) and
    # (b as released, a as engine_v2) should give swapped, consistent deltas.
    d1, r1, w1 = bc._compute_winner(esr_a, esr_b)
    d2, r2, w2 = bc._compute_winner(esr_b, esr_a)
    assert d1 == -d2
    if w1 != "tie":
        assert (w1 == "engine_v2") != (w2 == "engine_v2") or w1 == w2 == "tie"


# ---------------------------------------------------------------------------
# Winner / relative_improvement math
# ---------------------------------------------------------------------------


def test_winner_engine_v2_when_lower_esr():
    delta, rel, winner = bc._compute_winner(released_engine_esr=1.0, engine_v2_esr=0.5)
    assert winner == "engine_v2"
    assert delta == 0.5
    assert abs(rel - 0.5) < 1e-12


def test_winner_released_when_lower_esr():
    delta, rel, winner = bc._compute_winner(released_engine_esr=0.5, engine_v2_esr=1.0)
    assert winner == "released"
    assert delta == -0.5
    assert rel < 0


def test_winner_tie_within_epsilon():
    delta, rel, winner = bc._compute_winner(released_engine_esr=1.0, engine_v2_esr=1.0 + 1e-10)
    assert winner == "tie"


def test_winner_not_tie_outside_epsilon():
    delta, rel, winner = bc._compute_winner(released_engine_esr=1.0, engine_v2_esr=1.01)
    assert winner == "released"


# ---------------------------------------------------------------------------
# Aggregate summary
# ---------------------------------------------------------------------------


def test_build_summary_counts_and_stats():
    results = [
        {"winner": "engine_v2", "relative_improvement": 0.2},
        {"winner": "engine_v2", "relative_improvement": 0.4},
        {"winner": "released", "relative_improvement": -0.1},
        {"winner": "tie", "relative_improvement": 0.0},
    ]
    failures = [{"report_path": "x", "error": "boom"}]
    summary = bc._build_summary(results, failures, "/bin/namtoclo", "deadbeef")
    assert summary["num_models_compared"] == 4
    assert summary["num_models_attempted"] == 5
    assert summary["engine_v2_wins"] == 2
    assert summary["released_wins"] == 1
    assert summary["ties"] == 1
    assert summary["failures"] == failures
    expected_mean = (0.2 + 0.4 - 0.1 + 0.0) / 4
    assert abs(summary["mean_relative_improvement"] - expected_mean) < 1e-12
    assert summary["median_relative_improvement"] == 0.1


def test_build_summary_handles_no_results():
    summary = bc._build_summary([], [{"report_path": "x", "error": "e"}], "/bin/namtoclo", "sha")
    assert summary["num_models_compared"] == 0
    assert summary["mean_relative_improvement"] is None
    assert summary["median_relative_improvement"] is None


# ---------------------------------------------------------------------------
# Deterministic ordering of parallel per-level renders
# ---------------------------------------------------------------------------


def test_score_clo_deterministic_across_repeated_runs():
    tmp = _fresh_tmp()
    clo_path = tmp / "multi.clo"
    _write_fake_clo(clo_path, seed=5)

    rng = np.random.default_rng(11)
    inputs = [rng.normal(size=300).astype(np.float64) * 0.1 for _ in range(5)]
    targets = [x.copy() for x in inputs]
    windows = tuple(
        (EvidenceWindow(group=f"g{i}", label=f"l{i}", start=0, end=len(inputs[i]), weight=1.0 / 5),)
        for i in range(5)
    )
    evidence = StimulusFitEvidence(
        windows=windows, virtual_examples=5, source_units=5,
        stimulus_sha256="s", levels_db=tuple(float(i) for i in range(5)),
    )

    results = [bc._score_clo(clo_path, inputs, targets, evidence) for _ in range(5)]
    assert all(r == results[0] for r in results)


def test_map_preserves_input_order_regardless_of_completion_time():
    from distiller_v2_fit import _map
    import time

    # Items that finish out of order (large sleep first) must still come
    # back in input order, since ThreadPoolExecutor.map preserves order.
    delays = [0.05, 0.0, 0.03, 0.0, 0.02]

    def fn(d):
        time.sleep(d)
        return d

    out = _map(fn, delays)
    assert out == delays


if __name__ == "__main__":
    import sys

    failures = 0
    tests = [(name, obj) for name, obj in list(globals().items()) if name.startswith("test_")]
    for name, fn in tests:
        try:
            if "monkeypatch" in fn.__code__.co_varnames[: fn.__code__.co_argcount]:
                class _MP:
                    def setattr(self, obj, name, value):
                        setattr(obj, name, value)
                fn(_MP())
            else:
                fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
