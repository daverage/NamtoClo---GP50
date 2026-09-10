# EngineV2 research north star

This document exists to prevent the clean-sheet NAM -> GP50 research from drifting into incremental patching of the existing converter, overfitting individual NAMs, or turning exploratory ideas into architecture without evidence.

It describes the original purpose of EngineV2, why the research corpora and teacher dataset were created, what "teacher/student" means in this project, what counts as progress, and what should trigger a reset.

## The original goal

The goal is **not** to recreate Valeton's undocumented NAM-to-CLO conversion algorithm more accurately.

The goal is **not** to reproduce the original NamToClo software internally and then optimise a few constants around it.

The goal is **not** to make a small set of hand-picked NAMs score well.

The goal is:

> Given an arbitrary Neural Amp Modeler model, produce the best GP50/GP5 CLO that the known GP50 DSP structure can physically represent, using a clean-sheet teacher/student distillation approach rather than inheriting the original converter's fitting philosophy.

The source NAM is the behavioural authority.

The GP50 CLO structure is a constrained student architecture.

EngineV2 exists to discover the best general method for projecting one into the other.

## What "clean-sheet" means here

We know the GP50 runtime structure we must ultimately serialize:

```text
PRE -> A128 -> oversampled asymmetric P/K nonlinearity -> POST -> B512
```

That structure is a **hardware constraint**, not a prescribed fitting algorithm.

EngineV2 is free to determine A, P/K and B in any evidence-based way that produces a better behavioural approximation of the NAM.

We should therefore avoid reasoning such as:

> "The old converter estimated P/K this way, so EngineV2 should improve that estimate."

The better question is:

> "Given the GP50's available degrees of freedom, what fitting strategy most faithfully reproduces the NAM across relevant inputs?"

The existing production converter is a baseline to beat, not a design template.

## Why we created the audio research corpus

A single stimulus cannot tell us whether a converter is genuinely good.

A converter can fit one waveform extremely closely while still failing on:

- different playing levels;
- different pickups or guitars;
- clean-up when the guitar volume is reduced;
- transient attack;
- compression;
- harmonic balance;
- low-end retention;
- high-frequency fizz;
- different styles of playing;
- high-gain versus clean models.

The research corpus was therefore deliberately built from several kinds of source material.

### Real guitar DI material

Real DI performances tell us whether a candidate behaves like an amp when used by an actual player.

They are important for perceptual and behavioural validation, but they are not sufficient by themselves for system identification because the excitation is uncontrolled.

### Controlled guitar samples

Controlled notes, pickups, velocities and instruments provide repeatable examples that expose differences which may be hidden in one performance.

### Synthetic system-identification probes

Synthetic probes exist to reveal specific properties of the NAM and the GP50 student architecture.

Examples include:

- impulses;
- polarity pulses;
- low-level log sweeps;
- multisine ladders;
- smooth drive ramps;
- 1 kHz level ladders;
- frequency x level matrices;
- transient/pick-like probes.

These are measurement tools, not listening references.

They should help us understand **why** a conversion succeeds or fails, but a synthetic metric must never override real audio evidence simply because it is easier to optimise.

### Independent held-out human material

The corpus deliberately includes material that is not used to tune the algorithm.

This exists to answer the most important research question:

> Does the method generalise to playing it has never seen?

If performance improves only on fit or selection material, we have not improved the converter.

## Why we created the NAM corpus

The NAM corpus exists to stop us from designing EngineV2 around one favourite amp or one type of distortion.

It deliberately includes controlled families and progressions such as:

- near-linear solid-state clean amps;
- tube clean -> edge-of-breakup -> crunch progressions;
- classic crunch gain progressions;
- modern high-gain amps;
- pedals and strongly nonlinear models;
- calibrated and uncalibrated captures;
- development, selection and sealed model groups.

The purpose of same-amp gain progressions is especially important.

If one algorithm works on JCM800 G3 but fails on G7/G10, or works on one clean setting but fails as the same amp becomes nonlinear, that tells us something about the fitting method rather than merely about the amplifier identity.

The sealed models exist so that, after we believe the algorithm is finished, we can test whether it generalises to NAMs that did not influence its design.

## Why we created the teacher dataset

The teacher dataset is the central idea behind EngineV2.

For each chosen source input, the authoritative NAM renderer produces the corresponding NAM output:

```text
input audio -> NAMCore renderer -> NAM target audio
```

That pair is a teacher example.

The GP50 renderer can then process the same input:

```text
same input audio -> candidate GP50 model -> CLO output
```

The research problem becomes a direct behavioural comparison:

```text
Teacher: input -> NAM -> target
Student: input -> GP50 structure -> prediction
```

We are therefore not trying to reverse-engineer the hidden internal parameters of the NAM.

We are asking the GP50 student to reproduce the NAM's observable input/output behaviour as closely as its much smaller fixed architecture allows.

This is model distillation in the behavioural sense.

## Why the teacher dataset is cached

