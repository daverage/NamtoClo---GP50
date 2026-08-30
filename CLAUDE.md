# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows x64 GUI application (`NamToClo.exe`) that converts Neural Amp Modeler (`.nam`) models into `.clo` files compatible with Valeton GP-200/GP-5/GP-50 hardware, and uploads `.clo` files directly to those devices over USB MIDI. It reimplements undocumented vendor behavior (the GP-200 native NAM→CLO conversion algorithm and the GP-5/GP-50 SnapTone upload protocol) reverse-engineered from the official tools and USB-MIDI captures — this is *not* a wrapper around Valeton's own software. There is no runtime dependency on Valeton Suite.

## Build

Requires Windows x64, CMake 3.24+, and MSVC (Visual Studio C++ desktop workload). The CMake project only configures on Windows/x64 — it hard-fails otherwise.

```powershell
cmake --preset windows-x64
cmake --build build --config Release --parallel
```

The `windows-x64` preset in `CMakePresets.json` pins a Visual Studio generator version — update it to match whatever VS major version is actually installed on the machine (it does not auto-detect).

Output executable: `build/Release/NamToClo.exe`. To run a build, copy `nam_input_wav.wav` (repo root) next to the exe — the Convert tab requires it at runtime and it's not copied by the build.

There is no test suite and no linter configured in this repo.

### Dependencies fetched at configure time

`CMakeLists.txt` uses `FetchContent` to pull three third-party source trees via git at configure time (requires git + network, or local overrides):

| Dependency | Pin | Override variable |
|---|---|---|
| `NeuralAmpModelerCore` | `v0.5.4` | `-DNAM_CORE_SOURCE_DIR=<path>` |
| `r8brain-free-src` | `version-3.7` (`NTC_R8BRAIN_REF`) | `-DR8BRAIN_SOURCE_DIR=<path>` |
| SoXR (only for Ooura FFT4G `src/fft4g.c`) | `0.1.3` | `-DNTC_OOURA_SOURCE_DIR=<soxr-source-path>` |

NeuralAmpModelerCore is built as a CMake `OBJECT` library (not `STATIC`) — this is deliberate, not an oversight: NAMCore v0.5.x registers architecture parsers (WaveNet, LSTM, ConvNet, ...) via static initializers, and a normal static lib lets MSVC's linker discard parser-only translation units that nothing directly references, breaking `get_dsp()` at runtime with "No config parser registered for architecture: X". Don't change this back to `STATIC` without re-verifying every architecture still loads.

### CI

`.github/workflows/build-windows.yml` builds on every push/PR (`cmake -S . -B build -A x64`, no preset). `.github/workflows/release.yml` does the same on `v*` tags and publishes a zip (exe + README + LICENSE + THIRD_PARTY.md) as a GitHub release asset. Neither workflow uses `CMakePresets.json`.

## Architecture

All application code is under `src/`, in `namespace ntc` (GP-5-specific code additionally nests in `ntc::gp5`, GP-200 in `ntc::gp200`). `src/gui.cpp` is the single Win32 entry point/message loop and owns all three tabs; everything else is a self-contained conversion or upload module it calls into.

**Conversion pipeline** (Convert to CLO tab), in the order a conversion actually runs:

