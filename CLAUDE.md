# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows x64 GUI application (`NamToClo.exe`) that converts Neural Amp Modeler (`.nam`) models into `.clo` files compatible with Valeton GP-200/GP-5/GP-50 hardware, and uploads `.clo` files directly to those devices over USB MIDI. It reimplements undocumented vendor behavior (the GP-200 native NAM→CLO conversion algorithm and the GP-5/GP-50 SnapTone upload protocol) reverse-engineered from the official tools and USB-MIDI captures — this is *not* a wrapper around Valeton's own software. There is no runtime dependency on Valeton Suite.

This is an independent research/reimplementation project, not affiliated with or endorsed by Valeton or Hotone, and is a public fork of [Goaltoday/NamtoClo](https://github.com/Goaltoday/NamtoClo) (see README for what's different from upstream).

## Build

Requires Windows x64, CMake 3.24+, and MSVC (Visual Studio C++ desktop workload). The CMake project only configures on Windows/x64 — it hard-fails otherwise.

```powershell
cmake --preset windows-x64
cmake --build build --config Release --parallel
```

The `windows-x64` preset in `CMakePresets.json` pins a Visual Studio generator version — update it to match whatever VS major version is actually installed on the machine (it does not auto-detect).

Output executable: `build/Release/NamToClo.exe`. To run a build, copy `nam_input_wav.wav` (repo root) next to the exe — the Convert tab requires it at runtime and it's not copied by the build. `resources/reference_clips/*.wav` (Tone Match "Auto"/named reference clips) and `resources/icons/*` are copied next to the exe automatically as a post-build step.

There is no test suite and no linter configured in this repo. Verification is done via headless CLI flags on the built exe (see "Research/diagnostic CLI tooling" below) plus manual GUI smoke tests and, for conversion-quality claims, real hardware listening.

### Dependencies fetched at configure time

`CMakeLists.txt` uses `FetchContent` to pull three third-party source trees via git at configure time (requires git + network, or local overrides):

| Dependency | Pin | Override variable |
|---|---|---|
| `NeuralAmpModelerCore` | `v0.5.4` | `-DNAM_CORE_SOURCE_DIR=<path>` |
| `r8brain-free-src` | `version-3.7` (`NTC_R8BRAIN_REF`) | `-DR8BRAIN_SOURCE_DIR=<path>` |
| SoXR (only for Ooura FFT4G `src/fft4g.c`) | `0.1.3` | `-DNTC_OOURA_SOURCE_DIR=<soxr-source-path>` |

NeuralAmpModelerCore is built as a CMake `OBJECT` library (not `STATIC`) — this is deliberate, not an oversight: NAMCore v0.5.x registers architecture parsers (WaveNet, LSTM, ConvNet, ...) via static initializers, and a normal static lib lets MSVC's linker discard parser-only translation units that nothing directly references, breaking `get_dsp()` at runtime with "No config parser registered for architecture: X". Don't change this back to `STATIC` without re-verifying every architecture still loads.

### CI

`.github/workflows/build-windows.yml` builds on every push/PR (`cmake -S . -B build -A x64`, no preset). `.github/workflows/release.yml` does the same on `v*` tags and publishes a zip (exe + `nam_input_wav.wav` + `resources/` + README + LICENSE + THIRD_PARTY.md) as a GitHub release asset. Neither workflow uses `CMakePresets.json`.

## Architecture

All application code is under `src/`, in `namespace ntc` (GP-5-specific code additionally nests in `ntc::gp5`, GP-200 in `ntc::gp200`). `src/gui.cpp` is the single Win32 entry point/message loop and owns all three tabs; everything else is a self-contained conversion or upload module it calls into.

**Conversion pipeline** (Convert to CLO tab), in the order a conversion actually runs:

1. `stimulus.cpp` — builds the 70-second mono PCM16/44.1kHz conversion stimulus from `nam_input_wav.wav` (first 50s fixed) plus either the original file's tail or a user-supplied "Recorded Audio" WAV for the final 20s (`TailMode`). An alternative stimulus WAV was A/B tested against the shipped one in 2026-09 (see "Quality investigation history" below) and found to be neutral-to-slightly-worse on the actual production path — not adopted; the shipped `nam_input_wav.wav` is unchanged.
2. `native_converter.cpp`/`.hpp` — the core reverse-engineered NAM→CLO algorithm: renders the stimulus through the NAM model (via the fetched `NeuralAmpModelerCore`), resamples with `r8brain`, and reconstructs the GP-200 1024-tap CLO byte format bit-for-bit against the original tool. Comments in this file cite the exact disassembly addresses (e.g. `GP-200.exe 0x559d80`, `HTUSBTools.dll 0x18009ad86`) the logic was reconstructed from — preserve those references when touching this file, they're load-bearing documentation of *why* the math is shaped the way it is, not incidental.
3. `corrective_ir.cpp`/`.hpp` — optional post-processing: convolves a user-selected corrective IR WAV into the CLO after native conversion, with RMS normalization and a post-gain stage.
4. `clo_refiner.cpp`/`.hpp` — optional "Tone Match" refinement: fits Block B of the CLO against a target render (NAM rendered through the same/corrected stimulus, or a real reference clip). For GP-5/GP-50 specifically, this is where the verified production wins live (direct/multi-level Block B least-squares solve — see below). Slower than a plain conversion; when enabled it replaces rather than supplements the standard output file.
5. `common.cpp`/`.hpp` — shared low-level helpers used across the above: WAV/CLO byte inspection (`CloInfo`), CRC, hex dump, UTF-8/UTF-16 path conversion, and the GP-200 CLO compare/diagnostic utilities.

Output naming convention: `<name>_NATIVE_GP200_1024.clo`, or `<name>_NATIVE_GP200_1024_TONEMATCH.clo` when Tone Match is enabled (mutually exclusive outputs, not both).

**Upload paths** (GP-200 Uploader / GP-5+GP-50 Uploader tabs) — two independent, protocol-incompatible USB-MIDI implementations, each with a `_clo_upload` (payload construction) / `_midi` (transport, device scan, ACK/retry handling) pair:

- `gp200_clo_upload.*` + `gp200_midi.*` — targets the GP-200's 10 SnapTone slots (AMP 1-5, DIST 1-5 → global slots 0-9). No per-slot name readback; the slot combo is a fixed 10-entry list.
- `gp5_clo_upload.*` + `gp5_midi.*` — targets GP-5 **and GP-50** (same hardware protocol family, see below), exposing only SnapTone slots 51-80 (zero-based 50-79) — see "GP-5/GP-50 slot range" below for why this isn't a temporary restriction. The slot combo shows real on-device SnapTone names, read via `ntc::gp5::readSnapToneCatalogue` (selector `0x24`) and populated by `populateGp5SlotCombo` in `gui.cpp`.

The GP-5/GP-50 upload path always adapts the source CLO **in memory** immediately before transfer (extracting A128/B512 from a larger CLO into the wrapped VTSI transfer format); the file on disk is never modified. Transfer framing: 19-byte payload chunks under command `0x92`, CRC-8 (poly `0x07`), nibble-encoded SysEx, one ACK (`B2 01 00 03 14 08 00`) required per block before the next is sent, completion signaled by `CE 01 00 06 12 1B 03 00 00 00`.

**GP-5/GP-50 SnapTone catalogue auto-refresh (2026-09-05).** After a successful upload, `gui.cpp`'s `WM_APP_GP5_UPLOAD_DONE` handler now calls `refreshGp5CatalogueAsync(hwnd)` so the slot combo's on-device names update automatically to reflect the newly-uploaded tone, instead of requiring the user to click "Rescan" manually. `refreshGp5CatalogueAsync` already no-ops safely if a scan/upload is in progress, so this is a pure UX fix with no new gating logic.

### GP-5/GP-50 slot range — not a placeholder

The GP-5 and GP-50 both ship with the same SnapTone capacity: 50 factory-preloaded + 80 total, i.e. exactly 30 user-uploadable slots (51-80). This was confirmed against both reverse-engineered captures and official specs, so the `slot < 50 || slot >= 80` bound in `gp5_clo_upload.cpp` and the `for (int i = 51; i <= 80; ++i)` combo population in `gui.cpp` are correct for both devices, not a GP-5-only limitation waiting to be widened for GP-50.

What *is* still unconfirmed for GP-50 specifically: whether its wire protocol (per-block ACK bytes, final completion message) is byte-identical to GP-5's. The current code assumes it is and reuses the GP-5 upload path unchanged; on a completion timeout it surfaces the last decoded SysEx message received so real GP-50 hardware testing can confirm or correct this assumption. Don't "fix" the GP-5/GP-50 sharing by forking a separate GP-50 code path without new hardware-capture evidence that the protocols actually diverge.

### Compact CLO format (GP-5/GP-50 transfer representation)

Magic `VTSI`/`HTSI`, FIR A = 128 taps, FIR B = first 512 taps of a larger CLO's Block B, declared size `0x0A88`, payload size `0x0A00`, CRC16/MODBUS recalculated on adaptation. Preceded by a reconstructed 74-byte SnapTone wrapper (destination slot + name) for the full 2770-byte transfer payload (146 blocks: 145×19 bytes + 1×15 bytes).

## What's actually shipping today (GP-5/GP-50 conversion quality)

This project went through an extensive quality-investigation phase (2026-08 through 2026-09); the full blow-by-blow history with all measured numbers has been trimmed from this file for length but remains in git history (`CLAUDE.md` at earlier commits, particularly the pre-2026-09-05 version) and in `test_assets/quality_results/*` (gitignored locally, not in the repo) for anyone who wants the raw evidence. What actually ships, and why, condensed:

**Shipped and verified (production path, `convertNamToClo`'s GP-5/GP-50 Tone Match gate):**

- **Direct Block B least-squares solve** (`solveBlockBLeastSquares`, `clo_refiner.cpp`) replaces the older "compute a correction IR and convolve it into B" approach. Verified as a large, real, held-out spectral/waveform fidelity win across a broad amp corpus (clean through extreme-high-gain, multiple brands) — never regresses.
- **Multi-level shared Block B solve** (`sweepKAndSolveSharedB` with a fixed `kMultiplier=1.0`, i.e. the closed-form B-only mechanism, not a P/K search) solves B jointly across a 6-level gain sweep of the Tone Match reference clip instead of one operating point. Verified as a smaller but genuine, never-regressing win directly on the true GP-5/GP-50 512-tap format (roughly 10-19% dynamics/fidelity improvement) — an earlier, much larger-looking result for this same feature turned out to have been measured against mislabeled GP-200-format "official" files, not real GP-5/GP-50 output; the 10-19% figure is the corrected, trustworthy one.
- All three candidates (do nothing / correction-IR / direct B solve / multi-level B solve) are compared on the same evaluation target before shipping, so the multi-level candidate only wins by actually scoring better, not by construction.
- The final linear gain-match step and the reverse-engineered `fitPk()`/`fitAB()` core fitting logic are unchanged from the original Valeton-style reconstruction.

**Tried and deliberately NOT shipped:**

- **Dynamics-aware P/K search** (`searchPkForDynamics`, `clo_refiner.cpp`): a coordinate-descent search that reopens the P/K shaper (frozen everywhere else) specifically to fix a real, measured dynamics-tracking gap on high/extreme-gain amps (GP-5/GP-50 "saturates too late" relative to the real amp as level drops). This looked like a strong, broadly-verified win on this project's own automated metrics (a zero-anchored relative-dynamics RMS error, later hardened with a spectral-shape regression gate after an early version let through a real fidelity regression on one amp). **Real hardware listening overruled the automated metrics**: converted patches came out substantially quieter than the official SnapTone conversion, and rolling off guitar volume mostly just made them quieter rather than cleaning up distortion the way a real amp (and the official converter) does — the automated dynamics-tracking metric was blind to exactly this failure mode. `NativeConverterConfig::dynamicsAwareFitting` now defaults to `false` and there is no GUI control for it (the checkbox was removed, not just unchecked). The search code, its safety gates, and the CLI tooling to re-run it remain in the codebase as an opt-in research path — do not re-enable it by default without new hardware-listening evidence, not just better automated-metric numbers.
- **Alternative conversion stimulus WAV** (2026-09-05): a user-supplied alternative `nam_input_wav.wav`, claimed to improve conversion quality, was A/B tested against the shipped file using the actual production code path (`--valeton-comparison`, `Auto` Tone Match reference — not `--quality-experiment`'s own separate scoring logic, which was tried first and gave a misleading result: a large apparent regression on one amp that did not reproduce at all against the real production path). Result across 8 amps spanning the full gain range: neutral on most amps, a small win on 2, and a real (if moderate) fidelity regression on one mid/high-gain amp (JCM800 High Gain: correlation -0.03, ESR +12.8%). Not adopted; the shipped stimulus file is unchanged. Lesson for future stimulus/algorithm A/B tests: prefer the actual production path (or a tool that calls `convertNamToClo` directly with `Auto` reference) over `--quality-experiment`'s bespoke scoring, which measures something real but is not a reliable stand-in for shipped behavior.
- A GP-5/GP-50 "pure" direct-fit P/K+A/B candidate, a Post-biquad frequency-scale search, three separate attempts at closing an EQ/tonal-balance gap on the B side (multi-clip B solve, frequency-weighted B solve, a gentle EQ-match correction), and a CloPlayer-default-gain hypothesis for an official-vs-ours gain offset — all measured, all either found no real win or actively made things worse, none shipped. Not re-detailed here; see git history if resurrecting one of these lines of investigation.

**Known, characterized, unfixed limitation:** on hard-driven (crunch through extreme-gain) amps, the shared oversampled P/K nonlinearity produces a harmonic-balance asymmetry (even harmonics suppressed, odd harmonics over-produced relative to Full A2) that gets *worse* at high frequency in a way consistent with aliasing rather than organic distortion. Isolated-stage testing traced this specifically to the U2 upsampling (88.2kHz→176.4kHz) allpass stage, which shows a severe, monotonic image-suppression breakdown in the top ~10-20% of its input band (from roughly -50dB suppression down to actually *amplifying* the image by +26dB at the extreme). The downstream decimation stage (D1/D2) is healthy and not the cause. This is real and reproducible but not root-caused to a specific fix — redesigning U2's allpass coefficients or band-limiting ahead of it are the two candidate directions, neither attempted. Notably, Valeton's own official converter shows a comparable or sometimes worse version of the same even/odd asymmetry, suggesting this may be an architectural ceiling of the single-memoryless-nonlinearity design rather than a defect unique to this project's fit.

### Research/diagnostic CLI tooling

`gui.cpp` dispatches a large number of headless `--<flag>` CLI entry points (quality experiments, level-response sweeps, P/K dynamics search/audition, harmonic/aliasing/imaging diagnostics, official-SnapTone benchmarking, frequency-response comparison, etc.) built up during the investigation summarized above. These are internal research/measurement tools, not user-facing features — none of them are reachable from the GUI, and removing or breaking one doesn't affect the shipped conversion or upload behavior. They're left in the codebase (compiled into the same `NamToClo.exe`) because they're the fastest way to re-verify or extend any of the findings above; if actually cleaning these out, `git grep -- '--' src/gui.cpp` and cross-reference against this section first, since a few duplicate methodology across files (e.g. `native_converter.cpp` and `clo_refiner.cpp` each have their own simplified spectral-loss helper, documented at their definitions as intentional duplication).

## Licensing / attribution notes

This is an independent research/reimplementation project, not affiliated with or endorsed by Valeton or Hotone. `THIRD_PARTY.md` tracks the three fetched dependencies (all MIT-licensed) and `CMakeLists.txt` installs their upstream LICENSE files alongside the built exe — if you change how a dependency is fetched or vendored, keep that install step in sync. `test_assets/` (real official SnapTone captures, real NAM models, copyrighted-content-named test clips) is gitignored and must stay that way — it is local research material, not something to publish.
