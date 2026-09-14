"""Python reader for the GP-5/GP-50 "compact" VTSI CLO format.

This is a byte-for-byte port of `parseModel` in `src/core/clo_refiner.cpp`
(read that function, not this docstring, if the two ever disagree — this file
exists so EngineV2 research code can decode a real CLO produced by the
shipped C++ engine without linking C++). Line numbers below are from
`src/core/clo_refiner.cpp` as of the EngineV2 baseline-comparison harness
(2026-09):

  - magic check "VTSI"/"HTSI"                         (clo_refiner.cpp ~L110)
  - `led()` = 8-byte little-endian double               (clo_refiner.cpp:51)
  - `lef()` = 4-byte little-endian float                (clo_refiner.cpp:50)
  - `le32()` = 4-byte little-endian uint32               (clo_refiner.cpp:49)
  - pre biquad: 5 doubles (b0,b1,b2,a1,a2) at 0x18,0x20,0x28,0x30,0x38
                                                          (clo_refiner.cpp:112)
  - post biquad: same 5-double layout at 0x40,0x48,0x50,0x58,0x60
                                                          (clo_refiner.cpp:113)
  - pp,pn,kp,kn: 4-byte floats at 0x68,0x6c,0x70,0x74     (clo_refiner.cpp:114)
  - sa,ca,sb,cb: 4-byte uint32 at 0x78,0x7c,0x80,0x84     (clo_refiner.cpp:115)
  - kCoeffBase = 0x88                                     (clo_refiner.cpp:20)
  - A[i] = lef(kCoeffBase + 4*(sa+i)), B[i] = lef(kCoeffBase + 4*(sb+i))
                                                          (clo_refiner.cpp:116-117)

Note the biquad coefficients are 8-byte doubles (`led`) while pp/pn/kp/kn and
the A/B FIR coefficients are 4-byte floats (`lef`) -- confirmed by reading the
literal helper bodies in clo_refiner.cpp, not assumed. This matches exactly
what `distiller_v2_dsp.write_clo` itself writes (`<d` for the two biquads,
`<f` for pk and the FIR coefficients), which is the independent ground truth
this module's self-test round-trips against.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

KCOEFF_BASE = 0x88


@dataclass
class CloModel:
    pre: np.ndarray  # [b0,b1,b2,a1,a2], float64
    post: np.ndarray  # [b0,b1,b2,a1,a2], float64
    pp: float
    pn: float
    kp: float
    kn: float
    a: np.ndarray  # A_TAPS float64
    b: np.ndarray  # B_TAPS float64


def read_clo(path) -> CloModel:
    path = Path(path)
    d = path.read_bytes()
    if len(d) < KCOEFF_BASE or d[:4] not in (b"VTSI", b"HTSI"):
        raise ValueError(f"Not a compact VTSI/HTSI CLO: {path}")

    def led(off: int) -> float:
        return struct.unpack_from("<d", d, off)[0]

    def lef(off: int) -> float:
        return struct.unpack_from("<f", d, off)[0]

    def le32(off: int) -> int:
        return struct.unpack_from("<I", d, off)[0]

    pre = np.array([led(0x18), led(0x20), led(0x28), led(0x30), led(0x38)], dtype=np.float64)
    post = np.array([led(0x40), led(0x48), led(0x50), led(0x58), led(0x60)], dtype=np.float64)
    pp = lef(0x68)
    pn = lef(0x6C)
    kp = lef(0x70)
    kn = lef(0x74)
    sa, ca, sb, cb = le32(0x78), le32(0x7C), le32(0x80), le32(0x84)

    if ca == 0 or cb == 0:
        raise ValueError(f"Truncated CLO coefficients (ca={ca}, cb={cb}): {path}")
    need = KCOEFF_BASE + 4 * max(sa + ca, sb + cb)
    if need > len(d):
        raise ValueError(f"Truncated CLO coefficients (need {need} bytes, have {len(d)}): {path}")

    a = np.array([lef(KCOEFF_BASE + 4 * (sa + i)) for i in range(ca)], dtype=np.float64)
    b = np.array([lef(KCOEFF_BASE + 4 * (sb + i)) for i in range(cb)], dtype=np.float64)

    return CloModel(pre=pre, post=post, pp=pp, pn=pn, kp=kp, kn=kn, a=a, b=b)
