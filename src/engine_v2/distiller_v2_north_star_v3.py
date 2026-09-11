from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from distiller_v2_dsp import controls_to_a, pre_fir
from distiller_v2_fit import Candidate, _map, _selection_score, _workspace
from distiller_v2_north_star_v2 import (
    FitEvidence,
    _evaluate_fit,
    _evaluate_pk,
    _prepare_evidence,
    build_fit_evidence,
)


# Search-only evolution of North Star v2. The evidence, objective and GP50
# architecture remain unchanged; this module only makes the optimizer less
# dependent on one initial P/K basin and on four hard-coded search steps.
A_STEP_START_DB = 3.0
A_STEP_DECAY = 0.5
A_STEP_FLOOR_DB = 0.05
PK_LOG_STEP_START = 0.45
PK_LOG_STEP_DECAY = 0.6
PK_LOG_STEP_FLOOR = 0.02
DEFAULT_MAX_ROUNDS = 8
DEFAULT_MULTISTARTS = 3

# The first entry is the historical v2 seed for continuity. The next seeds keep
# approximately unit small-signal slope (P*K ~= 1) while placing the saturation
# knee at materially different operating levels. Later entries add deterministic
# asymmetry without knowing anything about an amp/model name.
_PK_SEED_SPECS = (
    ("legacy", (0.1, 0.1, 1.0, 1.0)),
    ("hard_symmetric", (0.05, 0.05, 20.0, 20.0)),
    ("wide_symmetric", (0.2, 0.2, 5.0, 5.0)),
    ("mid_symmetric", (0.1, 0.1, 10.0, 10.0)),
    ("asymmetric_a", (0.08, 0.16, 12.5, 6.25)),
    ("asymmetric_b", (0.16, 0.08, 6.25, 12.5)),
)
MAX_MULTISTARTS = len(_PK_SEED_SPECS)


@dataclass
class _Checkpoint:
    start_index: int
    seed_label: str
    round_index: int
    a_step_db: float
    pk_log_step: float
    candidate: Candidate


def _pk_seed_bank(count: int = DEFAULT_MULTISTARTS):
    count = int(count)
    if count < 1 or count > MAX_MULTISTARTS:
        raise ValueError(f"multistarts must be in [1, {MAX_MULTISTARTS}]")
    return tuple(
        (label, np.asarray(values, dtype=np.float64).copy())
        for label, values in _PK_SEED_SPECS[:count]
    )


def _search_steps(max_rounds: int = DEFAULT_MAX_ROUNDS):
    """Deterministic coarse-to-fine A/P-K step schedule.

    Unlike v2's four-element zip, requesting six rounds really yields six
    search steps. Search stops naturally after both step floors have been used.
    """
    max_rounds = max(1, int(max_rounds))
    out = []
    for r in range(max_rounds):
        a_step = max(A_STEP_FLOOR_DB, A_STEP_START_DB * (A_STEP_DECAY ** r))
        p_step = max(PK_LOG_STEP_FLOOR, PK_LOG_STEP_START * (PK_LOG_STEP_DECAY ** r))
        out.append((float(a_step), float(p_step)))
        if a_step <= A_STEP_FLOOR_DB and p_step <= PK_LOG_STEP_FLOOR:
            break
    return tuple(out)


def _checkpoint_key(checkpoint: _Checkpoint):
    # Selection is only a candidate/checkpoint chooser, never a trajectory gate.
    # FIT breaks exact selection ties, followed by deterministic provenance.
    c = checkpoint.candidate
    return (
        float(c.selection.esr),
        float(c.fit.esr),
        int(checkpoint.start_index),
        int(checkpoint.round_index),
    )


def _select_best_checkpoint(checkpoints):
    checkpoints = tuple(checkpoints)
    if not checkpoints:
        raise ValueError("no checkpoints to select from")
    return min(checkpoints, key=_checkpoint_key)


def _checkpoint_row(checkpoint: _Checkpoint) -> dict:
    c = checkpoint.candidate
    return {
        "round": checkpoint.round_index,
        "a_step_db": checkpoint.a_step_db,
        "pk_log_step": checkpoint.pk_log_step,
        "fit_evidence_esr": float(c.fit.esr),
        "selection_esr": float(c.selection.esr),
        "pk": [float(v) for v in c.pk],
    }


