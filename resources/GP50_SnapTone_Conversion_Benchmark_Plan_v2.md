# GP-50 SnapTone Conversion: Reverse-Engineered Pipeline, Our Improvements, and Benchmark Plan

## Purpose

This document records what we currently know about Valeton's SnapTone conversion pipeline, how our GP-5/GP-50 NAM-to-CLO implementation differs, and the testing protocol we will use to determine whether our converter is genuinely producing better GP-50 models rather than simply becoming more complicated.

The key principle is simple:

> **The Full NAM A2 model is the reference.**
>
> Valeton's official SnapTone conversion and our converter must both be judged by how closely their resulting GP-50 models reproduce the same original A2 model.

The destination is the same in both cases: the compact GP-5/GP-50 SnapTone DSP model. The question is whether our fitting process uses that limited model more effectively.

---

# 1. What a GP-5/GP-50 SnapTone actually is

The compact GP-5/GP-50 SnapTone model is a **2696-byte VTSI DSP object**.

Current confirmed structure:

| Offset | Content |
|---|---|
| `0x00..03` | `VTSI` |
| `0x04..07` | Declared size = `0x0A88` = 2696 bytes |
| `0x08..09` | CRC16/MODBUS, stored high byte first |
| `0x0A..0x13` | Reserved / unknown |
| `0x14..0x17` | Payload size = `0x0A00` = 2560 bytes |
| `0x18..0x3F` | Pre biquad, five doubles |
| `0x40..0x67` | Post biquad, five doubles |
| `0x68` | `Pp` float32 |
| `0x6C` | `Pn` float32 |
| `0x70` | `Kp` float32 |
| `0x74` | `Kn` float32 |
| `0x78` | A start |
| `0x7C` | A count = 128 |
| `0x80` | B start = 128 |
| `0x84` | B count = 512 |
| `0x88...` | A128 float32 + B512 float32 |

The effective DSP topology is:

```text
Input
  |
  v
Pre biquad
  |
  v
A128 FIR
  |
  v
4x oversampled asymmetric exponential nonlinearity
  |
  v
Post biquad
  |
  v
B512 FIR
  |
  v
Output
```

The nonlinear stage is:

```text
if x > 0:
    y = Pp * (1 - exp(-Kp * x))
else:
    y = -Pn * (1 - exp(Kn * x))
```

The 4x oversampling is implemented as two successive 2x upsampling stages followed by two successive 2x downsampling stages.

This structure is no longer just inferred from our converter. It is consistent with:

- factory model data found in GP-50 firmware;
- captured official SnapTone transfers;
- the current Valeton Suite conversion library;
- the fitting code paths inside Valeton's proprietary `startClone()` implementation.

---

# 2. What Valeton's official conversion is doing

Valeton is **not repackaging a NAM file** and the GP-50 is not directly executing the original NAM network.

The official conversion pipeline is effectively:

```text
NAM file
  |
  v
Valeton runs the actual NAM model
  |
  v
Reference audio produced from Valeton's conversion stimulus
  |
  v
startClone() proprietary fitter
  |
  v
Compact SnapTone model
  |
  v
GP-5 / GP-50
```

So both Valeton and our converter are solving the same problem:

> Approximate a much more complex NAM model using the much smaller fixed SnapTone architecture.

---

# 3. Reverse-engineered Valeton `startClone()` fitting process

The current Mac Valeton Suite 2.1.0 package contains the fitting code in:

```text
Valeton Suite.app/Contents/Frameworks/5868USB.dylib
```

Useful symbols are still present, including:

```text
HTKPA::startClone()
HTKPA::iterAmpCoeff(...)
HTKPA::downSampleSmoothFreq(...)
HTKPA::smoothdata(...)
HTKPA::minPhaseIR(...)
HTKPA::tfestimate(...)
HTKPA::init(float)
getNamOutput(...)
getConvertNormalWav(...)
namConverter(...)
namConverterCloData(...)
```

`HTKPA::startClone()` is approximately 33 KB of ARM64 code in this build.

## 3.1 P/K fitting

The official fitter determines `Pp`, `Pn`, `Kp`, and `Kn` before the main A/B iterative fitting begins.

The process is effectively:

1. Analyse the first 5 seconds of the stimulus/response.
2. Divide it into approximately 100 ms windows.
3. For each window calculate:
   - maximum absolute input;
   - maximum positive output;
   - minimum negative output.
4. Set `Pp` and `Pn` from the observed positive and negative extrema.
5. Use the response up to approximately `0.5 * P` to estimate the small-signal slope.
6. Seed:

```text
Kp ~= positive slope / Pp
Kn ~= negative slope / Pn
```

7. Search the following multipliers around each seed:

```text
0.80
0.85
0.90
0.95
1.00
1.05
1.10
1.15
1.20
```

8. Select positive K first, then negative K.

Importantly:

> **P/K is then frozen.**

The later A/B fitting stages do not appear to go back and re-optimise P/K.

This means Valeton's official approach is not a joint alternating optimisation of nonlinear and linear parameters.

---

# 4. Valeton's A/B fitting

The main A/B fitter uses three phases:

| Phase | Stimulus region | Iterations |
|---|---:|---:|
| Sweep | ~23-28 s | 3 |
| Low-level | ~6-21 s | 2 |
| Multi-level | ~30-50 s | 5 |

Total: **10 iterations**.

The broad iteration is:

```text
Current correction spectrum
      |
      v
pow(correction, frequencyWeight * step)
      |
      v
Clamp correction to approx. 0.2 ... 5.0
      |
      v
Frequency conditioning / smoothing
      |
      v
Move spectral factor between A and B
      |
      v
Construct minimum-phase A FIR
      |
      v
Render Pre -> A -> P/K -> Post
      |
      v
Estimate residual transfer function
      |
      v
Construct minimum-phase B FIR
      |
      v
Render complete model
      |
      v
Measure residual spectral loss
```

