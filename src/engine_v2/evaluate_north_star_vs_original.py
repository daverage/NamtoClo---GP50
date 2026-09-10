#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from distiller_v2_data import Pair, group_models, load_pairs, read_pair
from distiller_v2_dsp import (
    A_TAPS,
    B_TAPS,
    CLO_BYTES,
    COEFF_BASE,
    SR,
    _bq,
    _fir,
    _preb_from_aout,
    crc16_modbus,
    warm,
)
from distiller_v2_fit import _linear_residual_db
from distiller_v2_north_star import _is_canonical_real_guitar


SPECTRUM_MIN_HZ = 20.0
SPECTRUM_MAX_HZ = 20000.0
DENSE_LOG_BANDS = 144
FULL_FFT = 65536
FULL_HOP = 16384
TAIL_FRAME = 4096
TAIL_FFT = 8192

# Broad summaries are descriptive only. The primary spectral evidence is the
# dense 20 Hz -> 20 kHz curve so a narrow resonance/notch cannot hide between
# hand-selected frequencies.
BANDS = (
    ("sub_headroom", 20.0, 35.0),
    ("bass_fundamental", 35.0, 60.0),
    ("low_bass", 60.0, 100.0),
    ("weight", 100.0, 160.0),
    ("body", 160.0, 250.0),
    ("low_mids", 250.0, 400.0),
    ("body_box", 400.0, 700.0),
    ("mids", 700.0, 1200.0),
    ("upper_mids", 1200.0, 2000.0),
    ("bite", 2000.0, 3500.0),
    ("presence", 3500.0, 5500.0),
    ("edge_fizz", 5500.0, 8000.0),
    ("upper_harmonics", 8000.0, 12000.0),
    ("air_residue", 12000.0, 18000.0),
    ("nyquist_headroom", 18000.0, 20000.0),
)


@dataclass(frozen=True)
class CompactClo:
    path: str
    pre: np.ndarray
    a: np.ndarray
    pk: np.ndarray
    post: np.ndarray
    b: np.ndarray


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._()+-]+", "_", s).strip("_")[:120]


def _dump(path: Path, obj) -> None:
    def cv(x):
        if isinstance(x, np.ndarray):
            return x.tolist()
        if isinstance(x, np.generic):
            return x.item()
        raise TypeError(type(x).__name__)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, default=cv))


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Compact CLO parsing / exact stored-coefficient render
# ---------------------------------------------------------------------------

def read_compact_clo(path: Path) -> CompactClo:
    path = Path(path).expanduser().resolve()
    data = path.read_bytes()
    if len(data) != CLO_BYTES or data[:4] != b"VTSI":
        raise RuntimeError(f"Not a compact GP5/GP50 CLO: {path}")
    if struct.unpack_from("<I", data, 4)[0] != CLO_BYTES:
        raise RuntimeError(f"Bad compact CLO size field: {path}")
    crc_file = (data[8] << 8) | data[9]
    if crc_file != crc16_modbus(data[0x0C:]):
        raise RuntimeError(f"CLO CRC mismatch: {path}")

    c0, c1, c2, c3 = struct.unpack_from("<IIII", data, 0x78)
    if (c0, c1, c2, c3) != (0, A_TAPS, A_TAPS, B_TAPS):
        raise RuntimeError(
            f"Unsupported compact CLO layout {c0}/{c1}/{c2}/{c3}: {path}"
        )

    pre = np.array(
        [
            struct.unpack_from("<d", data, off)[0]
            for off in (0x18, 0x20, 0x28, 0x30, 0x38)
        ],
        dtype=np.float64,
    )
    post = np.array(
        [
            struct.unpack_from("<d", data, off)[0]
            for off in (0x40, 0x48, 0x50, 0x58, 0x60)
        ],
        dtype=np.float64,
    )
    pk = np.array(struct.unpack_from("<ffff", data, 0x68), dtype=np.float64)
    coeffs = np.frombuffer(
        data,
        dtype="<f4",
        count=A_TAPS + B_TAPS,
        offset=COEFF_BASE,
    ).astype(np.float64)
    return CompactClo(
        str(path),
        pre,
        coeffs[:A_TAPS].copy(),
        pk,
        post,
        coeffs[A_TAPS:].copy(),
    )


def render_compact_clo(x: np.ndarray, clo: CompactClo) -> np.ndarray:
    """Device-style render using PRE/POST/A/P-K/B stored in this exact file."""
    y = _bq(np.asarray(x, dtype=np.float64), clo.pre)
    y = _fir(y, clo.a)
    y = _preb_from_aout(
        y,
        clo.pk[0],
        clo.pk[1],
        clo.pk[2],
        clo.pk[3],
        clo.post,
    )
    return _fir(y, clo.b)


# ---------------------------------------------------------------------------
# Controlled held-out material / CLO discovery
# ---------------------------------------------------------------------------

def _performance_key(p: Pair):
    source = p.source_sha256 or p.source_path
    if not source:
        return None
    return (
        p.dataset,
        source,
        round(float(p.start_s), 6),
        round(float(p.duration_s), 6),
    )


def _base_role_map(pairs: list[Pair], role: str) -> dict[tuple, Pair]:
    grouped: dict[tuple, list[Pair]] = {}
    for p in pairs:
        if p.role != role or p.synthetic or not _is_canonical_real_guitar(p):
            continue
        key = _performance_key(p)
        if key is not None:
            grouped.setdefault(key, []).append(p)

    out = {}
    for key, group in grouped.items():
        out[key] = min(
            group,
            key=lambda p: (
                abs(float(p.level_offset_db)),
                float(p.level_offset_db) != 0.0,
                p.task_id,
            ),
        )
    return out


