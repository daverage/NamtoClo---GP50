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


# V4.2 changes only convergence mechanics after a V4-family basin has been found.
# The evidence, objective, DSP, bounds and analytic B solve remain unchanged.
DEFAULT_POLISH_CYCLES = 12
# Stop after a complete A/P-K cycle contributes <= 0.1% relative FIT-ESR
# improvement. The CLI remains able to override this for convergence studies.
DEFAULT_POLISH_REL_TOL = 1.0e-3
DEFAULT_MAX_LINE_STEPS = 96
FINE_A_STEP_DB = float(A_STEP_FLOOR_DB)
FINE_PK_LOG_STEP = float(PK_LOG_STEP_FLOOR)
_REFINE_EXHAUSTIVE_WIDTH = 8


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


def _a_trial_offset(
    base: Candidate,
    index: int,
    offset_steps: int,
    fit_ws,
    fit_evidence: StimulusFitEvidence,
    prepared,
):
    ctrl = base.controls_db.copy()
    old = float(ctrl[index])
    ctrl[index] = np.clip(
        old + int(offset_steps) * FINE_A_STEP_DB,
        -18.0,
        18.0,
    )
    if float(ctrl[index]) == old:
        return None
    return _evaluate_fit(ctrl, base.pk, fit_ws, fit_evidence, prepared)


def _pk_trial_offset(
    base: Candidate,
    index: int,
    offset_steps: int,
    aouts,
    fit_ws,
    fit_evidence: StimulusFitEvidence,
    prepared,
):
    pk = base.pk.copy()
    old = float(pk[index])
    pk[index] *= math.exp(int(offset_steps) * FINE_PK_LOG_STEP)
    pk[:2] = np.clip(pk[:2], 0.01, 2.0)
    pk[2:] = np.clip(pk[2:], 0.05, 80.0)
    if float(pk[index]) == old:
        return None
    return _evaluate_pk(aouts, base.controls_db, pk, fit_ws, fit_evidence, prepared)


