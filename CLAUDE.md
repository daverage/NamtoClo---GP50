# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A cross-platform converter that turns Neural Amp Modeler (`.nam`) models into `.clo` files compatible with Valeton GP-200/GP-5/GP-50 hardware, and uploads `.clo` files directly to those devices over USB MIDI. It reimplements undocumented vendor behavior (the GP-200 native NAM→CLO conversion algorithm and the GP-5/GP-50 SnapTone upload protocol) reverse-engineered from the official tools and USB-MIDI captures — this is *not* a wrapper around Valeton's own software. There is no runtime dependency on Valeton Suite.

Two front ends ship from the same portable conversion/protocol core (`src/core`):

- **Windows x64**: `NamToClo.exe`, a Win32 GUI (`src/gui/gui.cpp`) — the original, full-featured application.
- **macOS (Apple Silicon)**: `namtoclo`, a CLI (`src/cli/main.cpp`) — conversion, MIDI device discovery, SnapTone catalogue reading, and GP-5/GP-50 upload, driven over CoreMIDI. A native macOS GUI wraps this CLI via SwiftUI (`macos/NamToCloMac`).

This is an independent research/reimplementation project, not affiliated with or endorsed by Valeton or Hotone, and is a public fork of [Goaltoday/NamtoClo](https://github.com/Goaltoday/NamtoClo).

## Build

Requires CMake 3.24+ and a 64-bit toolchain. The CMake project configures on Windows x64 (MSVC) and macOS (Clang, Apple Silicon).

### Windows

```powershell
cmake --preset windows-x64
cmake --build build --config Release --parallel
```

The `windows-x64` preset in `CMakePresets.json` pins a Visual Studio generator version — update it to match the VS major version actually installed (it does not auto-detect).

Output: `build/Release/NamToClo.exe`. Copy `nam_input_wav.wav` next to the exe — the Convert tab requires it at runtime. `resources/reference_clips/*.wav` and `resources/icons/*` are copied automatically as a post-build step.

### macOS

```bash
cmake --preset macos-arm64
cmake --build build-macos --parallel
```

Requires Ninja: `brew install ninja`. Output: `build-macos/namtoclo`. `nam_input_wav.wav` and `resources/reference_clips/` are copied automatically.

For the native macOS GUI (a `.app` a normal user can launch from Finder):

```bash
scripts/build_macos_app.sh
open build-macos-app/NamToClo.app
```

This one script builds the CLI (CMake), builds the SwiftUI app (SwiftPM), and assembles the bundle.

### Dependencies

`CMakeLists.txt` uses `FetchContent` to pull three third-party source trees at configure time (requires git + network, or use local overrides):

| Dependency | Pin | Override variable |
|---|---|---|
| `NeuralAmpModelerCore` | `v0.5.4` | `-DNAM_CORE_SOURCE_DIR=<path>` |
| `r8brain-free-src` | `version-3.7` | `-DR8BRAIN_SOURCE_DIR=<path>` |
| SoXR (Ooura FFT4G) | `0.1.3` | `-DNTC_OOURA_SOURCE_DIR=<path>` |

**Important:** `NeuralAmpModelerCore` is built as a CMake `OBJECT` library (not `STATIC`). This is deliberate — NAMCore v0.5.x registers architecture parsers via static initializers, and a normal static lib lets MSVC's linker discard parser-only translation units, breaking runtime with "No config parser registered for architecture: X". Don't change this back to `STATIC` without re-verifying every architecture still loads.

### CI

- `.github/workflows/build-windows.yml`: builds `NamToClo.exe` on every push/PR.
- `.github/workflows/build-macos.yml`: builds `namtoclo` CLI on `macos-14` (Apple Silicon), runs `ctest` (protocol tests), builds SwiftUI app via `scripts/build_macos_app.sh`.
- `.github/workflows/release.yml`: on `v*` tags, publishes Windows release asset (exe + resources + README + LICENSE + THIRD_PARTY.md).

## Architecture

All application code is under `src/`, in `namespace ntc` (nested `ntc::gp5` for GP-5-specific code, `ntc::gp200` for GP-200).

```
src/
  core/       portable C++20: conversion algorithm, protocol payload
              construction, protocol orchestration — everything except
              raw MIDI I/O and OS calls
  platform/
    windows/  Win32/WinMM implementations of platform.hpp / midi_transport.hpp
    macos/    CoreMIDI/POSIX implementations of the same two seams
  gui/        Win32 GUI (gui.cpp) — Windows only
  cli/        namtoclo CLI (main.cpp) — cross-platform
tests/        hardware-free protocol tests (ctest)
```

Both GUI and CLI link the same `namtoclo_core` (OBJECT library, for the static-initializer reason above) and never duplicate conversion or protocol logic.

### Platform boundary

Everything in `src/core` is platform-neutral except two narrow seams, each with exactly one implementation per OS:

**`platform.hpp`** (`toUtf8`/`fromUtf8`, `executablePath`, `platformErrorMessage`):
- `platform/windows/platform_win.cpp`: Win32
- `platform/macos/platform_mac.cpp`: UTF-8/UTF-32 conversion via `_NSGetExecutablePath` (since `wchar_t` is 32-bit on macOS vs 16-bit on Windows; every caller treats `wstring` as opaque Unicode, so substitution is transparent)

**`midi_transport.hpp`** (`MidiTransport`: `listInputs`/`listOutputs`/`openInput`/`openOutput`/`sendMessage`/`close`):
- `platform/windows/midi_transport_winmm.cpp`: WinMM
- `platform/macos/midi_transport_coremidi.cpp`: CoreMIDI (includes SysEx packet reassembly since CoreMIDI can split one logical SysEx across multiple `MIDIPacket`s)

`gp5_midi.cpp` and `gp200_midi.cpp` are fully portable: nibble encoding/decoding, CRC-8, ACK/completion byte matching, retry policy, and SnapTone catalogue parsing are written once against `MidiTransport` and never differ between platforms. Don't spread `#ifdef`s through `src/core` — extend `platform.hpp` or `midi_transport.hpp` instead.

### Conversion pipeline

**Order** (`stimulus.cpp` → `native_converter.cpp` → `corrective_ir.cpp` → `clo_refiner.cpp`), described in detail in `QUALITY.md`:

1. **stimulus.cpp**: builds 70-second mono PCM16/44.1kHz stimulus from `nam_input_wav.wav` (first 50s fixed) + original tail or user-supplied "Recorded Audio" for final 20s.
2. **native_converter.cpp**: reverse-engineered NAM→CLO algorithm. Renders stimulus through NAM model, resamples with r8brain, reconstructs GP-200 1024-tap CLO byte format. Comments cite exact disassembly addresses — preserve these references, they're load-bearing documentation.
3. **corrective_ir.cpp**: optional post-processing. Convolves user-selected IR WAV into CLO, with RMS normalization. Auto-resamples IR to 44.1kHz if needed.
4. **clo_refiner.cpp**: optional "Tone Match" refinement. Fits Block B against target render (NAM rendered through stimulus, or real reference clip). For GP-5/GP-50, direct/multi-level Block B least-squares solve is verified as production win.

Output: `<name>_NATIVE_GP200_1024.clo`, or `<name>_NATIVE_GP200_1024_TONEMATCH.clo` when Tone Match enabled.

### Upload paths

Two independent, protocol-incompatible USB-MIDI implementations:

- **`gp200_clo_upload.*` + `gp200_midi.*`**: targets GP-200's 10 SnapTone slots (AMP 1-5, DIST 1-5 → global slots 0-9). Fixed 10-entry list. GUI-only (not yet in CLI).
- **`gp5_clo_upload.*` + `gp5_midi.*`**: targets GP-5 and GP-50 (same hardware protocol family), exposing SnapTone slots 51-80 (user-uploadable). Slot combo/catalogue listing shows real on-device SnapTone names via selector `0x24`. Transfer framing: 19-byte payload chunks under command `0x92`, CRC-8 (poly `0x07`), nibble-encoded SysEx, one ACK required per block before next is sent. Completion signaled by `CE 01 00 06 12 1B 03 00 00 00`.

The GP-5/GP-50 upload path adapts the source CLO **in memory** immediately before transfer (extracting A128/B512 into compact VTSI transfer format); the file on disk is never modified. `tests/gp5_protocol_test.cpp` (`ctest`) regression-tests this framing without hardware.

**SnapTone features:**
- **Auto-refresh after upload (2026-09-05)**: after successful upload, slot combo's on-device names update automatically via `refreshGp5CatalogueAsync()`.
- **Delete (2026-09-15)**: `ntc::gp5::deleteSnapTone` (selector `0x27`) clears a SnapTone slot back to its "Empty N" placeholder. Confirmed working on real GP-50. Exposed via `namtoclo slots --slot N --delete`.
- **Rename**: `ntc::gp5::renameSnapTone` is **not implemented**. See `INVESTIGATION.md` for why, after extensive investigation.

## macOS port

Added 2026-09-07. The portable core (`src/core`) is unchanged in behavior on Windows and now builds and runs natively on Apple Silicon via the platform boundary (`platform.hpp` / `midi_transport.hpp`).

**Status:**
- ✅ CLI conversion (`namtoclo convert`): verified end-to-end on Apple Silicon (real `.nam` → valid VTSI CLO files, correct sizes and magic).
- ✅ CoreMIDI device enumeration (`namtoclo midi-list`), SnapTone catalogue reading (`namtoclo slots`), and upload (`namtoclo upload --slot N`) build and run.
- ✅ Native macOS GUI (SwiftUI): Convert, GP-5/GP-50, and GP-200 upload tabs implemented. CLI JSON contract with stable NDJSON event streaming.
- ✅ r8brain-free-src portability patch (`cmake/patch_r8brain_template_shadow.cmake`): r8brain's `CFixedBuffer<T>::alignptr()` redeclares its own template parameter as `T`, shadowing enclosing class template's `T` — legal on MSVC, a hard `[temp.local]` error on Clang. Patch renames only that inner parameter.
- ✅ GP-200 CLI subcommand (`namtoclo gp200-upload`) added 2026-09-16, reusing `gp200_clo_upload.*`/`gp200_midi.*` directly.
- ⬜ Conversion parity test comparing Windows vs. macOS output bytes for same `.nam` (no regression expected since algorithm has no platform-conditional code, but not machine-verified across both OSes).

**If you touch `src/core`:** it must stay free of `<windows.h>`, `#include <CoreMIDI/...>`, or any other OS header. If a change needs new platform behavior, add it to `platform.hpp` or `midi_transport.hpp` and implement it in both `platform/windows/` and `platform/macos/`.

## Quality & investigation

See `PRIORITY_1_BENCHMARK.md` for:
- Complete benchmark comparison of official-style baseline (no Tone Match) vs. Tone Match variant.
- Testing across clean, moderate, and high-gain amps.
- Results on fit/selection/held-out material.
- **Conclusion:** Tone Match produces 1-2 dB RMS improvement on high-gain models with no severe regressions.

See `QUALITY.md` for:
- Detailed analysis of what's shipping for GP-5/GP-50 and why.
- Tried and rejected approaches (EQ Match, dynamics-aware P/K search, alternative stimulus).
- Known limitations (harmonic asymmetry on hard-driven amps) and why they exist.
- Algorithm comparison to the official converter (what's identical, what's new).

See `INVESTIGATION.md` for:
- Deep technical investigations (SnapTone rename, U1/U2/D1/D2 verification, Dart disassembly work).
- GP-5/GP-50 slot range rationale.
- Compact CLO format (transfer representation).

## Licensing

This is an independent research/reimplementation project, not affiliated with or endorsed by Valeton or Hotone. `THIRD_PARTY.md` tracks the three fetched dependencies (all MIT-licensed) and `CMakeLists.txt` installs their LICENSE files alongside the built exe. `test_assets/` (official SnapTone captures, NAM models, copyrighted test clips) is gitignored — local research material, not to be published.
