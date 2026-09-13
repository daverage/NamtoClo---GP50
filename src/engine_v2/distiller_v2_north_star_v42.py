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


# V4.2 changes only the convergence mechanics after the exact V4 search.
# The latest EngineV2 evaluation path is inherited unchanged: _evaluate_fit /
# _evaluate_pk use the current threaded _map implementation, current DSP kernels,
# current balanced analytic B512 solve, and current V4 stimulus evidence.
DEFAULT_POLISH_CYCLES = 12
DEFAULT_POLISH_REL_TOL = 1.0e-5
DEFAULT_MAX_LINE_STEPS = 96
FINE_A_STEP_DB = float(A_STEP_FLOOR_DB)
FINE_PK_LOG_STEP = float(PK_LOG_STEP_FLOOR)


def _polish_stop_reason(
    before_esr: float,
    after_esr: float,
    accepted_steps: int,
    relative_tolerance: float = DEFAULT_POLISH_REL_TOL,
) -> str | None:
    if int(accepted_steps) == 0:
        return "no_moves"
    improvement = max(0.0, float(before_esr) - float(after_esr))
    threshold = max(1.0e-12, abs(float(before_esr)) * float(relative_tolerance))
    if improvement <= threshold:
        return "negligible_fit_improvement"
    return None


def _pause(pause_ms: float) -> None:
    if pause_ms > 0.0:
        time.sleep(pause_ms / 1000.0)


def _a_trial(
    work: Candidate,
    index: int,
    direction: float,
    fit_ws,
    fit_evidence: StimulusFitEvidence,
    prepared,
):
    ctrl = work.controls_db.copy()
    old = float(ctrl[index])
    ctrl[index] = np.clip(old + direction, -18.0, 18.0)
    if float(ctrl[index]) == old:
        return None
    return _evaluate_fit(ctrl, work.pk, fit_ws, fit_evidence, prepared)


def _pk_trial(
    work: Candidate,
    index: int,
    sign: float,
    aouts,
    fit_ws,
    fit_evidence: StimulusFitEvidence,
    prepared,
):
    pk = work.pk.copy()
    old = float(pk[index])
    pk[index] *= math.exp(sign * FINE_PK_LOG_STEP)
    pk[:2] = np.clip(pk[:2], 0.01, 2.0)
    pk[2:] = np.clip(pk[2:], 0.05, 80.0)
    if float(pk[index]) == old:
        return None
    return _evaluate_pk(aouts, work.controls_db, pk, fit_ws, fit_evidence, prepared)


def _line_search_a_coordinate(
    work: Candidate,
    index: int,
    fit_ws,
    fit_evidence: StimulusFitEvidence,
    prepared,
    *,
    max_line_steps: int,
    pause_ms: float = 0.0,
):
    """Move one A control repeatedly at the fine step until it stops helping.

    Both directions are first evaluated from the same starting candidate. The
    better improving direction is then followed one exact 0.05 dB step at a time.
    This is intentionally deterministic; + wins an exact numerical tie.
    """
    base = work
    evaluations = 0
    options = []
    for order, direction in enumerate((FINE_A_STEP_DB, -FINE_A_STEP_DB)):
        q = _a_trial(base, index, direction, fit_ws, fit_evidence, prepared)
        if q is None:
            continue
        evaluations += 1
        _pause(pause_ms)
        if q.fit.esr < base.fit.esr:
            options.append((float(q.fit.esr), order, direction, q))

    if not options:
        return work, 0, evaluations, False

    _, _, direction, work = min(options, key=lambda row: (row[0], row[1]))
    accepted = 1

    while accepted < int(max_line_steps):
        q = _a_trial(work, index, direction, fit_ws, fit_evidence, prepared)
        if q is None:
            break
        evaluations += 1
        _pause(pause_ms)
        if q.fit.esr < work.fit.esr:
            work = q
            accepted += 1
        else:
            break

    return work, accepted, evaluations, accepted >= int(max_line_steps)


