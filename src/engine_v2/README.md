# EngineV2 active research workspace

`ENGINE_V2_RESEARCH_NORTH_STAR.md` is the governing research document.

The active experiment is **V4.2**: TONE3000 multilevel stimulus-only NAM -> constrained GP50 distillation, with exact V4 basin selection followed by fine line-coordinate convergence. Guitar remains comparison-only.

Historical/unused EngineV2 files were moved to `research/engine_v2_legacy/`; they were not deleted. If the folder needs pruning again, use `scripts/clean_engine_v2_active.py`, which applies the conservative cleanup logic with the current V4.2 roots.

## Active entrypoints

- `train_namtoclo_north_star_v42.py` — active V4.2 trainer
- `test_north_star_v42.py` — V4.2 search-mechanics self-test
- `analyze_north_star_v4_stimulus_diagnostics.py` — diagnostic-only stimulus residual analysis

V4/V4.1 trainers remain in the active dependency tree as reproducible comparison/reference implementations.

## Active search path

- `distiller_v2_north_star_v42.py` — V4.2 line-coordinate convergence
- `distiller_v2_north_star_v4.py` — exact V4 stimulus-only multistart/coarse search
- `distiller_v2_north_star_v3.py` — shared deterministic seed/step definitions
- `distiller_v2_north_star_v2.py` — balanced evidence evaluation and analytic B solve
- `distiller_v2_fit.py` — current threaded clip-level evaluation helpers
- `distiller_v2_dsp.py` — GP50 student DSP and CLO serializer
- `north_star_v4_stimulus.py` — current multilevel T3K teacher preparation/cache path

## Research documents

- `ENGINE_V2_RESEARCH_NORTH_STAR.md`
- `NORTH_STAR_V4_STIMULUS_EXPERIMENT.md`
- `NORTH_STAR_V4_1_CONVERGENCE_EXPERIMENT.md`
- `NORTH_STAR_V4_2_LINE_CONVERGENCE_EXPERIMENT.md`

## V4.2 isolation rule

V4.2 changes only the post-V4 convergence mechanics: an improving A or P/K coordinate is followed repeatedly at the existing fine step until the next step fails. It does **not** change the stimulus, teacher targets, objective, GP50 architecture, P/K bounds, analytic B solve, guitar-training policy, or V4 output-calibration rule.

The current branch also contains runtime-only speed improvements (threaded clip evaluation and concurrent preparation of independent stimulus-level cache misses). V4.2 intentionally inherits those latest implementations rather than branching from an older V4.1 snapshot.