The optimiser keeps its best state. If a new iteration becomes substantially worse, approximately:

```text
newLoss > bestLoss * 1.20
```

it restores the best previous state and reduces the optimisation step.

There is also a normal step decay of approximately 0.9 per iteration.

---

# 5. Valeton's loss function

The official A/B optimiser primarily evaluates **spectral magnitude error**.

The residual is evaluated over roughly 512 perceptually spaced frequency points between about 80 Hz and 10 kHz.

Conceptually the loss is approximately:

```text
mean(abs(log(residual magnitude ratio + epsilon)))
```

This is important because it is a good tonal/spectral objective, but it is **not an explicit dynamics objective**.

It does not directly ask whether the model has the same input/output compression curve as the original NAM at multiple playing levels.

---

# 6. Valeton's final B refinement

After the main 3 + 2 + 5 fitting passes, Valeton performs an additional final B correction.

Broadly:

```text
Render current model
      |
      v
Measure remaining spectral residual
      |
      v
Smooth residual
      |
      v
Create minimum-phase correction IR
      |
      v
Convolve correction into B
      |
      v
Remove DC / mean
      |
      v
Energy normalise
```

Again, this is primarily a linear spectral correction.

---

# 7. Pre and Post filters

The reverse engineering so far does not show Valeton training a unique Pre and Post biquad for each NAM during the main iterative fitter.

The Pre stage is effectively neutral in the normal path.

The Post filter corresponds closely with the fixed sample-rate-derived filter already used by our implementation.

This is consistent with our previous experiment where optimising Post produced only small or inconsistent gains.

---

# 8. Native B512 support

The proprietary fitter contains support for at least two B sizes:

```text
mode 0      -> B2048
compact mode -> B512
```

So A128/B512 is clearly a native target configuration, not something we invented.

However, one remaining implementation detail is whether every current desktop/mobile GP-50 conversion invokes the compact B512 fitting path directly or performs an intermediate longer fit before producing the final compact object.

This does not change the actual GP-50 runtime constraint: the final hardware model contains B512.

---

# 9. How our converter differs

Our converter started by recreating the Valeton-style fitting process, but it is now deliberately adding optimisation stages which appear to go beyond the manufacturer's original objective.

## 9.1 Official-style P/K reconstruction

Our current `fitPk()` closely reconstructs Valeton's original P/K calculation:

- 100 ms extrema windows;
- first 5 seconds;
- separate positive and negative branch limits;
- half-P small-signal slope estimate;
- K seed from slope/P;
- 0.80 to 1.20 multiplier search.

This is now best regarded as our **baseline / manufacturer-style initialisation**, not the final word on dynamics.

## 9.2 Official-style A/B iteration

Our current A/B fitter mirrors the main proprietary sequence closely:

```text
Sweep       3 iterations
Low-level   2 iterations
Multi-level 5 iterations
```

with:

- frequency weighting;
- conditioned correction curves;
- minimum-phase FIR generation;
- best-state snapshots;
- rollback when loss becomes >1.2x worse;
- step reduction;
- final B refinement.

Therefore the baseline path is now close enough to the official fitter that further changes can legitimately be treated as **improvements**, rather than simply filling reverse-engineering gaps.

---

# 10. Our direct B512 solve

One of the strongest improvements already found is solving the final device-sized B512 directly.

Instead of deriving a larger correction and then truncating/compressing it, we freeze:

```text
Pre -> A -> P/K -> Post
```

and solve directly for the 512-tap B that best maps the pre-B signal to the Full A2 target.

In frequency-domain form this is approximately:

```text
H(f) = conj(P(f)) * T(f) / (|P(f)|^2 + lambda)
```

where:

- `P` is the current signal entering B;
- `T` is the target Full A2 output;
- `lambda` is regularisation.

This produced very large held-out loss reductions in our previous testing, approximately **54-91% depending on model/test case**.

This is a genuine algorithmic difference from the original minimum-phase residual correction approach.

It does not add any new runtime capability to the GP-50. It simply tries to use the same available B512 coefficients more effectively.

---

# 11. The dynamics problem we discovered

We measured the Full A2 and converted GP-50 model at input levels:

```text
-24 dB
-18 dB
-12 dB
 -6 dB
  0 dB
 +6 dB
```

Some representative results were:

| Model | Type | Full A2 sweep | GP50 sweep | Max relative error | RMS relative error |
|---|---|---:|---:|---:|---:|
| Fender Super Reverb | Clean | 29.8 dB | 29.3 dB | 0.28 dB | 0.19 dB |
| Fender Deluxe Reverb | Clean | 28.3 dB | 28.1 dB | 0.96 dB | 0.63 dB |
| JCM800 2203 Modified | Medium crunch | 16.2 dB | 17.3 dB | 0.90 dB | 0.49 dB |
| Best Metal Bass Amp | High gain bass | 9.0 dB | 11.0 dB | 1.70 dB | 1.14 dB |
| Marshall Silver Jubilee | Hot Marshall | 7.5 dB | 8.7 dB | 0.99 dB | 0.57 dB |
| MattFig Recto Crush | Extreme gain | 3.0 dB | 4.3 dB | 1.16 dB | 0.58 dB |
| PRS Archon 100 | Modern high gain | 1.6 dB | 4.6 dB | 2.69 dB | 1.50 dB |
| Fortin Meshuggah | Extreme gain | 1.4 dB | 4.4 dB | 3.04 dB | 1.50 dB |
| Bogner Uberschall MKII | Extreme gain | 1.3 dB | 9.5 dB | 7.29 dB | 4.00 dB |

