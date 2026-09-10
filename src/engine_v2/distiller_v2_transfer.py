#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from distiller_v2_data import group_models, load_audio, load_pairs
from distiller_v2_dsp import pk_render, warm

SR = 44100
PROBE_BASENAME = "09_1kHz_level_ladder.wav"
DEFAULT_LEVELS_DB = (-42.0, -36.0, -30.0, -24.0, -18.0, -12.0, -9.0, -6.0, -3.0, -1.0)
LEAD_S = 0.5
TONE_S = 0.65
GAP_S = 0.15
TRIM_S = 0.10
FUND_HZ = 1000.0
HARMONICS = 8


@dataclass(frozen=True)
class Signature:
    levels_db: list[float]
    fundamental_db: list[float]
    harmonic_dbc: list[list[float]]


@dataclass(frozen=True)
class FitResult:
    pk: list[float]
    objective: float
    fundamental_rmse_db: float
    harmonic_growth_rmse_db: float
    evaluations: int


def _amp_db(v: float) -> float:
    return 20.0 * math.log10(max(abs(float(v)), 1e-15))


def _tone_windows(n: int, levels=DEFAULT_LEVELS_DB):
    windows = []
    for i, level in enumerate(levels):
        start = LEAD_S + i * (TONE_S + GAP_S) + TRIM_S
        end = LEAD_S + i * (TONE_S + GAP_S) + TONE_S - TRIM_S
        a = int(round(start * SR))
        b = int(round(end * SR))
        if b > n:
            raise ValueError(
                f"{PROBE_BASENAME} is too short for the expected ladder layout "
                f"(need {b} samples, got {n})"
            )
        windows.append((float(level), a, b))
    return windows


def _harmonic_amplitudes(y: np.ndarray, a: int, b: int) -> np.ndarray:
    z = np.asarray(y[a:b], dtype=np.float64)
    if len(z) < 1024:
        raise ValueError("tone window too short")
    w = np.hanning(len(z))
    t = np.arange(len(z), dtype=np.float64) / SR
    scale = 2.0 / max(float(np.sum(w)), 1e-15)
    amps = []
    for h in range(1, HARMONICS + 1):
        f = FUND_HZ * h
        osc = np.exp(-2j * np.pi * f * t)
        amps.append(scale * abs(np.sum(z * w * osc)))
    return np.asarray(amps, dtype=np.float64)


def signature(y: np.ndarray) -> Signature:
    levels = []
    fundamental = []
    harmonic_dbc = []
    for level, a, b in _tone_windows(len(y)):
        amps = _harmonic_amplitudes(y, a, b)
        h1 = max(float(amps[0]), 1e-15)
        levels.append(level)
        fundamental.append(_amp_db(h1))
        harmonic_dbc.append([_amp_db(max(float(v), 1e-15) / h1) for v in amps[1:]])
    return Signature(levels, fundamental, harmonic_dbc)


def _relative(values: np.ndarray, anchor: int) -> np.ndarray:
    return values - values[anchor]


def compare_signatures(pred: Signature, target: Signature) -> tuple[float, float, float]:
    if pred.levels_db != target.levels_db:
        raise ValueError("signature level grids differ")
    levels = np.asarray(target.levels_db, dtype=np.float64)

    # Anchor fundamental response at -18 dBFS. Absolute gain belongs to the
    # linear/output stages; here we only care how the transfer bends with drive.
    anchor = int(np.argmin(np.abs(levels - (-18.0))))
    pf = np.asarray(pred.fundamental_db)
    tf = np.asarray(target.fundamental_db)
    fund_err = _relative(pf, anchor) - _relative(tf, anchor)
    fund_rmse = math.sqrt(float(np.mean(fund_err * fund_err)))

    ph = np.asarray(pred.harmonic_dbc, dtype=np.float64)
    th = np.asarray(target.harmonic_dbc, dtype=np.float64)
    growth_errors = []
    for j in range(ph.shape[1]):
        # A/B/POST are fixed linear filters, so each harmonic can have an
        # arbitrary constant frequency-response offset. Compare only how that
        # harmonic grows with input level; the constant offset cancels.
        usable = np.where(th[:, j] > -70.0)[0]
        if len(usable) < 2:
            continue
        ha = int(usable[np.argmin(np.abs(levels[usable] - (-18.0)))])
        pe = ph[usable, j] - ph[ha, j]
        te = th[usable, j] - th[ha, j]
        growth_errors.extend((pe - te).tolist())
    harm_rmse = math.sqrt(float(np.mean(np.square(growth_errors)))) if growth_errors else 0.0

    # Research metric only: transfer/compression shape + harmonic-growth shape.
    # No guitar ESR, no absolute level, and no final B/output-gain term here.
    objective = fund_rmse + harm_rmse
    return objective, fund_rmse, harm_rmse


