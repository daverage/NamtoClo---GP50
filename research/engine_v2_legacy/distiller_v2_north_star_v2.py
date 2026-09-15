from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from distiller_v2_dsp import B_TAPS, SR, controls_to_a, pk_render, pre_fir
from distiller_v2_fit import Candidate, Metrics, _map, _selection_score, _workspace


@dataclass(frozen=True)
class EvidenceWindow:
    group: str
    label: str
    start: int
    end: int
    weight: float

    @property
    def start_s(self) -> float:
        return self.start / float(SR)

    @property
    def duration_s(self) -> float:
        return (self.end - self.start) / float(SR)


@dataclass(frozen=True)
class FitEvidence:
    """Virtual fit examples built from the unchanged v1 source material."""

    windows: tuple[tuple[EvidenceWindow, ...], ...]
    virtual_examples: int
    source_units: int

    def manifest(self, audio) -> dict:
        rows = []
        group_weights: dict[str, float] = {}
        for (_, _, pair), windows in zip(audio, self.windows):
            for window in windows:
                group_weights[window.group] = (
                    group_weights.get(window.group, 0.0) + window.weight
                )
                rows.append(
                    {
                        "task_id": getattr(pair, "task_id", ""),
                        "source": Path(
                            getattr(pair, "source_path", "")
                            or getattr(pair, "input_path", "")
                        ).name,
                        "synthetic": bool(getattr(pair, "synthetic", False)),
                        "group": window.group,
                        "label": window.label,
                        "start_s": window.start_s,
                        "duration_s": window.duration_s,
                        "weight": window.weight,
                    }
                )
        return {
            "mode": "north-star-v2-segmented-level-balanced",
            "virtual_examples": self.virtual_examples,
            "source_units": self.source_units,
            "weighting": (
                "The six synthetic probe families keep the same total influence as six "
                "real guitar FIT clips. Operating-point windows inside each probe split "
                "that probe-family weight equally, so quiet and loud sections are both "
                "visible without letting probes with many cells dominate real guitar."
            ),
            "group_weights": group_weights,
            "windows": rows,
        }


@dataclass
class _PreparedEvidence:
    scales: list[np.ndarray]
    weighted_target_ffts: list[np.ndarray]


def _window_specs_for_probe(basename: str):
    """Active probe regions from build_namtoclo_research_corpus_hf.py.

    Each window includes a short piece of the following silence so decay/tail
    behaviour remains part of the teacher evidence. The remaining gap is still
    much longer than the 512-tap B FIR.
    """
    if basename == "04_log_sweep_-36dBFS.wav":
        return [("sweep_-36dBFS", 0.50, 8.10)]

    if basename == "07_multisine_level_ladder.wav":
        levels = (-36, -30, -24, -18, -12, -9, -6, -3)
        return [
            (f"multisine_{level:+d}dBFS", 0.50 + i * 1.25, 1.05)
            for i, level in enumerate(levels)
        ]

    if basename == "09_1kHz_level_ladder.wav":
        levels = (-42, -36, -30, -24, -18, -12, -9, -6, -3, -1)
        return [
            (f"1kHz_{level:+d}dBFS", 0.50 + i * 0.80, 0.70)
            for i, level in enumerate(levels)
        ]

    if basename == "10_frequency_level_matrix.wav":
        freqs = (100, 400, 1000, 2500, 5000)
        levels = (-30, -18, -9, -3)
        out = []
        i = 0
        for freq in freqs:
            for level in levels:
                out.append((f"{freq}Hz_{level:+d}dBFS", 0.50 + i * 0.55, 0.50))
                i += 1
        return out

    if basename == "11_two_tone_IMD_matrix.wav":
        tone_pairs = ((100, 1000), (250, 2000), (500, 4000), (1000, 5000))
        levels = (-24, -9, -3)
        out = []
        i = 0
        for f1, f2 in tone_pairs:
            for level in levels:
                out.append(
                    (f"{f1}+{f2}Hz_{level:+d}dBFS", 0.50 + i * 1.10, 0.95)
                )
                i += 1
        return out

    if basename == "13_tone_burst_transients.wav":
        freqs = (80, 160, 500, 1500, 4000)
        levels = (-18, -6, -3)
        out = []
        i = 0
        for freq in freqs:
            for level in levels:
                out.append(
                    (f"burst_{freq}Hz_{level:+d}dBFS", 0.50 + i * 0.40, 0.23)
                )
                i += 1
        return out

    raise RuntimeError(f"No North Star v2 segmentation layout for {basename}")