NAM rendering is expensive compared with evaluating the small GP50 student model.

Caching authoritative teacher renders gives us several advantages:

- every experiment sees exactly the same NAM output;
- fit/selection/benchmark comparisons are reproducible;
- optimisation can run quickly without repeatedly invoking NAM inference;
- renderer version, model identity and source provenance are recorded;
- we can compare competing fitting philosophies fairly;
- the benchmark target cannot accidentally change between experiments.

The cache is infrastructure for experimentation. It is not itself the conversion algorithm.

## Fit, selection and benchmark are deliberately different

The split is intended to stop us fooling ourselves.

### Fit

Fit material is allowed to influence the coefficients.

### Selection

Selection material may choose between general strategies or candidate rounds, but it must not be repeatedly mined until one particular model looks good.

### Benchmark

Benchmark material is evaluation only.

It must not influence coefficients, objective weights, gain rules or algorithm choices during the same experiment.

Once we start changing the algorithm because of a benchmark result, that benchmark has effectively become selection material and must no longer be described as held out.

### Sealed NAMs

Sealed NAMs are the final generalisation check after the fitting philosophy is frozen.

They should remain untouched until we are ready to make a genuine quality claim.

## The student architecture should be used as a whole

A major principle of the clean-sheet approach is that the GP50 blocks need not correspond one-to-one with conceptual parts of an amplifier.

For example, A128 does not have to mean simply "pre-EQ".

Because A occurs before the nonlinear stage, it can also act as **frequency-dependent drive shaping**.

A may deliberately boost or cut particular frequency regions before P/K so the fixed GP50 waveshaper produces a closer approximation of frequency-dependent NAM distortion. B512 can then correct the resulting post-nonlinearity spectrum.

Therefore a valid clean-sheet solution may use the blocks cooperatively:

```text
A128
  shapes both spectrum and what reaches the nonlinear stage

P/K
  supplies the fixed nonlinear family available on the GP50

POST
  fixed hardware behaviour

B512
  corrects final linear response and output level
```

The objective is not to assign an intuitive amp role to every parameter.

The objective is to find the best overall projection into the available hardware structure.

## Output volume is post-nonlinearity

This is a hard project principle.

If a candidate is too quiet, increasing signal **before** P/K is not a legitimate volume correction because it also changes distortion.

Final loudness correction belongs after the nonlinear section, currently through the final B/output stage.

```text
WRONG for volume correction:
input -> more drive -> P/K -> more distortion

RIGHT:
input -> same A/P/K behaviour -> post-P/K linear output gain
```

Pre-nonlinearity gain is allowed only when it is intentionally part of the fitted nonlinear behaviour, never as a substitute for output volume.

## What we learned from NAM Mixer

NAM Mixer is **not part of EngineV2**.

EngineV2 must not require NAM Mixer, its manifests, its modes, or its generated NAMs.

NAM Mixer was useful as a research reference because building and retraining NAMs forced us to think explicitly about concepts that are easy to blur together:

- input calibration;
- training targets;
- teacher construction;
- drive behaviour;
- tone correction;
- post-processing;
- receptive field limitations;
- validation at different input levels;
- the distinction between changing input drive and changing output gain.

Those ideas helped inspire the teacher/student research design.

They do **not** imply that NamToClo should become part of NAM Mixer or that NAM Mixer should become part of NamToClo.

The intended product remains:

```text
arbitrary .nam
    -> standalone NamToClo EngineV2
    -> GP50/GP5 .clo
```

If knowledge gained from NAM training research improves the algorithm, that knowledge should be implemented independently inside NamToClo.

## What the current experiments have taught us

The experiments to date have produced useful evidence, even where the candidate algorithm failed.

### Direct GP50 fitting is worthwhile

The GP50 B512 domain should be treated directly rather than assuming a longer GP200 solution can simply be truncated or rescaled.

### Clean conversion is not enough

Very low ESR on a clean amp does not validate the nonlinear fitting method.

The JC-120 result proved that the serializer, renderer and broad linear fitting path can work extremely well.

It did not prove crunch or high-gain conversion.

### Guitar level response matters

The same guitar performance rendered through the NAM at multiple input levels is valuable because it exposes compression and clean-up behaviour directly.

However, matching the output-level curve alone is not sufficient.

A candidate can reproduce the compression curve by saturating too strongly and still sound wrong.

### Synthetic distortion metrics are diagnostics, not truth

The JCM800 experiments showed that metrics can improve on fit/selection material while held-out behaviour and listening remain worse.

This is a warning against adding one more penalty every time a particular model exposes a failure.

### Static P/K decomposition is a diagnostic, not the philosophy

The 1 kHz transfer experiment is useful for understanding the GP50 nonlinear family and for validating measurement tools.

It should not turn EngineV2 into "estimate the old-style P/K more accurately".

The clean-sheet goal remains whole-system behavioural approximation.

## The preferred research direction