def select_shared_real_material(
    model_groups: dict[str, list[Pair]],
    model_keys: list[str],
    role: str = "benchmark",
    count: int = 3,
    seed: int = 260910,
) -> tuple[list[tuple], dict[str, list[Pair]]]:
    """Choose identical underlying held-out performances for every NAM."""
    maps = {key: _base_role_map(model_groups[key], role) for key in model_keys}
    common = set.intersection(*(set(maps[key]) for key in model_keys))
    if len(common) < count:
        raise RuntimeError(
            f"Only {len(common)} shared real-{role} performances exist across the selected "
            f"models; need {count}."
        )
    ordered = sorted(
        common,
        key=lambda k: hashlib.sha256(f"{seed}|{k}".encode()).hexdigest(),
    )[:count]
    return ordered, {key: [maps[key][k] for k in ordered] for key in model_keys}


def _discover_clo(root: Path, pair: Pair, kind: str) -> Path:
    root = Path(root).expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(root)
    files = (
        [root]
        if root.is_file() and root.suffix.lower() == ".clo"
        else list(root.rglob("*.clo"))
    )
    model_id = str(pair.model_id) if pair.model_id is not None else ""

    if kind == "original":
        candidates = [
            p
            for p in files
            if "NATIVE_GP5GP50_512" in p.name.upper()
            and (not model_id or model_id in p.name)
        ]
        if not candidates:
            safe_name = _safe(pair.model_name).lower()
            candidates = [
                p
                for p in files
                if "NATIVE_GP5GP50_512" in p.name.upper()
                and safe_name in _safe(p.stem).lower()
            ]
    elif kind == "north_star":
        # Current naming: NSV2__<model_id>__<model-name>.clo
        candidates = [
            p
            for p in files
            if p.name.startswith("NSV2__")
            and (
                pair.model_key in str(p.parent)
                or (model_id and p.name.startswith(f"NSV2__{model_id}__"))
            )
        ]
        # Backward compatibility with early v2 runs.
        if not candidates:
            candidates = [
                p
                for p in files
                if p.name == "distilled.clo"
                and (
                    pair.model_key in str(p.parent)
                    or (model_id and f"model{model_id}_" in str(p.parent))
                )
            ]
    else:
        raise ValueError(kind)

    if len(candidates) != 1:
        names = "\n  ".join(str(p) for p in candidates[:12]) or "<none>"
        raise RuntimeError(
            f"Expected exactly one {kind} CLO for {pair.model_name}; found "
            f"{len(candidates)}:\n  {names}"
        )
    return candidates[0]


# ---------------------------------------------------------------------------
# Time alignment / scalar metrics
# ---------------------------------------------------------------------------

def _nextpow2(n: int) -> int:
    return 1 << (max(1, n) - 1).bit_length()


