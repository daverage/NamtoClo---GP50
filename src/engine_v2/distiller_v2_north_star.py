from __future__ import annotations

import hashlib
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from distiller_v2_data import Pair
from distiller_v2_dsp import B_TAPS, controls_to_a, pk_render, pre_fir
from distiller_v2_fit import Candidate, Metrics, _map, _score_predictions, _selection_score, _workspace


# Canonical first-prototype identification set from ENGINE_V2_RESEARCH_NORTH_STAR.md.
# These probes are fitted together with a deliberately small real-DI set.
NORTH_STAR_PROBES = (
    ("quiet_log_sweep", "04_log_sweep_-36dBFS.wav"),
    ("multisine_level_ladder", "07_multisine_level_ladder.wav"),
    ("1khz_level_ladder", "09_1kHz_level_ladder.wav"),
    ("frequency_level_matrix", "10_frequency_level_matrix.wav"),
    ("two_tone_imd", "11_two_tone_IMD_matrix.wav"),
    ("transient_bursts", "13_tone_burst_transients.wav"),
)

# The source corpus deliberately contains bass and sample-library artifacts for
# broader research, but the canonical North Star "real" slots are dry guitar
# playing. Bass is a useful separate domain, not a substitute for one of the
# six guitar examples. Likewise release/noise/silence artifacts are instrument
# assets rather than performances/excitation we want counted as real playing.
_NORTH_STAR_BASS_DATASETS = frozenset({"growlybass", "black-blue-basses"})
_NORTH_STAR_NONPLAYING_COMPONENTS = frozenset(
    {"noise", "noises", "release", "releases", "rel", "silence"}
)
_NORTH_STAR_NONPLAYING_TOKENS = frozenset({"noise", "release", "silence"})


@dataclass(frozen=True)
class NorthStarMaterial:
    fit: list[Pair]
    selection: list[Pair]
    benchmark: list[Pair]
    probes: list[Pair]
    fit_real: list[Pair]
    selection_real: list[Pair]
    benchmark_real: list[Pair]

    def manifest(self) -> dict:
        def row(p: Pair) -> dict:
            return {
                "task_id": p.task_id,
                "dataset": p.dataset,
                "role": p.role,
                "synthetic": p.synthetic,
                "source": p.source_path,
                "level_offset_db": p.level_offset_db,
                "duration_s": p.duration_s,
            }

        return {
            "fit": [row(p) for p in self.fit],
            "selection": [row(p) for p in self.selection],
            "benchmark": [row(p) for p in self.benchmark],
            "canonical_probe_basenames": [name for _, name in NORTH_STAR_PROBES],
            "synthetic_role_override": (
                "All six canonical identification probes participate in FIT in North Star mode "
                "regardless of their historical teacher-dataset role tag. Real selection and "
                "benchmark material never moves into FIT."
            ),
            "real_material_policy": (
                "Canonical real slots use dry guitar material only: bass datasets and obvious "
                "release/noise/silence sample artifacts are excluded. The broader research "
                "corpus remains unchanged."
            ),
        }


def _hash_key(seed: int, *parts: object) -> str:
    return hashlib.sha256("|".join([str(seed), *(str(x) for x in parts)]).encode()).hexdigest()


def _real_performance_key(p: Pair):
    source = p.source_sha256 or p.source_path or p.input_id
    return (
        p.dataset,
        source,
        round(float(p.start_s), 6),
        round(float(p.duration_s), 6),
        p.role,
    )


def _is_canonical_real_guitar(p: Pair) -> bool:
    """True for real guitar excitation suitable for canonical real slots.

    This is intentionally a corpus-definition filter, not a model-dependent
    quality heuristic. It removes domains/assets that contradict the North Star
    definition of the real corpus while leaving the wider teacher dataset intact.
    """
    if p.synthetic:
        return False
    dataset = str(p.dataset).strip().lower()
    if dataset in _NORTH_STAR_BASS_DATASETS or "bass" in dataset:
        return False

    path = Path(p.source_path or p.input_path)
    components = {part.lower() for part in path.parts}
    if components & _NORTH_STAR_NONPLAYING_COMPONENTS:
        return False

    stem = path.stem.lower()
    for sep in ("-", ".", " "):
        stem = stem.replace(sep, "_")
    tokens = {tok for tok in stem.split("_") if tok}
    if tokens & _NORTH_STAR_NONPLAYING_TOKENS:
        return False
    return True


def _base_real_pairs(pairs: list[Pair], role: str) -> list[Pair]:
    """One deterministic near-0-dB task per underlying real guitar performance.

    The teacher corpus may contain -12/-6/0/+6 digital-drive variants of the
    same source segment. The canonical first prototype uses one base rendition
    per real performance; matched level variants remain available as held-out
    diagnostics rather than silently multiplying the real fitting set.

    The broader corpus also contains bass and sample-library release/noise
    artifacts. Those stay available to other research, but they are not allowed
    to consume one of the canonical real-guitar slots.
    """
    grouped: dict[tuple, list[Pair]] = {}
    for p in pairs:
        if p.role != role or not _is_canonical_real_guitar(p):
            continue
        grouped.setdefault(_real_performance_key(p), []).append(p)

    out = []
    for _, group in grouped.items():
        group = sorted(
            group,
            key=lambda p: (
                abs(float(p.level_offset_db)),
                float(p.level_offset_db) != 0.0,
                p.task_id,
            ),
        )
        out.append(group[0])
    return out


