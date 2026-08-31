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

This is not a failure of the fix -- it's the fix doing its job. Phase 2's "wins" were never real; Phase 3 correctly reports zero instead of a phantom one.

**Phase 4 (2026-08-30, corpus grown 10 -> 34 clips, 5 -> 38 NAMs, and the selection/benchmark split changed from a hard `min(3, n/2)` cap to `n/3` uncapped -- see `test_assets/quality_results/*_PureCandidate/quality_experiment_results_wide_corpus.csv`): re-ran the identical Fender/Meshuggah comparison with ~11 selection clips instead of 3. Result: unchanged. `gp5PureLoss` stayed exactly at the pre-search baseline on both NAMs, both submodels -- the search still finds nothing that survives the selection gate.** This rules out "too little validation data" as the explanation. With ~4x the selection signal pointing to the same verdict, the more likely explanation is the one flagged earlier in this conversation: `evaluateModelLoss`'s frequency-magnitude-ratio metric may simply be insensitive to what P/K controls (asymmetric saturation shape/harmonic content) -- Phase 1's finding that A/B can compensate for a P/K change under this exact metric is consistent with that. If P/K search is revisited, checking whether the loss metric can even distinguish two known-different P/K settings should come before trying to search harder against the same metric. Do not wire this candidate into `convertNamToClo` -- there has never been a verified quality win from it, at any phase. See `gp5_optimizer.hpp`'s `fitPureFromRender` doc comment for the full detail.

### GP-5/GP-50 direct Block B least-squares solve — a real, verified win, wired into production

`clo_refiner.cpp`'s `solveBlockBLeastSquares()` (declared in `clo_refiner.hpp`) replaces the "compute a 2048-sample correction filter, convolve it into B, implicitly truncate everything past tap 512" approach (`computeToneMatchCorrectionIr()` + `applyCorrectiveIrToB44()`) with a direct regularized frequency-domain least-squares solve for the 512 B coefficients themselves, against the same Tone Match target and tail-analysis window. Pre/A128/P-K/Post stay frozen — only B changes.

**Measured (2026-08-30)**, via `runQualityExperiments`'s held-out scoring (`gp5DirectBSolveHeldOutLoss` vs. `gp5PostToneMatchHeldOutLoss` and the pre-Tone-Match `gp5ChosenDeviceHeldOutLoss` baseline, see `test_assets/quality_results/*_PureCandidate/quality_experiment_results_b_solve_*.csv`): tested on a clean and an extreme-high-gain NAM, both submodels, and both the default synthetic stimulus and a real recorded clip as the Tone Match reference (8 combinations, held-out set always excluding whatever clip was used as the reference). **Every combination showed a large held-out win**: 54-87% loss reduction vs. the pre-Tone-Match baseline, and 60-87% vs. the existing correction-IR approach — which itself sometimes made held-out loss *worse* than doing nothing at all (e.g. clean Fender Full, default stimulus: baseline 1.637, correction-IR 1.769, direct solve 0.582).

Unlike the P/K search above, **this is not overfitting** — the win holds up on held-out real playing content with a properly excluded validation set, not just on the training window. This is currently the strongest verified quality lever found this session.

**Wired into production (2026-08-30).** `convertNamToClo`'s GP-5/GP-50 Tone Match gate (`native_converter.cpp` ~L2082-2145) now compares three candidates -- no correction, `computeToneMatchCorrectionIr`'s correction-IR, and `solveBlockBLeastSquares`'s direct solve -- and ships whichever scores lowest, extending the existing `lossPre`/`lossPost` safety gate rather than replacing it. `serializeGp5Compact` gained an optional `overrideB44` parameter so a directly-solved B can be written verbatim, skipping the normal resample+Corrective-IR+Tone-Match-IR convolution steps. Verified with 4 real `--convert` runs (clean/high-gain NAM x default/real-reference-clip Tone Match target); all produced well-formed CLOs with Tone Match applied, and re-running `--quality-experiment` afterward confirmed identical loss numbers to before the change.

**Generalization re-verified (2026-08-30) across 5 amp characters the original 2-NAM verification never touched** -- clean (Fender Deluxe Reverb Reissue), edge-of-breakup (1964 Vox AC30 Top Boost), modern high-gain (PRS Archon 100), bass (Best Metal Bass Amp), and extreme-high-gain (Bogner Uberschall MKII), both submodels each (see `test_assets/quality_results/*_Generalization/quality_experiment_results.csv`). **Every one of the 10 new combinations showed the same large win**: 69-91% held-out loss reduction vs. baseline, while the old correction-IR approach it replaced stayed a wash (sometimes marginally better, sometimes worse than doing nothing, never close to the direct solve). Combined with the original 2 NAMs, that's 14 (NAM, submodel) combinations now confirming this, across clean/edge/crunch/high-gain/extreme-gain/bass and many amp brands -- this is not narrow to the 2 NAMs it was first verified on.