def _pk_from_theta(theta: np.ndarray) -> np.ndarray:
    # Common P scale is degenerate with final B output gain. Hold geometric
    # mean(Pp,Pn)=0.1 and fit only asymmetry. K is defined for unity A gain at
    # 1 kHz; integration must preserve that A anchor rather than moving drive
    # between the pre-filter and K.
    ratio_log = float(np.clip(theta[0], -4.0, 4.0))
    pp = float(np.clip(0.1 * math.exp(0.5 * ratio_log), 0.01, 2.0))
    pn = float(np.clip(0.1 * math.exp(-0.5 * ratio_log), 0.01, 2.0))
    kp = float(np.clip(math.exp(theta[1]), 0.05, 80.0))
    kn = float(np.clip(math.exp(theta[2]), 0.05, 80.0))
    return np.array([pp, pn, kp, kn], dtype=np.float64)


def fit_pk(probe_input: np.ndarray, nam_output: np.ndarray) -> FitResult:
    target_sig = signature(nam_output)
    theta = np.array([0.0, 0.0, 0.0], dtype=np.float64)
    evaluations = 0

    def evaluate(th):
        nonlocal evaluations
        pk = _pk_from_theta(th)
        y = pk_render(np.asarray(probe_input, dtype=np.float64), pk)
        sig = signature(y)
        evaluations += 1
        return compare_signatures(sig, target_sig), pk

    (best_obj, best_fund, best_harm), best_pk = evaluate(theta)

    # Only three identifiable dimensions: P asymmetry, K+, K-. Search in log
    # space so low/high gain regions get comparable resolution without coupling
    # this experiment back to the A/B end-to-end optimiser.
    for step in (1.0, 0.5, 0.25, 0.125, 0.0625):
        for _ in range(6):
            moved = False
            for i in range(3):
                local_best = (best_obj, theta.copy(), best_fund, best_harm, best_pk.copy())
                for s in (1.0, -1.0):
                    q = theta.copy()
                    q[i] += s * step
                    (obj, fund, harm), pk = evaluate(q)
                    if obj < local_best[0] - 1e-9:
                        local_best = (obj, q, fund, harm, pk)
                if local_best[0] < best_obj - 1e-9:
                    best_obj, theta, best_fund, best_harm, best_pk = local_best
                    moved = True
            if not moved:
                break

    return FitResult(best_pk.tolist(), best_obj, best_fund, best_harm, evaluations)