def _diverse_pick(pairs: list[Pair], count: int, seed: int) -> list[Pair]:
    """Deterministic round-robin across datasets before taking repeats."""
    if count <= 0:
        return []
    by_dataset: dict[str, list[Pair]] = {}
    for p in pairs:
        by_dataset.setdefault(p.dataset, []).append(p)
    for dataset, pool in by_dataset.items():
        pool.sort(key=lambda p: _hash_key(seed, dataset, p.task_id))
    datasets = sorted(by_dataset, key=lambda d: _hash_key(seed ^ 0xD157, d))

    chosen: list[Pair] = []
    depth = 0
    while len(chosen) < count:
        moved = False
        for dataset in datasets:
            pool = by_dataset[dataset]
            if depth < len(pool):
                chosen.append(pool[depth])
                moved = True
                if len(chosen) >= count:
                    break
        if not moved:
            break
        depth += 1
    return chosen


def select_north_star_material(
    pairs: list[Pair],
    fit_real: int = 6,
    selection_real: int = 4,
    benchmark_real: int = 3,
    seed: int = 260910,
) -> NorthStarMaterial:
    """Select the canonical compact teacher/student experiment material."""
    by_basename: dict[str, list[Pair]] = {}
    for p in pairs:
        if p.synthetic:
            by_basename.setdefault(Path(p.source_path).name, []).append(p)

    probes: list[Pair] = []
    missing: list[str] = []
    for _, basename in NORTH_STAR_PROBES:
        hits = by_basename.get(basename, [])
        if not hits:
            missing.append(basename)
            continue
        probes.append(sorted(hits, key=lambda p: p.task_id)[0])
    if missing:
        raise RuntimeError(
            "North Star teacher material is missing canonical synthetic probes: "
            + ", ".join(missing)
        )

    fit_pool = _base_real_pairs(pairs, "fit")
    selection_pool = _base_real_pairs(pairs, "selection")
    benchmark_pool = _base_real_pairs(pairs, "benchmark")

    rf = _diverse_pick(fit_pool, fit_real, seed ^ 0xF17)
    rs = _diverse_pick(selection_pool, selection_real, seed ^ 0x5E1)
    rb = _diverse_pick(benchmark_pool, benchmark_real, seed ^ 0xB3A)

    shortages = []
    if len(rf) < fit_real:
        shortages.append(f"fit real guitar {len(rf)}/{fit_real}")
    if len(rs) < selection_real:
        shortages.append(f"selection real guitar {len(rs)}/{selection_real}")
    if len(rb) < benchmark_real:
        shortages.append(f"benchmark real guitar {len(rb)}/{benchmark_real}")
    if shortages:
        raise RuntimeError("Insufficient North Star real-DI material: " + ", ".join(shortages))

    return NorthStarMaterial(
        fit=[*probes, *rf],
        selection=rs,
        benchmark=rb,
        probes=probes,
        fit_real=rf,
        selection_real=rs,
        benchmark_real=rb,
    )