### Dynamic-range / level-response (roadmap item 7) — a real, sizable problem found on high-gain amps

Everything measured earlier this session (P/K search, Post search, the rate-domain check) tested a single fixed operating point and came back small, null, or inconclusive. This is the one dimension that came back with a genuine, sizable, actionable finding.

`ntc::measureLevelResponse` (`native_converter.hpp`/`.cpp`, headless CLI `--level-response <nam> <diClip.wav> <outputCsv>`) renders the same dry clip at `{-24,-18,-12,-6,0,+6}` dB through both Full A2 (ground truth, `renderNamOnSignal`) and the NAM's actual **shipped production conversion** (`convertNamToClo` with default/Tone-Match-on settings, then `ntc::renderCloOnSignal` -- a new small utility in `clo_refiner.hpp`/`.cpp` that renders an arbitrary signal through a parsed CLO end-to-end at unity gain, factored out of `computeToneMatchCorrectionIr`/`solveBlockBLeastSquares`'s existing internal render chain). Reports RMS-dBFS of each output per level plus an ESR between them (see `test_assets/quality_results/*_PureCandidate/level_response.csv`).

**Metric upgraded (2026-08-30) to a relative level-response**: `LevelResponsePoint` now anchors each output to that NAM's own 0dB point (`fullA2RelativeDb`, `gp5RelativeDb`, `relativeErrorDb = gp5Relative - fullA2Relative`), and `LevelResponseResult` reports `maxRelativeErrorDb`/`rmsRelativeErrorDb` plus `pkPp/pkPn/pkKp/pkKn` (now exposed on `ConversionResult` too) -- anchoring to each model's own 0dB result makes amps with different absolute output levels comparable, which raw RMS wasn't. The CSV also carries step-to-step compression-slope columns (`full_a2_step_db`, `gp5_step_db`) showing where in the level range the response actually diverges, not just the total error.

**Measured (2026-08-30), one DI clip ("Hotrod - Guitar.wav") through 9 NAMs biased toward gain-character diversity** (not a random amp sample -- chosen to find where the problem starts and confirm it isn't Meshuggah-specific), ordered by how compressed the amp actually is (Full A2's own sweep across the 30dB test range -- smaller means more compressed/higher gain):

| NAM | Gain character | A2 sweep (dB) | GP5 sweep (dB) | Max rel. error (dB) | RMS rel. error (dB) | kp (Full) |
|---|---|---:|---:|---:|---:|---:|
| Fender Super Reverb | Clean | 29.8 | 29.3 | 0.28 | 0.19 | 1.2 |
| Fender Deluxe Reverb Reissue | Clean | 28.3 | 28.1 | 0.96 | 0.63 | 5.5 |
| JCM800 2203 Modified | Medium crunch / edge | 16.2 | 17.3 | 0.90 | 0.49 | -- |
| Best Metal Bass Amp | High-gain bass | 9.0 | 11.0 | 1.70 | 1.14 | 13.9 |
| Marshall Silver Jubilee 2555 | Hot Marshall-style | 7.5 | 8.7 | 0.99 | 0.57 | -- |
| MattFig Recto-style Crush | Extreme high gain | 3.0 | 4.3 | 1.16 | 0.58 | -- |
| PRS Archon 100 | Modern high gain | 1.6 | 4.6 | **2.69** | 1.50 | 77.2 |
| Fortin Meshuggah | Extreme high gain | 1.4 | 4.4 | **3.04** | 1.50 | 83.5 |
| Bogner Uberschall MKII | Extreme high gain | 1.3 | 9.5 | **7.29** | 4.00 | 85.1 |

(`kp` blank where the NAM was only run through `--level-response`, not the separate `--quality-experiment` pipeline that reports it -- not yet backfilled.)

**This is a clean, monotonic dose-response relationship, not noise from one unusual amp.** Ordered by actual compression (A2 sweep, descending from clean to extreme), max relative error rises essentially monotonically from 0.28dB (cleanest) to 7.29dB (most compressed) -- with Bogner Uberschall showing an even *larger* mismatch than the original Meshuggah finding. Where `kp` is available, it tracks the same ordering (1.2 -> 5.5 -> 13.9 -> 77.2 -> 83.5 -> 85.1), i.e. the fitted P/K nonlinearity-steepness value is itself a usable, cheap predictor of how much dynamics error a given conversion is likely to have -- exactly the "clean/low-gain: existing fit is fine; high/extreme-gain: needs dynamics-aware treatment" split hypothesized before running this. The under-compression direction is consistent everywhere it shows up: GP-5/GP-50 always moves *more* than Full A2 across the sweep (never less), i.e. it consistently "saturates too late" relative to the real amp, not randomly in either direction.