def _best_lag(pred: np.ndarray, target: np.ndarray, maxlag: int = 256) -> int:
    n = min(len(pred), len(target))
    if n < 128:
        return 0
    p = np.asarray(pred[:n], dtype=np.float64)
    t = np.asarray(target[:n], dtype=np.float64)
    dec = max(1, n // 100000)
    ps = p[::dec]
    ts = t[::dec]
    ml = max(1, maxlag // dec)
    nf = _nextpow2(len(ps) + len(ts) - 1)
    corr = np.fft.irfft(
        np.fft.rfft(ps, nf) * np.fft.rfft(ts[::-1], nf),
        nf,
    )
    centre = len(ts) - 1
    lo = max(0, centre - ml)
    hi = min(len(ps) + len(ts) - 1, centre + ml + 1)
    return (lo + int(np.argmax(corr[lo:hi])) - centre) * dec


def _aligned_with_origin(pred: np.ndarray, target: np.ndarray):
    """Align pred to target and report the first original target sample retained."""
    n = min(len(pred), len(target))
    pred = np.asarray(pred[:n], dtype=np.float64)
    target = np.asarray(target[:n], dtype=np.float64)
    lag = _best_lag(pred, target)
    if lag >= 0:
        return pred[lag:], target[: n - lag], lag, 0
    return pred[: n + lag], target[-lag:], lag, -lag


def _aligned(pred: np.ndarray, target: np.ndarray):
    p, t, lag, _ = _aligned_with_origin(pred, target)
    return p, t, lag


def _rms(x: np.ndarray) -> float:
    return math.sqrt(float(np.mean(np.asarray(x, dtype=np.float64) ** 2)) + 1e-30)


def _db_ratio(num: float, den: float) -> float:
    return 20.0 * math.log10(max(float(num), 1e-15) / max(float(den), 1e-15))


def _pair_metrics(pred: np.ndarray, target: np.ndarray) -> dict:
    p, t, lag = _aligned(pred, target)
    den = float(np.dot(t, t))
    esr = float(np.dot(p - t, p - t) / den) if den > 1e-20 else 0.0
    level_error = _db_ratio(_rms(p), _rms(t))

    pden = float(np.dot(p, p))
    gain = float(np.dot(p, t) / pden) if pden > 1e-20 else 1.0
    if not np.isfinite(gain) or gain <= 0.0:
        gain = _rms(t) / max(_rms(p), 1e-15)
    pg = p * gain
    gm_esr = float(np.dot(pg - t, pg - t) / den) if den > 1e-20 else 0.0
    return {
        "lag_samples": int(lag),
        "esr": esr,
        "level_error_db": level_error,
        "gain_match_db": 20.0 * math.log10(max(gain, 1e-15)),
        "gain_matched_esr": gm_esr,
    }


# ---------------------------------------------------------------------------
# Full-spectrum analysis, 20 Hz -> 20 kHz
# ---------------------------------------------------------------------------

def _welch_power(x: np.ndarray, nfft: int = FULL_FFT, hop: int = FULL_HOP):
    x = np.asarray(x, dtype=np.float64)
    freqs = np.fft.rfftfreq(nfft, 1.0 / SR)
    if len(x) == 0:
        return freqs, np.zeros(nfft // 2 + 1)

    win = np.hanning(nfft)
    norm = max(float(np.dot(win, win)), 1e-30)
    acc = np.zeros(nfft // 2 + 1, dtype=np.float64)
    frames = 0

    if len(x) < nfft:
        z = np.zeros(nfft, dtype=np.float64)
        z[: len(x)] = x
        acc += np.abs(np.fft.rfft(z * win)) ** 2 / norm
        frames = 1
    else:
        starts = list(range(0, len(x) - nfft + 1, hop))
        for start in starts:
            acc += (
                np.abs(np.fft.rfft(x[start : start + nfft] * win)) ** 2 / norm
            )
            frames += 1
        final_start = len(x) - nfft
        if not starts or starts[-1] != final_start:
            acc += np.abs(np.fft.rfft(x[final_start:] * win)) ** 2 / norm
            frames += 1

    return freqs, acc / max(frames, 1)


def _band_power(freqs, power, lo: float, hi: float) -> float:
    mask = (freqs >= lo) & (freqs < hi)
    return float(np.sum(power[mask])) if np.any(mask) else 0.0


def _spectral_summary_from_power(freqs, target_power, pred_power, gain_power) -> dict:
    edges = np.geomspace(
        SPECTRUM_MIN_HZ,
        SPECTRUM_MAX_HZ,
        DENSE_LOG_BANDS + 1,
    )
    curve = []
    target_bands = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        tc = _band_power(freqs, target_power, lo, hi)
        pc = _band_power(freqs, pred_power, lo, hi)
        gc = _band_power(freqs, gain_power, lo, hi)
        target_bands.append(tc)
        curve.append(
            {
                "freq_hz": math.sqrt(lo * hi),
                "target_power": tc,
                "absolute_error_db": 10.0
                * math.log10(max(pc, 1e-30) / max(tc, 1e-30)),
                "gain_matched_error_db": 10.0
                * math.log10(max(gc, 1e-30) / max(tc, 1e-30)),
            }
        )

    max_target = max(target_bands, default=0.0)
    for row in curve:
        rel = 10.0 * math.log10(
            max(row["target_power"], 1e-30) / max(max_target, 1e-30)
        )
        row["teacher_relative_power_db"] = rel
        row["confident"] = bool(rel >= -70.0)

    broad = []
    broad_target = []
    for name, lo, hi in BANDS:
        tc = _band_power(freqs, target_power, lo, hi)
        pc = _band_power(freqs, pred_power, lo, hi)
        gc = _band_power(freqs, gain_power, lo, hi)
        broad_target.append(tc)
        broad.append(
            {
                "band": name,
                "lo_hz": lo,
                "hi_hz": hi,
                "target_power": tc,
                "absolute_error_db": 10.0
                * math.log10(max(pc, 1e-30) / max(tc, 1e-30)),
                "gain_matched_error_db": 10.0
                * math.log10(max(gc, 1e-30) / max(tc, 1e-30)),
            }
        )
    max_broad = max(broad_target, default=0.0)
    for row in broad:
        rel = 10.0 * math.log10(
            max(row["target_power"], 1e-30) / max(max_broad, 1e-30)
        )
        row["teacher_relative_power_db"] = rel
        row["confident"] = bool(rel >= -60.0)

    confident = [r for r in curve if r["confident"]]
    equal_rmse = (
        math.sqrt(
            float(
                np.mean([r["gain_matched_error_db"] ** 2 for r in confident])
            )
        )
        if confident
        else float("nan")
    )
    weights = np.array([r["target_power"] for r in confident], dtype=np.float64)
    errors = np.array(
        [r["gain_matched_error_db"] for r in confident],
        dtype=np.float64,
    )
    weighted_rmse = (
        math.sqrt(float(np.sum(weights * errors * errors) / np.sum(weights)))
        if len(weights) and float(np.sum(weights)) > 0.0
        else float("nan")
    )
    return {
        "curve": curve,
        "bands": broad,
        "gain_matched_spectral_rmse_db": equal_rmse,
        "gain_matched_spectral_energy_weighted_rmse_db": weighted_rmse,
    }


def _spectral_views(pred: np.ndarray, target: np.ndarray) -> dict:
    p, t, _ = _aligned(pred, target)
    gain = _rms(t) / max(_rms(p), 1e-15)
    freqs, target_power = _welch_power(t)
    _, pred_power = _welch_power(p)
    _, gain_power = _welch_power(p * gain)
    return _spectral_summary_from_power(
        freqs,
        target_power,
        pred_power,
        gain_power,
    )


# ---------------------------------------------------------------------------
# Low-level / fade analysis without concatenation artifacts
# ---------------------------------------------------------------------------

def _tail_indices(x: np.ndarray, frame: int = TAIL_FRAME):
    """Select low-level input frames, preferring frames shortly after loud playing."""
    x = np.asarray(x, dtype=np.float64)
    rows = []
    levels = []
    for start in range(0, len(x) - frame + 1, frame):
        db = 20.0 * math.log10(max(_rms(x[start : start + frame]), 1e-15))
        rows.append((start, start + frame, db))
        levels.append(db)
    if not levels:
        return []

    reference = float(np.percentile(levels, 95))
    candidates = [
        (a, b, db, i)
        for i, (a, b, db) in enumerate(rows)
        if reference - 50.0 <= db <= reference - 20.0 and db >= -85.0
    ]
    if not candidates:
        return []

    # A fade/decay is more informative than arbitrary quiet playing. Prefer a
    # low-level frame if the previous ~1 s contained normal playing. If a clip
    # has no such pattern, fall back to all usable low-level frames.
    lookback = max(1, int(round(SR / frame)))
    preferred = []
    for a, b, _db, i in candidates:
        lo = max(0, i - lookback)
        if any(levels[j] >= reference - 15.0 for j in range(lo, i)):
            preferred.append((a, b))
    return (
        preferred
        if len(preferred) >= 3
        else [(a, b) for a, b, _db, _i in candidates]
    )


def _mapped_ranges(pred: np.ndarray, target: np.ndarray, ranges):
    """Align once, then map original target/input ranges into aligned arrays."""
    p, t, lag, target_origin = _aligned_with_origin(pred, target)
    target_end = target_origin + len(t)
    mapped = []
    for start, end in ranges:
        a0 = max(start, target_origin)
        b0 = min(end, target_end)
        if b0 <= a0:
            continue
        a = a0 - target_origin
        b = b0 - target_origin
        mapped.append((a0, b0, a, b))
    return p, t, lag, mapped


def _range_metrics_aligned(p: np.ndarray, t: np.ndarray, mapped) -> dict:
    target_energy = 0.0
    pred_energy = 0.0
    error_energy = 0.0
    cross = 0.0
    samples = 0
    for _oa, _ob, a, b in mapped:
        pp = p[a:b]
        tt = t[a:b]
        target_energy += float(np.dot(tt, tt))
        pred_energy += float(np.dot(pp, pp))
        error_energy += float(np.dot(pp - tt, pp - tt))
        cross += float(np.dot(pp, tt))
        samples += len(pp)
    if samples == 0 or target_energy <= 1e-20:
        return {
            "esr": float("nan"),
            "level_error_db": float("nan"),
            "gain_match_db": 0.0,
            "gain_matched_esr": float("nan"),
        }

    gain = cross / pred_energy if pred_energy > 1e-20 else 1.0
    if not np.isfinite(gain) or gain <= 0.0:
        gain = math.sqrt(target_energy / max(pred_energy, 1e-30))

    gm_error = 0.0
    for _oa, _ob, a, b in mapped:
        d = p[a:b] * gain - t[a:b]
        gm_error += float(np.dot(d, d))

    return {
        "esr": error_energy / target_energy,
        "level_error_db": 10.0
        * math.log10(max(pred_energy, 1e-30) / max(target_energy, 1e-30)),
        "gain_match_db": 20.0 * math.log10(max(gain, 1e-15)),
        "gain_matched_esr": gm_error / target_energy,
    }


def _range_power(x: np.ndarray, mapped, nfft: int = TAIL_FFT):
    """Average independently windowed range spectra; never stitch ranges together."""
    acc = np.zeros(nfft // 2 + 1, dtype=np.float64)
    used = 0
    for _oa, _ob, a, b in mapped:
        seg = np.asarray(x[a:b], dtype=np.float64)
        if len(seg) < 128:
            continue
        win = np.hanning(len(seg))
        norm = max(float(np.dot(win, win)), 1e-30)
        acc += np.abs(np.fft.rfft(seg * win, nfft)) ** 2 / norm
        used += 1
    if used:
        acc /= used
    return np.fft.rfftfreq(nfft, 1.0 / SR), acc


def _range_rms_gain(p: np.ndarray, t: np.ndarray, mapped) -> float:
    pe = 0.0
    te = 0.0
    for _oa, _ob, a, b in mapped:
        pe += float(np.dot(p[a:b], p[a:b]))
        te += float(np.dot(t[a:b], t[a:b]))
    return math.sqrt(te / max(pe, 1e-30)) if te > 0.0 else 1.0


def _spectral_views_ranges(p: np.ndarray, t: np.ndarray, mapped) -> dict:
    gain = _range_rms_gain(p, t, mapped)
    freqs, target_power = _range_power(t, mapped)
    _, pred_power = _range_power(p, mapped)
    _, gain_power = _range_power(p * gain, mapped)
    return _spectral_summary_from_power(
        freqs,
        target_power,
        pred_power,
        gain_power,
    )


def _tail_nonlinear_residuals(x, p, t, mapped):
    """Framewise diagnostic so disjoint tails do not create fake FIR residuals."""
    target_rows = []
    pred_rows = []
    for oa, ob, a, b in mapped:
        inp = np.asarray(x[oa:ob], dtype=np.float64)
        tt = np.asarray(t[a:b], dtype=np.float64)
        pp = np.asarray(p[a:b], dtype=np.float64)
        n = min(len(inp), len(tt), len(pp))
        if n < 1024:
            continue
        target_rows.append(_linear_residual_db(inp[:n], tt[:n]))
        pred_rows.append(_linear_residual_db(inp[:n], pp[:n]))
    if not target_rows:
        return None, None
    return float(np.mean(target_rows)), float(np.mean(pred_rows))


def _tail_metrics(x, target, pred) -> dict | None:
    ranges = _tail_indices(x)
    if sum(b - a for a, b in ranges) < int(0.25 * SR):
        return None

    p, t, lag, mapped = _mapped_ranges(pred, target, ranges)
    seconds = sum(b - a for _oa, _ob, a, b in mapped) / float(SR)
    if seconds < 0.25:
        return None

    basic = _range_metrics_aligned(p, t, mapped)
    spec = _spectral_views_ranges(p, t, mapped)
    target_nl, pred_nl = _tail_nonlinear_residuals(x, p, t, mapped)
    return {
        "seconds": seconds,
        "frames": len(mapped),
        "lag_samples": int(lag),
        "esr": basic["esr"],
        "level_error_db": basic["level_error_db"],
        "gain_matched_esr": basic["gain_matched_esr"],
        "gain_matched_spectral_rmse_db": spec[
            "gain_matched_spectral_rmse_db"
        ],
        "gain_matched_spectral_energy_weighted_rmse_db": spec[
            "gain_matched_spectral_energy_weighted_rmse_db"
        ],
        "curve": spec["curve"],
        "bands": spec["bands"],
        "nam_nonlinear_residual_db": target_nl,
        "clo_nonlinear_residual_db": pred_nl,
    }


# ---------------------------------------------------------------------------
# Aggregation / presentation
# ---------------------------------------------------------------------------

def _mean(rows, key):
    vals = [
        float(r[key])
        for r in rows
        if r.get(key) is not None and np.isfinite(r.get(key))
    ]
    return float(np.mean(vals)) if vals else None


def _aggregate_curve(
    per_clip_specs: list[dict],
    system_key: str,
    field: str,
) -> list[dict]:
    if not per_clip_specs:
        return []
    count = len(per_clip_specs[0][system_key]["curve"])
    out = []
    for i in range(count):
        values = []
        target_rel = []
        confident = 0
        for spec in per_clip_specs:
            row = spec[system_key]["curve"][i]
            target_rel.append(row["teacher_relative_power_db"])
            if row["confident"]:
                values.append(row[field])
                confident += 1
        out.append(
            {
                "freq_hz": per_clip_specs[0][system_key]["curve"][i]["freq_hz"],
                field: float(np.mean(values)) if values else None,
                "confident_clips": confident,
                "teacher_relative_power_db_mean": float(np.mean(target_rel)),
            }
        )
    return out


def _aggregate_bands(per_clip_specs: list[dict], system_key: str) -> list[dict]:
    out = []
    for index, (name, lo, hi) in enumerate(BANDS):
        rows = [spec[system_key]["bands"][index] for spec in per_clip_specs]
        confident = [r for r in rows if r["confident"]]
        source = confident or rows
        out.append(
            {
                "band": name,
                "lo_hz": lo,
                "hi_hz": hi,
                "absolute_error_db": float(
                    np.mean([r["absolute_error_db"] for r in source])
                ),
                "gain_matched_error_db": float(
                    np.mean([r["gain_matched_error_db"] for r in source])
                ),
                "confident_clips": len(confident),
                "teacher_relative_power_db_mean": float(
                    np.mean([r["teacher_relative_power_db"] for r in rows])
                ),
            }
        )
    return out


def _plot_spectrum(
    path: Path,
    title: str,
    original_curve,
    north_curve,
    field: str,
) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return
    fig, ax = plt.subplots(figsize=(10, 5))
    for label, curve in (
        ("Original NamToClo", original_curve),
        ("North Star v2", north_curve),
    ):
        x = [r["freq_hz"] for r in curve if r.get(field) is not None]
        y = [r[field] for r in curve if r.get(field) is not None]
        ax.plot(x, y, label=label)
    ax.axhline(0.0, linewidth=1.0)
    ax.set_xscale("log")
    ax.set_xlim(SPECTRUM_MIN_HZ, SPECTRUM_MAX_HZ)
    ax.set_xlabel("Frequency (Hz)")
    ax.set_ylabel("Error vs NAM teacher (dB)")
    ax.set_title(title)
    ax.grid(True, which="both", alpha=0.25)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def _summary_metrics(clip_rows, system: str, specs: list[dict]) -> dict:
    prefix = f"{system}_"
    tails = [r for r in clip_rows if r.get(prefix + "tail_esr") is not None]
    return {
        "mean_esr": _mean(clip_rows, prefix + "esr"),
        "mean_abs_level_error_db": _mean(
            [
                {"v": abs(float(r[prefix + "level_error_db"]))}
                for r in clip_rows
            ],
            "v",
        ),
        "mean_signed_level_error_db": _mean(
            clip_rows,
            prefix + "level_error_db",
        ),
        "mean_gain_matched_esr": _mean(
            clip_rows,
            prefix + "gain_matched_esr",
        ),
        "mean_gain_matched_spectral_rmse_db": float(
            np.mean(
                [
                    s[system]["gain_matched_spectral_rmse_db"]
                    for s in specs
                ]
            )
        ),
        "mean_gain_matched_spectral_energy_weighted_rmse_db": float(
            np.mean(
                [
                    s[system][
                        "gain_matched_spectral_energy_weighted_rmse_db"
                    ]
                    for s in specs
                ]
            )
        ),
        "mean_tail_esr": _mean(tails, prefix + "tail_esr"),
        "mean_tail_gain_matched_esr": _mean(
            tails,
            prefix + "tail_gain_matched_esr",
        ),
        "mean_tail_gain_matched_spectral_rmse_db": _mean(
            tails,
            prefix + "tail_spectral_rmse_db",
        ),
        "mean_abs_nonlinear_residual_delta_db": _mean(
            [
                {"v": abs(float(r[prefix + "nonlinear_residual_delta_db"]))}
                for r in clip_rows
            ],
            "v",
        ),
    }


def evaluate_model(
    pairs: list[Pair],
    original_path: Path,
    north_path: Path,
    directory: Path,
) -> dict:
    original = read_compact_clo(original_path)
    north = read_compact_clo(north_path)
    directory.mkdir(parents=True, exist_ok=True)

    clip_rows = []
    spectra = []
    tail_spectra = []
    preview_written = False

    for clip_index, pair in enumerate(pairs):
        x, target, _ = read_pair(pair)
        pred_original = render_compact_clo(x, original)
        pred_north = render_compact_clo(x, north)

        original_metrics = _pair_metrics(pred_original, target)
        north_metrics = _pair_metrics(pred_north, target)
        original_spec = _spectral_views(pred_original, target)
        north_spec = _spectral_views(pred_north, target)
        original_tail = _tail_metrics(x, target, pred_original)
        north_tail = _tail_metrics(x, target, pred_north)

        target_nl = _linear_residual_db(x, target)
        original_nl = _linear_residual_db(x, pred_original)
        north_nl = _linear_residual_db(x, pred_north)

        def tail_delta(tail):
            if not tail:
                return None
            a = tail.get("nam_nonlinear_residual_db")
            b = tail.get("clo_nonlinear_residual_db")
            return None if a is None or b is None else b - a

        row = {
            "clip": clip_index,
            "task_id": pair.task_id,
            "dataset": pair.dataset,
            "source": Path(pair.source_path).name,
            "start_s": pair.start_s,
            "duration_s": pair.duration_s,
            "original_esr": original_metrics["esr"],
            "original_level_error_db": original_metrics["level_error_db"],
            "original_gain_match_db": original_metrics["gain_match_db"],
            "original_gain_matched_esr": original_metrics["gain_matched_esr"],
            "north_esr": north_metrics["esr"],
            "north_level_error_db": north_metrics["level_error_db"],
            "north_gain_match_db": north_metrics["gain_match_db"],
            "north_gain_matched_esr": north_metrics["gain_matched_esr"],
            "nam_nonlinear_residual_db": target_nl,
            "original_nonlinear_residual_db": original_nl,
            "original_nonlinear_residual_delta_db": original_nl - target_nl,
            "north_nonlinear_residual_db": north_nl,
            "north_nonlinear_residual_delta_db": north_nl - target_nl,
            "original_tail_esr": (
                original_tail["esr"] if original_tail else None
            ),
            "original_tail_gain_matched_esr": (
                original_tail["gain_matched_esr"] if original_tail else None
            ),
            "original_tail_spectral_rmse_db": (
                original_tail["gain_matched_spectral_rmse_db"]
                if original_tail
                else None
            ),
            "original_tail_nonlinear_residual_delta_db": tail_delta(
                original_tail
            ),
            "north_tail_esr": north_tail["esr"] if north_tail else None,
            "north_tail_gain_matched_esr": (
                north_tail["gain_matched_esr"] if north_tail else None
            ),
            "north_tail_spectral_rmse_db": (
                north_tail["gain_matched_spectral_rmse_db"]
                if north_tail
                else None
            ),
            "north_tail_nonlinear_residual_delta_db": tail_delta(north_tail),
            "tail_seconds": max(
                original_tail["seconds"] if original_tail else 0.0,
                north_tail["seconds"] if north_tail else 0.0,
            ),
        }
        clip_rows.append(row)
        spectra.append({"original": original_spec, "north": north_spec})
        if original_tail and north_tail:
            tail_spectra.append({"original": original_tail, "north": north_tail})

        if not preview_written:
            for tag, z in (
                ("input", x),
                ("nam", target),
                ("original", pred_original),
                ("north_star_v2", pred_north),
            ):
                sf.write(
                    directory / f"preview_{tag}.wav",
                    np.asarray(z, np.float32),
                    SR,
                    subtype="FLOAT",
                )
            preview_written = True

    original_abs = _aggregate_curve(
        spectra,
        "original",
        "absolute_error_db",
    )
    north_abs = _aggregate_curve(spectra, "north", "absolute_error_db")
    original_gm = _aggregate_curve(
        spectra,
        "original",
        "gain_matched_error_db",
    )
    north_gm = _aggregate_curve(spectra, "north", "gain_matched_error_db")
    original_bands = _aggregate_bands(spectra, "original")
    north_bands = _aggregate_bands(spectra, "north")

    spectrum_rows = []
    for oa, na, og, ng in zip(original_abs, north_abs, original_gm, north_gm):
        spectrum_rows.append(
            {
                "freq_hz": oa["freq_hz"],
                "original_absolute_error_db": oa["absolute_error_db"],
                "north_absolute_error_db": na["absolute_error_db"],
                "original_gain_matched_error_db": og[
                    "gain_matched_error_db"
                ],
                "north_gain_matched_error_db": ng["gain_matched_error_db"],
                "original_confident_clips": oa["confident_clips"],
                "north_confident_clips": na["confident_clips"],
                "teacher_relative_power_db_mean": oa[
                    "teacher_relative_power_db_mean"
                ],
            }
        )

    band_rows = []
    for original_band, north_band in zip(original_bands, north_bands):
        band_rows.append(
            {
                "band": original_band["band"],
                "lo_hz": original_band["lo_hz"],
                "hi_hz": original_band["hi_hz"],
                "original_absolute_error_db": original_band["absolute_error_db"],
                "north_absolute_error_db": north_band["absolute_error_db"],
                "original_gain_matched_error_db": original_band[
                    "gain_matched_error_db"
                ],
                "north_gain_matched_error_db": north_band[
                    "gain_matched_error_db"
                ],
                "confident_clips": min(
                    original_band["confident_clips"],
                    north_band["confident_clips"],
                ),
                "teacher_relative_power_db_mean": original_band[
                    "teacher_relative_power_db_mean"
                ],
            }
        )

    tail_spectrum_rows = []
    tail_band_rows = []
    tail_original_gm = []
    tail_north_gm = []
    if tail_spectra:
        tail_original_abs = _aggregate_curve(
            tail_spectra,
            "original",
            "absolute_error_db",
        )
        tail_north_abs = _aggregate_curve(
            tail_spectra,
            "north",
            "absolute_error_db",
        )
        tail_original_gm = _aggregate_curve(
            tail_spectra,
            "original",
            "gain_matched_error_db",
        )
        tail_north_gm = _aggregate_curve(
            tail_spectra,
            "north",
            "gain_matched_error_db",
        )
        tail_original_bands = _aggregate_bands(tail_spectra, "original")
        tail_north_bands = _aggregate_bands(tail_spectra, "north")

        for oa, na, og, ng in zip(
            tail_original_abs,
            tail_north_abs,
            tail_original_gm,
            tail_north_gm,
        ):
            tail_spectrum_rows.append(
                {
                    "freq_hz": oa["freq_hz"],
                    "original_absolute_error_db": oa["absolute_error_db"],
                    "north_absolute_error_db": na["absolute_error_db"],
                    "original_gain_matched_error_db": og[
                        "gain_matched_error_db"
                    ],
                    "north_gain_matched_error_db": ng[
                        "gain_matched_error_db"
                    ],
                    "confident_clips": min(
                        oa["confident_clips"],
                        na["confident_clips"],
                    ),
                    "teacher_relative_power_db_mean": oa[
                        "teacher_relative_power_db_mean"
                    ],
                }
            )

        for original_band, north_band in zip(
            tail_original_bands,
            tail_north_bands,
        ):
            tail_band_rows.append(
                {
                    "band": original_band["band"],
                    "lo_hz": original_band["lo_hz"],
                    "hi_hz": original_band["hi_hz"],
                    "original_absolute_error_db": original_band[
                        "absolute_error_db"
                    ],
                    "north_absolute_error_db": north_band[
                        "absolute_error_db"
                    ],
                    "original_gain_matched_error_db": original_band[
                        "gain_matched_error_db"
                    ],
                    "north_gain_matched_error_db": north_band[
                        "gain_matched_error_db"
                    ],
                    "confident_clips": min(
                        original_band["confident_clips"],
                        north_band["confident_clips"],
                    ),
                    "teacher_relative_power_db_mean": original_band[
                        "teacher_relative_power_db_mean"
                    ],
                }
            )

    summary = {
        "model_name": pairs[0].model_name,
        "model_key": pairs[0].model_key,
        "model_id": pairs[0].model_id,
        "tone_id": pairs[0].tone_id,
        "original_clo": str(original_path),
        "north_star_v2_clo": str(north_path),
        "pk": {
            "original": original.pk,
            "north_star_v2": north.pk,
        },
        "material": [
            {
                "task_id": pair.task_id,
                "dataset": pair.dataset,
                "source": pair.source_path,
                "source_sha256": pair.source_sha256,
                "start_s": pair.start_s,
                "duration_s": pair.duration_s,
                "level_offset_db": pair.level_offset_db,
            }
            for pair in pairs
        ],
        "original": _summary_metrics(clip_rows, "original", spectra),
        "north_star_v2": _summary_metrics(clip_rows, "north", spectra),
        "bands": band_rows,
        "spectrum": spectrum_rows,
        "tail_bands": tail_band_rows,
        "tail_spectrum": tail_spectrum_rows,
        "clip_metrics": clip_rows,
        "analysis": {
            "sample_rate": SR,
            "spectrum_hz": [SPECTRUM_MIN_HZ, SPECTRUM_MAX_HZ],
            "dense_log_bands": DENSE_LOG_BANDS,
            "full_welch_fft": FULL_FFT,
            "teacher_confidence_floor_db": -70.0,
            "broad_band_confidence_floor_db": -60.0,
            "tail_frame_samples": TAIL_FRAME,
            "tail_fft": TAIL_FFT,
            "tail_definition": (
                "4096-sample input frames 20-50 dB below each clip's "
                "95th-percentile input RMS, excluding <-85 dBFS; frames shortly "
                "after normal playing are preferred. Tail spectra are windowed/"
                "averaged per frame and are never concatenated, avoiding artificial "
                "boundary HF energy."
            ),
            "benchmark_only": True,
            "same_underlying_performances_across_models": True,
            "nonlinear_residual_is_diagnostic_only": True,
        },
    }

    _dump(directory / "summary.json", summary)
    _write_csv(directory / "per_clip.csv", clip_rows)
    _write_csv(directory / "spectrum.csv", spectrum_rows)
    _write_csv(directory / "bands.csv", band_rows)
    _write_csv(directory / "tail_spectrum.csv", tail_spectrum_rows)
    _write_csv(directory / "tail_bands.csv", tail_band_rows)
    _plot_spectrum(
        directory / "spectrum_absolute.png",
        f"{pairs[0].model_name} — absolute spectrum vs NAM",
        original_abs,
        north_abs,
        "absolute_error_db",
    )
    _plot_spectrum(
        directory / "spectrum_gain_matched.png",
        f"{pairs[0].model_name} — gain-matched spectrum vs NAM",
        original_gm,
        north_gm,
        "gain_matched_error_db",
    )
    if tail_spectra:
        _plot_spectrum(
            directory / "tail_spectrum_gain_matched.png",
            f"{pairs[0].model_name} — low-level/tail spectrum vs NAM",
            tail_original_gm,
            tail_north_gm,
            "gain_matched_error_db",
        )
    return summary


def _print_model_summary(summary: dict) -> None:
    original = summary["original"]
    north = summary["north_star_v2"]
    print(f"\n=== {summary['model_name']} ===")
    print("                         original      north-star-v2")
    print(
        f"mean ESR                 {original['mean_esr']:10.4f}   "
        f"{north['mean_esr']:10.4f}"
    )
    print(
        f"gain-matched ESR         {original['mean_gain_matched_esr']:10.4f}   "
        f"{north['mean_gain_matched_esr']:10.4f}"
    )
    print(
        f"signed level error       {original['mean_signed_level_error_db']:+10.2f}   "
        f"{north['mean_signed_level_error_db']:+10.2f} dB"
    )
    print(
        f"spectral RMSE            "
        f"{original['mean_gain_matched_spectral_rmse_db']:10.2f}   "
        f"{north['mean_gain_matched_spectral_rmse_db']:10.2f} dB"
    )
    if (
        original["mean_tail_esr"] is not None
        and north["mean_tail_esr"] is not None
    ):
        print(
            f"tail ESR                 {original['mean_tail_esr']:10.4f}   "
            f"{north['mean_tail_esr']:10.4f}"
        )
        print(
            f"tail spectral RMSE       "
            f"{original['mean_tail_gain_matched_spectral_rmse_db']:10.2f}   "
            f"{north['mean_tail_gain_matched_spectral_rmse_db']:10.2f} dB"
        )
    print(
        f"nonlinear residual |Δ|   "
        f"{original['mean_abs_nonlinear_residual_delta_db']:10.2f}   "
        f"{north['mean_abs_nonlinear_residual_delta_db']:10.2f} dB  DIAGNOSTIC"
    )
    print("gain-matched band error vs NAM (dB; + = more, - = less):")
    for row in summary["bands"]:
        if row["confident_clips"] <= 0:
            continue
        print(
            f"  {row['lo_hz']:5.0f}-{row['hi_hz']:5.0f} Hz  "
            f"orig {row['original_gain_matched_error_db']:+6.2f}   "
            f"NSv2 {row['north_gain_matched_error_db']:+6.2f}   "
            f"{row['band']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Three-way held-out evaluator: authoritative cached NAMCore teacher target "
            "vs ordinary NamToClo compact GP5/GP50 CLO vs frozen North Star v2 CLO."
        )
    )
    parser.add_argument("--teacher-root", default="~/NamtoCloTeacherDataset")
    parser.add_argument(
        "--original-root",
        required=True,
        help=(
            "Directory containing ordinary NamToClo "
            "*_NATIVE_GP5GP50_512.clo files."
        ),
    )
    parser.add_argument(
        "--north-star-root",
        required=True,
        help=(
            "North Star v2 output root containing NSV2__*.clo "
            "(or legacy distilled.clo)."
        ),
    )
    parser.add_argument("--output", default="~/NamtoCloThreeWayEval")
    parser.add_argument("--model-regex", required=True)
    parser.add_argument("--benchmark-count", type=int, default=3)
    parser.add_argument("--seed", type=int, default=260910)
    args = parser.parse_args()
    if args.benchmark_count < 1:
        raise SystemExit("--benchmark-count must be >= 1")

    teacher_root = Path(args.teacher_root).expanduser()
    groups = group_models(load_pairs(teacher_root))
    rx = re.compile(args.model_regex, re.I)
    keys = [
        key
        for key in sorted(groups)
        if rx.search(groups[key][0].model_name)
    ]
    if not keys:
        raise SystemExit("No teacher models matched --model-regex")

    shared_keys, selected = select_shared_real_material(
        groups,
        keys,
        role="benchmark",
        count=args.benchmark_count,
        seed=args.seed,
    )

    print("THREE-WAY EVALUATION — benchmark data only; no coefficients are changed")
    print("selected models:", *[groups[k][0].model_name for k in keys], sep="\n  ")
    print("shared held-out performances:")
    for index, _ in enumerate(shared_keys):
        p0 = selected[keys[0]][index]
        print(
            f"  {p0.dataset}: {Path(p0.source_path).name} @ "
            f"{p0.start_s:.2f}s ({p0.duration_s:.2f}s)"
        )
    print(
        "spectrum: 20 Hz-20 kHz, 144 dense log bands + broad summaries; "
        "low-energy teacher regions flagged"
    )
    print(
        "tail: independently windowed low-level/fade frames; "
        "no stitched-frame HF artifacts"
    )
    print("warming GP50 renderer...")
    warm()

    output = Path(args.output).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    results = []
    for key in keys:
        p0 = groups[key][0]
        original = _discover_clo(Path(args.original_root), p0, "original")
        north = _discover_clo(
            Path(args.north_star_root),
            p0,
            "north_star",
        )
        directory = output / _safe(p0.model_key + "__" + p0.model_name)
        summary = evaluate_model(selected[key], original, north, directory)
        results.append(summary)
        _print_model_summary(summary)

    root_summary = {
        "method": "north-star-v2-three-way-heldout-evaluation-v2",
        "north_star_document": "ENGINE_V2_RESEARCH_NORTH_STAR.md",
        "benchmark_only": True,
        "same_underlying_performances_across_models": True,
        "shared_performance_keys": [list(key) for key in shared_keys],
        "results": results,
    }
    _dump(output / "summary.json", root_summary)
    print("\nWrote:", output / "summary.json")
    print("No fitter coefficients were changed; benchmark remains evaluation-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
