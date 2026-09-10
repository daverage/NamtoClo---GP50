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

## The original experimental hypothesis

The central hypothesis behind EngineV2 was deliberately simple:

> If we expose the NAM teacher and the GP50 student to a small but highly informative set of controlled probes and real guitar performances, can we fit the complete GP50 architecture closely enough to beat the existing NamToClo converter on unseen guitar playing and real hardware?

The first prototype was never intended to require hours of random guitar or hundreds of arbitrary probes.

The intended starting point was a **compact identification corpus** containing enough complementary information to expose the important behaviours:

```text
controlled system-identification probes
+
small, varied real DI fitting corpus
+
disjoint real DI selection corpus
+
held-out real DI benchmark corpus
```

If that compact information set cannot produce a better general conversion, simply adding much more data is unlikely to fix a flawed fitting philosophy.

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

The **source pool may be large**, because it gives us pickup, player, guitar and performance diversity, but the active fitting set for the first prototype should remain deliberately small and varied. We want informative examples, not hours of redundant playing.

### Real guitar DI material

Real DI performances are part of the experiment from the beginning, not only a final listening test.

The original first-prototype plan was roughly **12 dry DI performances of 8-15 seconds each**, with ordinary playing levels and no post-recording normalisation. The performances should expose different behaviours rather than merely provide more minutes of guitar.

Useful playing types include:

- soft and hard-picked single notes;
- sustained notes across the instrument;
- open chords from gentle to hard;
- power chords;
- tight low-string palm mutes;
- fast alternate picking;
- legato;
- arpeggios;
- realistic mixed riffs;
- the same riff played progressively softer/harder;
- a genuine guitar-volume-pot rollback performance.

Where possible, use different pickup families across the splits, for example humbucker, traditional single coil and P90/Narrowfield-like sources.

The purpose is to test whether a method learned on one source type generalises to another.

### Real guitar split

The original plan deliberately allowed real DI to influence fitting while keeping other real playing independent.

A representative first prototype is:

```text
FIT
  ~6 real DI clips
  e.g. soft notes, sustained notes, chords, power chords,
       palm mutes, mixed riff

SELECTION
  ~4 different real DI clips
  e.g. hard-picked notes, fast picking, legato, dynamic riff
  preferably a different pickup/source

BENCHMARK
  2-4 completely held-out real DI clips
  e.g. arpeggios, actual guitar-volume rollback, unrelated riffs
  preferably another guitar/pickup
```

Fit guitar is allowed to influence coefficients.

Selection guitar is allowed to choose between genuinely general candidate strategies.

Benchmark guitar must not influence the experiment being evaluated.

### Controlled guitar samples

Controlled notes, pickups, velocities and instruments provide repeatable examples that expose differences which may be hidden in one performance.

They complement, rather than replace, played DI.

### Synthetic system-identification probes

Synthetic probes were created because different signals reveal different parts of the teacher/student mismatch much more cleanly than guitar alone.

They are deliberately artificial. They are not fake guitar and should not be judged as musical performances.

Importantly, **selected synthetic probes may participate directly in fitting**. Their purpose is not limited to post-hoc diagnostics.

The original prototype specifically identified the following as the useful starting set:

- quiet log sweep;
- multisine level ladder;
- 1 kHz level ladder;
- frequency x level matrix;
- two-tone intermodulation tests;
- transient bursts;
- optionally polarity probes and deterministic broadband noise.

They provide complementary information:

#### Quiet linear-response probes

A low-level sweep, especially around -36 dBFS, approximates the NAM's small-signal response and helps expose the combined linear behaviour of the student.

This is useful for fitting and initialisation, but does not imply that A and B should be interpreted as simple EQ blocks.

#### Multisine level ladder

The same 23-tone waveform repeated at multiple input levels is particularly valuable because the excitation is identical while drive changes.

It exposes changes in:

- gain;
- spectrum;
- harmonic/intermodulation content;
- compression;
- saturation onset and cleanup.

The smooth multisine drive ramp additionally tests whether the response changes continuously as drive rises and falls.

#### 1 kHz level ladder

A single-frequency ladder provides a simple view of:

- fundamental compression;
- harmonic growth;
- asymmetry;
- output RMS and peak behaviour.

It is useful information for understanding P/K, but it must not turn the project into "derive P/K first and then reconstruct the old converter". It is one view of the complete student fitting problem.

#### Frequency x level matrix

Testing several frequencies at several drive levels tells us whether nonlinear behaviour depends strongly on frequency.

This matters because A occurs **before** P/K. A can therefore act as frequency-dependent drive shaping so the fixed GP50 nonlinearity sees a more useful signal, with B subsequently correcting the post-nonlinearity spectrum.

#### Two-tone intermodulation

Two-tone tests expose nonlinear interaction products that single sine harmonics cannot.

They serve two purposes:

1. provide useful fitting information about nonlinear interaction;
2. reveal behaviours the GP50 student may fundamentally be unable to reproduce.

#### Broadband and transient probes

Deterministic broadband noise excites a large portion of the spectrum simultaneously and can be useful when solving/fitting the linear response.

Transient bursts and pick-like probes expose attack, recovery and short-term behaviour that steady-state tones can miss.

### Synthetic probes are fitting evidence, not perceptual authority

The correct principle is **not** "synthetic probes are diagnostics only".

The correct principle is:

> Synthetic probes can be deliberately chosen fitting material because they provide strong system-identification information, but success on synthetic probes does not override failure on independent real guitar or hardware playback.

A model that fits probes beautifully but sounds wrong on held-out guitar has not succeeded.

### Independent held-out human material

The corpus deliberately includes real playing that is not used to tune the algorithm.

This exists to answer the most important research question:

> Does the method generalise to playing it has never seen?

If performance improves only on fit or selection material, we have not improved the converter.

## Why we create level variants of the same real DI

One especially useful part of the original design is to take the **exact same recorded performance** and create deterministic relative input-level variants, for example:

```text
riff_-24
riff_-18
riff_-12
riff_-6
riff_0
```

Each scaled input must then be rendered independently through the NAM teacher.

This gives matched input/output examples where performance variation has been removed from the comparison.

They expose:

- compression;
- cleanup;
- distortion onset;
- level-dependent tone change.

The NAM target must never be created by simply scaling an already-rendered NAM output, because that would erase the nonlinear behaviour we are trying to learn.

A real guitar-volume-pot rollback recording remains valuable as a separate held-out test because the guitar's source impedance and spectral behaviour can change as the physical volume control is reduced. Digital level scaling and real volume rollback answer related but different questions.

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

Fit may include both deliberately selected system-identification probes and selected real DI material.

### Selection

Selection material may choose between general strategies or candidate rounds, but it must not be repeatedly mined until one particular model looks good.

Selection should include different real performances and preferably different pickup/source characteristics from fit.

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

## Analytic B is part of the original hypothesis

The first prototype deliberately avoided treating every B512 tap as an independent outer-search parameter.

For each proposed upstream student configuration, the intended approach is:

```text
candidate A + P/K
      -> render GP50 pre-B response on fitting material
      -> solve one shared B512 analytically against NAM teacher targets
      -> evaluate complete candidate
```

This is variable projection: expensive nonlinear/search variables remain outside, while the large linear B block is solved directly.

It is one of the central clean-sheet ideas and should remain the default unless evidence shows that the analytical solve itself is limiting quality.

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

This does not mean synthetic **inputs** should be removed from fitting. It means derived metrics must not be promoted into ad-hoc objective penalties merely because one particular result fails.

### Static P/K decomposition is a diagnostic, not the philosophy

The 1 kHz transfer experiment is useful for understanding the GP50 nonlinear family and for validating measurement tools.

It should not turn EngineV2 into "estimate the old-style P/K more accurately".

The clean-sheet goal remains whole-system behavioural approximation.

## The preferred research direction

The preferred direction is the original **compact system-identification / variable-projection student fitter**.

The fitting corpus should combine a deliberately small set of complementary controlled probes with a deliberately small set of varied real DI performances.

Conceptually:

```text
                    authoritative NAM teacher
                             |
        +--------------------+--------------------+
        |                    |                    |
 controlled probes       real DI fit        matched level variants
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

Instead, the identification/fitting set should contain enough complementary information that ordinary teacher-vs-student prediction error rewards the correct behaviour across operating conditions.

Where mathematically useful, B512 should continue to be solved analytically for each proposed upstream candidate rather than treated as hundreds of independent outer-search parameters.

A and P/K may need to be searched jointly because pre-nonlinearity spectral shaping changes the behaviour of the nonlinear block.

Real guitar is part of fitting and selection, while **independent held-out real guitar remains the perceptual/generalisation benchmark**.

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
   - different pickups/guitars;
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

The ultimate first-prototype comparison is:

```text
source NAM teacher
vs
existing/old NamToClo GP50 result
vs
new EngineV2 GP50 result
```

on the same held-out inputs and on the real GP50.

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
- excluding useful synthetic fitting probes merely because recent metric-based experiments overfit;
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
4. Determine whether the fitting corpus/objective gives the complete student enough information
5. Determine whether the GP50 architecture can express the required behaviour
6. Change the general fitting strategy if justified
7. Validate on independent real guitar material
8. Listen on actual GP50 hardware
9. Only then expand to additional NAM families
```

Do not begin by adding a penalty to make one result look better.

## Current status of EngineV2

EngineV2 remains research code.

Useful components already exist and should be retained unless evidence disproves them:

- reproducible research audio source pool;
- curated NAM corpus with development/selection/sealed intent;
- cached authoritative teacher renders;
- direct 44.1 kHz GP50 DSP model;
- exact compact GP50 CLO serializer;
- analytical B512 solving;
- fast candidate rendering;
- fit / selection / benchmark separation;
- matched real-DI level variants;
- level-response diagnostics;
- nonlinear/distortion diagnostics;
- hardware listening workflow;
- research-only transfer analyser.

The recent level-response and distortion-aware P/K objective experiments should be regarded as **research branches of thought**, not as proof that those penalties belong in the final fitting philosophy.

The next major work should return to the original central experiment:

> Can a compact combination of controlled system-identification probes and varied real DI teacher examples drive a whole-system A/P/K + analytic-B GP50 student fitter that generalises better than the existing NamToClo conversion method?

## Final north-star question

Before every substantial EngineV2 change, ask:

> Does this help us build a generally better standalone NAM -> GP50 distillation engine from the original compact teacher/student experiment, or are we patching the current result so that one example looks more convincing?

If it is the latter, stop and redesign the experiment.