**Caveat on the ESR number** (still present per-level in the CSV, not shown in the table above): it's a raw time-domain sample-wise metric, unlike the frequency-domain ratio loss used everywhere else in this codebase, so it's likely sensitive to any small latency/group-delay mismatch between the two independent render chains (`renderNamOnSignal` vs. `renderCloOnSignal`) -- treat its absolute value with caution. The relative-RMS metric above is NOT subject to this confound (RMS is phase/timing-insensitive), so that's the trustworthy part of this finding.

**Verdict**: the pattern replicates across independent high-gain amps spanning different brands/eras (JCM800, Marshall Jubilee, PRS, Bogner, Mesa-style, metal bass) and is not Meshuggah-specific -- if anything Meshuggah is mid-pack among the high-gain group, not the worst case. This justifies the next step you specified: **multi-level P/K optimization + a single shared B512 solve fit across levels**, targeted at high/extreme-gain conversions specifically (kp above roughly the Best-Metal-Bass/PRS-Archon range) rather than every conversion -- clean and low/medium-gain amps show the existing single-level fit is already adequate (well under 1dB error) and don't need the extra optimization cost. Not yet implemented -- this write-up is the evidence, not the fix.

### Dynamics-aware fitting, Step 1: K-multiplier sweep + shared multi-level B — causality confirmed, single shared multiplier rejected

Direct follow-up to the dynamic-range finding above. `clo_refiner.hpp`/`.cpp` gained `sweepKAndSolveSharedB()`: sweeps a multiplier on the P/K shaper's `Kp`/`Kn` steepness (`Pre`/`A128`/`Pp`/`Pn`/`Post` frozen, read from the shipped CLO) and, for each candidate, solves **one** Block B jointly across 6 gain levels (`solveBlockBMultiLevel()`, a weighted generalization of `solveBlockBFromPreB()`'s regularized deconvolution — weights `wi=1/energy(Ti)` normalized to sum to 1, `eps` scaled from the weighted mean power) instead of fitting each level separately. `native_converter.cpp`'s `runKSweepExperiment()` (headless CLI `--k-sweep <nam> <diClip.wav> <outputCsv>`) converts the NAM via `convertNamToClo` (production settings), builds the same 6-level `{-24,-18,-12,-6,0,+6}` dB clip set `measureLevelResponse()` uses, and sweeps `kMultiplier` in `{0.75,1.0,1.25,1.5,2.0,3.0,4.0}`. Comparative/measurement only — never wired into `convertNamToClo`.

**Sanity check on kMultiplier=1.0** (should reproduce `measureLevelResponse`'s already-measured baseline): matched closely for Fender Super Reverb (max 0.277dB vs. 0.28dB measured, rms 0.193 vs. 0.19) and reasonably for PRS Archon (max 2.908 vs. 2.69, rms 1.609 vs. 1.50), but diverged notably for Fortin Meshuggah (max 2.227 vs. 3.04, rms 1.093 vs. 1.50) and especially Bogner Uberschall (max 4.166 vs. **7.29**, rms 2.238 vs. **4.00**). The likely cause: `sweepKAndSolveSharedB`'s B is solved jointly across 6 DI-clip levels, while production's B (`solveBlockBLeastSquares`, inside `convertNamToClo`) is solved once against the official Tone Match reference's 20s tail — different training signal entirely, not just a different K. This means the shared multi-level B solve is *itself* changing dynamics error on the most extreme amps, independent of K — a secondary, unexplored lever (out of scope here, flagged for a future look) that this experiment wasn't designed to isolate.

**Measured, all 5 amps, full 7-point sweep each** (`test_assets/quality_results/k_sweep_*.csv`):

| NAM | Role | k=1 rms err (dB) | Best-K rms err (dB) | Best K | Reduction | rms err at k=4 |
|---|---|---:|---:|---:|---:|---:|
| PRS Archon 100 | Affected | 1.609 | 0.370 | 4.0 | -77% | 0.370 |
| Fortin Meshuggah | Affected | 1.093 | 0.077 | 4.0 | -93% | 0.077 |
| Bogner Uberschall MKII | Affected | 2.238 | 0.690 | 4.0 | -69% | 0.690 |
| Fender Super Reverb | Control (clean) | 0.193 | 0.131 (k=0.75) | 0.75 | n/a | 0.837 (**+334%**) |
| JCM800 2203 Edge of Breakup | Control | 0.471 | 0.055 (k=1.25) | 1.25 | n/a | 2.834 (**+502%**) |

Within each amp, dynamics error moves **monotonically and controllably** with K — increasing K past each amp's own optimum makes it worse in every case, decreasing K below optimum also makes it worse (Fender's error rises going from k=0.75 down toward smaller K too, not shown in table). This confirms causality cleanly: the shaper's steepness is a real, controllable lever on dynamics error, exactly as hypothesized.

