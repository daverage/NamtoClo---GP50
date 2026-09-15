# EngineV2 (archived 2026-09-15)

This entire directory was `src/engine_v2/` until 2026-09-15, when the
research was concluded (see "Status" below) and everything -- code, tests,
docs, the V4.2 model/batch/baseline-comparison tooling, and the second
`gp50_identification_probe_pack_v42/` probe set -- was moved here alongside
the earlier (V2/V3/V4.1-era) archived material already in this folder. This
is not partial pruning around an active engine (that's what
`cleanup_tools/clean_engine_v2_active.py` used to do while EngineV2 was
still being developed); nothing in `src/engine_v2` remains active as of this
archive. `ENGINE_V2_RESEARCH_NORTH_STAR.md` is the governing research
document from that effort.

## Project rule

EngineV2 is an alternative NAM -> GP50 conversion method. The source NAM is
the behavioural authority and the released NamToClo engine is the permanent
baseline to beat. Internal V4.x comparisons are diagnostics, not the product
success criterion.

## Status (2026-09-15): research paused, not deleted

EngineV2's core finding is real: across 15 batch-trained models plus one
ad-hoc conversion, the V4.2 line-search-converged CLO beat the shipped
engine's stimulus-fit-to-raw-NAM-teacher ESR in **16/16** cases (mean
relative improvement 74%, median 81%; see each model's `baseline_report.json`
and the aggregate `baseline_summary.json` for the exact numbers). No cheap
universal fix was found that reproduces this gain on the shipped engine's
fast path: neither a fixed P/K correction (no consistent directional bias
across models -- see the per-model `controls_db`/`pk` vs. shipped `pp/pn/kp/
kn` comparison done this session) nor swapping the Tone Match reference
audio (T3K sweep and a real-guitar clip both scored *worse* than the
existing curated Auto reference clip on held-out real guitar, on the one
model tested) nor multi-level B-fitting on the existing reference clip
(also worse). One real, shippable, structural bug WAS found and fixed in
the released engine as a direct result of this investigation: `AmpGainBucket`'s
High threshold (`src/core/native_converter.cpp`) was uncrossable by any real
NAM tested (0/16 models reached it, including amps explicitly named for
extreme gain) and has been lowered from 260 to 70 -- a real but only
partially-validated fix (helped one high-gain model's held-out ESR by 13%,
hurt another by 17%; not a clean win, left as the best available data-driven
placement). Two new manual (never Auto-selected) Tone Match reference modes,
`--reference t3k` and `--reference standard`, were added to the CLI and both
GUIs for advanced experimentation; neither beat Auto on the one model tested.

**Why EngineV2 itself is not shipping**: per-model conversion time is hours
(3-8h for a from-scratch model with no compatible warm start; only ~6 min
when warm-starting from an already-solved same-family basin), against the
shipped engine's ~1 minute -- not a marginal gap. A listening pass on real
hardware also judged the audible improvement not worth that wait, echoing
this project's earlier `searchPkForDynamics` lesson that automated-metric
wins do not reliably transfer to perceived quality. Further speedup ideas
were scoped but not attempted (a gradient-based optimizer instead of the
discrete coordinate search; parallelizing the 3 multistart attempts within
one model, currently sequential; GPU/JAX batched rendering) -- all face real
technical uncertainty and none, even combined, were expected to close the
gap to "shippable" without also cutting the number of search evaluations,
which only the gradient-based rewrite would do. A "train a fast predictor
from EngineV2's results" alternative was also scoped (roughly 4-8+ weeks,
dominated by ~11 days of compute to generate a large enough training set)
and not attempted.

All EngineV2 code, tooling, and data below are left in place and runnable --
nothing here is deleted -- for whoever picks this back up. The batch/ad-hoc
trainer entrypoints and `models_subset_v1.txt` (a deliberately varied
15-model panel spanning clean/crunch/high-gain/pedal/bass, used for the 16-
model result above) are a ready-made starting point for extending the corpus
without re-deriving that selection.

## Canonical active entrypoint

- `train_namtoclo_north_star_v42_fast.py`

Also active (added this session, not yet folded into the "fast" trainer):
- `train_namtoclo_north_star_v42.py` -- the pre-"_fast" batch trainer actually
  used to produce this session's 15-model result; takes `--model-regex` and
  `--teacher-root`/`--nam-root`/`--output` etc, see its own `--help`.
- `run_batch_parallel.py` -- runs several independent single-model
  `train_namtoclo_north_star_v42.py` invocations concurrently (pure
  orchestration, no science change; a rolling pool, not a fixed batch --
  see its module docstring) with `--jobs N` capping concurrency and
  `--models-file <file>` (one exact model name per line) or `--model-regex`
  to select models. `models_subset_v1.txt` is the 15-model panel used this
  session.