The pattern is very clear:

- clean models are already extremely close;
- moderate-gain models have some room for improvement;
- high-gain models can show large level-dependent errors;
- the worst models **saturate too late in the GP-50 approximation**.

This cannot be fixed by B512 alone because B is linear. A fixed linear filter cannot repair an input-level-dependent gain/compression error.

The likely variables are therefore:

- `Kp` / `Kn`;
- potentially `Pp` / `Pn`;
- the overall gain into A / nonlinear operating point.

---

# 12. Why our dynamics optimisation may outperform Valeton

Valeton's official P/K estimation is fixed before the A/B fitting begins.

Although the official stimulus contains a later multi-level section, the A/B optimiser still accepts or rejects its changes primarily on spectral magnitude loss.

It does **not appear to explicitly minimise the compression curve error across separately measured input levels**.

Our proposed dynamics-aware stage does.

For a set of input levels, e.g.:

```text
-24, -18, -12, -6, 0, +6 dB
```

we can optimise nonlinear parameters while asking directly:

```text
Does the converted model produce the same relative output level as Full A2
at every input level?
```

A useful next implementation is:

1. Start from the official-style P/K values.
2. Sweep K multipliers, initially something wider such as:

```text
0.75
1.00
1.25
1.50
2.00
3.00
4.00
```

3. For each candidate nonlinear operating point, render all input levels.
4. Solve **one shared B512** across those levels.
5. Score:
   - relative RMS dynamics error;
   - maximum level-response error;
   - spectral error;
   - held-out real-DI error.
6. Only accept the candidate if dynamics improve without an unacceptable tonal regression.
7. If K-only optimisation proves useful, optionally unlock Pp/Pn/Kp/Kn and/or A input gain in a later stage.

This is a materially different optimisation objective from the original Valeton fitter.

---

# 13. Why improvements are more likely on high-gain models

**Yes: we should expect the largest improvements on high-gain models, not clean models.**

The reason is structural.

## Clean amps

For a clean amp, most of its behaviour is approximately linear:

```text
input level +6 dB
        -> output level roughly +6 dB
```

The A and B FIRs can reproduce linear tone shaping very effectively, and the nonlinear P/K stage is not being pushed particularly hard.

Our Fender Super Reverb result is already within roughly 0.2 dB RMS on the level-response test.

There is very little remaining error for a more sophisticated nonlinear optimiser to remove.

## High-gain amps

A high-gain amp behaves very differently:

```text
input +6 dB
    does NOT necessarily mean
output +6 dB
```

As the amp approaches saturation, increasing the input produces progressively smaller increases in output.

The exact shape of that compression curve is dominated by the nonlinear model's operating point.

If K or the signal level entering the P/K stage is slightly wrong, the converted model may still have the right overall EQ while compressing at the wrong input level.

That is exactly what the measurements show.

For example:

```text
Bogner Uberschall MKII

Full A2 level sweep : 1.3 dB
Current GP50        : 9.5 dB
```

The GP-50 model is much less saturated across the test range than the original A2 model.

That leaves substantial room for dynamics-aware optimisation.

Therefore the expected pattern is:

```text
Clean            -> tiny gains / possibly no measurable improvement
Edge of breakup  -> small gains
Crunch           -> small to moderate gains
High gain        -> potentially substantial gains
Extreme gain     -> largest potential gains
```

This also gives us a useful sanity check. If a new algorithm claims enormous improvements on already-near-perfect clean captures but little improvement on the high-gain dynamics failures, we should be suspicious of the metric rather than immediately trusting the result.

---

# 14. Definitive benchmark protocol

The next decisive experiment is a direct three-way comparison.

For every selected **Full A2 NAM**:

```text
                     SAME DRY DI
                         |
          +--------------+--------------+
          |              |              |
          v              v              v
      Full A2 NAM    Valeton official   Our latest
       NAMCore          SnapTone         GP50 CLO
          |              |              |
          v              v              v
    Reference audio   GP50 renderer    GP50 renderer
          |              |              |
          +--------------+--------------+
                         |
                      compare
```

## 14.1 Model selection

Use approximately 6-8 Full A2 models covering the whole gain range.

Recommended categories:

- 1-2 clean;
- 1-2 edge-of-breakup / crunch;
- 1-2 high gain;
- 2 extreme/high-compression captures.

Particularly useful high-gain candidates are models similar to the ones where we have already measured large dynamics errors, e.g. Archon, Meshuggah and Uberschall-style captures.

## 14.2 Produce the three models

For each original A2 NAM:

### A. Ground truth

Run the dry DI through the **Full A2 NAM** using NAMCore.

### B. Official Valeton conversion

Convert the exact same A2 NAM using Valeton's SnapTone conversion.

Capture the transfer to the GP-50 and recover the official 2696-byte VTSI/SnapTone model.

### C. Our conversion

Convert the exact same original A2 NAM using the latest version of our GP-50 converter.

Keep the final 2696-byte CLO/VTSI model.

---

# 15. Render all comparisons through the same GP-50 renderer

For a fair software comparison:

```text
Official Valeton SnapTone -> our GP50 software renderer
Our CLO                   -> same GP50 software renderer
```

The Full A2 NAM remains the reference output.

This removes differences caused by capture hardware, analogue output stages, interfaces, cables, gain staging, etc.

A one-time validation against the physical GP-50 is still useful to make sure the software renderer matches the pedal closely enough, but the automated benchmark should use one consistent renderer for both compact models.

---

# 16. Test material

Use two distinct classes of DI input.

## A. Controlled dynamics test

Take the exact same DI and render it at:

```text
-24 dB
-18 dB
-12 dB
 -6 dB
  0 dB
 +6 dB
```