def distill_north_star_v3(
    fit,
    selection,
    controls: int = 24,
    rounds: int = DEFAULT_MAX_ROUNDS,
    pk_passes: int = 3,
    multistarts: int = DEFAULT_MULTISTARTS,
    status=print,
    pause_ms: float = 0.0,
    fit_evidence: FitEvidence | None = None,
    search_report: dict | None = None,
) -> Candidate:
    """North Star search-robust fitter.

    The v2 evidence, direct aligned ESR objective, A/P-K cooperation and
    analytic B512 variable projection are unchanged. What changes is search:

    * deterministic P/K multistart;
    * true coarse-to-fine steps down to explicit floors;
    * FIT-only trajectory movement (selection cannot reject an intermediate);
    * SELECTION chooses among retained trajectory checkpoints afterwards.
    """
    if not fit:
        raise ValueError("North Star v3 FIT material is empty")
    if not selection:
        raise ValueError("North Star v3 SELECTION material is empty")
    if controls < 1:
        raise ValueError("controls must be >= 1")
    if pk_passes < 1:
        raise ValueError("pk_passes must be >= 1")

    if fit_evidence is None:
        fit_evidence = build_fit_evidence(fit)

    fit_ws = _workspace(fit)
    prepared = _prepare_evidence(fit_ws, fit_evidence)
    selection_ws = _workspace(selection)
    steps = _search_steps(rounds)
    seeds = _pk_seed_bank(multistarts)
    max_pk_passes = max(1, int(pk_passes))

    all_checkpoints: list[_Checkpoint] = []
    trajectories: list[dict] = []

    for start_index, (seed_label, seed_pk) in enumerate(seeds):
        ctrl = np.zeros(controls, dtype=np.float64)
        work = _evaluate_fit(ctrl, seed_pk, fit_ws, fit_evidence, prepared)
        work.selection = _selection_score(selection, work, selection_ws)
        if pause_ms > 0:
            time.sleep(pause_ms / 1000.0)

        seed_checkpoint = _Checkpoint(
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
            f"selection-ESR={work.selection.esr:.6g} "
            f"pk=[{' '.join(f'{v:.3g}' for v in work.pk)}]"
        )

        for round_index, (a_step, p_step) in enumerate(steps, 1):
            started = time.monotonic()
            before_fit = float(work.fit.esr)
            evals = 0
            a_moves = 0

            # One full coordinate sweep at the current A resolution. The next
            # round revisits every coordinate at a finer resolution.
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

            # Important: selection is measured and retained, but it does NOT
            # decide whether this FIT trajectory may continue to finer steps.
            work.selection = _selection_score(selection, work, selection_ws)
            if pause_ms > 0:
                time.sleep(pause_ms / 1000.0)

            checkpoint = _Checkpoint(
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
                f"selection-ESR={work.selection.esr:.6g} "
                f"A-step={a_step:.4g}dB P/K-step={p_step:.4g} "
                f"A-moves={a_moves} PK-moves={pk_moves} pk-passes={passes_used} "
                f"pk=[{' '.join(f'{v:.3g}' for v in work.pk)}] "
                f"({evals} candidates, {elapsed:.1f}s)"
            )

        start_best = _select_best_checkpoint(start_checkpoints)
        status(
            f"start {start_index + 1}/{len(seeds)} BEST checkpoint="
            f"{start_best.round_index} selection-ESR={start_best.candidate.selection.esr:.6g} "
            f"fit-evidence-ESR={start_best.candidate.fit.esr:.6g}"
        )
        trajectories.append(
            {
                "start_index": start_index,
                "seed_label": seed_label,
                "seed_pk": [float(v) for v in seed_pk],
                "checkpoints": [_checkpoint_row(cp) for cp in start_checkpoints],
                "best_checkpoint": start_best.round_index,
                "best_selection_esr": float(start_best.candidate.selection.esr),
                "best_fit_evidence_esr": float(start_best.candidate.fit.esr),
            }
        )

    chosen = _select_best_checkpoint(all_checkpoints)
    best = chosen.candidate
    status(
        f"SELECT start {chosen.start_index + 1}/{len(seeds)} "
        f"{chosen.seed_label} checkpoint={chosen.round_index} "
        f"selection-ESR={best.selection.esr:.6g} "
        f"fit-evidence-ESR={best.fit.esr:.6g}"
    )

    if search_report is not None:
        search_report.clear()
        search_report.update(
            {
                "mode": "deterministic-multistart-fit-trajectories-selection-checkpoints",
                "selection_role": (
                    "Selection chooses among retained checkpoints only; it never blocks "
                    "a FIT-optimizing trajectory from continuing to finer search steps."
                ),
                "multistarts": len(seeds),
                "max_rounds_requested": int(rounds),
                "rounds_executed_per_start": len(steps),
                "a_step_start_db": A_STEP_START_DB,
                "a_step_decay": A_STEP_DECAY,
                "a_step_floor_db": A_STEP_FLOOR_DB,
                "pk_log_step_start": PK_LOG_STEP_START,
                "pk_log_step_decay": PK_LOG_STEP_DECAY,
                "pk_log_step_floor": PK_LOG_STEP_FLOOR,
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
                    "selection_esr": float(best.selection.esr),
                    "pk": [float(v) for v in best.pk],
                },
            }
        )

    return best
