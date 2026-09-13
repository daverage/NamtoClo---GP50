#!/usr/bin/env python3
"""Run the EngineV2 cleanup with V4.1 as the active experiment.

This wrapper reuses the conservative cleanup implementation in clean_engine_v2.py
but changes only the protected active roots/tests/documents so a later cleanup
cannot archive the new V4.1 files by mistake.
"""
from __future__ import annotations

import clean_engine_v2 as cleanup


cleanup.ACTIVE_ENTRYPOINTS = (
    "train_namtoclo_north_star_v41.py",
    "analyze_north_star_v4_stimulus_diagnostics.py",
    "test_north_star_v41.py",
    "test_north_star_v4_stimulus_diagnostics.py",
)

cleanup.PROTECTED_FILES = (
    "ENGINE_V2_RESEARCH_NORTH_STAR.md",
    "NORTH_STAR_V4_STIMULUS_EXPERIMENT.md",
    "NORTH_STAR_V4_1_CONVERGENCE_EXPERIMENT.md",
)

cleanup.ACTIVE_TESTS = (
    "test_north_star_v41.py",
    "test_north_star_v4_stimulus_diagnostics.py",
)

cleanup.CLI_CHECKS = (
    ("train_namtoclo_north_star_v41.py", "--help"),
    ("analyze_north_star_v4_stimulus_diagnostics.py", "--help"),
)


if __name__ == "__main__":
    raise SystemExit(cleanup.main())