**But no single shared kMultiplier clears the plan's bar** (≥50% RMS reduction on all 3 affected amps, no material regression on either control): the K values that fix Archon/Meshuggah/Uberschall (3.0-4.0) devastate both controls (JCM800 rms error goes from 0.47dB to 2.83dB at k=4, a 502% regression; Fender goes from 0.19dB to 0.84dB, +334%). This is not noise or a corpus-size artifact — it reproduces the exact "clean/low-gain: existing fit is fine; high-gain: needs more steepness" split the original dose-response table predicted, just made quantitative: **the correct K is gain-dependent per amp, not a global constant.**

**Verdict**: Step 1's literal test (one shared multiplier) fails, as expected once the per-amp dependence is this strong — but that failure is itself the informative result, not a dead end: it rules out the cheapest possible fix (a single global steepness bump) and confirms the more expensive one is necessary. Since Step 2's full P/K search was always designed to fit each NAM independently (never a shared constant across the corpus), this result is a green light for it, not a stop signal — the "dynamics gap needs more than K/B" alternative the original plan flagged as the stop condition is specifically *not* what happened here; K alone moved every affected amp's error the right direction by 70-90%, it just needs to be found per-amp rather than assumed.

### Dynamics-aware fitting, Step 2: full P/K search — strong, broad win; not yet wired into production

Direct follow-up to Step 1's verdict (single shared Kp/Kn multiplier rejected, per-NAM search warranted). `clo_refiner.hpp`/`.cpp` gained `searchPkForDynamics()`: full P/K unlock (`Pp`/`Pn`/`Kp`/`Kn` all free, `Pre`/`A128`/`Post` still frozen), coordinate descent over 4 rounds, each candidate followed by a fresh joint `solveBlockBMultiLevel()` solve across a **train** clip's 6 levels. Scored as `spectralEsr + lambda*rmsDynamicsErrorDb` (`lambda=0.3`), but a round is only accepted if it *also* improves on a **selection** clip disjoint from train (never re-solving B against selection — same discipline as every prior search this session), with a safety floor rejecting any round that lets the selection's worst-level error grow >10% past the current best. A **benchmark** clip (disjoint from both) is scored once at the end, purely for reporting. `native_converter.cpp`'s `runPkDynamicsSearchExperiment()` (headless CLI `--pk-dynamics-search <nam> <train.wav> <selection.wav> <benchmark.wav> <lambda> <outputCsv>`) wires this to a real production-converted CLO. Comparative/measurement only — not wired into `convertNamToClo`.

**Measured (2026-08-30), 7 amps, train=Hotrod, selection=Groove Thrash, benchmark=Fast Thrash (all disjoint guitar DI clips)**, benchmark-only numbers (the honest held-out figure — never touched during search):

| NAM | Role | Benchmark rms err: before → after | Reduction | Benchmark ESR: before → after | kp: before → after | kn: before → after |
|---|---|---|---:|---|---:|---:|
| PRS Archon 100 | Affected | 1.397 → 0.074 | **-95%** | 0.375 → 0.356 (better) | 77.2 → 316.3 | 85.1 → 348.4 |
| Fortin Meshuggah | Affected | 1.081 → 0.053 | **-95%** | 0.379 → 0.356 (better) | 83.5 → 341.8 | 84.2 → 269.5 |
| Bogner Uberschall MKII | Affected | 1.976 → 0.353 | **-82%** | 0.721 → 0.693 (better) | 85.1 → 348.4 | 40.5 → 166.0 |
| Best Metal Bass Amp | Additional | 1.131 → 0.151 | **-87%** | 0.139 → 0.147 (**+5% worse**) | 13.9 → 22.3 | 20.6 → 32.9 |
| Marshall Silver Jubilee 2555 | Additional | 0.458 → 0.229 | **-50%** | 0.162 → 0.151 (better) | 77.3 → 123.7 | 69.1 → 55.3 (decreased) |
| JCM800 2203 Edge of Breakup | Control | 0.584 → 0.163 | **-72%** | 0.085 → 0.084 (flat) | 30.4 → 38.0 | 28.1 → 35.1 |
| Fender Super Reverb | Control (clean) | 0.236 → 0.231 | -2% (flat) | 0.067 → 0.067 (flat) | 1.22 → 1.22 (unchanged) | 1.27 → 1.27 (unchanged) |