def build_fit_evidence(fit_audio) -> FitEvidence:
    """Create level-balanced virtual evidence without rerendering the NAM.

    The student still renders each original probe as one continuous signal, so
    state/timing are preserved. Only scoring and the analytic B solve see the
    generated probe's logical operating-point windows.
    """
    if not fit_audio:
        raise ValueError("North Star v2 FIT audio is empty")

    source_weight = 1.0 / float(len(fit_audio))
    rows: list[tuple[EvidenceWindow, ...]] = []
    virtual_examples = 0

    for x, target, pair in fit_audio:
        n = min(len(x), len(target))
        if n <= 0:
            raise RuntimeError(
                f"Empty North Star v2 FIT example: {getattr(pair, 'task_id', '')}"
            )

        if bool(getattr(pair, "synthetic", False)):
            basename = Path(
                getattr(pair, "source_path", "") or getattr(pair, "input_path", "")
            ).name
            specs = _window_specs_for_probe(basename)
            local_weight = source_weight / float(len(specs))
            windows = []
            for label, start_s, duration_s in specs:
                start = int(round(start_s * SR))
                end = int(round((start_s + duration_s) * SR))
                if start < 0 or end <= start or end > n + 2:
                    raise RuntimeError(
                        f"North Star v2 probe layout exceeds teacher audio for {basename}: "
                        f"{label} needs {end / SR:.3f}s, audio is {n / SR:.3f}s"
                    )
                windows.append(
                    EvidenceWindow(
                        group=f"probe:{basename}",
                        label=label,
                        start=start,
                        end=min(end, n),
                        weight=local_weight,
                    )
                )
        else:
            windows = [
                EvidenceWindow(
                    group=f"real:{getattr(pair, 'task_id', '')}",
                    label="whole_real_clip",
                    start=0,
                    end=n,
                    weight=source_weight,
                )
            ]

        virtual_examples += len(windows)
        rows.append(tuple(windows))

    return FitEvidence(
        windows=tuple(rows),
        virtual_examples=virtual_examples,
        source_units=len(fit_audio),
    )


def _nextpow2(n: int) -> int:
    return 1 << (max(1, n) - 1).bit_length()