1. `stimulus.cpp` — builds the 70-second mono PCM16/44.1kHz conversion stimulus from `nam_input_wav.wav` (first 50s fixed) plus either the original file's tail or a user-supplied "Recorded Audio" WAV for the final 20s (`TailMode`).
2. `native_converter.cpp`/`.hpp` — the core reverse-engineered NAM→CLO algorithm: renders the stimulus through the NAM model (via the fetched `NeuralAmpModelerCore`), resamples with `r8brain`, and reconstructs the GP-200 1024-tap CLO byte format bit-for-bit against the original tool. Comments in this file cite the exact disassembly addresses (e.g. `GP-200.exe 0x559d80`, `HTUSBTools.dll 0x18009ad86`) the logic was reconstructed from — preserve those references when touching this file, they're load-bearing documentation of *why* the math is shaped the way it is, not incidental.
3. `corrective_ir.cpp`/`.hpp` — optional post-processing: convolves a user-selected corrective IR WAV into the CLO after native conversion, with RMS normalization and a post-gain stage.
4. `clo_refiner.cpp`/`.hpp` — optional "Tone Match" refinement: iteratively fits Block B of the CLO against a target render (NAM rendered through the same/corrected stimulus) using a minimum-phase IR solver. Slower; when enabled it replaces rather than supplements the standard output file.
5. `common.cpp`/`.hpp` — shared low-level helpers used across the above: WAV/CLO byte inspection (`CloInfo`), CRC, hex dump, UTF-8/UTF-16 path conversion, and the GP-200 CLO compare/diagnostic utilities.

Output naming convention: `<name>_NATIVE_GP200_1024.clo`, or `<name>_NATIVE_GP200_1024_TONEMATCH.clo` when Tone Match is enabled (mutually exclusive outputs, not both).

**Upload paths** (GP-200 Uploader / GP-5+GP-50 Uploader tabs) — two independent, protocol-incompatible USB-MIDI implementations, each with a `_clo_upload` (payload construction) / `_midi` (transport, device scan, ACK/retry handling) pair:

- `gp200_clo_upload.*` + `gp200_midi.*` — targets the GP-200's 10 SnapTone slots (AMP 1-5, DIST 1-5 → global slots 0-9).
- `gp5_clo_upload.*` + `gp5_midi.*` — targets GP-5 **and GP-50** (same hardware protocol family, see below), exposing only SnapTone slots 51-80 (zero-based 50-79) — see "GP-5/GP-50 slot range" below for why this isn't a temporary restriction.

The GP-5/GP-50 upload path always adapts the source CLO **in memory** immediately before transfer (extracting A128/B512 from a larger CLO into the wrapped VTSI transfer format); the file on disk is never modified. Transfer framing: 19-byte payload chunks under command `0x92`, CRC-8 (poly `0x07`), nibble-encoded SysEx, one ACK (`B2 01 00 03 14 08 00`) required per block before the next is sent, completion signaled by `CE 01 00 06 12 1B 03 00 00 00`.

### GP-5/GP-50 slot range — not a placeholder

The GP-5 and GP-50 both ship with the same SnapTone capacity: 50 factory-preloaded + 80 total, i.e. exactly 30 user-uploadable slots (51-80). This was confirmed against both reverse-engineered captures and official specs, so the `slot < 50 || slot >= 80` bound in `gp5_clo_upload.cpp` and the `for (int i = 51; i <= 80; ++i)` combo population in `gui.cpp` are correct for both devices, not a GP-5-only limitation waiting to be widened for GP-50.

What *is* still unconfirmed for GP-50 specifically: whether its wire protocol (per-block ACK bytes, final completion message) is byte-identical to GP-5's. The current code assumes it is and reuses the GP-5 upload path unchanged; on a completion timeout it surfaces the last decoded SysEx message received so real GP-50 hardware testing can confirm or correct this assumption. Don't "fix" the GP-5/GP-50 sharing by forking a separate GP-50 code path without new hardware-capture evidence that the protocols actually diverge.

### Compact CLO format (GP-5/GP-50 transfer representation)

Magic `VTSI`/`HTSI`, FIR A = 128 taps, FIR B = first 512 taps of a larger CLO's Block B, declared size `0x0A88`, payload size `0x0A00`, CRC16/MODBUS recalculated on adaptation. Preceded by a reconstructed 74-byte SnapTone wrapper (destination slot + name) for the full 2770-byte transfer payload (146 blocks: 145×19 bytes + 1×15 bytes).

### GP-5/GP-50 "pure" fit candidate — P/K search's overfitting is fixed, but it has never found a real win

