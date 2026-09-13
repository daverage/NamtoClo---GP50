# NamtoClo research corpus builder

Run:

```bash
python3 -m pip install requests numpy py7zr
python3 build_namtoclo_research_corpus.py ~/NamtoCloResearchCorpus
```

The default `full` profile downloads the complete Guitar-TECHS and EGFxSet Zenodo
records, the useful GuitarSet mono pickup mix + annotations, and four CC0 electric
guitar sample sources. It also generates the NamtoClo synthetic system-identification
probe pack locally.

Before a large download, inspect the current Zenodo file lists and sizes:

```bash
python3 build_namtoclo_research_corpus.py --list-zenodo
```

For a smaller bring-up run:

```bash
python3 build_namtoclo_research_corpus.py ~/NamtoCloResearchCorpus --profile core
```

Downloads resume using HTTP Range where supported. The builder writes:
- `provenance.json`
- `download_manifest.json` with SHA-256 hashes
- `audio_inventory.csv`
- `CORPUS_PLAN.md`

The raw corpus is intentionally not the final runtime corpus. Its purpose is to
develop and validate the clean-sheet CLO distiller and then discover a much smaller
runtime probe set.