This directly measures the input/output compression curve.

## B. Held-out real playing

Use one or more guitar performances that were **not used by the fitting/training process**.

This prevents the converter from simply becoming better at reproducing the fitting stimulus while becoming worse on real guitar material.

Ideally include:

- single notes;
- chords;
- palm-muted transients;
- sustained notes;
- low notes;
- high notes;
- dynamic playing from soft to hard.

---

# 17. Metrics to record

Do **not** reduce everything to one "quality score" initially.

Keep tone and dynamics separate.

## Spectral / tonal metrics

- spectral magnitude loss;
- frequency-response deviation;
- aligned waveform RMS error;
- correlation;
- output gain error;
- peak error.

## Dynamics metrics

For the six-level test:

- relative output level at every input level;
- total output-level sweep;
- RMS relative dynamics error;
- maximum relative dynamics error;
- optionally compression-curve slope error between adjacent levels.

## Generalisation metrics

Run the same spectral/waveform comparison on held-out real guitar DI.

---

# 18. Suggested result table

For each model, record Valeton and our error against the Full A2 reference.

Example:

| NAM | Category | Metric | Valeton error | Our error | Winner |
|---|---|---|---:|---:|---|
| Model A | Clean | Spectral loss | ... | ... | ... |
| Model A | Clean | Dynamics RMS | ... | ... | ... |
| Model B | Crunch | Spectral loss | ... | ... | ... |
| Model B | Crunch | Dynamics RMS | ... | ... | ... |
| Model C | High gain | Spectral loss | ... | ... | ... |
| Model C | High gain | Dynamics RMS | ... | ... | ... |
| Model D | Extreme | Max dynamics error | ... | ... | ... |

Also report per-category averages:

```text
Clean
Crunch
High gain
Extreme gain
All models
```

---

# 19. What counts as a real improvement?

A change should not be called better merely because it lowers one training loss.

A useful acceptance rule is:

### Strong improvement

- meaningfully lower held-out spectral error;
- and/or at least ~50% lower dynamics error on models with a known dynamics problem;
- with no material regression in the other metric.

### Neutral

- tiny differences within measurement noise;
- especially on clean models that are already very close.

### Regression

- better fitting-stimulus score but worse held-out guitar performance;
- substantially better tone but significantly worse dynamics;
- substantially better dynamics but clearly worse tonal accuracy.

The goal is not to win every individual metric on every NAM. The goal is for our conversion to be **consistently as good as or better than Valeton's on real A2 models, with clear gains where the official-style fitter currently struggles**.

---

# 20. The key question this benchmark answers

We are deliberately trying to distinguish between two possibilities.

## Possibility A - we are overcomplicating the same answer

```text
Valeton official ~= ours
```

If the two converters consistently produce effectively identical accuracy against Full A2, then our extra optimisation is providing little practical benefit.

In that case simplicity may be preferable.

## Possibility B - our additions genuinely improve use of the GP-50 model

```text
our error < Valeton error
```

particularly on held-out high-gain material and multi-level dynamics tests.

That would demonstrate that we are not merely reproducing Valeton's fitter differently. We are obtaining a **better approximation of the original A2 NAM within the same fixed GP-50 DSP architecture**.

---

# 21. Current working hypothesis

Based on everything measured and reverse engineered so far:

> **Large improvements are much more likely on high-gain and strongly compressed models than on clean models.**

Clean models are already close to the representational ceiling of the existing fitting process because most of their behaviour can be represented by the linear FIR stages.

High-gain models expose the weakest part of the approximation: the operating point and shape of the compact nonlinear stage.

Therefore the most valuable signal in the next benchmark is not whether we gain another tiny fraction on a Fender clean model. It is whether we materially reduce the compression/dynamics error on difficult high-gain A2 captures while preserving their tone.

---

# 22. Decisions already made: avenues we should not casually reopen

One purpose of this document is to stop us repeatedly revisiting ideas that have already been tested, disproved, or made unnecessary by stronger evidence.

> **Do not reopen one of these avenues unless new evidence directly contradicts the conclusion, or the definitive Valeton-vs-ours benchmark exposes a specific failure that the avenue could plausibly explain.**

The goal is the best 2696-byte CLO, not the most elaborate conversion pipeline.

## 22.1 CLOSED: a larger hidden NAM-style model inside the GP-50

**Why we considered it:** before the compact format was understood, the 2696-byte object could have been only a wrapper for some larger hidden representation.

**Why we stopped:** factory firmware contains records whose final 2560 bytes decode cleanly as exactly 640 float32 values; official Valeton transfers produce the same A128/B512 structure; and the Suite fitter itself exposes the matching A/B/P/K processing path.

**Conclusion:** the useful runtime model really is:

```text
Pre -> A128 -> P/K nonlinearity -> Post -> B512
```

There is no evidence of a large hidden neural model accompanying it.

**Reopen only if:** a future firmware or official transfer contains additional DSP data that cannot be explained as wrapper/metadata.

---

## 22.2 CLOSED: preserving/transplanting a GP-200-derived A seed

**Why we considered it:** the direct GP-50 path initially inherited some GP-200-style fitting state, so the A seed might have been influencing the result.

**What we tested:** an independent Candidate C path started from a neutral GP-50 state rather than inheriting that seed.

**Result:** the compact fits were often byte-identical or effectively identical.

**Conclusion:** the optimiser converges to essentially the same answer. The inherited GP-200 A seed is not a meaningful quality lever.

**Reopen only if:** future tests show systematic dependence on initial A state or clearly different local minima.

---

## 22.3 CLOSED: per-model Post-biquad optimisation as a major quality route

**Why we considered it:** the CLO exposes a Post biquad, so perhaps it could recover spectral detail that A/B could not.

