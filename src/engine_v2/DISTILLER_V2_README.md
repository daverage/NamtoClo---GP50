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

## Canonical North Star source material

Both North Star v1 and v2 use the same source-material definition from the North Star
document:

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
moves into FIT. Canonical real slots exclude bass datasets and obvious release/noise/silence
sample artifacts; the broader teacher corpus remains unchanged.

Matched real-guitar level sweeps and nonlinear-residual measurements are reported only as
diagnostics after the fitted coefficients are frozen. Final loudness correction, when
enabled, uses only real FIT material and scales B512 after P/K.

## North Star v1 — frozen reference

`train_namtoclo_north_star.py` is the frozen first canonical implementation. It treats each
whole synthetic probe file as one normalized FIT example. It is retained so the first
hardware results remain exactly reproducible.

The v1 optimiser uses joint/cooperative A + P/K search, one shared analytic B512 solve for
every candidate, direct NAM-vs-GP50 aligned ESR, a disjoint real-DI selection gate, and no
level-response/distortion-excess/named-amp objective terms.

Example v1 G5 run:

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

## North Star v2 — segmented / level-balanced evidence

`train_namtoclo_north_star_v2.py` keeps the v1 architecture, source corpus, selection gate
and anti-overfitting rules, but changes how the structured synthetic FIT probes contribute.

The student still renders every original probe continuously, preserving state and timing.
For FIT scoring and the analytic B solve, the structured probes are interpreted as logical
operating-point windows:

```text
quiet log sweep                 1 window
multisine level ladder          8 windows
1 kHz level ladder             10 windows
frequency x level matrix       20 windows
two-tone IMD matrix            12 windows
transient bursts               15 windows
real guitar FIT                 6 whole-clip windows
                               --
                               72 virtual FIT examples
```

A short part of the silence after each generated tone/burst is retained so decay/tail
behaviour contributes to the teacher evidence.

Crucially, 72 windows do **not** mean synthetic evidence gets 66/72 of the total weight.
The six synthetic probe families retain the same total high-level influence as the six real
FIT clips. Each probe family gets one unit; windows inside that probe divide its unit equally.
Thus a -36 dB operating point can matter as much as the -3 dB point inside the same ladder,
without a 20-cell matrix overwhelming the real guitar corpus simply because it has more cells.

The v2 B weighting is applied after A/P-K/POST as linear least-squares evidence. It never
changes pre-P/K drive. Level-response and distortion-excess measurements remain diagnostics
only.

Run the v2 self-tests first:

```bash
python3 test_north_star.py
python3 test_north_star_v2.py
python3 test_distiller_v2.py
```

Then run the first v2 G5 comparison:

```bash
python3 train_namtoclo_north_star_v2.py \
  --teacher-root ~/NamtoCloTeacherDataset \
  --output ~/NamtoCloNorthStarV2_JCM800_G5 \
  --model-regex "^JCM800 2203 D\.I\. - G5 B5 M5 T5 P5 V5 - STD$" \
  --a-controls 24 \
  --rounds 3 \
  --pk-passes 3 \
  --level-groups 3 \
  --threads 1
```

Each NAM output directory contains:

- `report.json` with the exact material/task manifest and optimisation philosophy;
- v2 reports also include the complete virtual evidence-window manifest and weights;
- `distilled.clo`;
- real-DI fit/selection/benchmark NAM-vs-CLO preview WAVs.

The output root contains `summary.json`.

## Three-way held-out evaluator

`evaluate_north_star_vs_original.py` is evaluation-only. It never changes fit coefficients.
It compares the authoritative cached NAMCore teacher target against both an ordinary NamToClo
compact GP5/GP50 CLO and a frozen North Star v2 CLO.

To avoid the earlier model-to-model benchmark mismatch, it first intersects the real benchmark
corpus by dataset + source identity + start + duration and chooses the exact same underlying
held-out performances for every selected NAM. Different NAMs therefore see identical DI
performances during the comparison.

The evaluator renders each compact CLO from the coefficients actually stored in that file,
including PRE, A128, P/K, POST and B512. This avoids assuming that the ordinary converter and
North Star wrote identical fixed filter coefficients.

For each system it reports:

- absolute aligned ESR and signed output-level error;
- gain-matched ESR;
- dense teacher-relative spectral error from 20 Hz to 20 kHz using 144 logarithmic bands;
- broad summaries from 20-35 Hz sub headroom through bass, body, mids, presence, fizz, air and
  18-20 kHz Nyquist headroom;
- teacher-energy confidence flags so near-silent frequency regions are not over-interpreted;
- low-level/tail ESR and full-spectrum tail error using input frames 20-50 dB below normal
  playing level while excluding numerical silence;
- nonlinear residual difference as a diagnostic only;
- CSV/JSON output plus optional PNG plots when matplotlib is installed;
- a four-way listening preview for the first held-out clip: input, NAM teacher, original CLO,
  North Star v2 CLO.

Put the three ordinary NamToClo GP5/GP50 CLO files in one directory, then run the self-test:

```bash
python3 test_three_way_evaluator.py
```

Example JC / JCM G3 / JCM G10 comparison:

```bash
python3 evaluate_north_star_vs_original.py \
  --teacher-root ~/NamtoCloTeacherDataset \
  --original-root ~/NamtoCloOriginal_TestTriad \
  --north-star-root ~/NamtoCloNorthStarV2_TestTriad \
  --output ~/NamtoCloThreeWayEval \
  --model-regex "^(Roland JC 120B Jazz Chorus: Bright Off, SM57|JCM800 2203 D\.I\. - G(3|10) B5 M5 T5 P5 V5 - STD)$" \
  --benchmark-count 3
```

The ordinary converter directory is discovered by model ID from filenames ending in
`_NATIVE_GP5GP50_512.clo`. North Star v2 files are discovered from the normal per-model
`distilled.clo` output directories.

The model directory contains `per_clip.csv`, `bands.csv`, `spectrum.csv`, tail equivalents,
preview WAVs, `summary.json`, and spectral plots when matplotlib is available. The output root
contains the overall `summary.json`.

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

Install matplotlib as well if you want the evaluator PNG plots:

```bash
python3 -m pip install matplotlib
```

## Validation

Validate a generated file with the existing EngineV2 CLI, adjusting the binary path for your
build directory:

```bash
../../build-macos/namtoclo clo-info ~/NamtoCloNorthStarV2_JCM800_G5/.../distilled.clo
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