def _accelerated_discrete_line_search(
    base: Candidate,
    trial_offset,
    *,
    max_line_steps: int,
    pause_ms: float = 0.0,
):
    """Bracket and refine a coordinate on the existing fine-step grid.

    The scientific search resolution is unchanged. We first test +/- one fine
    step, exponentially expand in the improving direction (1,2,4,8,...) to
    bracket the local minimum, then refine the integer fine-step interval and
    exhaustively check the final small bracket. This replaces V4.2's expensive
    one-fine-step-at-a-time walk without changing the objective or parameter
    grid.
    """
    if max_line_steps < 1:
        raise ValueError("max_line_steps must be >= 1")

    cache: dict[int, Candidate] = {0: base}
    evaluations = 0

    def evaluate(offset: int):
        nonlocal evaluations
        offset = int(offset)
        if offset in cache:
            return cache[offset]
        q = trial_offset(offset)
        if q is not None:
            cache[offset] = q
            evaluations += 1
            _pause(pause_ms)
        return q

    options = []
    for order, offset in enumerate((1, -1)):
        q = evaluate(offset)
        if q is not None and q.fit.esr < base.fit.esr:
            options.append((float(q.fit.esr), order, offset, q))

    if not options:
        return base, 0, evaluations, False

    _, _, signed_one, best = min(options, key=lambda row: (row[0], row[1]))
    direction = 1 if signed_one > 0 else -1
    best_abs = 1

    # Exponential expansion. Keep the best sampled point; the first worsening
    # point brackets the minimum with the previous power-of-two sample.
    probe = 2
    failed_abs = None
    while probe <= int(max_line_steps):
        q = evaluate(direction * probe)
        if q is None:
            failed_abs = probe
            break
        if q.fit.esr < best.fit.esr:
            best = q
            best_abs = probe
            if probe == int(max_line_steps):
                break
            probe = min(int(max_line_steps), probe * 2)
            if probe == best_abs:
                break
        else:
            failed_abs = probe
            break

    if failed_abs is None:
        high = int(max_line_steps)
    else:
        high = min(int(max_line_steps), failed_abs)
    low = 0 if best_abs <= 1 else max(0, best_abs // 2)

    # Discrete ternary refinement on fine-step indices, then exact exhaustive
    # evaluation of the final narrow interval. The latter guarantees that the
    # returned coordinate lies on the same 0.05 dB / 0.02-log grid as V4.1.
    while high - low > _REFINE_EXHAUSTIVE_WIDTH:
        span = high - low
        m1 = low + span // 3
        m2 = high - span // 3
        if m1 == m2:
            break
        q1 = evaluate(direction * m1)
        q2 = evaluate(direction * m2)
        e1 = float("inf") if q1 is None else float(q1.fit.esr)
        e2 = float("inf") if q2 is None else float(q2.fit.esr)
        if e1 <= e2:
            high = m2 - 1
        else:
            low = m1 + 1

    for step_abs in range(max(0, low), min(int(max_line_steps), high) + 1):
        q = evaluate(direction * step_abs)
        if q is not None and q.fit.esr < best.fit.esr:
            best = q
            best_abs = step_abs

    if best.fit.esr >= base.fit.esr:
        return base, 0, evaluations, False

    capped = bool(best_abs >= int(max_line_steps))
    return best, int(best_abs), evaluations, capped


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
    base = work
    return _accelerated_discrete_line_search(
        base,
        lambda offset: _a_trial_offset(
            base,
            index,
            offset,
            fit_ws,
            fit_evidence,
            prepared,
        ),
        max_line_steps=max_line_steps,
        pause_ms=pause_ms,
    )


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
    base = work
    return _accelerated_discrete_line_search(
        base,
        lambda offset: _pk_trial_offset(
            base,
            index,
            offset,
            aouts,
            fit_ws,
            fit_evidence,
            prepared,
        ),
        max_line_steps=max_line_steps,
        pause_ms=pause_ms,
    )


def evaluate_warm_start_v42(
    fit,
    *,
    controls_db,
    pk,
    fit_evidence: StimulusFitEvidence,
) -> Candidate:
    """Re-evaluate a prior V4-family A/P-K state on current authoritative FIT.

    B512 is solved again from the current teacher evidence, so a warm start does
    not trust a stored B or stored score. It is purely an optimization restart.
    """
    fit_ws = _workspace(fit)
    prepared = _prepare_evidence(fit_ws, fit_evidence)
    controls_db = np.asarray(controls_db, dtype=np.float64)
    pk = np.asarray(pk, dtype=np.float64)
    if controls_db.ndim != 1:
        raise ValueError("warm-start controls_db must be 1-D")
    if pk.shape != (4,):
        raise ValueError("warm-start pk must contain four values")
    return _evaluate_fit(controls_db, pk, fit_ws, fit_evidence, prepared)


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
    """FIT-only bracket/refine coordinate convergence of a V4-family basin."""
    if not fit:
        raise ValueError("North Star v4.2 FIT material is empty")
    if controls < 1:
        raise ValueError("controls must be >= 1")
    if len(start.controls_db) != int(controls):
        raise ValueError(
            f"start has {len(start.controls_db)} A controls but controls={controls}"
        )
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
            coord_before = float(work.fit.esr)
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
            # Progress is intentionally per-coordinate so a long sweep no longer
            # appears hung on full 190 s x 5 level evidence.
            status(
                f"  A {index + 1:02d}/{controls}: ESR {coord_before:.6g}->{work.fit.esr:.6g} "
                f"offset={moved} fine-steps evals={used}"
            )

        a_fixed = controls_to_a(work.controls_db)
        aouts = _map(lambda bqx: pre_fir(bqx, a_fixed), fit_ws.bqx)

        pk_steps = 0
        pk_coordinates = 0
        pk_capped = 0
        passes_used = 0
        for pass_index in range(max(1, int(pk_passes))):
            pass_before = float(work.fit.esr)
            pass_steps = 0
            for index in range(4):
                coord_before = float(work.fit.esr)
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
                status(
                    f"  PK pass {pass_index + 1}/{pk_passes} coord {index + 1}/4: "
                    f"ESR {coord_before:.6g}->{work.fit.esr:.6g} "
                    f"offset={moved} fine-steps evals={used}"
                )
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
            "a_fine_step_distance": a_steps,
            "a_coordinates_hit_line_cap": a_capped,
            "pk_coordinates_moved": pk_coordinates,
            "pk_fine_step_distance": pk_steps,
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
            f"A-coords={a_coordinates} A-distance={a_steps} "
            f"PK-coords={pk_coordinates} PK-distance={pk_steps} pk-passes={passes_used} "
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
                "mode": "selected-v4-family-basin-bracket-refine-coordinate-polish",
                "line_search": "fine-grid +/- direction test, exponential bracket, discrete refinement, exact local exhaustive finish",
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
    warm_start_controls_db=None,
    warm_start_pk=None,
    warm_start_source: str | None = None,
) -> Candidate:
    """V4-family basin search/restart followed by accelerated convergence.

    When a compatible V4.1 result is supplied, V4.2 re-evaluates that A/P-K state
    against the current authoritative FIT evidence, re-solves B, and continues
    from it. Otherwise it falls back to the exact V4 deterministic multistart.
    """
    if fit_evidence is None:
        fit_evidence = build_stimulus_fit_evidence(fit)

    v4_report: dict = {}
    warm_started = warm_start_controls_db is not None or warm_start_pk is not None
    if warm_started:
        if warm_start_controls_db is None or warm_start_pk is None:
            raise ValueError("both warm_start_controls_db and warm_start_pk are required")
        if len(warm_start_controls_db) != int(controls):
            raise ValueError(
                f"warm start has {len(warm_start_controls_db)} controls; expected {controls}"
            )
        base = evaluate_warm_start_v42(
            fit,
            controls_db=warm_start_controls_db,
            pk=warm_start_pk,
            fit_evidence=fit_evidence,
        )
        status(
            f"V4.2 WARM START re-evaluated prior V4-family basin "
            f"fit-evidence-ESR={base.fit.esr:.6g} source={warm_start_source or 'unspecified'}"
        )
    else:
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
        f"V4.2 BRACKET/REFINE POLISH basin fit-evidence-ESR={base.fit.esr:.6g} "
        f"at A-step={FINE_A_STEP_DB:.4g}dB P/K-step={FINE_PK_LOG_STEP:.4g} "
        f"max-line-distance={max_line_steps} fine steps"
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
                "mode": (
                    "v41-warm-start-plus-bracket-refine-coordinate-convergence"
                    if warm_started
                    else "v4-multistart-plus-bracket-refine-coordinate-convergence"
                ),
                "candidate_choice": (
                    "FIT stimulus evidence only. A compatible prior V4.1 state may be "
                    "used purely as an optimization restart; it is re-evaluated on current "
                    "FIT evidence and B512 is solved again. Real guitar is excluded from "
                    "movement and candidate choice."
                ),
                "warm_start": {
                    "used": bool(warm_started),
                    "source": warm_start_source,
                    "reevaluated_fit_evidence_esr": float(base.fit.esr),
                },
                "v4_search": v4_report,
                "line_polish": line_report,
                "selected": {
                    "starting_fit_evidence_esr": float(base.fit.esr),
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