**What we found:** gains were small or inconsistent and sometimes negative. Reverse engineering also indicates Valeton treats Post as a fixed sample-rate-derived filter rather than a per-model learned stage.

**Conclusion:** extra Post optimisation adds complexity without consistent benefit.

**Reopen only if:** official-vs-ours comparisons show a repeatable frequency-shaped error that B512 cannot correct and official Post coefficients genuinely differ.

---

## 22.4 DEFERRED: rewriting the whole fitter around 44.1 kHz

**Why we considered it:** the final compact representation is tied to 44.1-kHz processing/serialization.

**Why we stopped:** the initial experiment was confounded and did not demonstrate a meaningful quality penalty from the current rate path. Valeton itself also contains explicit sample-rate conversion stages.

**Conclusion:** a 44.1-kHz rewrite is a large architectural change with no demonstrated payoff.

**Reopen only if:** a controlled A/B test isolates resampling as a repeatable source of held-out error.

---

## 22.5 CLOSED: using B512/tone matching to fix nonlinear dynamics

**Why it was tempting:** the direct B512 solver produced very large spectral improvements.

**Why it cannot solve this problem:** B512 is a fixed linear filter. The high-gain failure we measured changes with input level. A linear filter cannot repair the wrong compression curve.

**Conclusion:**

```text
B512 -> tone / linear residual correction
P/K and nonlinear operating point -> dynamics / saturation
```

Do not keep adding B tricks to solve a nonlinear error.

---

## 22.6 ACTIVE, but with restraint: improving P/K beyond the official estimate

The official P/K method is now substantially understood and reproduced:

- 100 ms extrema windows;
- first ~5 seconds;
- P from branch extrema;
- K seed from small-signal slope;
- only a 0.80-1.20 multiplier search;
- P/K frozen before A/B fitting.

That makes it an excellent **baseline**, but the high-gain multi-level tests show it is not always the best possible representation of the A2 compression curve.

So dynamics-aware P/K work remains justified.

However:

> **Do not hard-code a rule such as `K >= 77 => special high-gain treatment`.**

Our current dataset has a large gap between moderate and very high K values. Optimisation must be triggered by measured dynamics error, not an arbitrary K threshold.

---

## 22.7 CLOSED: treating a single nominal-level spectral score as “quality”

A model can have good spectral agreement at the fitting level while still having a badly wrong input/output compression curve.

That is exactly what the high-gain measurements exposed.

**Permanent rule:** no future change is accepted solely because it lowers fitting spectral loss.

At minimum, assess:

- held-out spectral accuracy;
- multi-level dynamics;
- maximum relative level error;
- real guitar material.

---

## 22.8 CLOSED for now: one overall “quality percentage”

A single score could hide:

- great tone with bad compression;
- great dynamics with obvious tonal regression;
- a metric weighting that simply rewards whichever number is easiest to improve.

**Conclusion:** keep tone and dynamics visible separately. A combined score can be added later for presentation, but not used as the sole decision metric.

---

## 22.9 CLOSED: judging changes on the fitting stimulus alone

An optimiser can become excellent at reproducing the exact conversion stimulus while getting worse on real playing.

Our direct-B work only became convincing once the improvement survived held-out tests.

**Permanent rule:** training loss is diagnostic; held-out performance decides whether an algorithm is genuinely better.

---

## 22.10 CLOSED as the preferred path: “fit B2048 then truncate to B512”

This was a reasonable first approach, but the direct device-budget B512 solve produced very large held-out improvements.

**Conclusion:** B512 should normally be optimised at the actual hardware tap budget.

The B2048/truncate result may remain as a comparison/fallback candidate, but should not be assumed superior.

---

## 22.11 CLOSED: adding more free parameters just because they exist

Potential parameters include Pre, Post, Pp/Pn, Kp/Kn, A gain and extra spectral shaping.

Every additional degree of freedom creates more risk of:

- overfitting;
- unstable optimisation;
- parameter-identifiability problems;
- better training metrics but worse real-world results.

In particular, A gain and K interact because changing the level entering the nonlinear stage also changes where saturation occurs.

**Preferred order of attack:**

```text
official-style baseline
        ↓
direct B512
        ↓
dynamics-aware K / operating point
        ↓
only then consider P and/or A gain
```

Do not jointly optimise everything unless simpler experiments prove it necessary.

---

## 22.12 CLOSED: assuming more complexity means a better converter

This is the overarching engineering rule.

For any proposed new stage, ask:

```text
Does it reduce error against Full A2
on material that was not used to fit it?
```

and, for nonlinear work:

```text
Does it improve dynamics
without materially damaging tone?
```

If not, remove it or leave it experimental.

---

# 23. Deferred tooling questions

These are still useful, but they are not blockers for conversion quality.

## Direct SnapTone readback from the GP-50

We have proven slot/name reads, official upload transfer and extraction of a SnapTone during upload.

We have **not yet proven** a command that exports an already-stored 2696-byte SnapTone from the pedal.

Selector `0x1C` remains a promising lead.

This does not block next week's benchmark because official converted models can already be recovered from upload captures.

## Exact remaining wrapper/header semantics

The DSP payload, sizes and CRC are strongly validated. A few reserved/header bytes remain unexplained.

Unless those bytes are shown to affect DSP behaviour or compatibility, they are not a conversion-quality priority.

## One-time physical GP-50 renderer validation

The software renderer is the right tool for automated Valeton-vs-ours comparison because both compact models are processed identically.

A one-time physical-pedal comparison is still worthwhile to validate the renderer, but it should not replace automated testing.

---

# 24. Stop/reopen rule

Use three states for future ideas:

### CLOSED

Evidence says the avenue is not materially useful.

Do not revisit without contradictory evidence.

### DEFERRED

