from __future__ import annotations

import hashlib
import json
import math
import subprocess
import urllib.request
from pathlib import Path

import numpy as np
import soundfile as sf

from build_namtoclo_teacher_dataset import (
    NamModel,
    resample_audio,
    sha256_file,
    write_float_wav,
)
from distiller_v2_data import Pair
from distiller_v2_dsp import SR


DEFAULT_STIMULUS_URL = "https://www.tone3000.com/T3K-sweep-v3.wav"
DEFAULT_STIMULUS_LEVELS_DB = (0.0, -6.0, -12.0, -18.0, -24.0)


def parse_levels(text: str) -> tuple[float, ...]:
    vals = []
    seen = set()
    for token in str(text).split(","):
        token = token.strip()
        if not token:
            continue
        value = float(token)
        if not math.isfinite(value):
            raise ValueError("stimulus levels must be finite")
        if value not in seen:
            vals.append(value)
            seen.add(value)
    if not vals:
        raise ValueError("at least one stimulus level is required")
    return tuple(vals)


def _dbamp(db: float) -> float:
    return 10.0 ** (float(db) / 20.0)


def ensure_stimulus(source: str, cache_root: Path) -> Path:
    """Resolve a local stimulus path, downloading the official T3K file if needed.

    The stimulus itself is intentionally not committed to this repository; the
    TONE3000 licence permits use but not redistribution. The actual SHA256 is
    recorded in every v4 report for reproducibility.
    """
    if source.startswith("http://") or source.startswith("https://"):
        cache_root.mkdir(parents=True, exist_ok=True)
        dest = cache_root / "T3K-sweep-v3.wav"
        if not dest.exists():
            print(f"[v4 stimulus] downloading {source}")
            req = urllib.request.Request(
                source,
                headers={"User-Agent": "NamtoClo-EngineV2/4 stimulus research"},
            )
            with urllib.request.urlopen(req, timeout=120) as r, dest.open("wb") as f:
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        path = dest
    else:
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Stimulus does not exist: {path}")

    info = sf.info(str(path))
    if info.frames <= 0 or info.samplerate <= 0:
        raise RuntimeError(f"Invalid stimulus WAV: {path}")
    return path.resolve()


def load_stimulus_mono(path: Path) -> tuple[np.ndarray, int]:
    x, sr = sf.read(str(path), dtype="float32", always_2d=True)
    if x.shape[1] == 1:
        mono = x[:, 0]
    else:
        mono = np.mean(x, axis=1, dtype=np.float64)
    mono = np.asarray(mono, dtype=np.float64)
    if not np.all(np.isfinite(mono)):
        raise RuntimeError(f"Stimulus contains NaN/Inf: {path}")
    return mono, int(sr)


def _render_nam(
    renderer: Path,
    nam: NamModel,
    native_input: Path,
    native_output: Path,
    slim_value: float = 1.0,
) -> None:
    native_output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(renderer)]
    if nam.slimmable:
        cmd += ["--slim", f"{float(slim_value):.6f}"]
    cmd += [nam.path, str(native_input), str(native_output)]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"NAMCore render failed ({proc.returncode})\n"
            f"command: {cmd}\nstdout:\n{proc.stdout[-4000:]}\n"
            f"stderr:\n{proc.stderr[-4000:]}"
        )

    ii = sf.info(str(native_input))
    oi = sf.info(str(native_output))
    if oi.frames != ii.frames or int(oi.samplerate) != int(ii.samplerate):
        raise RuntimeError(
            f"NAMCore render mismatch input={ii.frames}@{ii.samplerate}, "
            f"output={oi.frames}@{oi.samplerate}"
        )


