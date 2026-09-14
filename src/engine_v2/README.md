# EngineV2 active research workspace

`ENGINE_V2_RESEARCH_NORTH_STAR.md` is the governing research document.

## Project rule

EngineV2 is an alternative NAM -> GP50 conversion method. The source NAM is the behavioural authority and the released NamToClo engine is the permanent baseline to beat. Internal V4.x comparisons are useful diagnostics, but they are not the product success criterion.

## Canonical active entrypoint

- `train_namtoclo_north_star_v42_fast.py` — current V4.2 trainer with accelerated bracket/refine convergence and compatible historical V4-family warm starts

Current tests/diagnostics:

- `test_north_star_v42.py`
- `test_north_star_v42_fast_warm_start.py`
- `analyze_north_star_v4_stimulus_diagnostics.py`
- `test_north_star_v4_stimulus_diagnostics.py`

## Required implementation chain

The current trainer deliberately imports shared code from earlier experiments, so some versioned filenames remain genuine dependencies rather than competing active engines:

- `train_namtoclo_north_star_v42.py`
- `distiller_v2_north_star_v42.py`
- `distiller_v2_north_star_v4.py`
- `distiller_v2_north_star_v3.py`
- `distiller_v2_north_star_v2.py`
- `distiller_v2_north_star.py`
- `train_namtoclo_north_star_v4.py`
- `train_namtoclo_north_star.py`
- `distiller_v2_fit.py`
- `distiller_v2_dsp.py`
- `distiller_v2_data.py`
- `north_star_v4_stimulus.py`
- `build_namtoclo_teacher_dataset.py`

## Research documents

Keep in the active directory:

- `ENGINE_V2_RESEARCH_NORTH_STAR.md`
- `NORTH_STAR_V4_2_LINE_CONVERGENCE_EXPERIMENT.md`

Older V4/V4.1 experiment notes and unused code belong under `research/engine_v2_legacy/`.

## Cleanup

Use the conservative cleanup wrapper:

```bash
python3 scripts/clean_engine_v2_active.py
python3 scripts/clean_engine_v2_active.py --apply
```

The first command is a dry run. The apply step verifies the active dependency closure and tests before and after moving unused files to `research/engine_v2_legacy/`. Historical files are preserved rather than deleted.