def _best_lag(pred, target, maxlag: int = 256) -> int:
    """Return the same alignment lag convention used by the v1 ESR scorer."""
    n = min(len(pred), len(target))
    if n < 128:
        return 0

    pred = np.asarray(pred[:n], dtype=np.float64)
    target = np.asarray(target[:n], dtype=np.float64)
    dec = max(1, n // 100000)
    ps = pred[::dec]
    ts = target[::dec]
    ml = max(1, maxlag // dec)
    nf = _nextpow2(len(ps) + len(ts) - 1)
    corr = np.fft.irfft(np.fft.rfft(ps, nf) * np.fft.rfft(ts[::-1], nf), nf)
    centre = len(ts) - 1
    lo = max(0, centre - ml)
    hi = min(len(ps) + len(ts) - 1, centre + ml + 1)
    return (lo + int(np.argmax(corr[lo:hi])) - centre) * dec


def evidence_esr(preds, targets, evidence: FitEvidence) -> float:
    """Weighted ESR across virtual windows, with one alignment search per source."""
    total = 0.0
    used_weight = 0.0

    for pred, target, windows in zip(preds, targets, evidence.windows):
        n = min(len(pred), len(target))
        if n < 2:
            continue

        pred = np.asarray(pred[:n], dtype=np.float64)
        target = np.asarray(target[:n], dtype=np.float64)
        lag = _best_lag(pred, target)

        if lag >= 0:
            p_aligned = pred[lag:]
            t_aligned = target[: n - lag]
            target_origin = 0
        else:
            p_aligned = pred[: n + lag]
            t_aligned = target[-lag:]
            target_origin = -lag

        aligned_n = min(len(p_aligned), len(t_aligned))
        target_end = target_origin + aligned_n

        for window in windows:
            start = max(window.start, target_origin)
            end = min(window.end, target_end)
            if end <= start:
                continue
            a = start - target_origin
            b = end - target_origin
            pp = p_aligned[a:b]
            tt = t_aligned[a:b]
            den = float(np.dot(tt, tt))
            if den <= 1e-20:
                continue
            esr = float(np.dot(pp - tt, pp - tt) / den)
            total += window.weight * esr
            used_weight += window.weight

    return total / used_weight if used_weight > 0.0 else float("inf")


def _evidence_metrics(preds, targets, evidence: FitEvidence) -> Metrics:
    esr = evidence_esr(preds, targets, evidence)
    # v2 intentionally selects only from direct window-balanced waveform error.
    return Metrics(esr, esr, 0.0, 0.0, 0.0)


def _prepare_evidence(ws, evidence: FitEvidence) -> _PreparedEvidence:
    if len(evidence.windows) != len(ws.targets):
        raise ValueError("North Star v2 evidence/source count mismatch")

    scales: list[np.ndarray] = []
    weighted_target_ffts: list[np.ndarray] = []

    for target, n, windows in zip(ws.targets, ws.lengths, evidence.windows):
        target = np.asarray(target[:n], dtype=np.float64)
        scale = np.zeros(n, dtype=np.float64)

        for window in windows:
            end = min(window.end, n)
            if end <= window.start:
                continue
            segment = target[window.start:end]
            energy = max(float(np.dot(segment, segment)), 1e-20)
            scale[window.start:end] = math.sqrt(window.weight / energy)

        scales.append(scale)
        weighted_target_ffts.append(np.fft.rfft(target * scale, ws.nfft))

    return _PreparedEvidence(
        scales=scales,
        weighted_target_ffts=weighted_target_ffts,
    )


def solve_b_balanced(prebs, ws, evidence: FitEvidence, prepared=None):
    """One shared analytic B512 solve using the same level-balanced evidence.

    Weighting is applied only after A/P-K/POST to the linear least-squares
    evidence. It therefore cannot increase pre-P/K drive or create a hidden
    level-dependent nonlinear rule.
    """
    if not prebs or len(prebs) != len(ws.targets):
        raise ValueError("empty/mismatched North Star v2 B solve")
    if prepared is None:
        prepared = _prepare_evidence(ws, evidence)

    num = np.zeros(ws.nfft // 2 + 1, np.complex128)
    den = np.zeros(ws.nfft // 2 + 1, dtype=np.float64)
    unweighted_pffts = []

    for pre_b, n, scale, target_fft in zip(
        prebs, ws.lengths, prepared.scales, prepared.weighted_target_ffts
    ):
        pre_b = np.asarray(pre_b[:n], dtype=np.float64)
        weighted_p = np.fft.rfft(pre_b * scale, ws.nfft)
        num += np.conj(weighted_p) * target_fft
        den += np.abs(weighted_p) ** 2
        unweighted_pffts.append(np.fft.rfft(pre_b, ws.nfft))

    eps = max(1e-3 * float(np.mean(den)), 1e-20)
    response = num / (den + eps)
    b = np.fft.irfft(response, ws.nfft)[:B_TAPS].astype(np.float64)
    B = np.fft.rfft(b, ws.nfft)
    preds = [
        np.fft.irfft(P * B, ws.nfft)[:n]
        for P, n in zip(unweighted_pffts, ws.lengths)
    ]
    return b, _evidence_metrics(preds, ws.targets, evidence)


def _evaluate_fit(ctrl, pk, ws, evidence, prepared) -> Candidate:
    a = controls_to_a(ctrl)
    prebs = _map(lambda bqx: pk_render(pre_fir(bqx, a), pk), ws.bqx)
    b, metrics = solve_b_balanced(prebs, ws, evidence, prepared)
    inf = Metrics(float("inf"), float("inf"), float("inf"), float("inf"), float("inf"))
    return Candidate(ctrl.copy(), pk.copy(), b, metrics, inf)


def _evaluate_pk(aouts, ctrl, pk, ws, evidence, prepared) -> Candidate:
    prebs = _map(lambda aout: pk_render(aout, pk), aouts)
    b, metrics = solve_b_balanced(prebs, ws, evidence, prepared)
    inf = Metrics(float("inf"), float("inf"), float("inf"), float("inf"), float("inf"))
    return Candidate(ctrl.copy(), pk.copy(), b, metrics, inf)


def _yield_cpu(pause_ms: float) -> None:
    if pause_ms > 0:
        time.sleep(pause_ms / 1000.0)


def distill_north_star_v2(
    fit,
    selection,
    controls: int = 24,
    rounds: int = 3,
    pk_passes: int = 3,
    status=print,
    pause_ms: float = 0.0,
    fit_evidence: FitEvidence | None = None,
) -> Candidate:
    """North Star v2: same architecture/objective philosophy, better evidence use."""
    if not fit:
        raise ValueError("North Star v2 FIT material is empty")
    if not selection:
        raise ValueError("North Star v2 SELECTION material is empty")

    if fit_evidence is None:
        fit_evidence = build_fit_evidence(fit)

    fit_ws = _workspace(fit)
    prepared = _prepare_evidence(fit_ws, fit_evidence)
    selection_ws = _workspace(selection)

    ctrl = np.zeros(controls, dtype=np.float64)
    pk = np.array([0.1, 0.1, 1.0, 1.0], dtype=np.float64)

    best = _evaluate_fit(ctrl, pk, fit_ws, fit_evidence, prepared)
    best.selection = _selection_score(selection, best, selection_ws)
    _yield_cpu(pause_ms)
    status(
        f"seed fit-evidence-ESR={best.fit.esr:.6g} "
        f"selection-ESR={best.selection.esr:.6g}"
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

        for i in range(controls):
            for direction in (astep, -astep):
                c = work.controls_db.copy()
                c[i] = np.clip(c[i] + direction, -18.0, 18.0)
                q = _evaluate_fit(c, work.pk, fit_ws, fit_evidence, prepared)
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
                    q = _evaluate_pk(
                        aouts, work.controls_db, p, fit_ws, fit_evidence, prepared
                    )
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
                f"fit-evidence-ESR {before.fit.esr:.6g}->{best.fit.esr:.6g} "
                f"pk=[{pks}] pk-passes={passes_used} "
                f"({evals} candidates, {elapsed:.1f}s)"
            )
        else:
            status(
                f"round {r} REJECT selection-ESR "
                f"{before.selection.esr:.6g}->{work.selection.esr:.6g} "
                f"fit-evidence-ESR {before.fit.esr:.6g}->{work.fit.esr:.6g} "
                f"pk=[{pks}] pk-passes={passes_used} "
                f"({evals} candidates, {elapsed:.1f}s)"
            )

    return best