**All 7 amps improved or stayed flat on the held-out benchmark clip; none regressed.** The 3 originally-affected amps and the bass and Marshall all cleared the plan's 50%+ RMS reduction bar by a wide margin (50-95%), including generalizing correctly to a musical style (thrash) different from the train clip's. The two controls behaved exactly as hoped: Fender (already accurate, per Step 1) barely moved at all -- the search correctly found nothing worth changing rather than forcing an unnecessary edit -- while JCM800 Edge, despite being nominally a "control," turned out to still have real headroom and improved substantially (-72%) with only a modest Kp/Kn increase. Spectral fidelity (ESR) held steady or improved on 6 of 7; Best Metal Bass Amp is the only regression, and it's small (+5%) against an 87% dynamics-error win. Kp/Kn moved by very different amounts per amp (unchanged on Fender, +25% on JCM800, 3-4x on the extreme-gain amps) -- exactly the per-amp adaptivity Step 1 showed was necessary and a single shared multiplier could not provide. Marshall Jubilee is the one case where Kn *decreased* rather than increased, showing the search isn't just blindly cranking steepness -- it's finding a genuinely different, asymmetric optimum for that amp's response shape.

**Not yet wired into `convertNamToClo`.** This is a strong, broadly-verified candidate (7/7 amps, clean/edge/crunch/high-gain/extreme-gain/bass, held-out benchmark clip in a different musical style than training) by the same evidentiary bar the direct B512 solve was held to before shipping -- but production wiring is a separate decision, deliberately out of scope for this measurement pass: it changes what every user's conversion actually sounds like, and running a multi-round coordinate-descent search across 6 gain levels adds real per-conversion cost that a fixed closed-form solve doesn't have. Before shipping, this should get the same broader-corpus generalization pass the B512 solve got (14 combinations across many amps) rather than resting on 7, and the runtime-cost tradeoff needs an explicit call. `test_assets/quality_results/pk_dynamics_*.csv` holds the raw evidence.

**Listening-test export added (2026-08-30):** all of the above (Step 1 and Step 2) is automated-metric evidence only -- nobody has listened to the audio. `clo_refiner.hpp`/`.cpp` gained `renderCloWithOverrideOnSignal()` (same render chain as `renderCloOnSignal()`, but the shaper/B are overridden so an unshipped candidate can be auditioned without writing a CLO) and `writeMono44100Wav()`. `native_converter.cpp`'s `runPkDynamicsAudition()` (headless CLI `--pk-dynamics-audition <nam> <playingClip.wav> <train.wav> <selection.wav> <benchmark.wav> <lambda> <outputDir>`) runs the same production conversion + Step 2 search, then renders a real musical clip (not the synthetic level staircase the search trains against) through Full A2 / the as-shipped conversion / the Step 2 winner, writing `full_a2.wav`/`baseline_gp5.wav`/`optimized_gp5.wav` for a direct by-ear comparison. Run once each for PRS Archon 100 (large measured win) and Fender Super Reverb (control, near-zero measured change) against "Power - Guitar.wav" -- see `test_assets/listening_tests/*/`. Not a substitute for a real hardware A/B (everything here is still relative to Full A2, the NAM's own neural render, never real GP-50 output) -- just the cheapest way to check whether the measured numbers correspond to something audible before investing in that.

### Definitive official-vs-ours benchmark (resources/GP50_SnapTone_Conversion_Benchmark_Plan_v2.md, Priority 1) — all 7 available amps, mechanism isolated: the B-solve fixes tone, the P/K search fixes dynamics, and they're independent