def _line_search_pk_coordinate(
    work: Candidate,
    index: int,
    aouts,
    fit_ws,
    fit_evidence: StimulusFitEvidence,
    prepared,
    *,
    max_line_steps: int,
    pause_ms: float = 0.0,
):
    """Move one P/K coordinate multiplicatively until it stops helping."""
    base = work
    evaluations = 0
    options = []
    for order, sign in enumerate((1.0, -1.0)):
        q = _pk_trial(base, index, sign, aouts, fit_ws, fit_evidence, prepared)
        if q is None:
            continue
        evaluations += 1
        _pause(pause_ms)
        if q.fit.esr < base.fit.esr:
            options.append((float(q.fit.esr), order, sign, q))

    if not options:
        return work, 0, evaluations, False

    _, _, sign, work = min(options, key=lambda row: (row[0], row[1]))
    accepted = 1

    while accepted < int(max_line_steps):
        q = _pk_trial(work, index, sign, aouts, fit_ws, fit_evidence, prepared)
        if q is None:
            break
        evaluations += 1
        _pause(pause_ms)
        if q.fit.esr < work.fit.esr:
            work = q
            accepted += 1
        else:
            break

    return work, accepted, evaluations, accepted >= int(max_line_steps)


def polish_north_star_v42(
    start: Candidate,
    fit,
    *,
    fit_evidence: StimulusFitEvidence,
    controls: int,
    pk_passes: int = 3,
    polish_cycles: int = DEFAULT_POLISH_CYCLES,
    polish_relative_tolerance: float = DEFAULT_POLISH_REL_TOL,
    max_line_steps: int = DEFAULT_MAX_LINE_STEPS,
    status=print,
    pause_ms: float = 0.0,
    polish_report: dict | None = None,
) -> Candidate:
    """Fine FIT-only line-coordinate convergence of the V4-selected basin.

    V4.1 proved that one fine increment per coordinate per cycle was still far
    from convergence. V4.2 keeps the exact same fine resolutions but follows an
    improving coordinate in the same direction until the next step fails. After
    the A sweep, P/K receives the same treatment with A held fixed. Full A/P-K
    cycles repeat until they stall, improvement is negligible, or the safety cap
    is reached. Every accepted candidate still re-solves the shared analytic B.
    """
    if not fit:
        raise ValueError("North Star v4.2 FIT material is empty")
    if controls < 1:
        raise ValueError("controls must be >= 1")
    if pk_passes < 1:
        raise ValueError("pk_passes must be >= 1")
    if polish_cycles < 1:
        raise ValueError("polish_cycles must be >= 1")
    if polish_relative_tolerance < 0.0:
        raise ValueError("polish_relative_tolerance must be >= 0")
    if max_line_steps < 1:
        raise ValueError("max_line_steps must be >= 1")

    fit_ws = _workspace(fit)
    prepared = _prepare_evidence(fit_ws, fit_evidence)
    work = start
    rows: list[dict] = []
    stop_reason = "max_polish_cycles"

    for cycle in range(1, int(polish_cycles) + 1):
        started = time.monotonic()
        before = float(work.fit.esr)
        evaluations = 0
        a_steps = 0
        a_coordinates = 0
        a_capped = 0

        for index in range(int(controls)):
            work, moved, used, capped = _line_search_a_coordinate(
                work,
                index,
                fit_ws,
                fit_evidence,
                prepared,
                max_line_steps=max_line_steps,
                pause_ms=pause_ms,
            )
            evaluations += used
            a_steps += moved
            if moved:
                a_coordinates += 1
            if capped:
                a_capped += 1

        a_fixed = controls_to_a(work.controls_db)
        aouts = _map(lambda bqx: pre_fir(bqx, a_fixed), fit_ws.bqx)

        pk_steps = 0
        pk_coordinates = 0
        pk_capped = 0
        passes_used = 0
        for _ in range(max(1, int(pk_passes))):
            pass_before = float(work.fit.esr)
            pass_steps = 0
            for index in range(4):
                work, moved, used, capped = _line_search_pk_coordinate(
                    work,
                    index,
                    aouts,
                    fit_ws,
                    fit_evidence,
                    prepared,
                    max_line_steps=max_line_steps,
                    pause_ms=pause_ms,
                )
                evaluations += used
                pk_steps += moved
                pass_steps += moved
                if moved:
                    pk_coordinates += 1
                if capped:
                    pk_capped += 1
            passes_used += 1
            if pass_steps == 0 or pass_before - work.fit.esr < 1.0e-8:
                break

        after = float(work.fit.esr)
        improvement = before - after
        rel_improvement = improvement / max(abs(before), 1.0e-30)
        total_steps = a_steps + pk_steps
        elapsed = time.monotonic() - started
        reason = _polish_stop_reason(
            before,
            after,
            total_steps,
            polish_relative_tolerance,
        )

        row = {
            "cycle": cycle,
            "before_fit_evidence_esr": before,
            "after_fit_evidence_esr": after,
            "absolute_improvement": improvement,
            "relative_improvement": rel_improvement,
            "a_coordinates_moved": a_coordinates,
            "a_accepted_steps": a_steps,
            "a_coordinates_hit_line_cap": a_capped,
            "pk_coordinates_moved": pk_coordinates,
            "pk_accepted_steps": pk_steps,
            "pk_coordinates_hit_line_cap": pk_capped,
            "pk_passes": passes_used,
            "candidate_evaluations": evaluations,
            "pk": [float(v) for v in work.pk],
            "elapsed_seconds": elapsed,
            "stop_reason": reason,
        }
        rows.append(row)

        cap_note = ""
        if a_capped or pk_capped:
            cap_note = f" line-cap(A={a_capped},PK={pk_capped})"
        status(
            f"line-polish {cycle}/{polish_cycles} FIT-ESR {before:.6g}->{after:.6g} "
            f"improve={improvement:.3g} ({rel_improvement:.3g} rel) "
            f"A-coords={a_coordinates} A-steps={a_steps} "
            f"PK-coords={pk_coordinates} PK-steps={pk_steps} pk-passes={passes_used} "
            f"pk=[{' '.join(f'{v:.3g}' for v in work.pk)}] "
            f"({evaluations} candidates, {elapsed:.1f}s){cap_note}"
        )

        if reason is not None:
            stop_reason = reason
            break

    if polish_report is not None:
        polish_report.clear()
        polish_report.update(
            {
                "mode": "selected-v4-basin-fine-line-coordinate-polish",
                "a_step_db": FINE_A_STEP_DB,
                "pk_log_step": FINE_PK_LOG_STEP,
                "max_line_steps_per_coordinate": int(max_line_steps),
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


def distill_north_star_v42(
    fit,
    controls: int = 24,
    rounds: int = 8,
    pk_passes: int = 3,
    multistarts: int = 3,
    polish_cycles: int = DEFAULT_POLISH_CYCLES,
    polish_relative_tolerance: float = DEFAULT_POLISH_REL_TOL,
    max_line_steps: int = DEFAULT_MAX_LINE_STEPS,
    status=print,
    pause_ms: float = 0.0,
    fit_evidence: StimulusFitEvidence | None = None,
    search_report: dict | None = None,
) -> Candidate:
    """Exact V4 search followed by efficient line-coordinate convergence."""
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
        f"V4.2 LINE POLISH selected V4 basin fit-evidence-ESR={base.fit.esr:.6g} "
        f"at A-step={FINE_A_STEP_DB:.4g}dB P/K-step={FINE_PK_LOG_STEP:.4g} "
        f"max-line-steps={max_line_steps}"
    )
    line_report: dict = {}
    best = polish_north_star_v42(
        base,
        fit,
        fit_evidence=fit_evidence,
        controls=controls,
        pk_passes=pk_passes,
        polish_cycles=polish_cycles,
        polish_relative_tolerance=polish_relative_tolerance,
        max_line_steps=max_line_steps,
        status=status,
        pause_ms=pause_ms,
        polish_report=line_report,
    )

    if search_report is not None:
        search_report.clear()
        search_report.update(
            {
                "mode": "v4-multistart-plus-selected-basin-line-coordinate-convergence",
                "candidate_choice": (
                    "FIT stimulus evidence only. Exact V4 chooses the basin; V4.2 "
                    "follows improving A/P-K coordinates at the existing fine step "
                    "floors until local line descent stalls. Real guitar is excluded "
                    "from movement and candidate choice."
                ),
                "v4_search": v4_report,
                "line_polish": line_report,
                "selected": {
                    "v4_fit_evidence_esr": float(base.fit.esr),
                    "v42_fit_evidence_esr": float(best.fit.esr),
                    "absolute_improvement": float(base.fit.esr - best.fit.esr),
                    "relative_improvement": float(
                        (base.fit.esr - best.fit.esr) / max(abs(base.fit.esr), 1.0e-30)
                    ),
                    "pk": [float(v) for v in best.pk],
                },
            }
        )

    return best
