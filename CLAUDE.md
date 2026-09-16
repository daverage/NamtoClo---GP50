# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A cross-platform converter that turns Neural Amp Modeler (`.nam`) models into `.clo` files compatible with Valeton GP-200/GP-5/GP-50 hardware, and uploads `.clo` files directly to those devices over USB MIDI. It reimplements undocumented vendor behavior (the GP-200 native NAM→CLO conversion algorithm and the GP-5/GP-50 SnapTone upload protocol) reverse-engineered from the official tools and USB-MIDI captures — this is *not* a wrapper around Valeton's own software. There is no runtime dependency on Valeton Suite.

Two front ends ship from the same portable conversion/protocol core (`src/core`, see "Cross-platform architecture" below):

- **Windows x64**: `NamToClo.exe`, a Win32 GUI (`src/gui/gui.cpp`) — the original, full-featured application.
- **macOS (Apple Silicon first)**: `namtoclo`, a CLI (`src/cli/main.cpp`) — conversion, MIDI device discovery, SnapTone catalogue reading, and GP-5/GP-50 upload, driven over CoreMIDI. A native macOS GUI is a planned follow-up (see "macOS port" below); the CLI is a complete, hardware-capable engineering interface in the meantime.

This is an independent research/reimplementation project, not affiliated with or endorsed by Valeton or Hotone, and is a public fork of [Goaltoday/NamtoClo](https://github.com/Goaltoday/NamtoClo) (see README for what's different from upstream).

## Build

Requires CMake 3.24+ and a 64-bit toolchain. The CMake project configures on Windows x64 (MSVC) and macOS (Clang, Apple Silicon first) — it hard-fails on anything else.

### Windows

```powershell
cmake --preset windows-x64
cmake --build build --config Release --parallel
```

The `windows-x64` preset in `CMakePresets.json` pins a Visual Studio generator version — update it to match whatever VS major version is actually installed on the machine (it does not auto-detect).

Output executable: `build/Release/NamToClo.exe`. To run a build, copy `nam_input_wav.wav` (repo root) next to the exe — the Convert tab requires it at runtime and it's not copied by the build. `resources/reference_clips/*.wav` (Tone Match "Auto"/named reference clips) and `resources/icons/*` are copied next to the exe automatically as a post-build step.

### macOS

```bash
cmake --preset macos-arm64
cmake --build build-macos --parallel
```

(Requires Ninja: `brew install ninja`. `git`/network access, or the `-D*_SOURCE_DIR` overrides below, for the fetched dependencies.) Output executable: `build-macos/namtoclo`. `nam_input_wav.wav` and `resources/reference_clips/` are copied next to it automatically, same convention as Windows. Usage: `namtoclo --help`; see "macOS port" below.

For the native macOS GUI (a `.app` a normal user can launch from Finder, wrapping this same CLI -- see "macOS SwiftUI GUI" below):

```bash
scripts/build_macos_app.sh
open build-macos-app/NamToClo.app
```

This one script builds the CLI (CMake), builds the SwiftUI app (SwiftPM), and assembles+ad-hoc-signs the bundle -- no manual copying of binaries between the two build systems.

There is no GUI test suite and no linter configured in this repo. Verification is done via headless CLI flags on the built exe/CLI (see "Research/diagnostic CLI tooling" below) plus manual GUI smoke tests and, for conversion-quality claims, real hardware listening. A small protocol unit-test target (`ctest`, no hardware required) covers GP-5/GP-50 payload construction — see "macOS port" below.

### Dependencies fetched at configure time

`CMakeLists.txt` uses `FetchContent` to pull three third-party source trees via git at configure time (requires git + network, or local overrides):

| Dependency | Pin | Override variable |
|---|---|---|
| `NeuralAmpModelerCore` | `v0.5.4` | `-DNAM_CORE_SOURCE_DIR=<path>` |
| `r8brain-free-src` | `version-3.7` (`NTC_R8BRAIN_REF`) | `-DR8BRAIN_SOURCE_DIR=<path>` |
| SoXR (only for Ooura FFT4G `src/fft4g.c`) | `0.1.3` | `-DNTC_OOURA_SOURCE_DIR=<soxr-source-path>` |

NeuralAmpModelerCore is built as a CMake `OBJECT` library (not `STATIC`) — this is deliberate, not an oversight: NAMCore v0.5.x registers architecture parsers (WaveNet, LSTM, ConvNet, ...) via static initializers, and a normal static lib lets MSVC's linker discard parser-only translation units that nothing directly references, breaking `get_dsp()` at runtime with "No config parser registered for architecture: X". Don't change this back to `STATIC` without re-verifying every architecture still loads.

### CI

`.github/workflows/build-windows.yml` builds `NamToClo.exe` on every push/PR (`cmake -S . -B build -A x64`, no preset). `.github/workflows/build-macos.yml` builds the `namtoclo` CLI on `macos-14` (Apple Silicon runner) on every push/PR, using the `macos-arm64` preset, and runs `ctest` (the hardware-free protocol test). `.github/workflows/release.yml` does the Windows build on `v*` tags and publishes a zip (exe + `nam_input_wav.wav` + `resources/` + README + LICENSE + THIRD_PARTY.md) as a GitHub release asset; it does not yet package a macOS release artifact.

## Architecture

All application code is under `src/`, in `namespace ntc` (GP-5-specific code additionally nests in `ntc::gp5`, GP-200 in `ntc::gp200`), split into a portable core and thin platform layers:

```
src/
  core/       portable C++20: conversion algorithm, protocol payload
              construction, protocol orchestration (ACK/retry/catalogue
              parsing) -- everything except raw MIDI I/O and OS calls
  platform/
    windows/  Win32/WinMM implementations of the two seams below
    macos/    CoreMIDI/POSIX implementations of the same two seams
  gui/        Win32 GUI (gui.cpp) -- Windows only, unchanged behavior
  cli/        namtoclo CLI (main.cpp) -- cross-platform
tests/        hardware-free protocol tests (ctest)
```

`src/gui/gui.cpp` is the single Win32 entry point/message loop and owns all three tabs; the CLI (`src/cli/main.cpp`) is the macOS-first equivalent (`convert` / `midi-list` / `slots` / `upload` / `clo-info`, `--json` for machine-readable output). Both link the same `namtoclo_core` static/object target and never duplicate conversion or protocol logic between them.

**Platform boundary.** Everything in `src/core` is platform-neutral except two narrow seams, each with exactly one implementation per OS:

- `platform.hpp` (`toUtf8`/`fromUtf8`, `executablePath`, `platformErrorMessage`) — implemented by `platform/windows/platform_win.cpp` (Win32) and `platform/macos/platform_mac.cpp` (`_NSGetExecutablePath` + UTF-8/UTF-32 conversion, since `wchar_t` is 32-bit on macOS vs 16-bit on Windows; every caller treats `wstring` as an opaque Unicode container, never as UTF-16 code units, so this substitution is transparent).
- `midi_transport.hpp` (`MidiTransport`: `listInputs`/`listOutputs`/`openInput`/`openOutput`/`sendMessage`/`close`) — implemented by `platform/windows/midi_transport_winmm.cpp` (WinMM) and `platform/macos/midi_transport_coremidi.cpp` (CoreMIDI, including SysEx packet reassembly since CoreMIDI can split one logical SysEx across multiple `MIDIPacket`s).

`gp5_midi.cpp` and `gp200_midi.cpp` are themselves fully portable: nibble encoding/decoding, CRC-8, ACK/completion byte matching, retry policy, and SnapTone catalogue parsing are all written once against `MidiTransport` and never differ between platforms. Do not spread `#ifdef`s through `src/core` for a platform difference — extend `platform.hpp` or `midi_transport.hpp` instead.

**Conversion pipeline** (Convert to CLO tab / `namtoclo convert`), in the order a conversion actually runs:

1. `stimulus.cpp` — builds the 70-second mono PCM16/44.1kHz conversion stimulus from `nam_input_wav.wav` (first 50s fixed) plus either the original file's tail or a user-supplied "Recorded Audio" WAV for the final 20s (`TailMode`). An alternative stimulus WAV was A/B tested against the shipped one in 2026-09 (see "Quality investigation history" below) and found to be neutral-to-slightly-worse on the actual production path — not adopted; the shipped `nam_input_wav.wav` is unchanged.
2. `native_converter.cpp`/`.hpp` — the core reverse-engineered NAM→CLO algorithm: renders the stimulus through the NAM model (via the fetched `NeuralAmpModelerCore`), resamples with `r8brain`, and reconstructs the GP-200 1024-tap CLO byte format bit-for-bit against the original tool. Comments in this file cite the exact disassembly addresses (e.g. `GP-200.exe 0x559d80`, `HTUSBTools.dll 0x18009ad86`) the logic was reconstructed from — preserve those references when touching this file, they're load-bearing documentation of *why* the math is shaped the way it is, not incidental. Bit-identical between Windows and macOS: the algorithm itself has zero platform-conditional code (verified during the macOS port — see "macOS port" below).
3. `corrective_ir.cpp`/`.hpp` — optional post-processing: convolves a user-selected corrective IR WAV into the CLO after native conversion, with RMS normalization and a post-gain stage. The IR WAV is auto-resampled to the required 44.1kHz if supplied at a different rate (2026-09-08) — `decodeCorrectiveIr` reuses `resampleForCorrectiveIr` (a thin external-linkage wrapper around `native_converter.cpp`'s `resampleR8Brain24`, declared in `native_converter_internal.hpp`) rather than rejecting the file outright; previously a 48kHz/96kHz/etc. IR failed with "Corrective IR must be 44.1 kHz." Portable core change, so both the Windows GUI and the CLI/macOS GUI benefit automatically.
4. `clo_refiner.cpp`/`.hpp` — optional "Tone Match" refinement: fits Block B of the CLO against a target render (NAM rendered through the same/corrected stimulus, or a real reference clip). For GP-5/GP-50 specifically, this is where the verified production wins live (direct/multi-level Block B least-squares solve — see below). Slower than a plain conversion; when enabled it replaces rather than supplements the standard output file.
5. `common.cpp`/`.hpp` — shared low-level helpers used across the above: WAV/CLO byte inspection (`CloInfo`), CRC, hex dump, path/UTF-8 conversion (delegating the platform-specific half to `platform.hpp`), and the GP-200 CLO compare/diagnostic utilities.

Output naming convention: `<name>_NATIVE_GP200_1024.clo`, or `<name>_NATIVE_GP200_1024_TONEMATCH.clo` when Tone Match is enabled (mutually exclusive outputs, not both).

**Upload paths** (GP-200 Uploader / GP-5+GP-50 Uploader tabs, or `namtoclo upload`/`slots`/`midi-list`) — two independent, protocol-incompatible USB-MIDI implementations, each with a `_clo_upload` (portable payload construction) / `_midi` (portable protocol orchestration over `MidiTransport`) pair:

- `gp200_clo_upload.*` + `gp200_midi.*` — targets the GP-200's 10 SnapTone slots (AMP 1-5, DIST 1-5 → global slots 0-9). No per-slot name readback; the slot combo is a fixed 10-entry list. GUI-only for now (not yet exposed in the CLI — see "macOS port" below).
- `gp5_clo_upload.*` + `gp5_midi.*` — targets GP-5 **and GP-50** (same hardware protocol family, see below), exposing only SnapTone slots 51-80 (zero-based 50-79) — see "GP-5/GP-50 slot range" below for why this isn't a temporary restriction. The slot combo/catalogue listing shows real on-device SnapTone names, read via `ntc::gp5::readSnapToneCatalogue` (selector `0x24`) — populated by `populateGp5SlotCombo` in `gui.cpp`, or printed by `namtoclo slots`.

The GP-5/GP-50 upload path always adapts the source CLO **in memory** immediately before transfer (extracting A128/B512 from a larger CLO into the wrapped VTSI transfer format); the file on disk is never modified. Transfer framing: 19-byte payload chunks under command `0x92`, CRC-8 (poly `0x07`), nibble-encoded SysEx, one ACK (`B2 01 00 03 14 08 00`) required per block before the next is sent, completion signaled by `CE 01 00 06 12 1B 03 00 00 00`. `tests/gp5_protocol_test.cpp` (`ctest`) regression-tests this framing (chunk count, CRC-8 self-consistency, slot placement) without hardware.

**GP-5/GP-50 SnapTone catalogue auto-refresh (2026-09-05).** After a successful upload, `gui.cpp`'s `WM_APP_GP5_UPLOAD_DONE` handler now calls `refreshGp5CatalogueAsync(hwnd)` so the slot combo's on-device names update automatically to reflect the newly-uploaded tone, instead of requiring the user to click "Rescan" manually. `refreshGp5CatalogueAsync` already no-ops safely if a scan/upload is in progress, so this is a pure UX fix with no new gating logic.

**GP-5/GP-50 SnapTone delete/rename (2026-09-15/16).** `ntc::gp5::deleteSnapTone` (selector `0x27`, same write envelope shape as the read-catalogue request) clears a SnapTone slot back to its "Empty N" placeholder and is confirmed working on real GP-50 hardware — exposed via `namtoclo slots --slot N --delete`. `ntc::gp5::renameSnapTone` is **not implemented** — it always returns an error, and `ntc::gp5::sendSnapToneRenameWithCommitAttempt` exists only as an experimental research function behind `namtoclo slots --slot N --rename-experimental <name>`, not a supported feature.

This took an unusually long, thorough real-hardware investigation, summarized here so it isn't re-run from scratch:

- **Ruled out: a "Patch" full-rewrite mechanism.** Ported the read-live-body (selector `0x41`) + edit-name + rewrite (command `0x1D`, 19-byte blocks) approach from the independent, hardware-verified [github.com/drewmerc302/valeton-gp50](https://github.com/drewmerc302/valeton-gp50) project. This genuinely works end-to-end (confirmed: renamed an on-device slot, visible immediately, self-commits, no ACK/completion bytes needed) — but it targets a **separate "Patch" storage area (1-80)**, not SnapTone storage (51-80): `0x1D`'s slot byte is the on-screen patch number directly (no `-1`, unlike every other command in this file). A corrected-addressing test confirmed this by successfully renaming Patch slot 80 to "SafeTest" while SnapTone slot 80's own name stayed untouched. An earlier addressing mistake briefly renamed **factory Patch 50** to "diagtest" during testing (tone/effects confirmed unaffected on real hardware — cosmetic name only); left as-is, not restored (original name unknown).
- **The real SnapTone rename opcode is selector `0x26`** — same write-envelope shape as delete's `0x27` (`[CRC][famByte][0x00][LEN][0x11][SELECTOR][SLOT][0x00][0x00][0x0F][name...]`), captured directly from Valeton Suite doing real SnapTone renames. Byte-for-byte reverse-engineered and confirmed against three independent real captures (`ZXQ987TEST`/`ZXQ988TEST`/`ZXQ989TEST`, on slots 51/52): CRC-8 (poly `0x07`, init `0`) matches exactly, slot-byte mapping matches the usual `visibleSlot-1` convention, and the name field is ASCII padded to a fixed length (confirmed to hold the app's own stated 10-character max — see below — not a longer field as first assumed from limited samples).
- **Confirmed Suite's own `0x26` write genuinely changes the SnapTone catalogue**: a full before/after diff of two real, complete `readSnapToneCatalogue`-format (`0x24`/`CMD 0x48`) catalogue reads from one real Suite session showed **exactly 2 bytes differ**, precisely the ASCII digits that changed between `ZXQ989TEST` and `ZXQ990TEST`, nothing else (no hidden revision/flag/checksum field). This rules out any "smuggled extra field" theory.
- **The identical write, sent by this codebase, has zero effect.** Reconstructed the exact same `0x26` write byte-for-byte (verified via independent Python re-implementation and via decoding a real MIDI Monitor capture of our own CLI's actual wire output — not just what our C++ code believes it sent) and confirmed via a complete, gapless capture (parsed directly from MIDI Monitor's native `.mmon` NSKeyedArchiver session file, not hand-transcribed text) that three full 1362-byte catalogue snapshots (one before, two after our write) are **100% byte-identical** — not just at the target slot, anywhere in the table. Tried and also ruled out as the missing ingredient:
  - A suspected commit/flush companion message (`[CRC=0x0A][0x02][0x01][0x03][0x00,0x00,0x00]`, decoded from a message that consistently follows every real `0x26` write in every capture) — sent it right after our write; no effect, with or without it.
  - Re-reading the catalogue within the same continuous MIDI session as the write (matching Suite's own session lifetime) instead of a fresh reconnect — no effect either way, ruling out a session-lifetime/reconnect-timing theory.
  - Replaying Suite's entire captured pre-write read sequence (selectors `0x30`, `0x40`, `0x41`, `0x20`, `0x24`, `0x1A`, `0x1C`) before sending the write, in case the device needs that context established first — no effect.
  - CoreMIDI send granularity: confirmed `midi_transport_coremidi.cpp`'s `sendMessage` already submits the whole SysEx as one `MIDIPacketListAdd`/`MIDISend` call, not chunked, so that's not a point of difference either.
- **Static inspection of `/Applications/Valeton Suite.app`** (a Flutter/Dart-AOT app, `com.valeton.valetonsuite`, using the `flutter_midi_command` plugin for MIDI) confirmed via embedded UI strings that "Rename SnapTone File" is a real, distinct on-device feature (not just local file management) and that **SnapTone names are capped at 10 characters** ("The SnapTone File's name supports up to 10 characters, including English letters, numbers and punctuation marks") — consistent with, and now explaining, the fixed-length name field found in the captures. Also found sibling struct names `IrRenameStruct` / `CloneRenameStruct` alongside SnapTone's, suggesting the `0x26`-shaped opcode family may be shared/contextual across renaming SnapTones, IRs, and clones — a detail not yet disambiguated. The actual protocol logic is compiled Dart-AOT native code inside `Contents/Frameworks/App.framework/.../App`; getting further than string search would require real disassembly of that binary, which hasn't been attempted (bigger undertaking, and third-party commercial software — get explicit sign-off before doing that).

**Where this leaves things:** every angle checkable at the logical-MIDI-message level (payload bytes, CRC, slot/name encoding, message order, commit companion, session lifetime, full pre-write sequence, CoreMIDI send granularity) has been checked and ruled out as the difference between Suite's working write and this codebase's non-working, byte-identical write. The remaining plausible explanations are below the MIDI-message layer (raw USB packet timing/framing) or in Dart-AOT-compiled logic not visible to static string search — neither has been investigated. A real SnapTone rename still needs one of: (a) finding what that remaining difference actually is, (b) a genuinely different opcode not yet captured, or (c) a "read SnapTone tone binary" command (not yet found — `readSnapToneCatalogue` only reads names/occupancy) paired with the already-working `uploadCloToGp5` (`0x92`) to re-upload the same tone under a new name. See the large comment on `renameSnapTone` in `gp5_midi.cpp` for the in-code version of this writeup; the Patch-write and experimental-`0x26` code are left in place there as working references, not dead code to remove.

### GP-5/GP-50 slot range — not a placeholder

The GP-5 and GP-50 both ship with the same SnapTone capacity: 50 factory-preloaded + 80 total, i.e. exactly 30 user-uploadable slots (51-80). This was confirmed against both reverse-engineered captures and official specs, so the `slot < 50 || slot >= 80` bound in `gp5_clo_upload.cpp` and the `for (int i = 51; i <= 80; ++i)` combo population in `gui.cpp` are correct for both devices, not a GP-5-only limitation waiting to be widened for GP-50.

What *is* still unconfirmed for GP-50 specifically: whether its wire protocol (per-block ACK bytes, final completion message) is byte-identical to GP-5's. The current code assumes it is and reuses the GP-5 upload path unchanged; on a completion timeout it surfaces the last decoded SysEx message received so real GP-50 hardware testing can confirm or correct this assumption. Don't "fix" the GP-5/GP-50 sharing by forking a separate GP-50 code path without new hardware-capture evidence that the protocols actually diverge.

### Compact CLO format (GP-5/GP-50 transfer representation)

Magic `VTSI`/`HTSI`, FIR A = 128 taps, FIR B = first 512 taps of a larger CLO's Block B, declared size `0x0A88`, payload size `0x0A00`, CRC16/MODBUS recalculated on adaptation. Preceded by a reconstructed 74-byte SnapTone wrapper (destination slot + name) for the full 2770-byte transfer payload (146 blocks: 145×19 bytes + 1×15 bytes).

## macOS port

Added 2026-09-07. The portable core (`src/core`) is unchanged in behavior on Windows and now also builds and runs natively on Apple Silicon; see "Cross-platform architecture" above for the `platform.hpp`/`midi_transport.hpp` seam this relies on.

**Status:**
- ✅ Conversion (`namtoclo convert`): builds and runs; verified end-to-end on Apple Silicon (real `.nam` → valid `VTSI` GP-200 1024-tap and GP-5/GP-50 512-tap `.clo` files, correct declared/payload sizes and magic).
- ✅ CoreMIDI device enumeration (`namtoclo midi-list`), SnapTone catalogue reading (`namtoclo slots`), and upload (`namtoclo upload --slot N`) build and run, and fail with specific, actionable errors when no device is attached (verified without hardware in this environment; the "GP50 test sequence" hardware-in-the-loop pass against a real GP-50 is still outstanding).
- ✅ `namtoclo_core` is intentionally kept an `OBJECT` library, not `STATIC` — see the code comment above its `add_library()` call. Nesting `ntc_namcore` (also `OBJECT`, for the same static-initializer reason as on Windows) inside a `STATIC` `namtoclo_core` reintroduces exactly the "No config parser registered for architecture" failure this pattern exists to avoid; CMake also does not propagate `OBJECT` library objects through a second `OBJECT` library via `target_link_libraries`, so every final executable links `ntc_namcore`/`ntc_r8brain_base` directly too (see the `ntc_link_core()` CMake function).
- ✅ `cmake/patch_r8brain_template_shadow.cmake`, applied via `FetchContent`'s `PATCH_COMMAND`: r8brain-free-src's `CFixedBuffer<T>::alignptr()` redeclares its own template parameter as `T`, shadowing the enclosing class template's `T` -- legal on MSVC, a hard `[temp.local]` error on Clang. The patch renames only that inner, internally-used parameter; no behavioral change. This is the one genuine third-party portability bug found so far; everything else in `src/core` compiled on Clang/macOS unmodified.
- ✅ Native macOS GUI (`macos/NamToCloMac`, added 2026-09-07): a SwiftUI app that is a thin presentation/orchestration layer over the `namtoclo` CLI -- it contains no conversion, CLO, CRC, SysEx, or MIDI logic of its own (see "macOS SwiftUI GUI" below for the full design). Convert and GP-5/GP-50 upload workflows are implemented and manually verified without hardware attached (device-not-found/slot-read-failure paths); the GP-50 hardware-in-the-loop pass is still outstanding, same caveat as the CLI's own MIDI paths below. The GP-200 tab (added 2026-09-16, see the bullet immediately below) is now a real, working upload view, not a placeholder.
- ✅ GP-200 CLI subcommand (2026-09-16): `namtoclo gp200-upload <file.clo> --slot N|AMP1..AMP5|DIST1..DIST5 [--debug-midi] [--json]`, added to `src/cli/main.cpp` reusing `gp200_clo_upload.*`/`gp200_midi.*` directly (no protocol logic duplicated), mirroring `upload`'s (GP-5/GP-50) arg parsing, `--json` NDJSON `start`/`progress`/`complete` event shape, and exit-code convention exactly. `--slot` accepts either a raw global slot 0-9 or the fixed AMP1-5/DIST1-5 naming (case-insensitive), matching the Windows GUI's 10-entry combo. Verified by building `namtoclo` (macOS CLI) and running it with no device attached and no/invalid `.clo` file: fails with a specific `"Upload failed: The selected Sound Clone file does not exist."` message (same error-shape convention as GP-5/GP-50's own file-not-found path) at exit code 1, well-formed NDJSON on both the parse-error and runtime-failure paths, and exit code 2 with a clear usage message for a missing/out-of-range `--slot`. Real-hardware GP-200 upload itself is not verified (no device attached in this environment) -- same caveat as the rest of this section. The SwiftUI GP-200 tab (`Views/Gp200UploadView.swift`, new) now wraps this subcommand via a new `CLIBackend.gp200Upload()` (mirrors `upload()`'s NDJSON parsing) and `AppState.runGp200Upload()`/`gp200*` published state, with a `Gp200Slot` enum (`Models/BackendTypes.swift`) for the fixed 10-entry AMP/DIST picker; UI follows `Gp5UploadView.swift`'s conventions minus the catalogue list (GP-200 has no per-slot name readback). Verified the SwiftUI app still builds via `scripts/build_macos_app.sh` and launches without crashing with this tab wired in; real hardware interaction through the GUI is unverified, same as the CLI.
- ⬜ Conversion parity test comparing actual Windows vs. macOS output bytes/hashes for the same `.nam` (the algorithm has no platform-conditional code, so parity is expected, but hasn't been machine-verified across both OSes yet since this environment cannot run the Windows build).
- ✅ GitHub Actions macOS build job (`.github/workflows/build-macos.yml`): builds the CLI, runs `ctest`, builds the SwiftUI app via SwiftPM, and assembles/uploads `NamToClo.app` via `scripts/build_macos_app.sh` on every push/PR.

### macOS SwiftUI GUI (`macos/NamToCloMac`)

A SwiftPM executable target (not an `.xcodeproj` -- see below for why), built and driven entirely through `scripts/build_macos_app.sh` into a relocatable `NamToClo.app`. Structure:

```
macos/NamToCloMac/
  Package.swift
  Sources/NamToCloMac/
    NamToCloMacApp.swift       -- @main App entry, Cmd+O menu command
    Models/
      AppState.swift           -- @MainActor ObservableObject; all view state and
                                   @AppStorage-backed preferences (tone match toggle/
                                   reference, direct-fit toggle, last output folder,
                                   debug-MIDI toggle) live here, not in views
      BackendTypes.swift        -- hand-written structs mirroring namtoclo --json shapes
    Backend/
      ProcessRunner.swift       -- generic Process launch + NDJSON line streaming
      NamToCloBackend.swift     -- protocol NamToCloBackend + CLIBackend, the ONLY
                                   place that knows namtoclo's argument/JSON conventions
    Views/
      ContentView.swift         -- Convert / GP-5/GP-50 / GP-200 (placeholder) / Debug tabs
      ConvertView.swift, Gp5UploadView.swift, DebugView.swift
```

`CLIBackend.resolveBundled()` locates `namtoclo` at `Contents/MacOS/namtoclo` next to the SwiftUI app's own executable inside the bundle (never `$PATH`), with a `../../build-macos/namtoclo` dev-mode fallback for `swift run`/`swift build` iteration outside the packaged app. Every CLI invocation passes `--json`; `ProcessRunner` reassembles stdout into NDJSON lines (one JSON object per line) and routes stderr to the Debug tab's diagnostic log, never onto the same stream as structured output.

**Why SwiftPM instead of a hand-authored `.xcodeproj`:** an `.xcodeproj`'s `.pbxproj` is a large, easy-to-corrupt generated format not meant to be hand-written; SwiftPM's `Package.swift` is plain, reviewable Swift and `swift build -c release` produces the exact same Mach-O executable Xcode would. `scripts/build_macos_app.sh` then hand-assembles the `.app` bundle (`Contents/MacOS/{NamToCloMac,namtoclo}`, `Contents/Resources`, a generated `Info.plist`, ad-hoc `codesign`) -- this is the "one documented command" that builds the whole product end to end (CMake backend, then SwiftPM frontend, then bundle+sign); nothing needs manual copying. If a real Xcode project ever becomes necessary (e.g. for notarization or a richer asset catalog), generate it from this same source layout rather than maintaining two parallel Swift source trees.

**CLI JSON contract this GUI depends on** (`src/cli/main.cpp`): every subcommand's `--json` output is structured data on stdout ONLY (diagnostics go to stderr); long-running commands (`convert`, `upload`) emit newline-delimited JSON event objects (`{"event":"start"|"progress"|"complete",...}`) instead of a single final line, so the GUI can show incremental progress without polling or parsing human-formatted text. `upload`'s progress events carry `current`/`total` block counts (from the existing GP-5/GP-50 block-by-block ACK loop); `convert`'s carry a `message` string (the existing native converter status callback text) since the converter has no natural block-count analogue. `midi-list --json` additionally lists every CoreMIDI device the OS reports (`devices: [{id,name,input,output}]`) alongside the existing GP-5/GP-50-specific detection fields -- useful for the GUI's "all MIDI devices" disclosure, though device *selection* by id is not yet wired into the protocol layer (`gp5_midi.cpp` still only auto-detects a device it recognizes by name; see "GP-5/GP-50 slot range" above for the same one-supported-device assumption elsewhere in this codebase). Don't parse `namtoclo`'s non-`--json` human-readable output from Swift -- that format is not a stable contract and can change freely.

**Not done / left for a hardware pass:** real-GP-50-in-the-loop validation of the full SwiftUI workflow (connect → read slots → upload → verify on device) has not been run in this environment (no hardware attached); everything above is verified via the app launching, rendering, and the CLI's own error paths (no-device-found, catalogue-read-failure) surfacing correctly in the UI.

**If you touch `src/core`:** it must stay free of `<windows.h>`, `#include <CoreMIDI/...>`, or any other OS header. If a change needs new platform behavior, add it to `platform.hpp` or `midi_transport.hpp` and implement it in both `platform/windows/` and `platform/macos/`.

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

**Known, characterized, unfixed limitation:** on hard-driven (crunch through extreme-gain) amps, the shared oversampled P/K nonlinearity produces a harmonic-balance asymmetry (even harmonics suppressed, odd harmonics over-produced relative to Full A2) that gets *worse* at high frequency in a way consistent with aliasing rather than organic distortion. Isolated-stage testing traced this specifically to the U2 upsampling (88.2kHz→176.4kHz) allpass stage, which shows a severe, monotonic image-suppression breakdown in the top ~10-20% of its input band (from roughly -50dB suppression down to actually *amplifying* the image by +26dB at the extreme). The downstream decimation stage (D1/D2) is healthy and not the cause. Redesigning U2's allpass coefficients or band-limiting ahead of it were the two candidate fix directions — **both are now closed off, not just unattempted** (see the two 2026-09-16 findings immediately below): the U1/U2/D1/D2 coefficients and the 2-stage up/down topology are bit-for-bit and structurally identical to Valeton's real firmware, so this is a proven architectural ceiling of the real device's single-memoryless-nonlinearity design, not a defect in this project's reconstruction.

### EQ Match / post-fit spectral correction: re-tested broadly (2026-09-16), still a net regression

The 2026-09-02 "EQ Match" finding above (a gentle, clamped, smoothed post-fit EQ correction into Block B) was originally measured on a single amp (Meshuggah) with Tone Match always on. Re-ran it via a new `namtoclo eq-match-batch <corpusDir> <outputDir>` CLI command (`src/cli/main.cpp`, wraps the existing portable `ntc::runEqMatchExperiment`, previously Windows-GUI-only) across every real capture in `NamtoCloNAMCorpus`'s `development`+`selection` splits — 57 `.nam` files across the corpus's 15 curated amp families — run twice each (Tone Match on/off, via a new `toneMatchEnabled` parameter added to `runEqMatchExperiment`), 114 runs total, all succeeded.

**Result: EQ Match is a broad, consistent regression, confirming the single-amp finding at scale, in both Tone Match regimes.** Waveform ESR and correlation vs. Full A2 got worse in ~90%+ of amps with Tone Match on (53/57, 53/57) and a clear majority with it off (42/57, 42/57); the presence-band accuracy it's specifically meant to fix got worse in 50/57 amps in **both** regimes. Normalized spectral loss alone was roughly a coin-flip (order 50/50) — meaning a candidate can look neutral on raw magnitude-ratio loss while still moving measurably further from Full A2 on waveform and band-energy metrics, exactly the failure mode the original single-amp test warned about. It's also slightly worse layered on top of Tone Match than without it, consistent with the correction fighting work Tone Match's own B-fit already did.

Why a static, gain-neutral, band-limited EQ correction derived from measuring the mismatch makes the result *diverge* further from what it's matching against, rather than converge: (1) the real error is level/content-dependent (a nonlinearity artifact, not a fixed tonal tilt — see the harmonic-asymmetry finding above), so a filter fit to one clip is measurably wrong on others; (2) `applyEqMatchCorrection` reconstructs the measured magnitude curve as a 256-tap **minimum-phase** FIR, which introduces its own group-delay/phase behavior that waveform-domain metrics (ESR, correlation) are sensitive to even when the average magnitude match improves; (3) `applyCorrectiveIrToB44`'s RMS-renormalization back to the pre-correction energy is a single global scalar, so it doesn't respect the correction's own per-band shape and can leave the added filter's ringing/phase cost in place without reliably keeping its intended per-band benefit.

Don't re-attempt a linear post-fit EQ/spectral-matching correction on B (including a filter-bank/biquad-cascade topology, e.g. modeled after a parametric spectrum-matcher plugin, instead of a single smoothed-FIR magnitude correction) expecting a different outcome without new evidence — the mechanism above predicts any linear B-side correction fails for the same structural reason, independent of which specific filter topology derives it. `eq-match-batch`'s summary CSVs are not checked in (regenerate via the corpus, same convention as `test_assets/quality_results`).

### U1/U2/D1/D2 confirmed bit-identical to the real firmware, via `5868USB.dylib` disassembly (2026-09-16)

The macOS companion app `/Applications/Valeton Suite.app` (a Flutter/JUCE app; NAM conversion logic is NOT in the Dart AOT snapshot but in a native, **unstripped**, x86_64+arm64 universal `Contents/Frameworks/5868USB.dylib`, which statically links the same `NeuralAmpModelerCore`/r8brain dependencies this project vendors, plus JUCE) gave direct, disassembly-level confirmation of the U1/U2/D1/D2 reconstruction above, closing off "maybe our port is subtly wrong" as a possibility:

- **Coefficients are bit-exact.** `nm`'d symbols `_HT_COEFFS_UP_IIR`/`_HT_COEFFS_DOWN_IIR` (`HT` = Hotone, Valeton's parent) in the thin x86_64 slice (extract with `lipo -thin x86_64`; `__TEXT` segment has `vmaddr==fileoff==0` so symbol addresses are usable directly as file offsets) contain, byte-for-byte, the exact same float32 literals as `native_converter.cpp`'s `u1`/`u2`/`d1`/`d2` `Poly{...}` constructions (lines ~793-796, ~810-813): `u1`'s 4+3 allpass coefficients and `u2`'s 3+2 match the first 12 floats of `HT_COEFFS_UP_IIR`; `d2`'s 3+3 and `d1`'s 2+2 match the first 10 floats of `HT_COEFFS_DOWN_IIR`.
- **Topology is confirmed identical too, not just the coefficients.** Disassembling the three call-site groups of `_HT_IIRUpX2`/`_HT_IIRDownX2` (`otool -tV`, e.g. around `0x1d9af`-`0x1dbad` in the thin x86_64 slice) shows the real code calls up-stage `1` then `2` (`esi` argument), then runs a 4-lane SIMD asymmetric-`expf` block (the real P/K shaper, gated by a sign-based `blendvps` — matches this project's asymmetric-exponential PK exactly), then calls down-stage `2` then `1` (`edx` argument). That's `up(U1)→up(U2)→exp-shaper→down(D2)→down(D1)` — stage-for-stage identical to this project's `Model` chain, with no missing stage. (Both coefficient tables have a few extra float values past what U1/U2/D1/D2 use, but no call site ever passes a stage index besides `1`/`2` — those extra values belong to some other, unrelated filter in the app, not a missing oversampling stage in the NAM/CLO shaper path; that hypothesis was checked and ruled out.)

**Conclusion:** the U2 imaging-breakdown limitation documented above is now proven, not inferred, to be a property of the real Valeton/Hotone algorithm — this project's reconstruction is exact. There is no fix available here without deliberately diverging from bit-matched real hardware behavior, which would contradict this project's own stated goal of reimplementing real vendor behavior rather than building a different, independently "improved" one. Don't re-open U1/U2 coefficient or topology changes expecting a quality win without new evidence that specifically targets what a real firmware *update* changed (not just a locally-invented redesign).

### How this compares to the official algorithm (2026-09-16)

Same `5868USB.dylib` disassembly session as above, going further: the real fitting engine is `HTKPA::startClone()` (`HTKPA` is the real class name; "Clone" is Hotone/Valeton's own internal term for a converted tone — confirmed by sibling symbol names `CloneDataStruct`/`CloneRenameStruct`/`CloneListItem` in the same binary), with a companion `HTKPA::iterAmpCoeff(float*, float*, int, int, float, float, ...)` that matches this project's `fitAB()`'s per-iteration coefficient update almost function-for-function. This settles, with evidence rather than inference, what this project's reconstruction gets right vs. where it genuinely goes beyond the official algorithm:

- **Confirmed identical (in addition to U1/U2/D1/D2 above): the GP-5/GP-50 512-tap fit is very likely native, not a truncation of a 2048-tap fit.** Near the top of `startClone`, a device-mode flag (`testl %r15d,%r15d` / `cmovel`, thin-x86_64 offset `0x1a1d8`) selects an analysis-window size of `0x800` (2048) or `0x200` (512) before any A/B optimization runs, then feeds that size into a `log2`/`exp2` window calculation — i.e. the same engine natively fits at either tap budget depending on target device. This means `NativeConverterConfig::gp5DirectFit` (this project's independent direct-512 fit, see its doc comment in `native_converter.hpp`) is very probably matching official behavior, not beating a "truncate from 2048" official method the way it was originally framed when added — the truncation fallback (`gp5DirectFit=false`) is the more likely candidate for diverging from real hardware, not the default.
- **Confirmed genuinely different: Tone Match has no official equivalent at all.** The real exported conversion entry point, `namConverterCloData(juce::String name, juce::String name, std::string deviceTag, void* outBuffer, int* outSize, std::function<void(int,int)> progress, std::atomic<bool> const* cancel)`, takes no reference-audio parameter whatsoever — it's a single-shot NAM fit with no mechanism to compare against or refine toward a captured reference clip. Every part of this project's `CloRefineConfig`/Tone Match pipeline (direct Block B least-squares solve, multi-level gain-sweep solve, final output-level match) is this project's own addition on top of the reverse-engineered core, not a reconstruction of an official feature.
- **Corrective IR post-processing** (user-selected IR convolved into the CLO, RMS-normalized, auto-resampled at any input rate) is the same kind of addition — no equivalent in the single-shot official API.

**Net answer to "what do we do differently/better than official": not the core NAM-fitting algorithm** (oversampled nonlinearity and the 512-vs-2048 tap-budget fit are both now evidenced to be faithful reconstructions, not independent redesigns) **— it's Tone Match and Corrective IR**, both of which exploit having the real NAM model available at conversion time to correct residual fit error against ground truth, something the official single-shot converter has no mechanism to do at all.

### Research/diagnostic CLI tooling

`gui.cpp` dispatches a large number of headless `--<flag>` CLI entry points (quality experiments, level-response sweeps, P/K dynamics search/audition, harmonic/aliasing/imaging diagnostics, official-SnapTone benchmarking, frequency-response comparison, etc.) built up during the investigation summarized above. These are internal research/measurement tools, not user-facing features — none of them are reachable from the GUI, and removing or breaking one doesn't affect the shipped conversion or upload behavior. They're left in the codebase (compiled into the same `NamToClo.exe`) because they're the fastest way to re-verify or extend any of the findings above; if actually cleaning these out, `git grep -- '--' src/gui.cpp` and cross-reference against this section first, since a few duplicate methodology across files (e.g. `native_converter.cpp` and `clo_refiner.cpp` each have their own simplified spectral-loss helper, documented at their definitions as intentional duplication).

## Licensing / attribution notes

This is an independent research/reimplementation project, not affiliated with or endorsed by Valeton or Hotone. `THIRD_PARTY.md` tracks the three fetched dependencies (all MIT-licensed) and `CMakeLists.txt` installs their upstream LICENSE files alongside the built exe — if you change how a dependency is fetched or vendored, keep that install step in sync. `test_assets/` (real official SnapTone captures, real NAM models, copyrighted-content-named test clips) is gitignored and must stay that way — it is local research material, not something to publish.
