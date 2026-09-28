# Technical Investigations & Research

This file documents deep technical investigations and research that informed the codebase but aren't part of the shipping feature set.

## GP-5/GP-50 SnapTone rename — investigation summary

`ntc::gp5::renameSnapTone` is **not implemented**. It exists only as an experimental research function behind `namtoclo slots --slot N --rename-experimental <name>`, not a supported feature. This is the result of an extensive, thorough investigation.

### What was investigated

- **Patch full-rewrite mechanism:** ported read-live-body (selector `0x41`) + edit-name + rewrite (command `0x1D`, 19-byte blocks) from independent, hardware-verified [github.com/drewmerc302/valeton-gp50](https://github.com/drewmerc302/valeton-gp50). This genuinely works end-to-end (renamed an on-device slot, visible immediately, self-commits) — but targets a **separate "Patch" storage area (1-80)**, not SnapTone storage (51-80). A corrected-addressing test confirmed this by successfully renaming Patch slot 80 while SnapTone slot 80's name stayed untouched. (An earlier addressing mistake briefly renamed factory Patch 50 to "diagtest" during testing; tone/effects unaffected — cosmetic name only.)

- **Real SnapTone rename opcode `0x26`:** captured directly from Valeton Suite doing real SnapTone renames. Same write-envelope shape as delete's `0x27`. Byte-for-byte reverse-engineered and confirmed against three independent real captures (`ZXQ987TEST`/`ZXQ988TEST`/`ZXQ989TEST` on slots 51/52): CRC-8 (poly `0x07`, init `0`) matches exactly, slot-byte mapping matches usual `visibleSlot-1` convention, name field is ASCII padded to fixed length (10-character max, confirmed from `/Applications/Valeton Suite.app` embedded UI strings).

- **Catalogue mutation verification:** full before/after diff of two real, complete `readSnapToneCatalogue`-format (`0x24`/`CMD 0x48`) catalogue reads from one real Suite session showed **exactly 2 bytes differ**, precisely the ASCII digits that changed between `ZXQ989TEST` and `ZXQ990TEST`, nothing else. This rules out any "smuggled extra field" or hidden checksum theory.

- **Wire-level parity (MIDI-message level):** reconstructed Suite's exact same `0x26` write byte-for-byte (verified via independent Python re-implementation and MIDI Monitor decoding of actual CLI wire output). Confirmed via complete, gapless MIDI Monitor capture (parsed from native `.mmon` NSKeyedArchiver session file, not hand-transcribed) that three full catalogue snapshots (before write, after first write, after second write) are **100% byte-identical** — not just at target slot, anywhere in table. Tried and ruled out:
  - Suspected commit/flush companion message (`[CRC=0x0A][0x02][0x01][0x03][0x00,0x00,0x00]`) — sent after write; no effect with or without it.
  - Re-reading catalogue within same continuous MIDI session as write (matching Suite's session lifetime) — no effect either way.
  - Replaying Suite's entire captured pre-write read sequence (selectors `0x30`, `0x40`, `0x41`, `0x20`, `0x24`, `0x1A`, `0x1C`) before write — no effect.
  - CoreMIDI send granularity (confirmed `sendMessage` submits whole SysEx as one `MIDIPacketListAdd`/`MIDISend` call, not chunked).

- **Raw USB-level investigation (2026-09-16, Windows + USBPcap/Wireshark):** captured real Suite rename (`ZXQ994TEST`) at raw USB packet level on Windows PC, confirmed Suite's write really does update catalogue at USB level. Built `namtoclo` for Windows for first time and ran exact same experimental `0x26` write over real USB on **same PC, same physical GP-50, same capture tooling**. Wire output was **byte-identical to Suite's**, including getting same ACK and sending same commit companion, yet the catalogue **still did not change**, on either side of write, on real Windows USB.

  This also surfaced and fixed three pre-existing build bugs:
  1. `namtoclo` and `NamToClo` generated identically-named `.vcxproj` files on case-insensitive filesystem, silently clobbering one CMake target with the other's. Fixed by renaming GUI CMake target to `ntc_gui` (still ships as `NamToClo.exe` via `OUTPUT_NAME`).
  2. `namtoclo.exe` and `NamToClo.exe` collided as same path in shared output directory. Fixed by giving CLI its own `build/cli/<config>/` output directory on Windows.
  3. `namtoclo` never had `MSVC_RUNTIME_LIBRARY` set to match `namtoclo_core`/`ntc_namcore`/`ntc_r8brain_base` (all static CRT), causing `LNK2038`/`LNK2005` link errors. This had gone unnoticed because bug #1 meant `namtoclo.exe` was never actually linked standalone on Windows before.

- **Dart-AOT disassembly attempt (2026-09-16, exploratory, incomplete):** `/Applications/Valeton Suite.app` is a Flutter/Dart-AOT app with unstripped snapshot embedded in `Contents/Frameworks/App.framework/.../App` (confirmed via `nm`: `_kDartVmSnapshotData/Instructions`, `_kDartIsolateSnapshotData/Instructions` symbols, `vmaddr==fileoff==0` for `__TEXT` so symbol addresses usable directly as file offsets).

  Progress made:
  - Extracted all four raw snapshot blobs to standalone files (confirmed valid via `F5F5DCDC` Dart snapshot magic + embedded SDK version hash).
  - Confirmed exact Dart SDK version: **3.5.1 (stable)** from `FlutterMacOS.framework` strings, and snapshot's embedded flags: `product no-code_comments no-dwarf_stack_traces_mode dedup_instructions no-tsan no-msan arm64 macos no-compressed-pointers`.
  - [`blutter`](https://github.com/worawit/blutter) (standard Dart-AOT disassembly tool) only supports Android ELF input, not Mach-O. Worked around by writing minimal **synthetic ELF wrapper** (`build_fake_elf.py`) containing extracted blobs at correctly-named symbols, fed to `blutter`'s pipeline unmodified.
  - Fixed two bugs in `blutter` itself: (1) build script defined `DART_TARGET_OS_MACOS_IOS` without parent `DART_TARGET_OS_MACOS` that `dart.cc`'s `#if` chain checks first (caused `#error What operating system?`); (2) `Disassembler_arm64.h` only defined `CSREG_DART_HEAP` under `DART_COMPRESSED_POINTERS`, while `CodeAnalyzer_arm64.cpp` references it unconditionally. Also added proper `macos` `TARGET_OS` option to `blutter`'s build script.
  - **Blocked on unimplemented feature gap, not a bug:** real ELF `libapp.so` exports two more symbols our synthetic ELF doesn't have (`_kDartVmSnapshotBss`, `_kDartIsolateSnapshotBss`, per `dart_api.h`'s symbol name macros) carrying relocation-stub table the VM needs at snapshot-load time. Real Mach-O `App` binary has no equivalent symbols — Mach-O builds get this via `LC_NOTE` load command instead (per `ElfHelper.cpp`'s own abandoned comment: "`>= 2.19, LC_NOTE command is used`"), a mechanism `blutter` never implemented. Closing this gap would mean implementing Mach-O `LC_NOTE` command parsing and BSS data reconstruction — a real, new feature, not a fix.

### Conclusion

MIDI-message-level and raw-USB-level parity are both fully exhausted — every byte, every message, every order, every timing variant checkable in wire traffic has been checked and matches Suite exactly, on two OSes and two physical machines. The only unexplored avenue is Dart-AOT disassembly of Suite's actual compiled logic, which hit a real, understood blocker (missing Mach-O BSS/relocation support in the tooling) rather than a dead end from lack of trying.

A real SnapTone rename still needs one of:
1. Implementing Mach-O `LC_NOTE` BSS extraction to finish disassembly path and read Suite's rename logic.
2. A genuinely different opcode not yet captured.
3. A "read SnapTone tone binary" command (not yet found — `readSnapToneCatalogue` only reads names/occupancy) paired with the already-working `uploadCloToGp5` (`0x92`) to re-upload same tone under new name.

The extracted Dart snapshot files, synthetic-ELF builder, and patched local `blutter` checkout are scratchpad-only (not committed) — see git history/session notes for reproduction if resuming this path.

**Patch-write and experimental-`0x26` code are left in place as working references, not dead code to remove.**

## U1/U2/D1/D2 coefficient and topology verification

Confirmed bit-identical to real Valeton firmware via `5868USB.dylib` (a native, unstripped, x86_64+arm64 universal binary in `/Applications/Valeton Suite.app/Contents/Frameworks/5868USB.dylib` that statically links the same `NeuralAmpModelerCore`/r8brain dependencies this project vendors, plus JUCE).

**Coefficient verification:** `nm`'d symbols `_HT_COEFFS_UP_IIR`/`_HT_COEFFS_DOWN_IIR` in thin x86_64 slice contain, byte-for-byte, exact same float32 literals as `native_converter.cpp`'s `u1`/`u2`/`d1`/`d2` `Poly{...}` constructions (lines ~793-796, ~810-813): `u1`'s 4+3 allpass coefficients and `u2`'s 3+2 match first 12 floats of `HT_COEFFS_UP_IIR`; `d2`'s 3+3 and `d1`'s 2+2 match first 10 floats of `HT_COEFFS_DOWN_IIR`.

**Topology verification:** disassembling call-site groups of `_HT_IIRUpX2`/`_HT_IIRDownX2` (e.g. `otool -tV` around `0x1d9af`-`0x1dbad` in thin x86_64 slice) shows real code calls up-stage `1` then `2`, runs a 4-lane SIMD asymmetric-`expf` block (the real P/K shaper, gated by sign-based `blendvps` — matches this project's asymmetric-exponential PK exactly), then calls down-stage `2` then `1`. That's `up(U1)→up(U2)→exp-shaper→down(D2)→down(D1)` — stage-for-stage identical to this project's `Model` chain, with no missing stage. (Both coefficient tables have extra float values past what U1/U2/D1/D2 use, but no call site passes a stage index besides `1`/`2` — those extra values belong to some other, unrelated filter.)

**Implication:** the U2 imaging-breakdown limitation (documented in QUALITY.md) is now proven, not inferred, to be a property of the real Valeton/Hotone algorithm — this project's reconstruction is exact. Don't re-open U1/U2 coefficient/topology changes without evidence that specifically targets what a real firmware *update* changed.

## GP-5/GP-50 slot range — confirmed not a placeholder

Both GP-5 and GP-50 ship with same SnapTone capacity: 50 factory-preloaded + 80 total, exactly 30 user-uploadable slots (51-80). Confirmed against both reverse-engineered captures and official specs, so the `slot < 50 || slot >= 80` bound in `gp5_clo_upload.cpp` and `for (int i = 51; i <= 80; ++i)` combo population in `gui.cpp` are correct for both devices, not a GP-5-only limitation.

**Unconfirmed for GP-50 specifically:** whether its wire protocol (per-block ACK bytes, final completion message) is byte-identical to GP-5's. Current code assumes it is and reuses GP-5 upload path unchanged; on completion timeout it surfaces the last decoded SysEx message received so real GP-50 hardware testing can confirm or correct this assumption. Don't fork a separate GP-50 code path without new hardware-capture evidence that protocols actually diverge.

## Compact CLO format (GP-5/GP-50 transfer representation)

Magic `VTSI`/`HTSI`, FIR A = 128 taps, FIR B = first 512 taps of larger CLO's Block B, declared size `0x0A88`, payload size `0x0A00`, CRC16/MODBUS recalculated on adaptation. Preceded by reconstructed 74-byte SnapTone wrapper (destination slot + name) for full 2770-byte transfer payload (146 blocks: 145×19 bytes + 1×15 bytes).