Potentially useful, but not currently a bottleneck.

Revisit only when a benchmark result gives us a reason.

### ACTIVE

Supported by measurements and capable of addressing a known failure.

At present the main ACTIVE quality avenue is:

> **Dynamics-aware nonlinear optimisation for difficult high-gain A2 models, while retaining the direct B512 tonal improvement and protecting held-out accuracy.**


# 25. Next test checklist

- [ ] Select 6-8 known Full A2 NAMs covering clean -> extreme gain.
- [ ] Keep exact original NAM files and hashes/names.
- [ ] Convert each with our latest converter.
- [ ] Convert each with Valeton SnapTone.
- [ ] Capture/recover each official Valeton 2696-byte model.
- [ ] Verify parsed A128/B512/Pp/Pn/Kp/Kn for both converted models.
- [ ] Render same held-out dry DI through Full A2, Valeton and ours.
- [ ] Render the six-level dynamics series.
- [ ] Calculate spectral metrics.
- [ ] Calculate waveform/alignment metrics.
- [ ] Calculate dynamics curve metrics.
- [ ] Record results per model and per gain category.
- [ ] Inspect parameter differences where Valeton wins.
- [ ] Inspect parameter differences where ours wins.
- [ ] Only promote new fitting stages to production when they improve held-out results, not just training loss.

---

## Bottom line

Valeton and our converter target the **same 2696-byte GP-50 SnapTone architecture**.

Our baseline implementation now appears to reproduce most of Valeton's fundamental fitting strategy closely enough that the additional work can be evaluated as genuine algorithmic improvements.

The strongest current differences are:

1. direct optimisation at the final B512 device budget;
2. direct regularised B512 solving;
3. explicit multi-level compression measurement;
4. dynamics-aware nonlinear optimisation;
5. held-out real-DI validation.

The next official-A2 benchmark will determine whether those additions produce a measurably better compact model, and—based on the evidence so far—the largest gains should be expected on **high-gain / highly compressed captures rather than already-linear clean amps**.

---

# 23. Decision log: avenues we should NOT keep reopening

This section exists specifically to prevent future development from circling back to ideas that have already been tested, reverse engineered, or shown to be poor uses of time.

There are two different statuses:

- **CLOSED** — evidence is strong enough that we should treat the conclusion as settled unless genuinely contradictory new evidence appears.
- **DEFERRED** — not proven impossible, but current evidence does not justify spending more time on it. Reopen only if a specific trigger appears.

The purpose of this section is not to forbid experimentation. It is to make sure any reopened avenue has a concrete reason rather than simply sounding plausible again several weeks later.

## 23.1 Do not invent additional hidden GP-50 DSP stages

**Status: CLOSED**

We previously had uncertainty about whether the compact CLO might hide substantial additional processing beyond the structure we had identified.

Current evidence now converges on the same runtime model from multiple independent directions:

```text
Pre biquad
   -> A128 FIR
   -> 4x oversampled asymmetric P/K exponential nonlinearity
   -> Post biquad
   -> B512 FIR
```

Evidence includes:

- the 2696-byte official SnapTone object captured from Valeton's own transfer;
- the `VTSI` field layout and A128/B512 counts;
- factory GP-50 firmware containing exactly 640 trailing float coefficients = 128 + 512;
- Valeton Suite's proprietary fitter using the same FIR/nonlinear topology;
- explicit 4x oversampling and exponential nonlinear processing in `startClone()`.

There is simply no plausible space in the compact runtime object for another large neural network or major hidden convolution stage.

The reserved/unknown header bytes around `0x0A..0x13` may still have minor semantics, but they should **not** be treated as evidence for missing DSP.

**Only reopen if:**

- a real GP-50 hardware/software comparison reveals repeatable behaviour that our confirmed topology cannot reproduce; or
- an official model contains additional parameter data that cannot be explained by the known structure.

---

## 23.2 Do not redesign the runtime format specifically for A2

**Status: CLOSED**

A2 changes the complexity of the original NAM being approximated. It does **not** create a different GP-50 runtime architecture.

The official process is:

```text
A1 or A2 NAM
     -> run NAM model
     -> create target/reference audio
     -> fit compact SnapTone model
     -> same GP-50 runtime architecture
```

Therefore Full A2 should be treated as a **better/more difficult source model**, not as something requiring an "A2 CLO format".

Our job is to fit A2 behaviour more accurately into the existing A128/P-K/B512 budget.

**Only reopen if:** Valeton produces an official GP-50 SnapTone from A2 that has a genuinely different VTSI structure or runtime parameter layout.

---

## 23.3 Do not keep chasing GP-200 seed contamination in the GP-50 fit

**Status: CLOSED**

We investigated whether the GP-50 result was being harmed because its direct fit inherited A/P-K or other state from the GP-200-style path.

An independent GP-50 candidate was created with neutral A/B state at the GP-50 budget rather than relying on the GP-200-derived seed.

In testing, the independent candidate and the existing direct GP-50 fit frequently converged to effectively the same result, in some cases byte-identically.

Conclusion:

> The GP-200-derived starting state was not the material cause of the remaining GP-50 error.

The fitter is sufficiently convergent that spending more time on "cleaner" initial A/B seeds is unlikely to solve the real fidelity problem.

**Only reopen if:** a future model exhibits strong seed dependence — i.e. materially different held-out results from different valid initial states.

---

## 23.4 Do not expect Pre/Post biquad optimisation to deliver the main fidelity gain

**Status: CLOSED for general optimisation**

We tested optimisation of the Post biquad.

Observed gains were small and inconsistent:

- one Fender Full case improved by roughly 4%;
- other captures showed negligible improvement or regression.

Reverse engineering subsequently strengthened the reason for this result:

- Valeton's normal Pre is effectively neutral;
- the Post filter is largely a fixed sample-rate-derived stage;
- `iterAmpCoeff()` does not train custom Pre/Post biquads per NAM.

Therefore continuously adding more freedom to Pre/Post is unlikely to address the dominant modelling limitation.

It also risks letting a linear filter reduce a chosen spectral metric while obscuring the actual nonlinear mismatch.

**Only reopen if:** a broad held-out benchmark shows a repeatable frequency-shaped residual that cannot be removed by A/B at the available tap budget.

---

## 23.5 Do not attempt to fix level-dependent dynamics with B512 alone

**Status: CLOSED**

B512 is a linear time-invariant filter.

It can correct:

- frequency response;
- phase/time-domain response within its tap budget;
- overall linear gain/shape.

It cannot make the model respond differently at -24 dB than at 0 dB in the way required to repair a wrong compression curve.

Our measured high-gain error is explicitly **level dependent**. For example, the converted high-gain models often increase output too much as the input is raised, meaning they saturate too late relative to Full A2.

Therefore:

> Use B512 for tone/linear residuals. Use the nonlinear operating point — P/K and possibly gain into A/nonlinearity — for dynamics.

Do not keep adding increasingly sophisticated B-only corrections and expect them to solve compression behaviour.

**Only reopen if:** the measured error is shown to be level independent after proper gain normalisation.

---

## 23.6 Do not return to "fit B2048 then simply truncate to B512" as the preferred GP-50 method

**Status: CLOSED as the default approach**

The old route was effectively:

```text
fit long B2048
     -> convert/truncate
     -> B512
```

We tested solving B directly at the actual GP-50 device budget instead.

The direct regularised B512 solve produced very large improvements on held-out material — approximately 54-91% loss reduction across the tests performed.

Therefore the device-sized solve is not merely an implementation convenience. It makes better use of the coefficients the hardware actually has.

The old/truncated result may remain as a **candidate/fallback for comparison**, because there is little harm in retaining it when the converter automatically scores both and keeps the better result. It should not become the preferred path again without benchmark evidence.

**Only reopen as preferred method if:** a broad benchmark shows the truncated 2048 solution consistently beating the direct B512 solve on held-out material.

---

## 23.7 Do not trust an in-sample P/K improvement on the old spectral metric

**Status: CLOSED as a selection method**

We tried a bounded coordinate-descent P/K optimiser.

It could improve the optimisation/training score, but those gains did not survive held-out selection across the broader corpus.

We subsequently discovered why this is plausible: the manufacturer-style objective is dominated by spectral magnitude accuracy and can be largely blind to the level-response/compression mismatch that matters on high-gain captures.

Therefore:

> A P/K change is not an improvement merely because the original fitting loss goes down.

Any future nonlinear optimiser must be judged using explicit dynamics measurements plus held-out tonal performance.

**Only reopen the old P/K optimiser if:** its objective is changed to include the multi-level dynamics criteria and it passes held-out testing.

---

## 23.8 Do not hard-code a `Kp` threshold for "high gain"

**Status: CLOSED for now / insufficient evidence**

Our measurements showed a strong association between very large K values and the worst dynamics mismatches, but the dataset currently has a substantial gap between moderate examples and the extreme high-gain group.

For example, we observed values around:

```text
~14       moderate/high gain example
~77-85    extreme high-gain examples
```

That is not enough evidence to define a production rule such as:

```text
if Kp >= 77 -> enable special dynamics mode
```

Doing so would overfit the small dataset and could create a discontinuity in converter behaviour.

Prefer measuring the actual A2 level-response/compression error directly. The error itself should decide whether additional nonlinear optimisation is needed.

**Only reopen a K-based classifier if:** a much larger benchmark demonstrates a stable threshold or continuous relationship with out-of-sample predictive value.

---

## 23.9 Do not rewrite the whole conversion around 44.1 kHz yet

**Status: DEFERRED**

We investigated whether sample-rate conversion or the 44.1-kHz path was responsible for meaningful fidelity loss.

The measurement was confounded/inconclusive and did not establish a meaningful resampling penalty large enough to justify rearchitecting the converter.

Valeton's own implementation also contains deliberate sample-rate conversion and sample-rate-specific handling, so the mere presence of resampling is not evidence of a defect.

A large sample-rate rewrite would touch many already-validated stages and create substantial regression risk.

**Only reopen if:** a controlled A/B benchmark isolates sample rate as a measurable source of held-out error, using otherwise-identical processing and alignment.

---

## 23.10 Do not spend more time unpacking factory firmware record headers just for conversion quality

**Status: DEFERRED**

The factory firmware records were useful because their final 2560 bytes independently confirmed the 128 + 512 coefficient structure and yielded plausible production P/K values.

The variable record headers are interesting, but decoding every byte of that packed/internal firmware representation is not currently required to improve NAM -> SnapTone conversion.

We now have something better for format validation: a real official 2696-byte VTSI object captured from Valeton's own conversion transfer.

Therefore firmware-header archaeology is no longer on the critical path.

**Only reopen if:** those fields become necessary for direct device export/import, factory model replacement, or explaining a hardware behaviour absent from standalone VTSI models.

---

## 23.11 Do not assume SnapTone slot metadata is the SnapTone DSP model

**Status: CLOSED**

Several device protocol reads return names, slot records, preset bodies or catalogue information. These should not be confused with the actual 2696-byte SnapTone DSP object.

Known examples include catalogue/name and preset/body responses.

A successful model export/readback must ultimately yield either:

- the 2696-byte `VTSI` object; or
- a wrapper containing that object, analogous to the captured 2770-byte upload transfer.

Small metadata responses are not evidence that we have exported the model.

