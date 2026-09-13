# EngineV2 active research workspace

`ENGINE_V2_RESEARCH_NORTH_STAR.md` is the governing research document.

The active experiment is V4 stimulus-only NAM -> constrained GP50 distillation.
Historical/unused EngineV2 files were moved to `research/engine_v2_legacy/` by
`scripts/clean_engine_v2.py`; they were not deleted.

## Active entrypoints

- `train_namtoclo_north_star_v4.py`
- `analyze_north_star_v4_stimulus_diagnostics.py`
- `test_north_star_v4.py`
- `test_north_star_v4_stimulus_diagnostics.py`

## Current Python dependency closure

- `build_namtoclo_nam_corpus.py`
- `build_namtoclo_research_corpus.py`
- `build_namtoclo_research_corpus_hf.py`
- `build_namtoclo_teacher_dataset.py`
- `distiller_v2_data.py`
- `distiller_v2_dsp.py`
- `distiller_v2_fit.py`
- `distiller_v2_north_star.py`
- `distiller_v2_north_star_v2.py`
- `distiller_v2_north_star_v3.py`
- `distiller_v2_north_star_v4.py`
- `north_star_v4_stimulus.py`
- `train_namtoclo_north_star.py`

## Research documents

- `ENGINE_V2_RESEARCH_NORTH_STAR.md`
- `NORTH_STAR_V4_STIMULUS_EXPERIMENT.md`

Do not restore legacy files to this directory merely for convenience. If V4/V4.1
needs shared behaviour, extract the minimum reusable code deliberately and verify
that the fitting/DSP behaviour is unchanged.
