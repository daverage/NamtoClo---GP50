# NamtoClo HF-first corpus builder

This replaces the Zenodo-first corpus builder.

Install:

```bash
python3 -m pip install huggingface_hub pyarrow requests numpy soundfile py7zr
```

Inspect the plan:

```bash
python3 build_namtoclo_research_corpus_hf.py --plan
```

Build the corpus:

```bash
python3 build_namtoclo_research_corpus_hf.py ~/NamtoCloResearchCorpus
```

Optionally try adding full Guitar-TECHS without making it a hard dependency:

```bash
python3 build_namtoclo_research_corpus_hf.py ~/NamtoCloResearchCorpus --with-guitar-techs
```

The default build uses GuitarJam, EGFxSet Clean, GuitarSet pickup mix, four CC0
guitar sample libraries, two CC0 bass libraries, and the NamtoClo synthetic probes.

The Hugging Face EGFxSet and GuitarSet mirrors store audio in Parquet. The builder
materialises only the useful audio columns/rows, then removes the Parquet container
by default. Use `--keep-containers` if you want to retain those downloaded containers.

Recommended free disk before the first run: at least 25 GB.
