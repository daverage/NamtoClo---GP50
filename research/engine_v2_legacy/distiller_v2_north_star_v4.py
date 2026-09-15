from __future__ import annotations

import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from distiller_v2_dsp import controls_to_a, pre_fir
from distiller_v2_fit import Candidate, _map, _workspace
from distiller_v2_north_star_v2 import (
    EvidenceWindow,
    _evaluate_fit,
    _evaluate_pk,
    _prepare_evidence,
)
from distiller_v2_north_star_v3 import (
    DEFAULT_MAX_ROUNDS,
    DEFAULT_MULTISTARTS,
    MAX_MULTISTARTS,
    _pk_seed_bank,
    _search_steps,
)


@dataclass(frozen=True)
class StimulusFitEvidence:
    """Whole-waveform evidence from one canonical stimulus at several levels.

    Every level variant has equal total weight and is target-energy normalized by
    the existing analytic B solve. No guitar material participates in fitting
    or in checkpoint/start selection.
    """

    windows: tuple[tuple[EvidenceWindow, ...], ...]
    virtual_examples: int
    source_units: int
    stimulus_sha256: str
    levels_db: tuple[float, ...]

    def manifest(self, audio) -> dict:
        rows = []
        group_weights: dict[str, float] = {}
        for (_, _, pair), windows in zip(audio, self.windows):
            for window in windows:
                group_weights[window.group] = group_weights.get(window.group, 0.0) + window.weight
                rows.append(
                    {
                        "task_id": getattr(pair, "task_id", ""),
                        "source": Path(
                            getattr(pair, "source_path", "")
                            or getattr(pair, "input_path", "")
                        ).name,
                        "level_offset_db": float(getattr(pair, "level_offset_db", 0.0)),
                        "group": window.group,
                        "label": window.label,
                        "start_s": window.start_s,
                        "duration_s": window.duration_s,
                        "weight": window.weight,
                    }
                )
        return {
            "mode": "north-star-v4-canonical-stimulus-multilevel-whole-waveform",
            "stimulus_sha256": self.stimulus_sha256,
            "levels_db": list(self.levels_db),
            "virtual_examples": self.virtual_examples,
            "source_units": self.source_units,
            "weighting": (
                "One complete canonical stimulus render per input level; every level "
                "has equal total influence and target-energy normalization. No real "
                "guitar participates in coefficient fitting or candidate selection."
            ),
            "group_weights": group_weights,
            "windows": rows,
        }


def build_stimulus_fit_evidence(fit_audio, stimulus_sha256: str = "") -> StimulusFitEvidence:
    if not fit_audio:
        raise ValueError("North Star v4 stimulus FIT audio is empty")

    source_weight = 1.0 / float(len(fit_audio))
    rows: list[tuple[EvidenceWindow, ...]] = []
    levels: list[float] = []

    for x, target, pair in fit_audio:
        n = min(len(x), len(target))
        if n <= 0:
            raise RuntimeError(
                f"Empty North Star v4 stimulus example: {getattr(pair, 'task_id', '')}"
            )
        level = float(getattr(pair, "level_offset_db", 0.0))
        levels.append(level)
        rows.append(
            (
                EvidenceWindow(
                    group=f"stimulus_level:{level:+.3f}dB",
                    label=f"whole_stimulus_{level:+.3f}dB",
                    start=0,
                    end=n,
                    weight=source_weight,
                ),
            )
        )

    return StimulusFitEvidence(
        windows=tuple(rows),
        virtual_examples=len(rows),
        source_units=len(rows),
        stimulus_sha256=str(stimulus_sha256),
        levels_db=tuple(levels),
    )


@dataclass
class _FitCheckpoint:
    start_index: int
    seed_label: str
    round_index: int
    a_step_db: float
    pk_log_step: float
    candidate: Candidate


def _checkpoint_key(checkpoint: _FitCheckpoint):
    return (
        float(checkpoint.candidate.fit.esr),
        int(checkpoint.start_index),
        int(checkpoint.round_index),
    )


def _select_best_fit_checkpoint(checkpoints):
    checkpoints = tuple(checkpoints)
    if not checkpoints:
        raise ValueError("no v4 fit checkpoints to select from")
    return min(checkpoints, key=_checkpoint_key)


def _checkpoint_row(checkpoint: _FitCheckpoint) -> dict:
    c = checkpoint.candidate
    return {
        "round": checkpoint.round_index,
        "a_step_db": checkpoint.a_step_db,
        "pk_log_step": checkpoint.pk_log_step,
        "fit_evidence_esr": float(c.fit.esr),
        "pk": [float(v) for v in c.pk],
    }


