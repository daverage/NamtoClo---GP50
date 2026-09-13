#!/usr/bin/env python3
"""Run the conservative EngineV2 cleanup using the current V4.2 active roots.

This delegates all safety checks, dependency-closure discovery, verification,
archiving and dry-run/apply behavior to clean_engine_v2.py. Only the active
entrypoints/documents are updated here so the historical cleanup implementation
is not forked.
"""
from __future__ import annotations

import clean_engine_v2 as cleanup


cleanup.ACTIVE_ENTRYPOINTS = (
    "train_namtoclo_north_star_v42.py",
    "analyze_north_star_v4_stimulus_diagnostics.py",
    "test_north_star_v42.py",
    "test_north_star_v4_stimulus_diagnostics.py",
)

cleanup.PROTECTED_FILES = (
    "ENGINE_V2_RESEARCH_NORTH_STAR.md",
    "NORTH_STAR_V4_STIMULUS_EXPERIMENT.md",
    "NORTH_STAR_V4_1_CONVERGENCE_EXPERIMENT.md",
    "NORTH_STAR_V4_2_LINE_CONVERGENCE_EXPERIMENT.md",
)

cleanup.ACTIVE_TESTS = (
    "test_north_star_v42.py",
    "test_north_star_v4_stimulus_diagnostics.py",
)

cleanup.CLI_CHECKS = (
    ("train_namtoclo_north_star_v42.py", "--help"),
    ("analyze_north_star_v4_stimulus_diagnostics.py", "--help"),
)


if __name__ == "__main__":
    raise SystemExit(cleanup.main())