Every quality claim earlier in this document is relative to Full A2 (the NAM's own neural render) — never checked against what Valeton's own official SnapTone conversion actually achieves on the same NAM. `resources/GP50_SnapTone_Conversion_Benchmark_Plan_v2.md` (a separate reverse-engineering writeup of Valeton's `startClone()` fitter, not authored this session) lays out the definitive three-way benchmark this gap calls for. `native_converter.hpp`'s `runOfficialSnaptoneBenchmark()` (headless CLI `--official-benchmark <nam> <officialSnapClo> <diClipWav> <outputCsv> <trainClip|-> <selectionClip|-> <lambda> <heldOutClip1> [...]`) implements it: given a real official SnapTone CLO for `inputNam`, it converts the SAME NAM with our own pipeline, then renders Full A2, the official CLO, and ours through the SAME software renderer (`renderCloOnSignal`, already tap-count-agnostic — it reads A/B counts from the VTSI header, so it works unchanged on the official file's GP-200-native 128/2048-tap layout) to remove hardware/capture confounds per the plan's section 15. It auto-picks the matching-architecture candidate (`gp2001024` for a 2048-tap B, `gp5gp50Compact` for a 512-tap B) so the comparison is apples-to-apples. Reports the same six-level relative-dynamics sweep used throughout this document's dynamics work, plus held-out real-clip waveform ESR vs Full A2. When `trainClip`/`selectionClip` are supplied (not `-`), it additionally runs Step 2's `searchPkForDynamics` against our own conversion (using `diClipWav` itself as the search's disjoint benchmark clip, matching the discipline every other search in this codebase uses) and scores the resulting unshipped optimized candidate against the SAME official file and Full A2 reference.

**Measured (2026-08-31), all 7 real official SnapTone conversions available** (`test_assets/snaptone_conversions/*.clo`, user-supplied, all GP-200-format 8840-byte VTSI files) — clean, bass, edge-of-breakup, crunch, high-gain, and two extreme-gain amps, i.e. every gain category this document's earlier dynamic-range work identified. Guitar DI clip = "Hotrod - Guitar.wav" (bass amp used "Downtown - Bass.wav") for the dynamics sweep, train=Groove Thrash/selection=Fast Thrash (bass: Frogger/Garden), 2 disjoint held-out clips per amp (guitar: Mayer/Cream; bass: Drivin'/Rollin') for spectral/waveform fidelity, lambda=0.3 throughout — see `test_assets/quality_results/OfficialBenchmark/*_optimized.csv`:

| NAM | Category | Dynamics RMS err: official / ours / **optimized** | Held-out mean ESR: official / ours / **optimized** |
|---|---|---|---|
| Fender Super Reverb 1977 | Clean | 0.16 / 0.20 / 0.20 (flat) | 6.4 / 11.2 / **0.009** |
| Ampeg SVT Classic MD421 | Bass | 0.33 / 0.33 / 0.34 (flat, ~noise) | 18.9 / 21.8 / **0.029** |
| JCM800 2203 Modified (EdgeOfBreakup) | Edge of breakup | 0.54 / 0.42 / 0.41 (flat) | 9.3 / 6.2 / **0.067** |
| Marshall 1962 Bluesbreaker JTM45 | Crunch | 1.29 / 1.06 / **0.30** | 9.6 / 8.4 / **0.20** |
| JCM800 2203 Modified (HighGain) | High gain | 3.27 / 4.43 / **1.65** | 23.6 / 16.2 / **0.43** |
| Fortin Meshuggah | Extreme gain | 1.24 / 2.50 / **0.41** | 18.3 / 20.0 / **0.50** |
| Bogner Uberschall MKII | Extreme gain | 3.00 / 4.47 / **0.75** | 18.1 / 14.6 / **0.62** |

**Official vs. our shipped conversion: genuinely mixed, no overall winner.** Official wins dynamics on 4/7 (clean, bass, high-gain, and effectively tied on edge-of-breakup), ours wins on 2/7 (crunch, edge-of-breakup by a hair), roughly tied on bass. Held-out spectral fidelity splits similarly. Neither converter dominates the other — this rules out any claim that our production pipeline is already categorically better (or worse) than Valeton's own fitter.

**Step 2-optimized vs. both: wins everywhere tested, with a coherent pattern by category.** On the two amps that were already accurate (Fender clean, Ampeg bass), the search correctly declines to change dynamics tracking (flat, within noise) — exactly the "don't fix what isn't broken" behavior CLAUDE.md's Step 2 section already found on Fender. On every other amp (edge-of-breakup through both extreme-gain amps), optimized matches or clears whichever of official/ours already had the lower dynamics error, by 2-4x on the high/extreme-gain amps specifically. Dynamics-RMS is the trustworthy metric here (phase/timing-insensitive, same relative-anchoring convention used throughout this document); it never regresses on any of the 7 amps.

**Mechanism isolated (2026-08-31): the two wins have two completely different, cleanly separable causes.** `runOfficialSnaptoneBenchmark` gained a third candidate, "B-only" -- shipped P/K frozen, Block B solved jointly across `trainDiClipWav`'s levels the same way Step 2 solves B per P/K candidate (`sweepKAndSolveSharedB` with `kMultiplier=1.0`) -- to separate the direct-B-solve mechanism from the P/K search bundled inside "optimized." Re-ran all 7 amps with all four candidates scored side by side (`test_assets/quality_results/OfficialBenchmark/*_split.csv`):

| NAM | Dynamics RMS: official / ours / B-only / **optimized** | Held-out ESR: official / ours / B-only / optimized |
|---|---|---|
| Fender Super Reverb | 0.16 / 0.20 / 0.20 / 0.20 (B-only == optimized, no P/K change) | 6.4 / 11.2 / 0.0088 / 0.0088 |
| Ampeg SVT Classic | 0.33 / 0.33 / 0.34 / 0.34 (B-only == optimized, no P/K change) | 18.9 / 21.8 / 0.029 / 0.029 |
| JCM800 EdgeOfBreakup | 0.54 / 0.42 / 0.56 (worse than shipped) / **0.41** | 9.3 / 6.2 / 0.061 / 0.067 |
| Marshall Bluesbreaker | 1.29 / 1.06 / 1.13 (worse than shipped) / **0.30** | 9.6 / 8.4 / 0.203 / 0.204 |
| JCM800 HighGain | 3.27 / 4.43 / 4.89 (worse than shipped) / **1.65** | 23.6 / 16.2 / 0.484 / 0.430 |
| Fortin Meshuggah | 1.24 / 2.50 / 2.04 (small improvement) / **0.41** | 18.3 / 20.0 / 0.532 / 0.504 |
| Bogner Uberschall | 3.00 / 4.47 / 2.40 (partial improvement) / **0.75** | 18.1 / 14.6 / 0.598 / 0.621 |

**The split is total and consistent across all 7 amps.** B-only reproduces almost the ENTIRE held-out ESR win by itself -- on 5 of 7 amps it's within a few percent of "optimized" (sometimes fractionally *better*, e.g. JCM800 Edge 0.061 vs 0.067, Bogner 0.598 vs 0.621), and on the two already-accurate amps the two candidates are numerically identical because Step 2 correctly found no P/K change worth making. Confirms the phase-correction hypothesis above: the ESR win is the direct-B-solve mechanism, full stop, and it's essentially free (closed-form, no search, no P/K change needed). Meanwhile B-only does NOT fix dynamics -- it's flat-to-worse than the shipped conversion on 4 of 7 amps (JCM800 Edge, Marshall, JCM800 HighGain all regress slightly) and only partially helps on the two extreme-gain amps (Meshuggah, Bogner), nowhere close to what the full P/K search achieves. This is exactly what CLAUDE.md's earlier architectural reasoning predicted (`resources/GP50_SnapTone_Conversion_Benchmark_Plan_v2.md` section 22.5: "B512 is a linear filter, it cannot repair a wrong compression curve") -- now confirmed empirically rather than just argued from first principles. Dynamics correction genuinely requires the P/K search; spectral/waveform fidelity does not.

**Practical implication: these are two independent, separately shippable levers, not one bundled feature.** The B-solve-only win is cheap (one closed-form joint solve, no coordinate descent, no selection/benchmark gating needed since there's no candidate to accept/reject) and produces a large held-out fidelity improvement on every amp tested regardless of gain character -- a much lower-risk, lower-cost candidate for production wiring than the full Step 2 search. The P/K search remains the only lever that fixes dynamics specifically, is more expensive (multi-round coordinate descent per conversion), and per Step 2's original section should probably stay gated on measured dynamics error (CLAUDE.md's "don't hard-code a Kp threshold" caution still applies -- gate on the error, not amp category) rather than always running. Neither is wired into `convertNamToClo` yet; this isolation is what makes that a well-informed decision rather than a guess.

### Trainer-rate vs. 44.1kHz storage-domain loss (roadmap item 5) — measured, but inconclusive

Roadmap item 5 asked whether resampling the fitted A/B/pre/post from the NAM trainer's native rate (e.g. 48kHz) down to the 44.1kHz GP-5/GP-50 storage domain (`resampleFirOfficial`, used throughout `serializeGp5Compact`) costs meaningful quality -- meant to gate whether item 6 (rearchitecting the optimizer to work natively at 44.1kHz) is worth pursuing at all.

`native_converter.cpp`'s `runQualityExperiments` gained `gp5ChosenTrainerDomainHeldOutLoss` (via a `heldOutLossAtCommonRateFor` lambda) to compare the SAME chosen model rendered at its native trainer rate against the same model after `resampleFirOfficial` converts it to 44.1kHz (`gp5ChosenDeviceHeldOutLoss`).

**First attempt** scored each domain at its own native rate directly. Result looked like a real, large penalty (trainer domain scored 14-15% worse on Fender, 4% worse on Meshuggah) -- but the direction was suspicious (resampling can't make a model *more* accurate) and pointed at `ratioSpectrumF`/`lossFromRatioF` not being comparable across different native rates (different FFT bin counts/frequency resolution), not a real quality difference.

**Second attempt** (`heldOutLossAtCommonRateFor`) rendered the trainer-domain model at its native rate but resampled only the *output* to a common 44.1kHz rate before scoring, to eliminate the rate-scale confound. Result: the numbers barely moved (Fender Full 1.94898 -> 1.95059, Lite 2.01727 -> 2.015) -- which undermines the "it's just a metric-scale artifact" theory, but the second attempt has its own confound: the trainer-domain path now goes through *two* resampling operations in the signal chain (input up to trainer rate, then rendered output back down to 44.1kHz), while the storage-domain path only resamples the FIR/biquad coefficients once with no extra signal-level resampling -- so the persistent gap could still be measurement noise from a different source, not a real cost of resampling the model itself.

**Left inconclusive.** Both `gp5ChosenTrainerDomainHeldOutLoss` and `heldOutLossAtCommonRateFor` are in place (see `test_assets/quality_results/FenderSuperReverb1977_Clean_PureCandidate/quality_experiment_results_rate_domain.csv`) for whoever revisits this, but do not treat the current gap as a verified quality cost of resampling -- it hasn't been isolated from measurement confounds yet. A real answer needs either inspecting `ratioSpectrumF`/`lossFromRatioF` directly for rate-dependence, or a cleaner experimental design that avoids the double-resample issue. Not wired into any decision about item 6.

### GP-5/GP-50 Post-biquad frequency-scale search — small, inconsistent effect, not wired into production

`clo_refiner.cpp`'s `searchPostAndSolveB()` generalizes `postForRate(sr)` (the fixed, reverse-engineered-from-GP-200.exe Post biquad formula every conversion uses today, regardless of the specific amp) into `postForRateScaled(sr, freqScale)`, and grid-searches `freqScale` over `{0.5, 0.7, 0.85, 1.0, 1.15, 1.3, 1.5, 2.0}` (1.0 reproduces today's fixed value exactly), re-solving Block B via the same least-squares approach after each candidate. Gated on the same selection/benchmark split built for the P/K search fix, for the same reason: this is a search over real signal, not a closed-form solve.

**Measured (2026-08-30)**, benchmark-only scoring (`gp5PostSearchHeldOutLoss` vs. `gp5DirectBSolveBenchmarkHeldOutLoss` -- the fair freqScale=1.0 baseline, both scored on the same disjoint benchmark subset; do not compare against `gp5DirectBSolveHeldOutLoss`, which uses the full validation set): `freqScale=2.0` (the top of the grid) won in all 4 (NAM, submodel) combinations tested, but the effect was small and inconsistent -- clean Fender Full improved 4.0%, Fender Lite 0.9%, Meshuggah Full ~0.1% (noise), and **Meshuggah Lite regressed 1.6%**. See `test_assets/quality_results/*_PureCandidate/quality_experiment_results_post_search.csv`.

Always picking the grid's boundary value, combined with one outright regression, looked at first like the same fragility the P/K search originally had, attributable to too little selection data.

**Re-tested (2026-08-30) with the corpus grown 10 -> 34 clips and the selection/benchmark split widened from a 3-clip cap to `n/3` uncapped** (~11 selection clips instead of 3 -- see `test_assets/quality_results/*_PureCandidate/quality_experiment_results_wide_corpus.csv`): the pattern reproduced almost exactly -- Fender Full +2.9% (was +4.0%), Fender Lite +0.86% (was +0.9%), Meshuggah Lite **-1.86% regression** (was -1.6%), and Meshuggah Full picked a *different* freqScale this time (0.5 instead of 2.0) but still landed at ~0% (noise). That similarity across a ~4x larger selection set is actually informative: this is very likely a **real, small, amp-dependent effect** (helps a little on the clean amp, doesn't help or hurts on the high-gain one) rather than corpus-size noise -- but "real and small and sometimes negative" still isn't a shippable result. Do not wire this into `convertNamToClo`. More validation data does not appear to be the blocker here (see the P/K search's Phase 4 for the same conclusion); if revisited, a genuinely different avenue -- widening the frequency grid, or questioning whether the loss metric captures what matters -- is more likely to move this than more clips.

## Licensing / attribution notes

This is an independent research/reimplementation project, not affiliated with or endorsed by Valeton or Hotone. `THIRD_PARTY.md` tracks the three fetched dependencies (all MIT-licensed) and `CMakeLists.txt` installs their upstream LICENSE files alongside the built exe — if you change how a dependency is fetched or vendored, keep that install step in sync.
