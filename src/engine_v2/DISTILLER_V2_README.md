# EngineV2 clean-sheet CLO distiller

This directory contains the experimental teacher/student NAM -> GP50 distillation work.
The governing research intent is recorded in `ENGINE_V2_RESEARCH_NORTH_STAR.md`.

The GP5/GP50 runtime structure is fixed to:

```text
PRE(identity) -> A128 -> 4x asymmetric P/K -> POST(fixed) -> B512
```

The GP50 blocks are hardware constraints, not assumed conceptual amp stages. In particular,
A and P/K are allowed to cooperate: A may deliberately shape which frequencies drive the
nonlinear stage, while B completes the post-nonlinearity linear response and output level.

## Canonical North Star experiment

Use `train_namtoclo_north_star.py` for the original first-prototype hypothesis described in
the North Star document.

For each NAM it fits exactly the compact evidence set:

```text
SYSTEM IDENTIFICATION (FIT)
  04_log_sweep_-36dBFS.wav
  07_multisine_level_ladder.wav
  09_1kHz_level_ladder.wav
  10_frequency_level_matrix.wav
  11_two_tone_IMD_matrix.wav
  13_tone_burst_transients.wav

REAL GUITAR
  6 deterministic real-DI FIT performances
  4 disjoint real-DI SELECTION performances
  3 held-out real-DI BENCHMARK performances
```

The historical role tag on a synthetic probe does not control the canonical experiment: all
six identification probes participate in FIT. Real selection and benchmark material never
moves into FIT.

The North Star optimiser uses:

- joint/cooperative A + P/K coordinate search;
- one shared analytic B512 solve for every candidate;
- target-energy-normalized B solving so loud probes do not dominate quiet probes;
- mean aligned ESR (direct teacher/student waveform error) for FIT candidate moves;
- disjoint real-DI mean aligned ESR as the round-selection gate;
- no level-response penalty;
- no distortion-excess penalty;
- no independently derived P/K target;
- no named-amp special cases.

Matched real-guitar level sweeps and nonlinear-residual measurements are still reported, but
they are diagnostics only and are calculated after the fitted coefficients are frozen.

Final loudness correction, when enabled, uses only real FIT material and scales B512 after
P/K. It never increases pre-P/K drive as a volume fix.

### First North Star run

From `src/engine_v2`:

```bash
python3 train_namtoclo_north_star.py \
  --teacher-root ~/NamtoCloTeacherDataset \
  --output ~/NamtoCloNorthStar_JCM800_G5 \
  --model-regex "^JCM800 2203 D\.I\. - G5 B5 M5 T5 P5 V5 - STD$" \
  --a-controls 24 \
  --rounds 3 \
  --pk-passes 3 \
  --level-groups 3 \
  --threads 1
```

Run the North Star self-tests first after pulling new changes:

```bash
python3 test_north_star.py
python3 test_distiller_v2.py
```

Each NAM output directory contains:

- `report.json` with the exact material/task manifest and optimisation philosophy;
- `distilled.clo`;
- real-DI fit/selection/benchmark NAM-vs-CLO preview WAVs.

The output root contains `summary.json`.

## Earlier experimental trainer

`train_namtoclo_distiller.py` is retained for comparison with the earlier clean-sheet
experiments. It supports matched-level and distortion-excess objective terms that were useful
research probes but are **not** part of the canonical North Star experiment.

Do not use those terms as the default EngineV2 philosophy merely because they can improve one
named NAM's development metrics.

Example legacy smoke run:

```bash
python3 train_namtoclo_distiller.py \
  --teacher-root ~/NamtoCloTeacherDataset \
  --output ~/NamtoCloDistillerSmoke \
  --model-regex "JC" \
  --fit-seconds 12 \
  --selection-seconds 8 \
  --benchmark-seconds 8 \
  --a-controls 12 \
  --rounds 1
```

## Install

```bash
python3 -m pip install numpy scipy soundfile numba
```

## Validation

Validate a generated file with the existing EngineV2 CLI, adjusting the binary path for your
build directory:

```bash
../../build-macos/namtoclo clo-info ~/NamtoCloNorthStar_JCM800_G5/.../distilled.clo
```

The decisive research comparison remains:

```text
NAM teacher
vs
existing/old NamToClo GP50 result
vs
North Star EngineV2 GP50 result
```

on the same held-out real guitar and then on real GP50 hardware. Software metrics are
engineering evidence; hardware listening remains an acceptance gate.

This is research code, not yet the production converter. Only after the North Star method
beats the existing converter broadly across clean, crunch, high-gain and held-out NAMs should
it move into `src/core`.