`native_converter.cpp`'s `runQualityExperiments` carries a third GP-5/GP-50 candidate (`ntc::gp5::fitPureFromRender`, `gp5_optimizer.hpp`/`.cpp`) that fits Block A/B directly at the device tap budget from a neutral (flat) seed, with zero dependency on the GP-200 2048-tap fit — unlike the existing direct-fit candidate (`native_converter.cpp`'s `m5`), which seeds its A128 from whatever the 2048-tap fit converged to.

**Phase 1** (2026-08-29, P/K left untouched, see `test_assets/quality_results/*_PureCandidate/quality_experiment_results.csv`): produced a **byte-identical** `.clo` to the existing direct-fit candidate on both a clean and an extreme-high-gain NAM. `fitAB()`'s sweep/low-level/multi-level search is seed-independent for A/B when P/K and the pre/post biquads are held fixed — which they were in both candidates. Decoupling the A-seed from GP-200 alone bought nothing measurable.

**Phase 2** (2026-08-29, bounded local P/K search added, alternating with a full A/B refit — see the same directory's `*_pk_search.csv`): on 2 of 4 (NAM, submodel) combinations tested, the search found and kept a genuine in-sample loss improvement (e.g. clean Fender Full: -32%); on the other 2, a monotonicity guard correctly rejected a round that would have regressed in-sample loss after the A/B refit. **But on every combination where an improvement was kept, held-out loss (scored against real playing content) got worse, not better** — e.g. clean Fender Full: in-sample -32%, held-out +23%. This is overfitting to the 70-second synthetic conversion stimulus: both the P/K search and its round-acceptance check only ever score against the same signal `fitAB()` was fit against, never against held-out content.

**Phase 3** (2026-08-30, train/selection/held-out-benchmark split added -- see the same directory's `*_pk_search_selection_gated.csv`): `runQualityExperiments` now splits `validationClips` into a disjoint selection subset (up to 3 clips, threaded into `fitPureFromRender` as `SelectionClip`s -- gates round-acceptance) and a benchmark subset (never seen during fitting or selection -- what actually gets reported as `gp5PureHeldOutLoss`). The fix works as intended: on the *same* clean-Fender-Full case that Phase 2 shipped a phantom -32%/+23% "win" on, the search still finds that identical in-sample improvement, but the new selection gate correctly rejects it (doesn't generalize even to the small selection set) and leaves the model unchanged from baseline. Across all 4 (NAM, submodel) combinations re-tested, every round was rejected -- the search has never yet found an improvement that survives contact with real playing content, not even 3 clips' worth.

This is not a failure of the fix -- it's the fix doing its job. Phase 2's "wins" were never real; Phase 3 correctly reports zero instead of a phantom one. What it does mean: **P/K search is not a productive lever with the current ~10-clip validation corpus** (`test_assets/tone3000_inputs`). Before trying this again, either grow the real-playing corpus (roadmap item 8) or accept that this particular lever may not pay off without one. Do not wire this candidate into `convertNamToClo` -- there has never been a verified quality win from it, at any phase. See `gp5_optimizer.hpp`'s `fitPureFromRender` doc comment for the full detail.

### GP-5/GP-50 direct Block B least-squares solve — a real, verified win, but still not wired into production

`clo_refiner.cpp`'s `solveBlockBLeastSquares()` (declared in `clo_refiner.hpp`) replaces the "compute a 2048-sample correction filter, convolve it into B, implicitly truncate everything past tap 512" approach (`computeToneMatchCorrectionIr()` + `applyCorrectiveIrToB44()`) with a direct regularized frequency-domain least-squares solve for the 512 B coefficients themselves, against the same Tone Match target and tail-analysis window. Pre/A128/P-K/Post stay frozen — only B changes.

**Measured (2026-08-30)**, via `runQualityExperiments`'s held-out scoring (`gp5DirectBSolveHeldOutLoss` vs. `gp5PostToneMatchHeldOutLoss` and the pre-Tone-Match `gp5ChosenDeviceHeldOutLoss` baseline, see `test_assets/quality_results/*_PureCandidate/quality_experiment_results_b_solve_*.csv`): tested on a clean and an extreme-high-gain NAM, both submodels, and both the default synthetic stimulus and a real recorded clip as the Tone Match reference (8 combinations, held-out set always excluding whatever clip was used as the reference). **Every combination showed a large held-out win**: 54-87% loss reduction vs. the pre-Tone-Match baseline, and 60-87% vs. the existing correction-IR approach — which itself sometimes made held-out loss *worse* than doing nothing at all (e.g. clean Fender Full, default stimulus: baseline 1.637, correction-IR 1.769, direct solve 0.582).

Unlike the P/K search above, **this is not overfitting** — the win holds up on held-out real playing content with a properly excluded validation set, not just on the training window. This is currently the strongest verified quality lever found this session.

**Wired into production (2026-08-30).** `convertNamToClo`'s GP-5/GP-50 Tone Match gate (`native_converter.cpp` ~L2082-2145) now compares three candidates -- no correction, `computeToneMatchCorrectionIr`'s correction-IR, and `solveBlockBLeastSquares`'s direct solve -- and ships whichever scores lowest, extending the existing `lossPre`/`lossPost` safety gate rather than replacing it. `serializeGp5Compact` gained an optional `overrideB44` parameter so a directly-solved B can be written verbatim, skipping the normal resample+Corrective-IR+Tone-Match-IR convolution steps. Verified with 4 real `--convert` runs (clean/high-gain NAM x default/real-reference-clip Tone Match target); all produced well-formed CLOs with Tone Match applied, and re-running `--quality-experiment` afterward confirmed identical loss numbers to before the change.

### GP-5/GP-50 Post-biquad frequency-scale search — small, inconsistent effect, not wired into production

`clo_refiner.cpp`'s `searchPostAndSolveB()` generalizes `postForRate(sr)` (the fixed, reverse-engineered-from-GP-200.exe Post biquad formula every conversion uses today, regardless of the specific amp) into `postForRateScaled(sr, freqScale)`, and grid-searches `freqScale` over `{0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 2.0}` (1.0 reproduces today's fixed value exactly), re-solving Block B via the same least-squares approach after each candidate. Gated on the same selection/benchmark split built for the P/K search fix, for the same reason: this is a search over real signal, not a closed-form solve.