def _solve_b_normalized(prebs: list[np.ndarray], ws):
    """Solve one shared B512 with one normalized contribution per teacher example.

    The legacy research solve minimizes raw squared error, so a loud probe can
    dominate a quiet probe. North Star mode weights each example by inverse
    target energy. This makes the inner linear solve match the outer
    per-example normalized waveform objective much more closely.
    """
    if not prebs or len(prebs) != len(ws.targets):
        raise ValueError("empty/mismatched North Star B solve")
    energies = np.array(
        [
            max(float(np.dot(t[:n], t[:n])), 1e-20)
            for t, n in zip(ws.targets, ws.lengths)
        ],
        dtype=np.float64,
    )
    weights = 1.0 / energies
    weights /= float(np.sum(weights))

    num = np.zeros(ws.nfft // 2 + 1, np.complex128)
    den = np.zeros(ws.nfft // 2 + 1, dtype=np.float64)
    pffts = []
    for p, T, w in zip(prebs, ws.target_ffts, weights):
        P = np.fft.rfft(p, ws.nfft)
        pffts.append(P)
        num += w * np.conj(P) * T
        den += w * np.abs(P) ** 2

    eps = max(1e-3 * float(np.mean(den)), 1e-20)
    H = num / (den + eps)
    b = np.fft.irfft(H, ws.nfft)[:B_TAPS].astype(np.float64)
    B = np.fft.rfft(b, ws.nfft)
    preds = [
        np.fft.irfft(P * B, ws.nfft)[:n]
        for P, n in zip(pffts, ws.lengths)
    ]
    return b, _score_predictions(preds, ws.targets)


def _evaluate_fit_ns(ctrl: np.ndarray, pk: np.ndarray, ws) -> Candidate:
    a = controls_to_a(ctrl)
    prebs = _map(lambda bqx: pk_render(pre_fir(bqx, a), pk), ws.bqx)
    b, metrics = _solve_b_normalized(prebs, ws)
    inf = Metrics(float("inf"), float("inf"), float("inf"), float("inf"), float("inf"))
    return Candidate(ctrl.copy(), pk.copy(), b, metrics, inf)


def _evaluate_pk_ns(
    aouts: list[np.ndarray],
    ctrl: np.ndarray,
    pk: np.ndarray,
    ws,
) -> Candidate:
    prebs = _map(lambda aout: pk_render(aout, pk), aouts)
    b, metrics = _solve_b_normalized(prebs, ws)
    inf = Metrics(float("inf"), float("inf"), float("inf"), float("inf"), float("inf"))
    return Candidate(ctrl.copy(), pk.copy(), b, metrics, inf)


def _yield_cpu(pause_ms: float) -> None:
    if pause_ms > 0:
        time.sleep(pause_ms / 1000.0)


def distill_north_star(
    fit,
    selection,
    controls: int = 24,
    rounds: int = 3,
    pk_passes: int = 3,
    status=print,
    pause_ms: float = 0.0,
) -> Candidate:
    """Canonical clean-sheet teacher/student fit.

    Objective:
      * FIT candidate moves: mean aligned ESR across the canonical FIT examples.
      * ROUND gate: mean aligned ESR on disjoint real SELECTION examples.
      * B512: one target-energy-normalized analytic solve per A/P-K candidate.

    No level-response, distortion-excess, named-amp, or transfer-identification
    penalty participates in coefficient selection.
    """
    if not fit:
        raise ValueError("North Star FIT material is empty")
    if not selection:
        raise ValueError("North Star SELECTION material is empty")

    fit_ws = _workspace(fit)
    selection_ws = _workspace(selection)
    ctrl = np.zeros(controls, dtype=np.float64)
    pk = np.array([0.1, 0.1, 1.0, 1.0], dtype=np.float64)

    best = _evaluate_fit_ns(ctrl, pk, fit_ws)
    best.selection = _selection_score(selection, best, selection_ws)
    _yield_cpu(pause_ms)
    status(
        f"seed fit-ESR={best.fit.esr:.6g} selection-ESR={best.selection.esr:.6g} "
        f"(diagnostic composite {best.fit.composite:.6g}/{best.selection.composite:.6g})"
    )

    max_pk_passes = max(1, int(pk_passes))
    for r, (astep, pstep) in enumerate(
        zip((3.0, 1.5, 0.75, 0.35), (0.45, 0.28, 0.16, 0.08)),
        1,
    ):
        if r > rounds:
            break
        started = time.monotonic()
        before = best
        work = best
        evals = 0

        # A and P/K are cooperative upstream degrees of freedom. A is not
        # treated as "EQ"; every accepted A move is immediately completed by
        # a fresh analytic B solve.
        for i in range(controls):
            for direction in (astep, -astep):
                c = work.controls_db.copy()
                c[i] = np.clip(c[i] + direction, -18.0, 18.0)
                q = _evaluate_fit_ns(c, work.pk, fit_ws)
                evals += 1
                _yield_cpu(pause_ms)
                if q.fit.esr < work.fit.esr:
                    work = q

        a_fixed = controls_to_a(work.controls_db)
        aouts = _map(lambda bqx: pre_fir(bqx, a_fixed), fit_ws.bqx)

        passes_used = 0
        for _ in range(max_pk_passes):
            pass_start = work.fit.esr
            moved = False
            for i in range(4):
                for sign in (1.0, -1.0):
                    p = work.pk.copy()
                    p[i] *= math.exp(sign * pstep)
                    p[:2] = np.clip(p[:2], 0.01, 2.0)
                    p[2:] = np.clip(p[2:], 0.05, 80.0)
                    q = _evaluate_pk_ns(aouts, work.controls_db, p, fit_ws)
                    evals += 1
                    _yield_cpu(pause_ms)
                    if q.fit.esr < work.fit.esr:
                        work = q
                        moved = True
            passes_used += 1
            if not moved or pass_start - work.fit.esr < 1e-8:
                break

        work.selection = _selection_score(selection, work, selection_ws)
        _yield_cpu(pause_ms)
        elapsed = time.monotonic() - started
        pks = " ".join(f"{v:.3g}" for v in work.pk)

        if work.selection.esr < before.selection.esr:
            best = work
            status(
                f"round {r} ACCEPT selection-ESR "
                f"{before.selection.esr:.6g}->{best.selection.esr:.6g} "
                f"fit-ESR {before.fit.esr:.6g}->{best.fit.esr:.6g} "
                f"pk=[{pks}] pk-passes={passes_used} "
                f"({evals} candidates, {elapsed:.1f}s)"
            )
        else:
            status(
                f"round {r} REJECT selection-ESR "
                f"{before.selection.esr:.6g}->{work.selection.esr:.6g} "
                f"fit-ESR {before.fit.esr:.6g}->{work.fit.esr:.6g} "
                f"pk=[{pks}] pk-passes={passes_used} "
                f"({evals} candidates, {elapsed:.1f}s)"
            )
    return best
