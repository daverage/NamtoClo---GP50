# North Star V4.1 convergence experiment

V4.1 is a deliberately narrow follow-up to the V4 stimulus-only experiment.

It does **not** change the teacher evidence, GP50 student architecture, objective, parameter bounds, multistart seed bank, analytic B512 solve, guitar policy, or output calibration. Its only question is whether V4 stopped before its selected FIT basin had genuinely converged.

## Motivation

The V4 diagnostic panel showed that several difficult models were still accepting changes on most of the 24 A controls in the last coarse-to-fine round, even though the A and P/K step schedules had already reached their floors. That means the search could have stopped because the schedule ended rather than because the selected solution had stalled.

Before interpreting broad residuals as a GP50 representability limit, the North Star requires us to rule out ordinary search/convergence failure.

## Frozen V4 behavior

V4.1 retains exactly:

- TONE3000 `T3K-sweep-v3.wav` as the only fitting stimulus;
- independent NAM renders at `0, -6, -12, -18, -24 dB` by default;
- one equal-weight whole-waveform FIT unit per input level;
- direct aligned teacher/student ESR as the fitting objective;
- the same A128 parameterization and `[-18, +18] dB` control limits;
- the same asymmetric P/K family and P/K bounds;
- the same deterministic multistart seed bank;
- the same V4 coarse-to-fine search trajectory;
- one shared target-energy-normalized analytic B512 solve for every A/P-K candidate;
- no guitar in coefficient fitting, candidate choice, or output calibration;
- the existing V4 post-output calibration rule, even where diagnostics suggest it may deserve a later experiment.

Real guitar remains comparison-only development evidence.

## The one V4.1 change

After exact V4 search selects its best FIT-only checkpoint, V4.1 takes that single basin and repeatedly performs:

```text
A sweep at 0.05 dB per coordinate
        ->
P/K sweep at log-step 0.02
        ->
A sweep again
        ->
P/K sweep again
        ->
...
```

Every accepted candidate is still evaluated with the complete GP50 student and a newly solved shared B512.

The default safety cap is 12 polish cycles. Polishing stops earlier when either:

1. a complete cycle accepts no A or P/K moves; or
2. the cycle's relative FIT-ESR improvement is at or below `1e-5`.

Only the V4-selected basin is polished. The other multistart basins are not made more expensive.

## What would count as evidence

If repeated fine polishing materially improves stimulus FIT and unrelated real-guitar comparison across several model families, then V4's search termination was still a meaningful bottleneck.

If the selected basin genuinely stalls while broad stimulus and guitar residuals remain, then the evidence for an objective/evidence/representability limitation becomes stronger.

A result on one named model is not enough to justify new penalties, new parameter bounds, or architecture assumptions.

## Explicit non-goals

V4.1 does not:

- raise the K=80 bound;
- add level-response, spectrum, harmonic, IMD, or transient penalties;
- add guitar to training;
- use device/amp/pedal identity;
- change stimulus levels;
- change B solving;
- change output calibration;
- change the GP50 DSP model.

Those remain separate hypotheses and must not be mixed into this convergence experiment.
