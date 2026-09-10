# GP50 / NAM identification probe pack

This pack is for a clean-sheet NAM -> GP50 CLO distillation/system-identification experiment.

## Canonical domain
- Mono float32 WAV
- 44.1 kHz
- Preserve absolute levels. Do **not** peak-normalise each NAM render.
- If a NAM requires another rate, resample the *input* to that rate, render it, then resample and latency-align the target back to 44.1 kHz.

## Suggested use
### Initialisation
- `04_log_sweep_-36dBFS.wav`
- `02_impulse_-12dBFS.wav` only as a secondary diagnostic
- `09_1kHz_level_ladder.wav`
- `10_frequency_level_matrix.wav`

### Core fit
- `07_multisine_level_ladder.wav`
- `08_multisine_drive_ramp.wav`
- `11_two_tone_IMD_matrix.wav`
- `12_broadband_noise_level_ladder.wav`
- `13_tone_burst_transients.wav`

### Diagnostics / held-out probes
Keep at least some entire files out of fitting. A good first split is:
- FIT: 04, 07, 09, 10, 12
- SELECTION: 05, 08, 11, 13
- BENCHMARK: 06, 03, 14 plus real guitar DI

## Important
The synthetic files are **identification probes**, not the perceptual benchmark. Real dry DI guitar material should decide whether a CLO is actually better.
