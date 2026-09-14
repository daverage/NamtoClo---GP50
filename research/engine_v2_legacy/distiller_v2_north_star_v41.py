from __future__ import annotations

import math
import time

import numpy as np

from distiller_v2_dsp import controls_to_a, pre_fir
from distiller_v2_fit import Candidate, _map, _workspace
from distiller_v2_north_star_v2 import _evaluate_fit, _evaluate_pk, _prepare_evidence
from distiller_v2_north_star_v3 import A_STEP_FLOOR_DB, PK_LOG_STEP_FLOOR
from distiller_v2_north_star_v4 import (
    StimulusFitEvidence,
    build_stimulus_fit_evidence,
    distill_north_star_v4,
)


# V4.1 changes only search convergence. The stimulus, objective, parameter bounds,
# DSP architecture, analytic B512 solve, multistart seeds and coarse-to-fine V4
# search are unchanged. After V4 has selected its best FIT-only basin, V4.1
# repeatedly revisits every A coordinate and P/K coordinate at the established
# V3/V4 step floors until the fine search stalls or reaches a safety cap.
DEFAULT_POLISH_CYCLES = 12
DEFAULT_POLISH_REL_TOL = 1.0e-5
FINE_A_STEP_DB = float(A_STEP_FLOOR_DB)
FINE_PK_LOG_STEP = float(PK_LOG_STEP_FLOOR)


def _polish_stop_reason(
    before_esr: float,
    after_esr: float,
    a_moves: int,
    pk_moves: int,
    relative_tolerance: float = DEFAULT_POLISH_REL_TOL,
) -> str | None:
    """Return the general convergence reason for a completed fine cycle."""
    if int(a_moves) == 0 and int(pk_moves) == 0:
        return "no_moves"
    improvement = max(0.0, float(before_esr) - float(after_esr))
    threshold = max(1.0e-12, abs(float(before_esr)) * float(relative_tolerance))
    if improvement <= threshold:
        return "negligible_fit_improvement"
    return None


def polish_north_star_v41(
    start: Candidate,
    fit,
    *,
    fit_evidence: StimulusFitEvidence,
    controls: int,
    pk_passes: int = 3,
    polish_cycles: int = DEFAULT_POLISH_CYCLES,
    polish_relative_tolerance: float = DEFAULT_POLISH_REL_TOL,
    status=print,
    pause_ms: float = 0.0,
    polish_report: dict | None = None,
) -> Candidate:
    """Repeated fine-resolution FIT-only coordinate polish of one V4 basin.

    Every candidate still receives the exact V4 teacher/student objective and a
    newly solved shared analytic B512. No guitar or diagnostic metric enters the
    search. A and P/K are revisited cooperatively because a P/K move can make
    earlier A coordinates stale and vice versa.
    """
    if not fit:
        raise ValueError("North Star v4.1 FIT material is empty")
    if controls < 1:
        raise ValueError("controls must be >= 1")
    if pk_passes < 1:
        raise ValueError("pk_passes must be >= 1")
    if polish_cycles < 1:
        raise ValueError("polish_cycles must be >= 1")
    if polish_relative_tolerance < 0.0:
        raise ValueError("polish_relative_tolerance must be >= 0")

    fit_ws = _workspace(fit)
    prepared = _prepare_evidence(fit_ws, fit_evidence)
    max_pk_passes = max(1, int(pk_passes))
    work = start
    rows: list[dict] = []
    stop_reason = "max_polish_cycles"

    for cycle in range(1, int(polish_cycles) + 1):
        started = time.monotonic()
        before = float(work.fit.esr)
        evals = 0
        a_moves = 0

        # Revisit all A coordinates at the finest V4 resolution. Each accepted
        # coordinate immediately becomes the reference for the remaining sweep.
        for i in range(int(controls)):
            for direction in (FINE_A_STEP_DB, -FINE_A_STEP_DB):
                c = work.controls_db.copy()
                c[i] = np.clip(c[i] + direction, -18.0, 18.0)
                q = _evaluate_fit(c, work.pk, fit_ws, fit_evidence, prepared)
                evals += 1
                if pause_ms > 0:
                    time.sleep(pause_ms / 1000.0)
                if q.fit.esr < work.fit.esr:
                    work = q
                    a_moves += 1

        # P/K uses the same fixed A for this block, exactly as V4 does. Multiple
        # P/K passes are retained; the next polish cycle then revisits A again.
        a_fixed = controls_to_a(work.controls_db)
        aouts = _map(lambda bqx: pre_fir(bqx, a_fixed), fit_ws.bqx)

        pk_moves = 0
        passes_used = 0
        for _ in range(max_pk_passes):
            pass_start = float(work.fit.esr)
            moved = False
            for i in range(4):
                for sign in (1.0, -1.0):
                    p = work.pk.copy()
                    p[i] *= math.exp(sign * FINE_PK_LOG_STEP)
                    p[:2] = np.clip(p[:2], 0.01, 2.0)
                    p[2:] = np.clip(p[2:], 0.05, 80.0)
                    q = _evaluate_pk(
                        aouts,
                        work.controls_db,
                        p,
                        fit_ws,
                        fit_evidence,
                        prepared,
                    )
                    evals += 1
                    if pause_ms > 0:
                        time.sleep(pause_ms / 1000.0)
                    if q.fit.esr < work.fit.esr:
                        work = q
                        moved = True
                        pk_moves += 1
            passes_used += 1
            if not moved or pass_start - work.fit.esr < 1.0e-8:
                break

        after = float(work.fit.esr)
        improvement = before - after
        rel_improvement = improvement / max(abs(before), 1.0e-30)
        elapsed = time.monotonic() - started
        reason = _polish_stop_reason(
            before,
            after,
            a_moves,
            pk_moves,
            polish_relative_tolerance,
        )
        row = {
            "cycle": cycle,
            "before_fit_evidence_esr": before,
            "after_fit_evidence_esr": after,
            "absolute_improvement": improvement,
            "relative_improvement": rel_improvement,
            "a_moves": a_moves,
            "pk_moves": pk_moves,
            "pk_passes": passes_used,
            "candidate_evaluations": evals,
            "pk": [float(v) for v in work.pk],
            "elapsed_seconds": elapsed,
            "stop_reason": reason,
        }
        rows.append(row)

        status(
            f"polish {cycle}/{polish_cycles} FIT-ESR {before:.6g}->{after:.6g} "
            f"improve={improvement:.3g} ({rel_improvement:.3g} rel) "
            f"A-moves={a_moves} PK-moves={pk_moves} pk-passes={passes_used} "
            f"pk=[{' '.join(f'{v:.3g}' for v in work.pk)}] "
            f"({evals} candidates, {elapsed:.1f}s)"
        )

        if reason is not None:
            stop_reason = reason
            break

    if polish_report is not None:
        polish_report.clear()
        polish_report.update(
            {
                "mode": "selected-v4-basin-repeated-fine-fit-only-polish",
                "a_step_db": FINE_A_STEP_DB,
                "pk_log_step": FINE_PK_LOG_STEP,
                "max_cycles": int(polish_cycles),
                "relative_improvement_tolerance": float(polish_relative_tolerance),
                "starting_fit_evidence_esr": float(start.fit.esr),
                "final_fit_evidence_esr": float(work.fit.esr),
                "stop_reason": stop_reason,
                "cycles_executed": len(rows),
                "cycles": rows,
            }
        )

    return work


