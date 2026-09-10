# NamtoClo Teacher Dataset Builder

This is the third piece of the clean-sheet research pipeline:

1. `build_namtoclo_research_corpus.py` — source guitar/probe corpus
2. `build_namtoclo_nam_corpus.py` — curated NAM teacher corpus
3. **`build_namtoclo_teacher_dataset.py`** — crosses the two and caches exact NAM outputs

## Install

```bash
python3 -m pip install numpy scipy soundfile
```

On macOS, if CMake/compiler tools are missing:

```bash
xcode-select --install
brew install cmake
```

## Recommended development run

Keep the modern sealed NAM set untouched while the CLO optimiser is being designed:

```bash
python3 build_namtoclo_teacher_dataset.py \
  --guitar-corpus ~/NamtoCloResearchCorpus \
  --nam-corpus ~/NamtoCloNAMCorpus \
  --output ~/NamtoCloTeacherDataset \
  --build-namcore \
  --profile full
```

The builder pins/builds NeuralAmpModelerCore `v0.5.3` by default and uses its
official `render` executable.

## Inspect before the long run

```bash
python3 build_namtoclo_teacher_dataset.py \
  --guitar-corpus ~/NamtoCloResearchCorpus \
  --nam-corpus ~/NamtoCloNAMCorpus \
  --output ~/NamtoCloTeacherDataset \
  --build-namcore \
  --profile full \
  --plan-only
```

## Smoke test

```bash
python3 build_namtoclo_teacher_dataset.py \
  --guitar-corpus ~/NamtoCloResearchCorpus \
  --nam-corpus ~/NamtoCloNAMCorpus \
  --output ~/NamtoCloTeacherSmoke \
  --build-namcore \
  --profile smoke \
  --limit-tasks 20
```

## Resume

Run the same command again. `render_journal.sqlite3` records every completed task.

## Final sealed test

Only after the converter/loss/optimiser is frozen:

```bash
python3 build_namtoclo_teacher_dataset.py \
  --guitar-corpus ~/NamtoCloResearchCorpus \
  --nam-corpus ~/NamtoCloNAMCorpus \
  --output ~/NamtoCloTeacherDataset \
  --build-namcore \
  --profile full \
  --include-sealed
```

## Default corpus rules

- Guitar-TECHS: DI/direct perspective only.
  - P1 -> fit
  - P2 -> selection
  - P3 -> benchmark
- EGFxSet: Clean files only -> selection
- GuitarSet mono pickup mix -> benchmark
- CC0 sample libraries -> fit
- Synthetic probes retain their absolute levels and have explicit roles.
- Real guitar is normalised to -9 dBFS peak before deterministic drive offsets.
- 25% of full-profile real-guitar segments get -12/-6/0/+6 dB relative variants.
- A2 is rendered at Slim=1.0 (full capacity).
- Old NAMs without an explicit sample rate are treated as 48 kHz.
- Raw NAM outputs are never normalised.
- Student pairs are additionally produced at 44.1 kHz.

## Output

```text
NamtoCloTeacherDataset/
├── prepared_inputs/          # Exact inputs sent to NAMCore
├── teacher_raw/              # Exact NAMCore output at NAM rate
├── student_44100/
│   ├── inputs/               # Student-side inputs
│   └── targets/              # Student-side NAM targets
├── prepared_inputs.jsonl
├── nam_models.jsonl
├── tasks.jsonl
├── run_manifest.json
├── render_journal.sqlite3
└── validation_report.json
```

The `.nam` files themselves remain in the separate local NAM corpus.
