# North Star V4.2 — line-coordinate convergence experiment

`ENGINE_V2_RESEARCH_NORTH_STAR.md` remains the governing document.

## Question

V4.1 established that the V4-selected basin was still descending after twelve fine-resolution sweeps. On the G3 development model, almost every A control still accepted one 0.05 dB move per cycle and the stimulus FIT ESR continued to fall materially. The V4.1 safety cap therefore stopped the search before convergence.

V4.2 asks one narrow question:

> If an A or P/K coordinate is already improving in one direction, can following that coordinate repeatedly at the existing fine resolution reach the same local basin minimum much more efficiently than allowing only one fine step per cycle?

## What stays frozen

V4.2 intentionally keeps the V4/V4.1 scientific problem unchanged:

- TONE3000 `T3K-sweep-v3.wav` is the only fitting stimulus;
- the stimulus is independently rendered through the NAM at `0, -6, -12, -18, -24 dB`;
- one CLO must fit all five levels;
- the fit objective is the exact V4 direct aligned, target-energy-normalized waveform ESR;
- A128, asymmetric P/K, fixed POST and analytic shared B512 are unchanged;
- P/K bounds are unchanged;
- the exact V4 deterministic multistart/coarse-to-fine search still chooses the starting basin;
- guitar remains comparison-only and cannot influence coefficients, candidate choice or output calibration;
- the V4 output calibration rule is deliberately unchanged and remains a separate research issue.

No spectral, level-response, harmonic, compression or named-model penalty is added.

## The only fitting change

After exact V4 basin selection, V4.2 uses the same established fine resolutions:

```text
A step       = 0.05 dB
P/K log step = 0.02
```

For each A coordinate:

```text
test +step and -step from the same current candidate
choose the better improving direction
keep taking that same exact step while FIT ESR improves
stop that coordinate when the next step fails or a safety cap is reached
```

P/K coordinates use the same rule multiplicatively. A complete A line sweep is followed by one or more P/K line sweeps with A held fixed. Full A/P-K cycles repeat until:

- no coordinate accepts a step;
- relative FIT improvement becomes negligible; or
- the cycle safety cap is reached.

Every accepted candidate still receives a newly solved shared analytic B512. This remains joint A/P-K/B teacher/student fitting, not isolated nonlinear identification.

## Runtime infrastructure

V4.2 is built on the latest `EngineV2` branch rather than an older V4.1 snapshot. It therefore inherits runtime-only speed work already present on the branch:

- clip-level DSP evaluation uses the current threaded `_map` path in `distiller_v2_fit.py`;
- the current V4 stimulus preparation can render independent level cache misses concurrently;
- V4.2 accepts `--stimulus-cache-root` so an existing matching V4-family teacher cache can be reused instead of re-rendering the NAM stimulus.

These are implementation/runtime efficiencies only. They do not change the teacher data, objective, DSP structure, parameter bounds or candidate-selection philosophy.

## First comparison

Run the same G3 development model used to diagnose V4.1. The useful signals are:

```text
V4 selected-basin FIT ESR
V4.2 final FIT ESR
line-polish cycles to stop
A coordinates moved / accepted fine steps
P/K coordinates moved / accepted fine steps
guitar comparison against V4.1, V4 and V3
```

A successful search change should reduce stimulus FIT error and then stall naturally, with accepted moves collapsing toward zero. Improvement on the already-consumed guitar comparison material is useful corroborating development evidence but is not a fresh held-out claim.

If the difficult models remain broadly poor after genuine convergence, that becomes stronger evidence for a representability limitation rather than an unfinished optimizer.