def distill_north_star_v41(
    fit,
    controls: int = 24,
    rounds: int = 8,
    pk_passes: int = 3,
    multistarts: int = 3,
    polish_cycles: int = DEFAULT_POLISH_CYCLES,
    polish_relative_tolerance: float = DEFAULT_POLISH_REL_TOL,
    status=print,
    pause_ms: float = 0.0,
    fit_evidence: StimulusFitEvidence | None = None,
    search_report: dict | None = None,
) -> Candidate:
    """V4 stimulus-only search plus convergence polish of the selected basin.

    V4.1 is deliberately a one-variable experiment: only convergence mechanics
    change. It first runs the exact V4 multistart/coarse-to-fine search, then
    takes V4's best FIT-only checkpoint and repeatedly revisits A/P-K at the same
    step floors. No new evidence, objective term, bound or architecture is added.
    """
    if fit_evidence is None:
        fit_evidence = build_stimulus_fit_evidence(fit)

    v4_report: dict = {}
    base = distill_north_star_v4(
        fit,
        controls=controls,
        rounds=rounds,
        pk_passes=pk_passes,
        multistarts=multistarts,
        status=status,
        pause_ms=pause_ms,
        fit_evidence=fit_evidence,
        search_report=v4_report,
    )

    status(
        f"V4.1 POLISH selected V4 basin fit-evidence-ESR={base.fit.esr:.6g} "
        f"at A-step={FINE_A_STEP_DB:.4g}dB P/K-step={FINE_PK_LOG_STEP:.4g}"
    )
    polish_report: dict = {}
    best = polish_north_star_v41(
        base,
        fit,
        fit_evidence=fit_evidence,
        controls=controls,
        pk_passes=pk_passes,
        polish_cycles=polish_cycles,
        polish_relative_tolerance=polish_relative_tolerance,
        status=status,
        pause_ms=pause_ms,
        polish_report=polish_report,
    )

    if search_report is not None:
        search_report.clear()
        search_report.update(
            {
                "mode": "v4-multistart-plus-selected-basin-convergence-polish",
                "candidate_choice": (
                    "FIT stimulus evidence only. V4 selects the basin; V4.1 polishes "
                    "that basin at the established fine A/P-K step floors. Real guitar "
                    "is excluded from movement and candidate choice."
                ),
                "v4_search": v4_report,
                "polish": polish_report,
                "selected": {
                    "v4_fit_evidence_esr": float(base.fit.esr),
                    "v41_fit_evidence_esr": float(best.fit.esr),
                    "absolute_improvement": float(base.fit.esr - best.fit.esr),
                    "relative_improvement": float(
                        (base.fit.esr - best.fit.esr) / max(abs(base.fit.esr), 1.0e-30)
                    ),
                    "pk": [float(v) for v in best.pk],
                },
            }
        )

    return best