**Only reopen a protocol response as a model candidate if:** its reconstructed payload contains the expected VTSI structure or a clearly identifiable packed equivalent.

---

## 23.12 Do not claim direct GP-50 SnapTone readback/export until it is observed

**Status: DEFERRED / NOT YET PROVEN**

We have proven:

- list/name reads;
- preset-related reads;
- official SnapTone upload/write;
- extraction of the official 2696-byte model while it is being uploaded.

We have **not yet proven** that an already-stored SnapTone can be requested back from the GP-50 as a full model.

The `0x1C` selector remains an interesting lead because another reverse-engineering project labels it "snaptone", but that label alone is not proof of model readback.

Do not build application behaviour around the assumption that readback exists until captured traffic proves it.

**Only reopen as an implementation task if:** a safe capture shows a device -> host response containing a model-sized SnapTone payload, or decompilation identifies an explicit read-model command and response format.

---

## 23.13 Do not use a single aggregate "quality score" while developing the fitter

**Status: CLOSED as a development methodology**

A single number can conceal exactly the failure we found:

- good spectral/tone score;
- wrong compression/level-response behaviour.

During development, keep at least these dimensions separate:

```text
Tonal / spectral fidelity
Dynamics / compression fidelity
Held-out real-DI fidelity
```

A combined score may eventually be useful for presentation, but only after the individual metrics are retained and inspected.

**Only introduce a combined score if:** it remains accompanied by the component metrics and cannot hide a significant regression in either tone or dynamics.

---

## 23.14 Do not optimise only against the conversion stimulus

**Status: CLOSED as a validation method**

A fitter can become better at reproducing the exact material used to derive its parameters without becoming a better amp model.

We have already seen optimisation changes that looked positive in-sample but failed held-out selection.

Every meaningful converter change must therefore be tested on audio that was **not used to fit the model**.

The official conversion stimulus remains useful for reconstructing/initialising the model, but it is not sufficient evidence of improvement.

**Only accept a production change if:** the gain survives held-out guitar material and, where relevant, the independent level-response test.

---

## 23.15 Do not assume "more complex" means "better"

**Status: PERMANENT DESIGN RULE**

This is the central reason for the three-way benchmark.

Every extra optimiser, candidate, parameter search or correction stage adds:

- execution time;
- code complexity;
- maintenance cost;
- opportunities for overfitting;
- opportunities for regressions.

Therefore a more complex path should only remain in production when it demonstrates a repeatable improvement against the Full A2 reference on held-out material.

If:

```text
Our result ~= Valeton result
```

then reproducing the simpler official-style solution may be preferable.

If:

```text
Our result < Valeton error
```

particularly on difficult high-gain captures without degrading clean/crunch models, then the extra complexity is justified.

---

# 24. Current development priorities after closing those avenues

Based on the evidence above, the remaining work should stay tightly focused.

## Priority 1 - definitive A2 manufacturer benchmark

Take the exact same Full A2 NAM and compare:

```text
Full A2 reference
       vs
Valeton official SnapTone
       vs
our latest GP-50 CLO
```

This tells us whether our added complexity produces a genuinely better approximation than Valeton's own converter.

## Priority 2 - dynamics-aware nonlinear fitting

Focus on models where the measured compression curve is wrong, especially high-gain captures.

Do not apply special processing simply because an amp is labelled "high gain". Trigger investigation from measured dynamics error.

## Priority 3 - shared B512 solve while nonlinear candidates are compared

For each nonlinear candidate, solve one B512 that must serve all tested levels rather than allowing a separate linear correction to conceal each level independently.

This keeps the comparison physically meaningful: one SnapTone model must reproduce all playing levels.

## Priority 4 - held-out validation

No production promotion based only on the fitting stimulus.

## Priority 5 - simplicity after the benchmark

Once the official-vs-ours benchmark is complete, remove stages that add complexity but do not materially improve results.

The final converter should be the **simplest pipeline that consistently wins or matches the official conversion against Full A2**.

---

# 25. Quick "do not restart this" checklist

Before starting a new experiment, check whether it is one of these:

- [ ] Looking for a large hidden DSP stage in the 2696-byte CLO? **Stop — topology is effectively confirmed.**
- [ ] Designing a separate A2 runtime format? **Stop — A2 is the source/reference, not a different GP-50 runtime.**
- [ ] Reworking GP-200 seeds again? **Stop unless seed-dependence is actually measured.**
- [ ] Adding broad Pre/Post optimisation? **Stop unless a repeatable residual specifically points there.**
- [ ] Trying to fix compression with B512 alone? **Stop — B is linear.**
- [ ] Returning to B2048 -> truncate as the main GP-50 solution? **Stop unless benchmarks overturn the direct-B512 result.**
- [ ] Trusting P/K because training spectral loss improved? **Stop — check dynamics + held-out audio.**
- [ ] Hard-coding `Kp >= X` as the high-gain rule? **Stop — dataset is not sufficient.**
- [ ] Rewriting everything around 44.1 kHz? **Stop until an isolated resampling penalty is demonstrated.**
- [ ] Reverse engineering firmware headers for conversion fidelity? **Stop — not currently on the critical path.**
- [ ] Treating a catalogue/preset response as exported SnapTone? **Stop unless it contains the real VTSI/model payload.**
- [ ] Claiming stored SnapTone readback works? **Stop until an actual readback is captured.**
- [ ] Reducing the benchmark to one score? **Stop — keep tone and dynamics visible separately.**
- [ ] Accepting a change because the fitting stimulus improved? **Stop — held-out validation is mandatory.**

The default question before adding another stage should be:

> **What measured failure does this stage solve, and what benchmark will prove that it solved it without making something else worse?**

If those two questions do not have concrete answers, do not add the stage yet.
