# Conversion Quality & Investigation

This file documents the conversion quality investigation (2026-08 through 2026-09), shipping decisions, and known limitations.

## What's shipping (GP-5/GP-50)

**Verified production path** (`convertNamToClo`'s GP-5/GP-50 Tone Match gate):

- **Direct Block B least-squares solve** (`solveBlockBLeastSquares`, `clo_refiner.cpp`): replaces older "correction IR convolved into B" approach. Verified as a large, real spectral/waveform fidelity win across broad amp corpus (clean through extreme-high-gain, multiple brands) — never regresses.
- **Multi-level shared Block B solve** (`sweepKAndSolveSharedB` with `kMultiplier=1.0`): solves B jointly across a 6-level gain sweep of the Tone Match reference clip instead of one operating point. Verified as 10-19% dynamics/fidelity improvement on true GP-5/GP-50 512-tap format (measured against real hardware output, not mislabeled files).
- All three candidates (do nothing / correction-IR / direct B solve / multi-level B solve) are compared on the same target before shipping — the multi-level candidate wins by actually scoring better.
- Final linear gain-match step and `fitPk()`/`fitAB()` core fitting logic are unchanged from reverse-engineered original.

**Benchmark validation:** See `PRIORITY_1_BENCHMARK.md` for detailed comparison of official-style baseline (no Tone Match) vs. Tone Match variant across clean/moderate/high-gain amps. Key result: Tone Match produces 1-2 dB RMS improvement on high-gain models, consistent across fit/selection/held-out material, with clean models confirming representational plateau.

## Tried and NOT shipped

- **Dynamics-aware P/K search** (`searchPkForDynamics`): coordinate-descent search to reopen P/K shaper specifically for high/extreme-gain amps. Looked like strong automated-metric win (zero-anchored relative-dynamics RMS error) but real hardware listening overruled it: converted patches came out substantially quieter than official SnapTone conversion, with guitar volume mostly just making them quieter rather than cleaning up distortion. `NativeConverterConfig::dynamicsAwareFitting` now defaults to `false` with no GUI control. Research code remains for opt-in re-evaluation with new hardware-listening evidence.

- **Alternative stimulus WAV** (2026-09-05): tested an alternative `nam_input_wav.wav` via actual production path (`--valeton-comparison` with `Auto` Tone Match reference) across 8 amps, full gain range. Result: neutral on most, small win on 2, real fidelity regression on 1 (JCM800 High Gain: correlation -0.03, ESR +12.8%). Shipped stimulus unchanged.

- **EQ Match / post-fit spectral correction** (re-tested 2026-09-16): a gentle, clamped, smoothed post-fit EQ into Block B. Originally measured on Meshuggah with Tone Match on; re-ran via new `namtoclo eq-match-batch` CLI across 57 `.nam` files, 114 total runs (with/without Tone Match). **Result: broad, consistent regression** — waveform ESR and correlation vs. Full A2 got worse in ~90% of amps (Tone Match on) and clear majority with it off. Not shipped. Why a static, gain-neutral correction diverges further: (1) real error is level/content-dependent (nonlinearity artifact, not fixed tonal tilt); (2) minimum-phase FIR reconstruction introduces group-delay/phase behavior; (3) RMS-renormalization is global scalar, doesn't respect per-band shape.

- **Alternative B-side corrections** (linear filter-bank, biquad cascade, frequency-weighted B solve, multi-clip B solve) — all measured, all either no real win or made things worse.

## Known, unfixed limitation

**Hard-driven amp harmonic asymmetry:** on crunch through extreme-gain amps, the shared oversampled P/K nonlinearity produces even-harmonic suppression and odd-harmonic over-production (worse at high frequency, consistent with aliasing rather than organic distortion). Isolated-stage testing traced this specifically to U2 upsampling (88.2kHz→176.4kHz) allpass stage with severe, monotonic image-suppression breakdown in top ~10-20% of input band (from ~-50dB suppression down to **amplifying** the image by +26dB at extreme).

**Why not fixed:** U2's allpass coefficients and the 2-stage up/down topology are **bit-for-bit and structurally identical to Valeton's real firmware** (confirmed via `5868USB.dylib` disassembly, see INVESTIGATION.md). This is a proven architectural ceiling of the real device's single-memoryless-nonlinearity design, not a defect in this project's reconstruction. Don't re-open U1/U2 coefficient/topology changes without new evidence that a real firmware *update* changed them.

## Algorithm comparison to official (via `5868USB.dylib` disassembly)

**Confirmed identical:**
- U1/U2/D1/D2 coefficients and topology (bit-exact match)
- GP-5/GP-50 512-tap fit is native, not a truncation of 2048-tap fit (device-mode flag selects window size before fitting runs)

**Genuinely different:**
- **Tone Match has no official equivalent.** Official single-shot `namConverterCloData()` takes no reference-audio parameter. Every part of `CloRefineConfig`/Tone Match pipeline (direct Block B solve, multi-level gain-sweep, output-level match) is this project's own addition on top of reverse-engineered core.
- **Corrective IR post-processing** (user-selected IR convolved into CLO, RMS-normalized, auto-resampled at any input rate) is same kind of addition — no equivalent in official single-shot converter.

**Net difference:** not in the core NAM-fitting algorithm (that's faithfully reconstructed) — it's Tone Match and Corrective IR, which exploit having the real NAM model available at conversion time to correct residual fit error against ground truth, something the official converter cannot do at all.

## Stimulus WAV and conversion pipeline

**Conversion order** (`stimulus.cpp` → `native_converter.cpp` → `corrective_ir.cpp` → `clo_refiner.cpp`):

1. `stimulus.cpp`: 70-second mono PCM16/44.1kHz stimulus from `nam_input_wav.wav` (first 50s fixed) + original file's tail or user-supplied "Recorded Audio" WAV for final 20s (`TailMode`). Shipped stimulus was A/B tested 2026-09 against alternative; shipped version is neutral-to-better across test corpus.

2. `native_converter.cpp`/`.hpp`: reverse-engineered NAM→CLO algorithm. Renders stimulus through NAM model (via fetched `NeuralAmpModelerCore`), resamples with `r8brain`, reconstructs GP-200 1024-tap CLO format bit-for-bit. Comments cite exact disassembly addresses (e.g. `GP-200.exe 0x559d80`, `HTUSBTools.dll 0x18009ad86`) — preserve these references, they're load-bearing documentation of why math is shaped the way it is.

3. `corrective_ir.cpp`/`.hpp`: optional post-processing. Convolves user-selected corrective IR WAV into CLO after native conversion, with RMS normalization and post-gain stage. Auto-resamples IR to required 44.1kHz if supplied at different rate (2026-09-08 change).

4. `clo_refiner.cpp`/`.hpp`: optional Tone Match refinement. Fits Block B against target render (NAM rendered through stimulus, or real reference clip). For GP-5/GP-50, direct/multi-level Block B least-squares solve verified as production win (see "What's shipping" above).

Output: `<name>_NATIVE_GP200_1024.clo`, or `<name>_NATIVE_GP200_1024_TONEMATCH.clo` when Tone Match enabled (mutually exclusive).
