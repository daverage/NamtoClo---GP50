# EngineV2 active research workspace

`ENGINE_V2_RESEARCH_NORTH_STAR.md` is the governing research document.

## Project rule

EngineV2 is an alternative NAM -> GP50 conversion method. The source NAM is
the behavioural authority and the released NamToClo engine is the permanent
baseline to beat. Internal V4.x comparisons are diagnostics, not the product
success criterion.

## Canonical active entrypoint

- `train_namtoclo_north_star_v42_fast.py`

The remaining entrypoints below are tests/diagnostics retained by the cleanup:
- `analyze_north_star_v4_stimulus_diagnostics.py`
- `test_north_star_v42.py`
- `test_north_star_v42_fast_warm_start.py`
- `test_north_star_v4_stimulus_diagnostics.py`

## Baseline comparison (shipped C++ engine vs. EngineV2)

`ENGINE_V2_RESEARCH_NORTH_STAR.md` names a decisive, previously-missing test:
comparing EngineV2's own reported ESR-to-NAM-teacher against the *shipped*
C++ engine's (`src/core/native_converter.cpp` + `clo_refiner.cpp`, via the
`namtoclo` CLI) ESR on the exact same NAM model, stimulus, and teacher
evidence. Two new modules provide this:

- `clo_reader.py` -- `read_clo(path)` decodes a real GP-5/GP-50 compact VTSI
  CLO (as produced by `namtoclo convert --tone-match --reference auto`) into
  its pre/post biquad, `pp`/`pn`/`kp`/`kn`, and A128/B512 FIR arrays. It is a
  direct byte-offset port of `parseModel` in `src/core/clo_refiner.cpp`
  (cites exact line numbers/offsets in its module docstring), independently
  verified by round-tripping it against this codebase's own `write_clo`
  (`distiller_v2_dsp.py`) as ground truth.
- `baseline_compare.py` -- standalone script: given an existing EngineV2
  output directory (one with `report.json`), runs `namtoclo convert` on the
  same `.nam` model, decodes the resulting CLO, renders it through
  `distiller_v2_dsp.py`'s student DSP (`render_full_with`, which is the
  existing renderer parameterized to accept a CLO's own pre/post biquad --
  added because a real shipped-engine CLO's post biquad is not always
  bit-identical to EngineV2's fixed `POST` constant, confirmed by direct
  comparison) across the identical cached 5-level teacher evidence the
  EngineV2 report used, scores it with the same `evidence_esr` EngineV2 uses,
  and writes `baseline_report.json` next to `report.json` with
  `shipped_engine_esr`, `engine_v2_esr`, `relative_improvement`, and an
  explicitly non-overreaching `verdict` string. Usage:
  `python3 baseline_compare.py --output-root <EngineV2 output dir> --namtoclo-bin build-macos/namtoclo`.
  This is a stimulus-fit-to-raw-NAM-teacher comparison only -- not a
  hardware-validated result; see `ENGINE_V2_RESEARCH_NORTH_STAR.md`
  "Existing converter baseline" for what more is required.

## Required Python dependency closure

- `build_namtoclo_nam_corpus.py`
- `build_namtoclo_research_corpus.py`
- `build_namtoclo_research_corpus_hf.py`
- `build_namtoclo_teacher_dataset.py`
- `clo_reader.py` (baseline comparison only, see below)
- `distiller_v2_data.py`
- `distiller_v2_dsp.py`
- `distiller_v2_fit.py`
- `distiller_v2_north_star.py`
- `distiller_v2_north_star_v2.py`
- `distiller_v2_north_star_v3.py`
- `distiller_v2_north_star_v4.py`
- `distiller_v2_north_star_v42.py`
- `north_star_v4_stimulus.py`
- `train_namtoclo_north_star.py`
- `train_namtoclo_north_star_v4.py`
- `train_namtoclo_north_star_v42.py`

Some dependency filenames contain earlier experiment version numbers because the
current fitter deliberately reuses proven shared search/evidence helpers. They are
implementation dependencies, not active competing engines.

## Research documents

- `ENGINE_V2_RESEARCH_NORTH_STAR.md`
- `NORTH_STAR_V4_2_LINE_CONVERGENCE_EXPERIMENT.md`

Historical/unused EngineV2 files live under `research/engine_v2_legacy/` and remain
available through git history. Do not restore them to `src/engine_v2` unless they
become a genuine dependency of the current engine.

Use `scripts/clean_engine_v2_active.py` for future pruning.