**Measured (2026-08-30)**, benchmark-only scoring (`gp5PostSearchHeldOutLoss` vs. `gp5DirectBSolveBenchmarkHeldOutLoss` -- the fair freqScale=1.0 baseline, both scored on the same disjoint benchmark subset; do not compare against `gp5DirectBSolveHeldOutLoss`, which uses the full validation set): `freqScale=2.0` (the top of the grid) won in all 4 (NAM, submodel) combinations tested, but the effect was small and inconsistent -- clean Fender Full improved 4.0%, Fender Lite 0.9%, Meshuggah Full ~0.1% (noise), and **Meshuggah Lite regressed 1.6%**. See `test_assets/quality_results/*_PureCandidate/quality_experiment_results_post_search.csv`.

Always picking the grid's boundary value, combined with one outright regression, is a smaller-scale version of the same fragility the P/K search originally had -- real headroom (if any) may be beyond `freqScale=2.0`, but the more likely read is that a 2-3-clip selection set is too little signal to trust a 1-in-4-regression result. Do not wire this into `convertNamToClo`. If revisited, widen the candidate grid past 2.0 and/or wait for a larger validation corpus (roadmap item 8) before trusting the selection gate's verdict here.

## Licensing / attribution notes

This is an independent research/reimplementation project, not affiliated with or endorsed by Valeton or Hotone. `THIRD_PARTY.md` tracks the three fetched dependencies (all MIT-licensed) and `CMakeLists.txt` installs their upstream LICENSE files alongside the built exe — if you change how a dependency is fetched or vendored, keep that install step in sync.