The preferred direction is a **system-identification / variable-projection student fitter**.

A candidate should be evaluated against a deliberately designed set of NAM teacher renders that excite complementary behaviours.

Conceptually:

```text
                    authoritative NAM teacher
                             |
        +--------------------+--------------------+
        |                    |                    |
   low-level probes     level / nonlinear     transient / real DI
        |                    |                    |
        +--------------------+--------------------+
                             |
                             v
                 candidate GP50 student
              A128 -> P/K -> POST -> B512
                             |
                             v
                     direct comparison
```

The important difference from the recent G5 tuning loop is that we should not invent a growing list of amp-specific penalties.

Instead, the identification set should contain enough information that ordinary teacher-vs-student prediction error rewards the correct behaviour across operating conditions.

Where mathematically useful, B512 should continue to be solved analytically for each proposed upstream candidate rather than treated as hundreds of independent outer-search parameters.

A and P/K may need to be searched jointly because pre-nonlinearity spectral shaping changes the behaviour of the nonlinear block.

Real guitar should then be used as validation of the resulting general method, not as a signal that causes one more model-specific objective term to be added.

## What counts as a real improvement

A new EngineV2 method is better only if it improves **generally**, not just numerically on one example.

Evidence should include all of the following:

1. **Clean fidelity**
   - waveform/tone similarity;
   - correct level;
   - hardware listening.

2. **Crunch behaviour**
   - full-level tone;
   - clean-up when input is reduced;
   - compression/attack;
   - no extra perceived distortion.

3. **High-gain behaviour**
   - low-end retention;
   - upper-mid/top-end balance;
   - fizz control;
   - harmonic balance;
   - correct saturation and compression.

4. **Generalisation**
   - different source performances;
   - different amp families;
   - same-amp gain progressions;
   - held-out benchmark material;
   - ultimately sealed NAMs.

5. **Real GP50 playback**
   - software metrics are necessary engineering tools;
   - hardware listening remains an acceptance gate.

6. **Comparison against the existing converter**
   - the production NamToClo result is the baseline;
   - EngineV2 needs to produce a material quality improvement, not merely a different result.

## Anti-drift rules

When deciding whether to change EngineV2, use these rules.

### Good reasons to change the algorithm

- the same failure appears across multiple NAM families;
- a controlled system-identification experiment reveals a structural weakness;
- fit, selection and independent benchmark evidence agree;
- the change corresponds to a real degree of freedom in the GP50 architecture;
- hardware listening confirms the improvement;
- the principle would make sense before seeing the name of the failing amp.

### Warning signs that we are drifting

Stop and reassess if we find ourselves doing any of these:

- repeatedly rerunning one NAM while changing weights;
- adding a metric because one specific amp sounds wrong;
- calling selection success a win when benchmark behaviour gets worse;
- moving volume into pre-P/K drive;
- tuning against benchmark material and continuing to call it held out;
- assuming lower ESR automatically means better playing behaviour;
- treating clean-amp success as proof of distorted-amp success;
- reconstructing the original converter merely because its parameter interpretation is familiar;
- treating NAM Mixer as a required stage or dependency;
- optimising synthetic probes at the expense of real guitar playback;
- inventing special cases for named amps, captures or creators.

A useful test is:

> Would we make this same algorithmic change if the failing NAM had an anonymous filename and we did not know what amp it represented?

If the answer is no, the change is probably overfitting.

## Experimental hierarchy

When investigating a failure, prefer this order:

```text
1. Verify renderer / serializer / DSP parity
2. Reproduce failure on more than one relevant model
3. Use controlled probes to identify the behaviour that differs
4. Determine whether the GP50 architecture can express the required behaviour
5. Change the general fitting strategy if justified
6. Validate on independent real guitar material
7. Listen on actual GP50 hardware
8. Only then expand to additional NAM families
```

Do not begin at step 5 by adding a penalty to make one result look better.

## Current status of EngineV2

EngineV2 remains research code.

Useful components already exist and should be retained unless evidence disproves them:

- reproducible research audio corpus;
- curated NAM corpus with development/selection/sealed intent;
- cached authoritative teacher renders;
- direct 44.1 kHz GP50 DSP model;
- exact compact GP50 CLO serializer;
- analytical B512 solving;
- fast candidate rendering;
- fit / selection / benchmark separation;
- level-response diagnostics;
- nonlinear/distortion diagnostics;
- hardware listening workflow;
- research-only transfer analyser.

The recent level-response and distortion-aware P/K objective experiments should be regarded as **research branches of thought**, not as proof that those penalties belong in the final fitting philosophy.

The next major work should return to the central question:

> What general system-identification and optimisation strategy best projects arbitrary NAM behaviour into the complete GP50 student architecture?

## Final north-star question

Before every substantial EngineV2 change, ask:

> Does this help us build a generally better standalone NAM -> GP50 distillation engine, or are we patching the current experiment so that one result looks more convincing?

If it is the latter, stop and redesign the experiment.
