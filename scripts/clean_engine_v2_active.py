#!/usr/bin/env python3
"""Run the conservative EngineV2 cleanup using the current active research path.

This delegates safety checks, dependency-closure discovery, verification,
archiving and dry-run/apply behavior to clean_engine_v2.py. Only the current
entrypoints/documents are declared here so historical experiment files can move
out of src/engine_v2 without risking the active fitter.
"""
from __future__ import annotations

import clean_engine_v2 as cleanup


# The fast launcher is the canonical current entrypoint. Its dependency closure
# automatically retains the authoritative V4.2 trainer and every older shared
# implementation module that is still genuinely imported by the active path.
cleanup.ACTIVE_ENTRYPOINTS = (
    "train_namtoclo_north_star_v42_fast.py",
    "analyze_north_star_v4_stimulus_diagnostics.py",
    "test_north_star_v42.py",
    "test_north_star_v42_fast_warm_start.py",
    "test_north_star_v4_stimulus_diagnostics.py",
)

# Keep only the governing North Star and the current experiment note in the
# active directory. Older V4/V4.1 and one-off warm-start notes belong in the
# reproducibility archive; git history remains available as well.
cleanup.PROTECTED_FILES = (
    "ENGINE_V2_RESEARCH_NORTH_STAR.md",
    "NORTH_STAR_V4_2_LINE_CONVERGENCE_EXPERIMENT.md",
)

cleanup.ACTIVE_TESTS = (
    "test_north_star_v42.py",
    "test_north_star_v42_fast_warm_start.py",
    "test_north_star_v4_stimulus_diagnostics.py",
)

cleanup.CLI_CHECKS = (
    ("train_namtoclo_north_star_v42_fast.py", "--help"),
    ("analyze_north_star_v4_stimulus_diagnostics.py", "--help"),
)


def _render_current_readme(kept_python, engine):
    names = sorted(p.relative_to(engine).as_posix() for p in kept_python)
    deps = [n for n in names if n not in cleanup.ACTIVE_ENTRYPOINTS]
    return "\n".join(
        [
            "# EngineV2 active research workspace",
            "",
            "`ENGINE_V2_RESEARCH_NORTH_STAR.md` is the governing research document.",
            "",
            "## Project rule",
            "",
            "EngineV2 is an alternative NAM -> GP50 conversion method. The source NAM is",
            "the behavioural authority and the released NamToClo engine is the permanent",
            "baseline to beat. Internal V4.x comparisons are diagnostics, not the product",
            "success criterion.",
            "",
            "## Canonical active entrypoint",
            "",
            "- `train_namtoclo_north_star_v42_fast.py`",
            "",
            "The remaining entrypoints below are tests/diagnostics retained by the cleanup:",
            *[f"- `{n}`" for n in cleanup.ACTIVE_ENTRYPOINTS if n != "train_namtoclo_north_star_v42_fast.py"],
            "",
            "## Required Python dependency closure",
            "",
            *[f"- `{n}`" for n in deps],
            "",
            "Some dependency filenames contain earlier experiment version numbers because the",
            "current fitter deliberately reuses proven shared search/evidence helpers. They are",
            "implementation dependencies, not active competing engines.",
            "",
            "## Research documents",
            "",
            *[f"- `{n}`" for n in cleanup.PROTECTED_FILES],
            "",
            "Historical/unused EngineV2 files live under `research/engine_v2_legacy/` and remain",
            "available through git history. Do not restore them to `src/engine_v2` unless they",
            "become a genuine dependency of the current engine.",
            "",
            "Use `scripts/clean_engine_v2_active.py` for future pruning.",
            "",
        ]
    )


cleanup.render_readme = _render_current_readme


if __name__ == "__main__":
    raise SystemExit(cleanup.main())
