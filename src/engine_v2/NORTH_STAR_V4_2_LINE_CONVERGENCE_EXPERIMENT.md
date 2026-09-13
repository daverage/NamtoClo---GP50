# North Star V4.2 — accelerated line-coordinate convergence experiment

`ENGINE_V2_RESEARCH_NORTH_STAR.md` remains the governing document.

## Question

V4.1 established that the V4-selected basin was still descending after twelve fine-resolution sweeps. On the G3 development model, almost every A control still accepted one 0.05 dB move per cycle and the stimulus FIT ESR continued to fall materially. The V4.1 safety cap therefore stopped the search before convergence.

The first V4.2 implementation proved that repeatedly walking a coordinate at the fine step is computationally impractical on five complete 190-second stimulus renders. The scientific question remains the same, but the line search itself now needs to reach the same fine-grid neighbourhood with fewer full candidate evaluations.

V4.2 asks one narrow question:

> Can we converge the same V4-family A/P-K/B student much more efficiently by bracketing and refining each improving coordinate on the existing fine grid, without changing the teacher evidence, objective, parameter bounds or DSP structure?

## What stays frozen

V4.2 intentionally keeps the V4/V4.1 scientific problem unchanged:

- TONE3000 `T3K-sweep-v3.wav` is the only fitting stimulus;
- the stimulus is independently rendered through the NAM at `0, -6, -12, -18, -24 dB`;
- one CLO must fit all five levels;
- the fit objective is the exact V4 direct aligned, target-energy-normalized waveform ESR;
- A128, asymmetric P/K, fixed POST and analytic shared B512 are unchanged;
- P/K bounds are unchanged;
- guitar remains comparison-only and cannot influence coefficients, candidate choice or output calibration;
- the V4 output calibration rule is deliberately unchanged and remains a separate research issue.

No spectral, level-response, harmonic, compression or named-model penalty is added.

## Warm-start reuse is optimization caching, not new evidence

If a compatible V4.1 report already exists for the same model, exact T3K stimulus SHA, level set and A-control count, V4.2 may reuse only its stored `controls_db` and `pk` as the starting optimization state.

The stored V4.1 B512 and stored score are not trusted. V4.2 re-evaluates the A/P-K state against the current authoritative FIT evidence and analytically solves B512 again before polishing.

This avoids rerunning the three-start/eight-round V4 basin search that has already been performed for that exact experiment. If no compatible V4.1 report is available, V4.2 falls back to the exact V4 deterministic multistart/coarse-to-fine search.

## Accelerated fine-grid line search

The established fine resolutions are unchanged:

```text
A step       = 0.05 dB
P/K log step = 0.02
```

For each coordinate, V4.2 now:

```text
test +1 and -1 fine step
choose the improving direction
probe 2, 4, 8, 16, ... fine-step offsets while improvement continues
bracket the local minimum
refine the bracket on integer fine-step offsets
exhaustively evaluate the final narrow bracket
```

Thus the returned A coordinate remains on the exact 0.05 dB grid and P/K remains on the exact 0.02 log-step grid. The change is only how many intermediate candidates are evaluated to locate the local coordinate minimum.

A complete A sweep is followed by one or more P/K sweeps with A held fixed. Full A/P-K cycles repeat until:

- no coordinate moves;
- relative FIT improvement becomes negligible; or
- the cycle safety cap is reached.

Every evaluated candidate still receives a newly solved shared analytic B512. This remains joint A/P-K/B teacher/student fitting, not isolated nonlinear identification.

## Runtime infrastructure

V4.2 is built on the latest `EngineV2` branch and inherits the current runtime-only speed work:

- clip-level DSP evaluation uses the current threaded `_map` path in `distiller_v2_fit.py`;
- independent stimulus-level cache misses can be prepared concurrently;
- `--stimulus-cache-root` can reuse an existing matching V4-family teacher cache;
- compatible V4.1 A/P-K state is used as a restart by default, avoiding redundant basin search;
- progress is printed per A and P/K coordinate so a long full-stimulus sweep no longer appears hung.

These are implementation/runtime efficiencies only. They do not change the teacher data, objective, DSP structure, parameter bounds or guitar-exclusion rule.

## First comparison

Run the same G3 development model used to diagnose V4.1. The useful signals are:

```text
re-evaluated V4.1 starting FIT ESR
V4.2 final FIT ESR
candidate evaluations per coordinate/cycle
line-polish cycles to stop
A/P-K fine-step distances
paired guitar comparison against V4.1, V4 and V3
```

A successful search change should reach or beat the V4.1 stimulus fit substantially faster and then stall naturally, with coordinate moves collapsing toward zero. Improvement on the already-consumed guitar comparison material is useful corroborating development evidence but is not a fresh held-out claim.

If difficult models remain broadly poor after genuine convergence, that becomes stronger evidence for a representability limitation rather than an unfinished optimizer.
