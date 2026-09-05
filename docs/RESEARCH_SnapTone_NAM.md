# SnapTone / NAM → CLO Research Notes

Research pass comparing this repo's reverse-engineered NAM→CLO pipeline against public
GitHub reverse-engineering of Valeton's SnapTone format, and against Valeton's own public
documentation of how NAM support works on the GP series. Compiled 2026-08-29.

## 1. What SnapTone actually is (confirmed from official sources)

Valeton GP-series pedals (GP-200, GP-200LT, GP-100, GP-5, GP-50) do **not** run NAM neural
network inference on-device. They convert `.nam` files into Valeton's own **SnapTone**
format offline (desktop software), then play back the converted SnapTone model on
hardware.

- GP-200/GP-200LT: the desktop editor "will convert the file and push it to the device."
- GP-5: has a dedicated "N→S" (NAM → SnapTone) conversion tool in its editor.
- GP-5 spec sheet separately advertises "up to 80 NAM and 20 IR slots" — this appears to
  be a slot-count/marketing description of converted SnapTone captures, not evidence of
  raw `.nam` playback on that device family. (See §4 for a genuinely different feature —
  direct `.nam` loading — that should not be confused with this.)

Sources:
- [TONE3000: Use TONE3000 NAM Captures on Valeton GP Pedals](https://www.tone3000.com/blog/tone3000-valeton-nam-guide)
- [GuitarPedalX: A simple overview of the Valeton GP-50 SnapTone and IR Loader](https://www.guitarpedalx.com/news/gpx-blog/a-simple-overview-of-the-valeton-gp-50-multi-effects-processor-snaptone-and-ir-loader)

This confirms the premise of this repo: SnapTone/CLO is a distinct, lightweight,
proprietary capture format, not a container for NAM weights, and there is no vendor
runtime dependency to lean on.

## 2. A second, independent reverse-engineering project

[`drewmerc302/valeton-gp50`](https://github.com/drewmerc302/valeton-gp50) is a
browser-based WebMIDI editor for GP-50/GP-5, built by reverse-engineering the device's
SysEx protocol from scratch (no vendor SDK). Repo layout: `app/` (FastAPI + WebMIDI
frontend), `a2a1/` (superseded NAM→SnapTone tooling, see §4), `re/` (RE captures/scripts),
`design/` (format research notes), `refs/` (sample models/DI files), `docs/`.

**Key finding: they have not solved the NAM→SnapTone fitting algorithm.** Their own
`re/REFIT_FINDINGS.md` (quoted in full below, §3) states the SnapTone payload-generation
routine (`AppNamConvertThread::run()` → `startClone`, ~32.5 KB of code in the vendor
dylib) remains unresolved, and that until it's cracked, SnapTone payloads for their tool
"must come from a Suite conversion (capture)" — i.e. they cannot independently generate a
SnapTone file from a `.nam` model at all.

**This repo's `src/native_converter.cpp` has already solved that exact problem** via a
full disassembly-level reconstruction of GP-200.exe / HTUSBTools.dll: the PK exponential
shaper, 128-tap FIR A / 2048-tap FIR B partitioned convolution engine, pre/post biquads,
minimum-phase reconstruction, and the 3-phase (`sweep` / `low-level` / `multi-level`)
iterative A/B fitting loop (`fitAB`, `optimizePhase`) plus final tail correction
(`refineB`). This is a materially more complete piece of reverse-engineering than what is
public elsewhere for this exact problem.

## 3. Raw capture: `re/SNAPTONE_PROTOCOL.md` (verbatim)

Fetched directly from
`https://raw.githubusercontent.com/drewmerc302/valeton-gp50/master/re/SNAPTONE_PROTOCOL.md`
on 2026-08-29:

> # GP-50 write-packet protocol (reverse-engineered)
>
> Recovered from `5868USB.dylib::getMidiMessage` (Ghidra) + verified against a MIDI
> Monitor spy capture of two SnapTone imports (**298/298 packets match**).
>
> ## Packet format (host → device)
>
> Every host→device SysEx is built from a small **pre-nibble buffer `BUF`**, then each
> byte is split into two 4-bit nibbles (hi first) and wrapped in `F0 … F7`:
>
> ```
> BUF (bytes, before nibble-encoding):
>   [0]  CHECKSUM   (CRC-8, see below)
>   [1]  command    (0x92 for SnapTone data-write blocks; other ops use other values)
>   [2]  block index  (0,1,2,… running within the transfer)
>   [3]  length     (# of payload data bytes; 0x13 = 19 for full blocks)
>   [4…] payload    ([3] data bytes)
>
> wire = 0xF0, then for each byte b in BUF: (b >> 4), (b & 0x0F), then 0xF7
> ```
>
> So a 19-byte-payload block is `4 + 19 = 23` BUF bytes → `2*23 = 46` nibbles →
> `48` bytes on the wire (`F0 … F7`). The device ACKs each with a fixed 16-byte status
> `F0 0B 02 00 01 00 00 00 03 01 04 00 08 00 00 F7`.
>
> ## Checksum — SOLVED
>
> **CRC-8, polynomial 0x07 (CRC-8/SMBUS), init 0x00, no reflection, no final XOR.**
> Computed over the **entire `BUF`** — command, index, length, and payload — with the
> checksum byte `BUF[0]` held at `0` during the computation. The 256-entry table lives in
> the dylib at vaddr `0xf5f10` (shared with the bundled FLAC decoder, which also uses
> CRC-8/0x07). Reference code (`a2a1`-style):
>
> ```python
> TBL = build_crc8_table(0x07)              # standard CRC-8/SMBUS table
> def checksum(buf):                        # buf[0] is the (zeroed) checksum slot
>     c = 0
>     for b in buf:
>         c = TBL[c ^ b]
>     return c                              # -> nibble-split into wire bytes 1,2
> ```
>
> Verified: `re/verify_crc.py` → 298/298.
>
> ## What this unblocks
> - We can now **build valid write packets ourselves** (compute the CRC, nibble-encode,
>   frame). This is the gate for the device-write product features (5/6 reassign/replace,
>   and 4 once SnapTone payloads can be generated).
> - **Still open:** how the ~2.7 KB SnapTone payload itself is produced from a NAM (the
>   refit). That runs on the worker thread `threadEntryProc` in `5868USB.dylib` — next
>   Ghidra target. Until then, SnapTone payloads must come from a Suite conversion (capture).
>
> ## Files
> - `re/5868USB_arm64.dylib` — extracted arm64 slice analyzed in Ghidra.
> - `re/DecompileValeton.java`, `re/FindCrcUser.java` — Ghidra headless scripts.
> - `re/ghidra_valeton_out.txt`, `re/ghidra_crc_user.txt` — decompiler output.
> - `re/dump_table.py` (confirm poly), `re/crc07_crack.py`, `re/verify_crc.py` (298/298).

### Cross-check against this repo's code

- Command `0x92`, 19-byte payload blocks, `F0 … F7` nibble framing, CRC-8 poly `0x07` —
  all match `gp5_clo_upload.cpp` / `gp5_midi.cpp` / CLAUDE.md exactly.
- **The GP-50 ACK resolves the "unconfirmed" note in CLAUDE.md.** `gp5_midi.cpp:214`
  expects the *decoded* (nibble-pairs-merged) ACK `B2 01 00 03 14 08 00` (7 bytes). Their
  raw capture shows a 16-byte SysEx frame `F0 0B 02 00 01 00 00 00 03 01 04 00 08 00 00 F7`
  — i.e. `F0` + 14 payload bytes + `F7`. Decoding those 14 bytes as 7 nibble pairs
  `(hi<<4)|lo`:

  ```
  0B,02 -> 0xB2
  00,01 -> 0x01
  00,00 -> 0x00
  00,03 -> 0x03
  01,04 -> 0x14
  00,08 -> 0x08
  00,00 -> 0x00
  ```

  Decoded result: `B2 01 00 03 14 08 00` — **an exact match** to the ACK this repo's
  `gp5_midi.cpp` already expects. Since their capture was taken on real GP-50 hardware,
  this is independent confirmation that the GP-5 upload protocol (ACK included) is
  byte-identical on GP-50, resolving the open question flagged in CLAUDE.md's "GP-5/GP-50
  slot range" section.

## 4. `nam-a2a1-converter` — a different, unrelated feature

[`drewmerc302/nam-a2a1-converter`](https://github.com/drewmerc302/nam-a2a1-converter),
spun out of the `a2a1/` folder of the main repo, targets a **separate** capability: some
Valeton pedals (GP-5, GP-50, GP-150 per their README) can load real `.nam` **A1**-format
models directly, without going through SnapTone/CLO at all. Do not confuse this with the
SnapTone/CLO pipeline — it's a firmware feature for native NAM playback, orthogonal to
everything in this repo.

Since A2 and A1 are different architectures with incompatible weights, their tool does
**knowledge distillation** rather than weight conversion:

> A2.nam ──render DI──▶ teacher.wav ──train an A1 to match──▶ A1.nam

Because the "teacher" signal (A2's output on a fixed DI) is deterministic and noise-free,
they note the resulting A1 clone is typically "a *tighter* match to the A2 than a
real-amp capture is to its amp."

Training presets: Draft (20 epochs, quick sanity check), Standard (60 epochs, default),
Best (120 epochs, diminishing returns). Runs on NAM 0.13.0 (loads A2, trains A1) but
exports 0.7.0 format; since A1-only devices need 0.5.x format, they do a pure Python
config reshape (weights stay byte-identical) rather than retraining for the older format.

**Relevance to this repo:** none directly — this is a different playback path (raw NAM A1
inference on-device) vs. this repo's SnapTone/CLO (FIR+shaper capture format). If GP-5/
GP-50 firmware genuinely exposes user-loadable native NAM slots as a separate feature from
SnapTone, that would be the higher-fidelity path when available, since it sidesteps
CLO's fixed, lossy representational capacity entirely. This is unconfirmed for this
project's actual target devices/firmware and would need direct hardware verification —
it is not something to build against speculatively.

## 5. Structural cross-validation of the SnapTone model shape

Their `re/REFIT_FINDINGS.md` (AI-summarized extraction, not verified verbatim like §3)
describes the on-device model, before it was marked unsolved, as:

- Pipeline: `AppNamConvertThread::run()` → normalize DI (`nam_input_wav.wav`, same
  bundled reference file this repo uses) → run genuine NAM WaveNet inference via a
  statically-linked NeuralAmpModelerCore (they even name
  `nam::activations::Activation::using_fast_tanh`) to get the target → `startClone` fits
  a compact proprietary model to (DI, NAM-output).
- Compact model components they identified: an "IR convolver" (`convolverInit`,
  `zzy_ir_instance_f32`), nonlinear shaping via `log2`/`exp2`/`expf`, "small dimensions
  (loops bounded at 16)," float→byte quantization, ~2755-byte payloads (~20× smaller than
  source NAM weights).

This is broadly consistent with (not a byte-level confirmation of) this repo's
`native_converter.cpp` model: FIR-A (128 taps) + PK exponential shaper (`expf`-based,
matches their `log2/exp2/expf` note) + FIR-B (2048 taps) + pre/post biquads, driven by the
same bundled `nam_input_wav.wav` stimulus concept, producing a similarly-sized CLO
(`kCloBytes = 0x2288` bytes total for the 2048-config core). Treat this section as
corroborating evidence, not independent verification — they never got far enough to
publish exact byte offsets for the fit itself.

## 6. Open question this repo could test: Full vs. Lite A2 submodel

`native_converter.cpp:prepareFullA2` always extracts the **highest-`max_value`** ("Full")
submodel from an A2 `SlimmableContainer` before conversion — replicating GP-200.exe's own
selection behavior exactly. That's correct as a *reimplementation of the official tool*,
but it's a separate question from "does using Full actually produce the best-sounding
CLO," because:

- CLO's representational capacity is fixed and small: one 128-tap FIR, one 2048-tap FIR,
  a single memoryless exponential shaper, and two biquads.
- The `fitAB` phases (`sweep`, `low-level`, `multi-level`, 3+2+5 iterations) are a
  loss-minimizing search (`lossFromRatioF`) against whatever residual is left after that
  fixed structure — there's a real possibility that a high-capacity Full A2 submodel has
  nonlinear/memory behavior the CLO structure structurally cannot represent, leaving more
  residual error than a lower-capacity Lite submodel would, even though Full is the more
  accurate model of the real amp on its own.

**Suggested experiment** (not yet implemented): add a build-time or config option to
`prepareFullA2` to select the *lowest*-`max_value` submodel instead, run both Full-sourced
and Lite-sourced conversions through the identical `fitAB`/`refineB` pipeline on the same
NAM file, and compare (a) the final `lossFromRatioF` residual from each fit and (b) an A/B
listening test of the resulting CLOs. If Lite's post-fit loss is consistently lower, that
would justify exposing submodel choice as a real quality knob rather than always mirroring
official Full-only behavior. This has not been tested — it's a hypothesis worth checking
before acting on it.

## 7. Summary / actionable items

1. **Resolved**: GP-50 SnapTone upload ACK is byte-identical to what `gp5_midi.cpp`
   already expects, per independent hardware capture (§3). CLAUDE.md's "unconfirmed"
   caveat on GP-50 protocol identity can be considered addressed for the ACK path
   specifically (the final completion message `CE 01 00 06 12 1B 03 00 00 00` was not
   separately re-verified against their capture and remains unconfirmed).
2. **Not actionable, no code change implied**: this repo's NAM→CLO fitting algorithm is
   more complete than the only other public implementation found; no gaps to close there.
3. **Untested hypothesis worth an experiment**: Lite vs. Full A2 submodel selection before
   fitting (§6) — could reduce fitting residual, at the cost of no longer mirroring
   official GP-200.exe behavior exactly.
4. **Out of scope / different feature**: native on-device NAM A1 loading (§4) is a
   separate playback path from SnapTone/CLO and not something to build against without
   first confirming it exists on the actual target firmware/devices.
