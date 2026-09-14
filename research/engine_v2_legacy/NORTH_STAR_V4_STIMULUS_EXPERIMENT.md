# North Star v4 stimulus-only experiment

This is a controlled EngineV2 experiment, not a replacement for `ENGINE_V2_RESEARCH_NORTH_STAR.md` unless the evidence later justifies changing the fitting philosophy.

The North Star remains the authority: the source NAM is the behavioural teacher and the GP50 CLO is the constrained student. V4 changes only the information used to fit that student.

## Question

Can the official TONE3000 `T3K-sweep-v3.wav` identification stimulus teach the complete GP50 student more effectively than the v3 mixture of hand-designed probes plus real guitar FIT material?

## Frozen architecture

```text
PRE -> A128 -> oversampled asymmetric P/K -> POST -> B512
```

PRE and POST remain fixed. A and P/K are searched jointly. B512 is still solved analytically for every A/P-K candidate. There are no named-amp rules and no added harmonic, cleanup, compression or spectral penalty terms.

## V4 FIT material

The official T3K sweep is rendered independently through each NAM at global input offsets:

```text
0, -6, -12, -18, -24 dB
```

One single CLO must approximate all level variants simultaneously. Each full stimulus/target pair is one equal-weight, target-energy-normalized FIT unit. The stimulus and NAM outputs are not amplitude-normalized.

The exact downloaded stimulus SHA256, NAM SHA256, NAMCore renderer SHA/commit and per-level student input/target hashes are recorded in every report.

## Guitar is comparison-only

Real guitar is never used to:

- move A or P/K;
- solve or select a checkpoint/start;
- choose between multistarts;
- calibrate final output gain.

After the V4 coefficients are frozen, the same deterministic real-guitar partitions used by V3 are evaluated so V3 and V4 can be compared on identical development evidence.

Those previously inspected partitions are development comparison material, not a fresh held-out benchmark claim. Sealed NAMs remain untouched.

## Search

V4 keeps the v3 deterministic multistart and coarse-to-fine search. The only candidate-selection metric is stimulus FIT evidence ESR.

## Decision rule

V4 is interesting only if the stimulus-only fit improves real-guitar behaviour broadly across unrelated NAM families while preserving already-good controls. A win on one named model, a lower stimulus ESR by itself, or a result that only looks better after inspecting previously consumed guitar material is not enough to replace the North Star fitting philosophy.

Hardware listening remains the final acceptance gate.
