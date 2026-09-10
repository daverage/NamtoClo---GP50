# EngineV2 clean-sheet CLO distiller

This is the first experimental distillation pass built around the cached teacher
dataset. It intentionally does **not** use an approximate CLO from the production
converter as its seed.

## What it fits

The GP5/GP50 structure is fixed to:

```text
PRE(identity) -> A128 -> 4x asymmetric P/K -> POST(fixed) -> B512
```

`B512` is not treated as 512 outer optimisation variables. For every proposed
`A + P/K` candidate, the script renders the pre-B signal and solves one shared
regularised least-squares B across all fit clips.

A is represented by smooth log-frequency magnitude controls and converted to a
minimum-phase A128 FIR.

Outer moves are accepted only when the disjoint `selection` material improves.
`benchmark` material is scored only after the search.

## Install

```bash
python3 -m pip install numpy scipy soundfile numba
```

## Start with a smoke test

```bash
python3 src/engine_v2/train_namtoclo_distiller.py \
  --teacher-root ~/NamtoCloTeacherDataset \
  --output ~/NamtoCloDistillerSmoke \
  --model-regex "JC" \
  --fit-seconds 12 \
  --selection-seconds 8 \
  --benchmark-seconds 8 \
  --a-controls 12 \
  --rounds 1
```

## Five-NAM proof

```bash
python3 src/engine_v2/train_namtoclo_distiller.py \
  --teacher-root ~/NamtoCloTeacherDataset \
  --output ~/NamtoCloDistillerProof \
  --proof5 \
  --fit-seconds 60 \
  --selection-seconds 30 \
  --benchmark-seconds 30 \
  --a-controls 24 \
  --rounds 3
```

## Outputs

Each NAM gets:

- `report.json`
- `distilled.clo`
- `preview_fit_input.wav`, `preview_fit_nam.wav`, `preview_fit_clo.wav` (and selection/benchmark equivalents when available)

The output root also gets `summary.json`.

Validate a generated file with the existing EngineV2 CLI:

```bash
./build/namtoclo clo-info ~/NamtoCloDistillerProof/.../distilled.clo
```

## Important status

This is a research candidate generator, not yet the production converter.

Before replacing production conversion we still need to:

1. compare its benchmark metrics against EngineV2's current direct-fit baseline;
2. listen to representative clean / crunch / high-gain / fuzz candidates;
3. upload selected candidates to real GP50 hardware;
4. verify the Python render against the C++ device-model renderer on identical
   coefficients and inputs.

Only then should the new algorithm move into `src/core`.