- `convert_one_nam_v42.py` -- runs the exact same V4.2 search against one
  arbitrary `.nam` file that is NOT in the pre-built teacher dataset (e.g.
  something a user just downloaded), building its own T3K teacher evidence
  directly rather than requiring teacher-dataset corpus membership. Produces
  a `report.json` in the same schema `baseline_compare.py` expects, so it can
  be scored the same way.

The remaining entrypoints below are tests/diagnostics retained by the cleanup:
- `analyze_north_star_v4_stimulus_diagnostics.py`
- `test_north_star_v42.py`
- `test_north_star_v42_fast_warm_start.py`
- `test_north_star_v4_stimulus_diagnostics.py`
- `baseline_compare.py` (its own entrypoint root; not imported by the
  trainer -- see "Baseline comparison" below)
- `test_baseline_compare.py`

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
- `baseline_compare.py` -- standalone, read-only scoreboard script: given one
  or more existing EngineV2 output directories (each with `report.json`), it
  runs `namtoclo convert` on the same `.nam` model (the released engine's
  fixed command: `convert <nam> --output <dir> --tone-match --reference auto
  --json`, never altered by this tool), decodes both the resulting released
  CLO *and* EngineV2's own actual final CLO (`report["clo_path"]`) with
  `clo_reader.py`, renders both through the identical code path
  (`distiller_v2_dsp.py`'s `render_full_with`, parameterized to accept each
  CLO's own pre/post biquad, since a real shipped-engine CLO's post biquad is
  not always bit-identical to EngineV2's fixed `POST` constant) across the
  identical cached 5-level teacher evidence the EngineV2 report used, and
  scores both with the same `evidence_esr` EngineV2 uses. Rendering across
  the 5 independent stimulus levels is parallelized with
  `distiller_v2_fit._map` (order-preserving) for both sides.

  Deliberately, EngineV2's side is never scored from `report.json`'s stored
  `optimizer_fit_evidence_metrics` -- that number is computed on the
  *uncalibrated* B, before the post-gain calibration step that produces the
  actual CLO bytes, so it can disagree with how the real final CLO scores.
  Only the rendered-from-the-actual-CLO number is trusted.

  A persistent **baseline cache** (default
  `<output-root>/_baseline_cache/`, override with `--baseline-cache-root`)
  avoids re-running `namtoclo convert` and re-rendering the released side on
  repeat invocations. Its identity/key covers: NAM file SHA256, namtoclo
  binary SHA256, the exact conversion argv, the stimulus SHA256, the
  `levels_db` tuple, and an internal `SCORER_VERSION` constant (bumped
  whenever the scoring path itself changes) -- any mismatch is a miss and
  triggers a fresh (cache-overwriting) recompute. `--no-baseline-cache`
  disables the cache entirely (always recompute, still refreshes the cache
  unless disabled); `--force-baseline` ignores an existing hit and
  recomputes, then updates the cache. Progress (`baseline cache: HIT`/`MISS`,
  per-stage status) prints incrementally with `flush=True` so a long
  multi-model run doesn't look hung.

  For each model it writes `baseline_report.json` next to that model's
  `report.json`, with (among other fields) `released_engine_esr`,
  `engine_v2_esr`, `esr_delta` (`released - engine_v2`; positive means
  EngineV2 is better), `relative_improvement`, `winner`
  (`"engine_v2"|"released"|"tie"`, tie tolerance `1e-6` relative), full
  provenance (binary/NAM/CLO paths and SHA256s, exact argv), and the
  explicitly non-overreaching `verdict` string. When `--output-root` covers
  several models it also writes `<output-root>/baseline_summary.json` with
  win/loss/tie counts, mean/median `relative_improvement`, and a `failures`
  list -- one model failing does not abort the run. Usage:
  `python3 -u baseline_compare.py --output-root <EngineV2 output dir> --nam-root <NAM corpus dir> --namtoclo-bin build-macos/namtoclo`.
  This is a stimulus-fit-to-raw-NAM-teacher comparison only -- not a
  hardware-validated result, and it never feeds results back into EngineV2's
  fitting in any way; see `ENGINE_V2_RESEARCH_NORTH_STAR.md`
  "Existing converter baseline" for what more is required. Tests:
  `test_baseline_compare.py` (cache identity, actual-CLO-vs-stored-metric
  scoring, symmetric render/score path, winner/summary math, deterministic
  parallel-render ordering -- all on small synthetic data, no real NAM
  conversion or built CLI needed).

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

## Generated / gitignored

`_baseline_cache/` (wherever `baseline_compare.py`'s `--baseline-cache-root`
points, default `<output-root>/_baseline_cache/`) and any CLOs
`baseline_compare.py` generates are local research artifacts, not something
to commit. The repo-root `.gitignore` covers `/src/engine_v2/_baseline_cache/`
for the common case of an output root inside the repo; an output root outside
the repo (e.g. under `$HOME`) is outside git's purview entirely.