def distill_north_star_v4(
    fit,
    controls: int = 24,
    rounds: int = DEFAULT_MAX_ROUNDS,
    pk_passes: int = 3,
    multistarts: int = DEFAULT_MULTISTARTS,
    status=print,
    pause_ms: float = 0.0,
    fit_evidence: StimulusFitEvidence | None = None,
    search_report: dict | None = None,
) -> Candidate:
    """Stimulus-only North Star fitter using v3's robust search mechanics.

    The DSP architecture and analytic B variable projection are unchanged.
    Unlike v3, real guitar is never passed into this function: search movement,
    checkpoint choice and multistart choice all depend only on the NAM response
    to the canonical stimulus at the configured input levels.
    """
    if not fit:
        raise ValueError("North Star v4 FIT material is empty")
    if controls < 1:
        raise ValueError("controls must be >= 1")
    if pk_passes < 1:
        raise ValueError("pk_passes must be >= 1")
    if not 1 <= int(multistarts) <= MAX_MULTISTARTS:
        raise ValueError(f"multistarts must be in [1, {MAX_MULTISTARTS}]")

    if fit_evidence is None:
        fit_evidence = build_stimulus_fit_evidence(fit)

    fit_ws = _workspace(fit)
    prepared = _prepare_evidence(fit_ws, fit_evidence)
    steps = _search_steps(rounds)
    seeds = _pk_seed_bank(multistarts)
    max_pk_passes = max(1, int(pk_passes))

    all_checkpoints: list[_FitCheckpoint] = []
    trajectories: list[dict] = []

    for start_index, (seed_label, seed_pk) in enumerate(seeds):
        ctrl = np.zeros(controls, dtype=np.float64)
        work = _evaluate_fit(ctrl, seed_pk, fit_ws, fit_evidence, prepared)
        if pause_ms > 0:
            time.sleep(pause_ms / 1000.0)

        seed_checkpoint = _FitCheckpoint(
            start_index=start_index,
            seed_label=seed_label,
            round_index=0,
            a_step_db=0.0,
            pk_log_step=0.0,
            candidate=work,
        )
        start_checkpoints = [seed_checkpoint]
        all_checkpoints.append(seed_checkpoint)
        status(
            f"start {start_index + 1}/{len(seeds)} {seed_label} "
            f"seed fit-evidence-ESR={work.fit.esr:.6g} "
            f"pk=[{' '.join(f'{v:.3g}' for v in work.pk)}]"
        )

        for round_index, (a_step, p_step) in enumerate(steps, 1):
            started = time.monotonic()
            before_fit = float(work.fit.esr)
            evals = 0
            a_moves = 0

            for i in range(controls):
                for direction in (a_step, -a_step):
                    c = work.controls_db.copy()
                    c[i] = np.clip(c[i] + direction, -18.0, 18.0)
                    q = _evaluate_fit(c, work.pk, fit_ws, fit_evidence, prepared)
                    evals += 1
                    if pause_ms > 0:
                        time.sleep(pause_ms / 1000.0)
                    if q.fit.esr < work.fit.esr:
                        work = q
                        a_moves += 1

            a_fixed = controls_to_a(work.controls_db)
            aouts = _map(lambda bqx: pre_fir(bqx, a_fixed), fit_ws.bqx)

            passes_used = 0
            pk_moves = 0
            for _ in range(max_pk_passes):
                pass_start = float(work.fit.esr)
                moved = False
                for i in range(4):
                    for sign in (1.0, -1.0):
                        p = work.pk.copy()
                        p[i] *= math.exp(sign * p_step)
                        p[:2] = np.clip(p[:2], 0.01, 2.0)
                        p[2:] = np.clip(p[2:], 0.05, 80.0)
                        q = _evaluate_pk(
                            aouts, work.controls_db, p, fit_ws, fit_evidence, prepared
                        )
                        evals += 1
                        if pause_ms > 0:
                            time.sleep(pause_ms / 1000.0)
                        if q.fit.esr < work.fit.esr:
                            work = q
                            moved = True
                            pk_moves += 1
                passes_used += 1
                if not moved or pass_start - work.fit.esr < 1e-8:
                    break

            checkpoint = _FitCheckpoint(
                start_index=start_index,
                seed_label=seed_label,
                round_index=round_index,
                a_step_db=a_step,
                pk_log_step=p_step,
                candidate=work,
            )
            start_checkpoints.append(checkpoint)
            all_checkpoints.append(checkpoint)

            elapsed = time.monotonic() - started
            status(
                f"start {start_index + 1}/{len(seeds)} round {round_index} "
                f"CHECKPOINT fit-evidence-ESR {before_fit:.6g}->{work.fit.esr:.6g} "
                f"A-step={a_step:.4g}dB P/K-step={p_step:.4g} "
                f"A-moves={a_moves} PK-moves={pk_moves} pk-passes={passes_used} "
                f"pk=[{' '.join(f'{v:.3g}' for v in work.pk)}] "
                f"({evals} candidates, {elapsed:.1f}s)"
            )

        start_best = _select_best_fit_checkpoint(start_checkpoints)
        status(
            f"start {start_index + 1}/{len(seeds)} BEST checkpoint="
            f"{start_best.round_index} fit-evidence-ESR={start_best.candidate.fit.esr:.6g}"
        )
        trajectories.append(
            {
                "start_index": start_index,
                "seed_label": seed_label,
                "seed_pk": [float(v) for v in seed_pk],
                "checkpoints": [_checkpoint_row(cp) for cp in start_checkpoints],
                "best_checkpoint": start_best.round_index,
                "best_fit_evidence_esr": float(start_best.candidate.fit.esr),
            }
        )

    chosen = _select_best_fit_checkpoint(all_checkpoints)
    best = chosen.candidate
    status(
        f"SELECT start {chosen.start_index + 1}/{len(seeds)} "
        f"{chosen.seed_label} checkpoint={chosen.round_index} "
        f"fit-evidence-ESR={best.fit.esr:.6g}"
    )

    if search_report is not None:
        search_report.clear()
        search_report.update(
            {
                "mode": "deterministic-multistart-stimulus-fit-only",
                "candidate_choice": (
                    "FIT evidence only. Real guitar is excluded from trajectory movement, "
                    "checkpoint choice and multistart choice."
                ),
                "multistarts": len(seeds),
                "max_rounds_requested": int(rounds),
                "rounds_executed_per_start": len(steps),
                "step_schedule": [
                    {"round": i + 1, "a_step_db": a, "pk_log_step": p}
                    for i, (a, p) in enumerate(steps)
                ],
                "trajectories": trajectories,
                "selected": {
                    "start_index": chosen.start_index,
                    "seed_label": chosen.seed_label,
                    "checkpoint": chosen.round_index,
                    "fit_evidence_esr": float(best.fit.esr),
                    "pk": [float(v) for v in best.pk],
                },
            }
        )

    return best