def prepare_multilevel_teacher_audio(
    nam: NamModel,
    model_pair: Pair,
    renderer: Path,
    renderer_sha256: str,
    renderer_commit: str,
    stimulus_path: Path,
    levels_db: tuple[float, ...],
    cache_root: Path,
    slim_value: float = 1.0,
):
    """Render one canonical stimulus through one NAM at several input levels.

    Returns student-domain 44.1-kHz (input, NAM target, Pair) tuples plus a
    provenance manifest. No amplitude normalization is applied to the official
    stimulus or to NAM outputs.
    """
    base, base_sr = load_stimulus_mono(stimulus_path)
    stimulus_sha = sha256_file(stimulus_path)
    model_dir = cache_root / model_pair.model_key
    model_dir.mkdir(parents=True, exist_ok=True)

    audio = []
    level_rows = []
    for level_db in levels_db:
        ident = {
            "nam_sha256": nam.sha256,
            "stimulus_sha256": stimulus_sha,
            "level_db": float(level_db),
            "renderer_sha256": renderer_sha256,
            "renderer_commit": renderer_commit,
            "slim_value": float(slim_value) if nam.slimmable else None,
            "native_rate": int(nam.expected_sample_rate),
            "student_rate": SR,
            "v4_cache_version": 1,
        }
        task_id = hashlib.sha256(
            json.dumps(ident, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
        tag = f"L{level_db:+06.1f}dB__{task_id[:10]}"
        native_input = model_dir / "native_inputs" / f"{tag}.wav"
        native_target = model_dir / "native_targets" / f"{tag}.wav"
        student_input = model_dir / "student_44100" / "inputs" / f"{tag}.wav"
        student_target = model_dir / "student_44100" / "targets" / f"{tag}.wav"

        if not student_input.exists() or not student_target.exists():
            scaled = base * _dbamp(level_db)
            native_x = resample_audio(scaled, base_sr, nam.expected_sample_rate)
            native_input.parent.mkdir(parents=True, exist_ok=True)
            write_float_wav(native_input, native_x, nam.expected_sample_rate)
            _render_nam(renderer, nam, native_input, native_target, slim_value)

            y_native, y_sr = sf.read(
                str(native_target), dtype="float32", always_2d=False
            )
            y_native = np.asarray(y_native, dtype=np.float64)
            if y_native.ndim > 1:
                y_native = y_native[:, 0]
            if not np.all(np.isfinite(y_native)):
                raise RuntimeError("NAM stimulus render contains NaN/Inf")

            student_x = resample_audio(native_x, nam.expected_sample_rate, SR)
            student_y = resample_audio(y_native, int(y_sr), SR)
            n = min(len(student_x), len(student_y))
            student_input.parent.mkdir(parents=True, exist_ok=True)
            student_target.parent.mkdir(parents=True, exist_ok=True)
            write_float_wav(student_input, student_x[:n], SR)
            write_float_wav(student_target, student_y[:n], SR)

        x, sx = sf.read(str(student_input), dtype="float32", always_2d=False)
        y, sy = sf.read(str(student_target), dtype="float32", always_2d=False)
        if int(sx) != SR or int(sy) != SR:
            raise RuntimeError("V4 student stimulus cache must be 44.1 kHz")
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if x.ndim > 1:
            x = x[:, 0]
        if y.ndim > 1:
            y = y[:, 0]
        n = min(len(x), len(y))
        x = x[:n]
        y = y[:n]

        pair = Pair(
            task_id=task_id,
            model_key=model_pair.model_key,
            model_name=model_pair.model_name,
            tone_id=model_pair.tone_id,
            model_id=model_pair.model_id,
            nam_split=model_pair.nam_split,
            role="fit",
            dataset="tone3000_t3k_sweep_v3",
            synthetic=True,
            input_path=str(student_input),
            target_path=str(student_target),
            duration_s=float(n) / SR,
            input_id=f"t3k-sweep-v3-{level_db:+.1f}dB",
            source_path=str(stimulus_path),
            source_sha256=stimulus_sha,
            start_s=0.0,
            level_offset_db=float(level_db),
        )
        audio.append((x, y, pair))
        level_rows.append(
            {
                "task_id": task_id,
                "level_db": float(level_db),
                "duration_s": float(n) / SR,
                "student_input": str(student_input),
                "student_target": str(student_target),
                "student_input_sha256": sha256_file(student_input),
                "student_target_sha256": sha256_file(student_target),
                "input_peak": float(np.max(np.abs(x))) if n else 0.0,
                "target_peak": float(np.max(np.abs(y))) if n else 0.0,
            }
        )

    manifest = {
        "source_url_or_path": str(stimulus_path),
        "stimulus_sha256": stimulus_sha,
        "stimulus_source_sample_rate": base_sr,
        "stimulus_source_frames": int(len(base)),
        "levels_db": [float(v) for v in levels_db],
        "nam_path": nam.path,
        "nam_sha256": nam.sha256,
        "nam_expected_sample_rate": int(nam.expected_sample_rate),
        "renderer": str(renderer),
        "renderer_sha256": renderer_sha256,
        "renderer_commit": renderer_commit,
        "slim_value": float(slim_value) if nam.slimmable else None,
        "student_rate": SR,
        "no_input_normalization": True,
        "no_teacher_output_normalization": True,
        "level_variants": level_rows,
    }
    return audio, manifest