def _make_probe_input() -> np.ndarray:
    """Recreate the corpus 1-kHz ladder deterministically for analyser tests."""
    parts = [np.zeros(int(round(LEAD_S * SR)), dtype=np.float64)]
    for level in DEFAULT_LEVELS_DB:
        n = int(round(TONE_S * SR))
        t = np.arange(n, dtype=np.float64) / SR
        tone = np.sin(2.0 * np.pi * FUND_HZ * t) * (10.0 ** (level / 20.0))
        # Match the corpus probe's short cosine edge fade. The signature trims
        # 100 ms from both ends, so this mainly keeps the generated control
        # faithful rather than influencing the measured steady state.
        fade_n = min(int(round(0.01 * SR)), len(tone) // 2)
        if fade_n > 1:
            f = np.linspace(0.0, 1.0, fade_n)
            tone[:fade_n] *= f
            tone[-fade_n:] *= f[::-1]
        parts.append(tone)
        parts.append(np.zeros(int(round(GAP_S * SR)), dtype=np.float64))
    return np.concatenate(parts)


def _print_signature(title: str, sig: Signature) -> None:
    print(f"\n{title}")
    print(" input     fundamental     H2      H3      H4      H5")
    for i, level in enumerate(sig.levels_db):
        hs = sig.harmonic_dbc[i]
        print(
            f"{level:+5.0f} dB  {sig.fundamental_db[i]:+9.2f} dB  "
            f"{hs[0]:+7.1f} {hs[1]:+7.1f} {hs[2]:+7.1f} {hs[3]:+7.1f}"
        )


def _roundtrip_self_test() -> int:
    """Known-family control: can the analyser recover a GP50 transfer at all?"""
    # Geometric mean(Pp,Pn)=0.1, matching fit_pk's identifiable convention.
    known = np.array([0.125, 0.08, 3.0, 2.0], dtype=np.float64)
    x = _make_probe_input()
    y = pk_render(x, known)
    result = fit_pk(x, y)
    recovered = np.asarray(result.pk, dtype=np.float64)
    pred = pk_render(x, recovered)
    sig_obj, sig_fund, sig_harm = compare_signatures(signature(pred), signature(y))

    print("GP50 transfer analyser round-trip control")
    print(f"known:     Pp={known[0]:.6g} Pn={known[1]:.6g} Kp={known[2]:.6g} Kn={known[3]:.6g}")
    print(f"recovered: Pp={recovered[0]:.6g} Pn={recovered[1]:.6g} Kp={recovered[2]:.6g} Kn={recovered[3]:.6g}")
    print(
        f"signature error: objective={sig_obj:.4f} dB "
        f"fundamental={sig_fund:.4f} dB harmonic-growth={sig_harm:.4f} dB "
        f"evaluations={result.evaluations}"
    )
    print("NOTE: this is a diagnostic control, not a CLO conversion.")
    return 0


def _find_probe(model_pairs):
    hits = [
        p for p in model_pairs
        if p.synthetic and Path(p.source_path).name == PROBE_BASENAME
    ]
    if not hits:
        raise RuntimeError(
            f"No {PROBE_BASENAME} teacher pair found for this model. "
            "Rebuild/populate the teacher dataset with synthetic probes."
        )
    return sorted(hits, key=lambda p: p.task_id)[0]


def main():
    ap = argparse.ArgumentParser(
        description=(
            "Research-only GP50 P/K decomposition experiment. Fits the GP50 "
            "static asymmetric transfer from the NAM 1 kHz level-ladder "
            "teacher render. It does not fit A/B and does not create a CLO."
        )
    )
    ap.add_argument("--teacher-root", default="~/NamtoCloTeacherDataset")
    ap.add_argument("--model-regex")
    ap.add_argument("--output", default="~/NamtoCloTransferAnalysis")
    ap.add_argument(
        "--self-test", action="store_true",
        help="Run a known-GP50 P/K round-trip control instead of analysing a NAM.",
    )
    args = ap.parse_args()

    print("warming JIT...")
    warm()
    if args.self_test:
        return _roundtrip_self_test()
    if not args.model_regex:
        raise SystemExit("Choose --self-test or provide --model-regex REGEX")

    root = Path(args.teacher_root).expanduser()
    groups = group_models(load_pairs(root))
    rx = re.compile(args.model_regex, re.I)
    keys = [k for k in sorted(groups) if rx.search(groups[k][0].model_name)]
    if not keys:
        raise SystemExit("No models matched")
    if len(keys) > 1:
        raise SystemExit(
            "Model regex matched more than one model; make it specific:\n  "
            + "\n  ".join(groups[k][0].model_name for k in keys)
        )

    ps = groups[keys[0]]
    pair = _find_probe(ps)
    x, nam, _ = load_audio([pair])[0]

    print(f"model: {pair.model_name}")
    print(f"probe: {Path(pair.source_path).name}")
    target = signature(nam)
    result = fit_pk(x, nam)
    fitted = signature(pk_render(np.asarray(x, dtype=np.float64), np.asarray(result.pk, dtype=np.float64)))

    _print_signature("NAM 1 kHz transfer signature", target)
    _print_signature("fitted GP50 1 kHz transfer signature", fitted)

    print("\ntransfer-derived GP50 P/K")
    print(
        f"Pp={result.pk[0]:.6g} Pn={result.pk[1]:.6g} "
        f"Kp={result.pk[2]:.6g} Kn={result.pk[3]:.6g}"
    )
    print(
        f"transfer objective={result.objective:.3f} dB "
        f"fundamental={result.fundamental_rmse_db:.3f} dB "
        f"harmonic-growth={result.harmonic_growth_rmse_db:.3f} dB "
        f"evaluations={result.evaluations}"
    )

    out = Path(args.output).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    report = {
        "method": "gp50-static-transfer-decomposition-v1",
        "research_only": True,
        "model_key": pair.model_key,
        "model_name": pair.model_name,
        "tone_id": pair.tone_id,
        "model_id": pair.model_id,
        "probe_task_id": pair.task_id,
        "probe_source": pair.source_path,
        "target_signature": asdict(target),
        "fitted_gp50_signature": asdict(fitted),
        "fit": asdict(result),
        "integration_constraint": "A magnitude must be anchored to 0 dB at 1 kHz; final level belongs to B/output.",
        "notes": [
            "P/K is fit independently of A/B and output gain.",
            "Common P scale is fixed because it is degenerate with final B gain.",
            "Harmonic growth is compared relatively across input levels so fixed linear EQ does not drive P/K.",
            "The full -42..-1 dBFS probe is diagnostic; control-model results must be validated before choosing a perceptually relevant operating range.",
            "This script does not create a CLO; validate the transfer fit before integrating it into the converter.",
        ],
    }
    path = out / f"{pair.model_key}_transfer.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
