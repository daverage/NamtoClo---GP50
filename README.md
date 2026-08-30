# NAM to CLO v2.7.0-gp50-test1

> **This is a fork** of [Goaltoday/NamtoClo](https://github.com/Goaltoday/NamtoClo), focused on GP-5/GP-50 support and conversion quality improvements. See [Changes in this fork](#changes-in-this-fork) below for what's different from upstream.

Windows x64 application for converting Neural Amp Modeler (`.nam`) models to CLO files and uploading CLO files directly to Valeton GP-200, GP-5 and GP-50 devices over USB MIDI.

The application has three tabs:

- **Convert to CLO** — convert one NAM model or every NAM model in a folder.
- **GP-200 Uploader** — upload an existing `.clo` file to one of the 10 GP-200 SnapTone slots.
- **GP-5 / GP-50 Uploader** — adapt a compatible CLO in memory to the GP-5/GP-50 runtime format and upload it to **SnapTone 51-80**.

### GP-5 / GP-50 support in this release

GP-5 upload support was reconstructed from USB-MIDI captures of Valeton Suite and validated by successful physical uploads to a GP-5.

The GP-50 uses the same SnapTone file/model format and the same SnapTone write protocol as the GP-5 (command `0x92`, 19-byte payload chunks, CRC-8/SMBUS, nibble-encoded SysEx, and the same per-block ACK `B2 01 00 03 14 08 00`). The uploader therefore reuses the existing GP-5 SnapTone payload builder and upload path unchanged for both devices; only USB MIDI device detection was extended to also recognize a GP-50 endpoint. **GP-50 support has been confirmed by successful physical uploads and playback on a GP-50.**

The current implementation intentionally exposes only **SnapTone 51-80**, the range validated during reverse engineering. The source CLO is never modified on disk: the GP-5/GP-50 representation is created in memory immediately before transfer.

> GP-50 test build based on upstream commit `6180001` (branch `claude/gp50-snapstone-uploader-2v8wkj`).

> This is an independent research/reimplementation project and is not affiliated with or endorsed by Valeton or Hotone.

## Quick start

1. Build or download `NamToClo.exe`.
2. Keep `nam_input_wav.wav` in the **same folder as `NamToClo.exe`**.
3. Open **Convert to CLO**.
4. Select a `.nam` file or a folder containing `.nam` files.
5. Select the output folder.
6. Leave the optional processing disabled for a standard conversion, or configure **Tail / Reamp**, **Corrective IR** and/or **Tone Match** as described below.
7. Press **Convert**.

The repository includes the required `nam_input_wav.wav`. The supplied file is a 70-second, 44.1 kHz, 16-bit PCM WAV. The application adapts it internally to the mono stimulus used by the converter.

---

## Convert to CLO

![Convert to CLO tab](assets/convert-to-clo.jpg)

### Input NAM or folder

Use one of the two buttons:

- **Load NAM...** converts a single `.nam` model.
- **Load Folder...** batch-converts every `.nam` file found directly in the selected folder.

The selected path is shown in **Input NAM or folder**.

### Output folder

Choose where the generated CLO file or files will be written. **Open output folder** opens this location in Windows Explorer after conversion.

### Standard output

Each NAM conversion now produces **two** files:

```text
<name>_NATIVE_GP200_1024.clo   -- for the GP-200 Uploader (1024-tap Block B)
<name>_NATIVE_GP5GP50_512.clo  -- for the GP-5 / GP-50 Uploader (512-tap Block B)
```

The GP-5/GP-50 file is not just the GP-200 file truncated. Block B is fit twice for that
device's smaller tap budget -- once by truncating the GP-200 fit, and once by fitting a
Block B directly sized for the 512 taps GP-5/GP-50 actually plays -- and the converter
automatically keeps whichever of the two measures a lower fitting loss for that specific
NAM. Testing across several NAM captures (clean through extreme high-gain) found the
direct fit wins more often, sometimes by a large margin on high-gain content, and is
never meaningfully worse than truncation.

Completing a conversion also pre-fills the file field on both Uploader tabs, so you can
switch tabs and press Upload without browsing for the file manually.

No 2048-tap intermediate CLO is exported.

When converting a folder, the same settings are applied to every NAM in that folder.

---

## Required stimulus: `nam_input_wav.wav`

`nam_input_wav.wav` must be placed beside the executable:

```text
NamToClo.exe
nam_input_wav.wav
```

The converter always uses the original conversion stimulus. There is no stimulus-profile selector in the release version.

The stimulus contains two logical sections:

- **first 50 seconds** — fixed conversion stimulus;
- **final 20 seconds** — Tail / Reamp section.

The first 50 seconds always come from `nam_input_wav.wav`. The final 20 seconds are controlled by **Tail / Reamp source**.

---

## Tail / Reamp source

### Original Preset Audio

This is the default mode. The complete 70-second `nam_input_wav.wav` is used:

```text
0–50 s   original conversion stimulus
50–70 s  original preset/reamp tail
```

Use this for the normal conversion behavior.

### Recorded Audio

Choose **Recorded Audio** when you want to replace only the final 20-second Tail / Reamp section with your own WAV.

```text
0–50 s   original conversion stimulus
50–70 s  selected Recorded WAV
```

Press **Browse WAV...** and select the recording. The application adapts the selected file automatically:

- stereo or multichannel audio is downmixed to mono;
- common PCM and floating-point WAV formats are accepted;
- other sample rates are converted to 44.1 kHz;
- audio longer than 20 seconds is trimmed;
- audio shorter than 20 seconds is zero-padded.

The source file itself is not modified.

---

## Corrective IR

Enable **Apply corrective IR** to apply a corrective impulse-response WAV to the normal CLO result.

1. Tick **Apply corrective IR**.
2. Press **Browse WAV...**.
3. Select the corrective IR.
4. Convert normally.

The corrective stage is applied after the native NAM-to-CLO conversion and before both
final CLOs are written -- it now reaches the GP-5/GP-50 512-tap output as well as the
GP-200 1024-tap CLO, not only the latter.

The generated filenames remain:

```text
<name>_NATIVE_GP200_1024.clo
<name>_NATIVE_GP5GP50_512.clo
```

### Corrective IR and Tone Match together

When both options are enabled, the normal native conversion is completed first. The selected Corrective IR is then applied to the native CLO, and the same effective Corrective IR (including the CLO-side RMS normalization and post gain) is applied to the NAM render used as the Tone Match target. Tone Match therefore compares **NAM + Corrective IR** against **CLO + Corrective IR**, and refines the already-corrected CLO.

Using only Corrective IR or only Tone Match keeps the same behavior as before.

---

## Tone Match

Enable **Apply Tone Match (slow)** when you want the converter to perform the additional CLO refinement stage.

Tone Match is intentionally slower than the standard conversion.

When enabled, the normal GP-200 CLO is not exported; the result is:

```text
<name>_NATIVE_GP200_1024_TONEMATCH.clo
```

The GP-5/GP-50 512-tap CLO (`<name>_NATIVE_GP5GP50_512.clo`) is still produced, and now
also receives the same Tone Match correction. That correction is the same filter fit
against the GP-200 2048-tap CLO's own render, reapplied to the GP-5/GP-50 Block B -- an
approximation, not a from-scratch analysis at the 512-tap length, but it means Tone
Match is no longer silently skipped for GP-5/GP-50 output.

### Reference audio mode

Tone Match refines the CLO against a target render, and **Reference audio** selects what that target is. The dropdown offers seven options; **Auto (recommended)** is selected by default:

```text
Default (standard stimulus)
Auto (recommended)
Clean
Moderate
High Gain
Bass
Custom WAV...
```

- **Default (standard stimulus)** — no reference WAV; the target is the original conversion stimulus rendered through the NAM. This is the original, pre-fork behavior.
- **Auto (recommended)** — the converter classifies the NAM's fitted gain shaper into a gain bucket and automatically picks one of the four bundled reference clips below to match. If classification or clip resolution fails, it falls back to Default.
- **Clean / Moderate / High Gain / Bass** — use one specific bundled reference clip directly, bypassing automatic classification. These clips ship with the application (`resources/reference_clips/`) and are installed next to the executable.
- **Custom WAV...** — enables **Browse WAV...**, letting you pick your own reference file. Only the **first 20 seconds** of the selected file are used; the converter combines that reference with the fixed 50-second stimulus and renders the same test through the NAM before refining the CLO.

For any reference WAV (bundled or custom), the audio is adapted automatically in the same way as Recorded Audio: channel conversion, sample-rate adaptation, trimming and padding are handled by the application.

---

## Conversion status

The status bar at the bottom of the application reports the current operation. During NAM rendering and CLO fitting, the controls are temporarily disabled to prevent a second conversion from starting at the same time.

A successful single conversion produces exactly one `.clo` file. In batch mode, one final `.clo` is produced for each successfully converted NAM.

---

## GP-200 Uploader

![GP-200 Uploader tab](assets/gp200-uploader.jpg)

The uploader sends an existing GP-200 compatible `.clo` file directly to a SnapTone slot through USB MIDI.

### 1. Connect the GP-200

Connect the GP-200 to the computer through USB and power it on before uploading.

Open the **GP-200 Uploader** tab. The application scans the available MIDI input and output ports and shows the detected device under **USB MIDI device**.

If the pedal was connected after opening the application, press **Rescan**.

### 2. Select the CLO

Press **Browse CLO...** and select the `.clo` file to upload. You can also drag a `.clo` file onto the application window while the Uploader tab is selected.

### 3. Select the destination SnapTone slot

Choose one of the ten GP-200 SnapTone destinations:

```text
SnapTone 1  (AMP 1)
SnapTone 2  (AMP 2)
SnapTone 3  (AMP 3)
SnapTone 4  (AMP 4)
SnapTone 5  (AMP 5)
SnapTone 6  (DIST 1)
SnapTone 7  (DIST 2)
SnapTone 8  (DIST 3)
SnapTone 9  (DIST 4)
SnapTone 10 (DIST 5)
```

Uploading replaces the CLO currently stored in the selected destination slot.

### 4. Upload

Press **Upload to GP-200**. The transfer progress bar and bottom status line show the current transfer state. Do not disconnect or power off the GP-200 while the upload is in progress.

---

## GP-5 / GP-50 Uploader

The **GP-5 / GP-50 Uploader** sends compatible CLO files directly to a Valeton GP-5 or GP-50 through USB MIDI, using a single shared SnapTone transfer path.

The implementation was reconstructed from Valeton Suite traffic and then validated with successful uploads to a physical GP-5. It does **not** emulate Valeton Suite; it implements only the subset of the protocol required to upload a SnapTone.

GP-50 support reuses this GP-5 SnapTone payload builder and upload implementation unchanged — the GP-50 shares the same SnapTone file/model format and write protocol, and its per-block ACK (`B2 01 00 03 14 08 00`) matches the one already expected by the GP-5 uploader. The application detects a GP-50 by its USB MIDI device name and follows exactly the same transfer path as a GP-5. This has not yet been confirmed against physical GP-50 hardware.

### Supported destination range

The current release allows uploads only to:

```text
SnapTone 51
...
SnapTone 80
```

This is deliberate. SnapTone 51 and SnapTone 80 were captured and validated during reverse engineering, and the uploader is restricted to the validated range.

Internally the GP-5/GP-50 uses zero-based slot numbering:

```text
SnapTone 51 -> 0x32
SnapTone 80 -> 0x4F
```

Attempts to upload outside this range are rejected by the application, not only hidden from the user interface.

### Compatible CLO files

The uploader accepts compatible **VTSI** or **HTSI** CLO files with:

```text
FIR A = 128 taps
FIR B = at least 512 taps
```

This includes:

- the **B512 GP-5/GP-50 CLO** (`<name>_NATIVE_GP5GP50_512.clo`) generated by NamToClo --
  fit directly for this device rather than truncated, and the recommended file to use;
- the **B1024 GP-200 CLO** generated by NamToClo;
- compatible **B2048 Valeton/Hotone/Ampero CLO** files.

If a B1024 or B2048 file is used here, the uploader falls back to truncating the first
512 taps of FIR B, matching earlier behavior. The dedicated B512 file is preferred where
available, since it is fit specifically for GP-5/GP-50's 512-tap budget instead of being
sliced from a fit optimized for a different length. The original CLO file on disk is not
changed either way.

### 1. Connect the GP-5 or GP-50

1. Connect the GP-5 or GP-50 to the computer through USB.
2. Power it on.
3. Open the **GP-5 / GP-50 Uploader** tab.
4. Confirm that the MIDI device is detected. The status line shows the actual detected MIDI endpoint name.
5. If the pedal was connected after NamToClo was opened, press **Rescan**.

Both MIDI input and MIDI output are required because the uploader waits for a device acknowledgement after every transfer block.

### 2. Select the CLO

Press **Browse CLO...** and select the file to upload.

You can also drag a `.clo` file onto the application while the **GP-5 / GP-50 Uploader** tab is active.

The selected source is adapted **in memory** immediately before transfer. Press **Upload SnapTone** to start the transfer; the progress bar and bottom status line show the current transfer state. Do not disconnect or power off the device while the upload is in progress.

### 3. GP-5/GP-50 CLO adaptation

The GP-5/GP-50 transfer representation reconstructed from Valeton Suite uses:

```text
Magic          VTSI
FIR A          128 taps
FIR B          first 512 taps
Declared size  0x0A88
Payload size   0x0A00
```

The uploader updates the CLO layout fields and recalculates the internal **CRC16/MODBUS** automatically.

The compact CLO is then preceded by a reconstructed **74-byte SnapTone wrapper** containing the destination slot and name.

The complete transfer payload is:

```text
74-byte GP-5/GP-50 wrapper
+
0x0A88-byte compact CLO
=
2770 bytes
```

### 4. Transfer protocol

The 2770-byte payload is divided into **146 blocks**:

```text
145 blocks x 19 bytes
1 final block x 15 bytes
```

Each block is sent using decoded command:

```text
0x92
```

The packet body is:

```text
[CRC8]
[0x92]
[sequence]
[payload length]
[payload]
```

The packet CRC is **CRC-8 polynomial 0x07**, initial value `0x00`, MSB-first.

Before transmission, every decoded byte is converted to two 4-bit nibbles and wrapped as MIDI SysEx:

```text
0xAB -> 0x0A 0x0B
```

### 5. ACK and retry handling

After each `0x92` block, the uploader waits for the device acknowledgement:

```text
B2 01 00 03 14 08 00
```

GP-50 captures observed during protocol analysis decode to this same ACK, so no GP-50-specific ACK handling was added.

Only after that ACK is received does the uploader continue with the next block.

If the ACK is not received in time, the same sequence block is retried automatically. The transfer is aborted after the retry limit is reached.

This ACK-driven transfer avoids relying on long fixed delays between blocks.

### 6. Final completion

After the final block, the uploader waits for the completion notification observed in the captured GP-5 transfers:

```text
CE 01 00 06 12 1B 03 00 00 00
```

When this message is received, the upload is reported as successfully completed. This first GP-50 test build reuses the same GP-5 completion message unchanged — it has not been confirmed whether a physical GP-50 sends the identical final confirmation. If the final confirmation times out, the error message includes the most recently received valid nibble-decoded SysEx message so this can be diagnosed during hardware testing without needing a full protocol capture.

### Confirmed vs inferred protocol details

**Confirmed from captures and physical testing:**

- command `0x92` carries the SnapTone transfer blocks;
- 19-byte block payloads are used, with a shorter final block;
- each block is acknowledged by `B2 01 00 03 14 08 00`;
- the packet uses CRC-8 polynomial `0x07`;
- SysEx data is nibble-encoded;
- the GP-5 runtime CLO uses A128/B512;
- SnapTone 51 maps to `0x32`;
- SnapTone 80 maps to `0x4F`;
- the complete uploader works on physical GP-5 hardware.

**Strongly supported by the captures:**

- `CE 01 00 06 12 1B 03 00 00 00` is the post-transfer completion notification.

**GP-50 specifically:**

- GP-50 protocol captures decode to the same command (`0x92`), CRC-8/SMBUS with init `0x00`, 19-byte payload chunks, nibble encoding and per-block ACK (`B2 01 00 03 14 08 00`) as GP-5.
- The complete transfer, including the final completion message, has been confirmed working end-to-end by successful physical uploads and playback on a GP-50. A completion timeout still surfaces the last decoded SysEx message received, in case a future GP-50 firmware revision changes this.

The application intentionally does **not** reproduce the complete Valeton Suite startup and state synchronization because the captured uploads begin the SnapTone transfer directly with command `0x92`. Only the protocol needed for the upload is implemented.

---

## File layout for a release

A minimal usable folder is:

```text
NamToClo.exe
nam_input_wav.wav
```

No external runtime folder is required for the Convert to CLO tab.

GP-200, GP-5 and GP-50 upload support is built into the executable; no separate MIDI runtime or Valeton Suite installation is required.

---

## Build from source

Requirements:

- Windows x64
- CMake 3.24 or newer
- Visual Studio / MSVC with C++ desktop development tools

Build with:

```powershell
cmake --preset windows-x64
cmake --build build --config Release --parallel
```

The executable is generated by the CMake build in the Release output directory.

> The `windows-x64` preset in `CMakePresets.json` pins a specific Visual Studio generator
> version. If `cmake --preset windows-x64` fails to find that generator, either install
> the matching Visual Studio version or edit the preset's `generator` field to match what
> you have installed — it is not auto-detected. CI (`.github/workflows/build-windows.yml`,
> `release.yml`) does not use this preset; it configures with the generator-agnostic
> `cmake -S . -B build -A x64` instead, which works with whatever VS version is on the
> runner.

---

## Changes in this fork

This fork builds on [Goaltoday/NamtoClo](https://github.com/Goaltoday/NamtoClo), focused
on GP-5/GP-50 conversion quality and confirming GP-50 hardware support end-to-end
(developed with AI assistance):

- **GP-50 hardware confirmed.** Physical uploads and playback on a real GP-50 succeeded,
  including the completion message that was previously unverified for that device.
- **Dedicated GP-5/GP-50 fit.** Block B is now fit directly for the 512-tap budget
  GP-5/GP-50 actually plays, instead of only truncating the GP-200 fit's 2048-tap
  result. The converter automatically compares both approaches per NAM file and keeps
  whichever measures better, producing `<name>_NATIVE_GP5GP50_512.clo` alongside the
  GP-200 output on every conversion.
- **Corrective IR and Tone Match now reach the GP-5/GP-50 output.** Previously both only
  applied to the GP-200 1024-tap CLO; the GP-5/GP-50 file silently missed them.
- **A2 submodel selection (Full vs. Lite) made explicit and validated**, rather than an
  unexamined assumption.
- **A held-out validation tool** (`--quality-experiment`, see below) for scoring
  conversions against real playing content instead of only the synthetic fitting
  stimulus, used to test the above across NAM captures spanning clean to extreme
  high-gain content.
- Uploader tabs and the Convert tab now clearly label which output is for GP-200
  (1024-tap) vs. GP-5/GP-50 (512-tap), and completing a conversion pre-fills both
  Uploader tabs' file fields.

### `--quality-experiment` (headless CLI)

```powershell
NamToClo.exe --quality-experiment <input.nam> <outputDir> [validationClipsDir] [toneMatchReferenceWav]
```

Loops a conversion over the Full and Lite A2 submodels and, for each, scores the
truncated-vs-direct-fit GP-5/GP-50 choice. When `validationClipsDir` is given (any
folder of `.wav` files, any encoding/sample rate), each clip is additionally rendered
through the Full A2 submodel as ground truth and used to score every candidate against
real playing content rather than only the synthetic conversion stimulus. When
`toneMatchReferenceWav` is also given, it is used as a Custom Tone Match reference (see
[Reference audio mode](#reference-audio-mode)) for the scored conversions. Writes
`quality_experiment_results.csv` into `outputDir`.

---

## Licensing

See [`LICENSE`](LICENSE) for the project license and [`THIRD_PARTY.md`](THIRD_PARTY.md) for third-party components and their licenses.
