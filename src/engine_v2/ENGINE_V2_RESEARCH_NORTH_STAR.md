# EngineV2 research north star

This document exists to prevent the clean-sheet NAM -> GP50 research from drifting into incremental patching of the existing converter, overfitting individual NAMs, or turning exploratory diagnostics into the conversion philosophy.

It records the **original EngineV2 idea**: why the corpus exists, why we created cached NAM teacher renders, what the teacher/student model means, how the GP50 student was intended to be fitted, and what evidence is required before we can say the new engine is actually better.

---

## The original goal

The goal is **not** to recreate Valeton's undocumented NAM-to-CLO conversion algorithm more accurately.

The goal is **not** to reproduce the original NamToClo fitting method and then improve a few constants.

The goal is **not** to make a handful of named NAMs score well.

The goal is:

> Given an arbitrary Neural Amp Modeler model, produce the best GP50/GP5 CLO that the known GP50 DSP structure can physically represent, using a clean-sheet teacher/student distillation approach rather than inheriting the original converter's fitting philosophy.

The source NAM is the behavioural authority.

The GP50 CLO structure is the constrained student architecture.

The existing NamToClo converter is the baseline to beat, not the design template.

---

## The central hypothesis

The original experiment was deliberately simple:

> If we expose the NAM teacher and the complete GP50 student to a small but highly informative combination of controlled system-identification probes and varied real guitar DI, can we fit the GP50 architecture closely enough to beat the existing NamToClo converter on unseen guitar playing and real hardware?

The intended experiment was **not** based on hours of random guitar and it was **not** based on one special conversion stimulus.

It was based on deliberately chosen information:

```text
controlled system-identification probes
+
small, varied real DI fitting corpus
+
disjoint real DI selection corpus
+
held-out real DI benchmark corpus
```

If that compact information set cannot produce a generally better conversion, adding hundreds more probes or hours of redundant material is unlikely to rescue a flawed fitting philosophy.

---

# Canonical first-prototype experiment

This section is the reference version of the experiment we originally set out to build.

## Real guitar corpus

The active guitar corpus should be deliberately small but varied. Roughly **12 dry DI performances of 8-15 seconds each** is enough for the first serious experiment.

Recordings should be:

- instrument/DI only;
- mono;
- no amp, cab, EQ, compression or noise gate;
- preferably 44.1 kHz / 24-bit for the canonical GP50 corpus;
- ordinary playing peaks around roughly -12 to -6 dBFS;
- not individually normalised afterwards.

The playing material should expose different behaviours rather than simply provide more minutes of guitar:

| Clip | Playing | Why |
| --- | --- | --- |
| G01 | Sparse single notes, soft picking | clean / low-level response |
| G02 | Similar notes, hard picking | pick dynamics |
| G03 | Sustained notes across low/mid/high strings | compression, decay, harmonics |
| G04 | Open chords, gentle -> hard | complex harmonic content |
| G05 | Power chords | typical distorted workload |
| G06 | Tight low-string palm mutes | transient / high-gain behaviour |
| G07 | Fast alternate-picked single notes | attack / recovery |
| G08 | Legato / hammer-ons / pull-offs | low-transient behaviour |
| G09 | Arpeggiated clean-ish chords | note separation |
| G10 | Mixed riff: chords, singles, mutes | realistic generalisation |
| G11 | Same riff gradually softer / harder | dynamic transition |
| G12 | Real guitar-volume rollback performance | real-world cleanup behaviour |

Where possible, use more than one pickup family. A useful pattern is:

```text
FIT       main guitar / pickup
SELECTION different pickup/source
BENCHMARK third pickup/source, fully held out
```

For example: humbucker, traditional single coil and P90/Narrowfield-like sources.

The point is not that these exact pickup types are mandatory. The point is that the method should not only succeed on the source it learned from.

### Suggested split

```text
FIT
  G01 soft single notes
  G03 sustained notes
  G04 chords
  G05 power chords
  G06 palm mutes
  G10 mixed riff

SELECTION
  G02 hard-picked notes
  G07 fast picking
  G08 legato
  G11 dynamic riff
  preferably another pickup/source

BENCHMARK
  G09 arpeggios
  G12 real guitar-volume rollback
  + one or two unrelated riffs
  preferably another guitar/pickup
```

Fit material may influence coefficients.

Selection material may decide whether a general candidate is better.

Benchmark material must not influence the experiment being evaluated.

---

## Synthetic identification corpus

Synthetic probes exist because deliberately artificial signals can reveal system behaviour much more clearly than guitar alone.

They are **not fake guitar**. They are mathematical identification signals.

Selected synthetic probes are allowed to participate directly in fitting. Their role is to give the student information that guitar alone may expose only ambiguously.

The first prototype only needs a compact subset:

```text
quiet log sweep
multisine level ladder
1 kHz level ladder
frequency x level matrix
two-tone IMD
transient bursts
```

Polarity probes, deterministic broadband noise and the other generated probes remain useful supporting material.

### Quiet log sweep

A quiet sweep around -36 dBFS gives an approximation of small-signal behaviour.

It helps reveal the combined low-drive response of:

```text
NAM teacher
versus
A -> P/K small-signal slope -> POST -> B
```

This can help fitting and initialisation. It does **not** mean A and B should simply be treated as pre-EQ and post-EQ.

### Multisine level ladder

The same 23-tone waveform is repeated at multiple levels, e.g.:

```text
-36
-30
-24
-18
-12
 -9
 -6
 -3 dBFS peak
```

Because the waveform is otherwise identical, this directly exposes how drive changes:

- gain;
- spectrum;
- harmonic/intermodulation content;
- compression;
- saturation onset;
- cleanup.

The smooth -36 -> -3 -> -36 dBFS drive ramp is especially useful for seeing whether saturation onset and cleanup behave continuously.

### 1 kHz level ladder

The 1 kHz ladder provides a simple controlled view of:

- fundamental compression;
- H2/H3/H4/etc. growth;
- positive/negative asymmetry;
- RMS and peak behaviour.

This is useful information about the nonlinear problem.

It is **not** a mandate to identify P/K independently and then rebuild the old conversion method around it. It is one source of information for fitting and understanding the complete GP50 student.

### Frequency x level matrix

Testing several frequencies at several drive levels asks an important question:

> Does the NAM distort different frequencies differently?

This is central to the new philosophy because A sits **before** P/K.

If the NAM saturates 2.5 kHz more strongly than 100 Hz at the same digital level, a valid GP50 approximation may deliberately shape those frequencies differently in A before they hit the fixed nonlinear stage, then compensate the final spectrum in B.

That is why A must be allowed to cooperate with P/K rather than being treated as merely a tone-EQ block.

### Two-tone IMD

Two-tone tests expose nonlinear interaction products that single-frequency harmonic tests cannot.

They can both:

1. provide fitting information about nonlinear interaction;
2. expose behaviour that the GP50 student may fundamentally be unable to reproduce.

### Transients

Frequency-specific transient bursts and pick-like broadband transients expose attack, recovery and short-term behaviour that steady-state tones miss.

Two conversions can have similar steady-state spectra and still feel completely different under the pick.

### Polarity probes

Matched positive/negative pulses help expose asymmetry relevant to the GP50's positive and negative P/K branches.

Again, they are information for the complete student rather than a reason to decompose the NAM into an assumed old-style parameter model.

---

## Matched digital level variants of real guitar

One useful trick from the original design is to take the **same recorded DI** and create deterministic digital drive variants, for example:

```text
riff_-24
riff_-18
riff_-12
riff_-6
riff_0
```

Every scaled input must be rendered independently through the NAM teacher.

Never create a lower-level NAM target by scaling an already-rendered NAM output. That would remove the nonlinear behaviour we are trying to learn.

Matched variants are useful because they remove performance variation and expose:

- compression;
- cleanup;
- distortion onset;
- level-dependent tone change.

A real G12 guitar-volume rollback remains a separate benchmark because the physical guitar volume control also changes the source behaviour of the pickup. Digital level scaling and real volume rollback answer related but different questions.

---

# Why we created the NAM corpus

The NAM corpus exists to stop EngineV2 being designed around one amp, one gain level or one kind of nonlinearity.

It deliberately includes:

- near-linear solid-state clean models;
- tube clean -> edge-of-breakup -> crunch progressions;
- classic crunch gain progressions;
- modern high-gain models;
- pedals and strongly nonlinear captures;
- calibrated and uncalibrated NAMs;
- development, selection and sealed groups.

Same-amp progressions are especially valuable. If a method works at G3 but fails at G7/G10 on the same JCM800 family, that reveals a problem with the fitting method rather than just an amp-name difference.

Sealed NAMs exist for the final generalisation test once the fitting philosophy has been frozen.

---

# Why we created the teacher dataset

The cached teacher dataset is the central EngineV2 infrastructure.

For every chosen source input, the authoritative NAM renderer creates the corresponding output:

```text
input audio -> NAMCore renderer -> NAM target audio
```

That input/target pair is a teacher example.

The GP50 student processes the **same input**:

```text
same input audio -> candidate GP50 DSP -> student prediction
```

The research problem is therefore:

```text
Teacher: input -> NAM -> target
Student: input -> GP50 structure -> prediction
```

We are not trying to infer the hidden internal parameters of the NAM.

We are trying to reproduce its **observable behaviour** using the much smaller GP50 architecture.

That is the meaning of teacher/student distillation in this project.

## Why the teacher outputs are cached

NAM rendering is relatively expensive; the small GP50 model can be evaluated many times cheaply.

Caching authoritative teacher outputs gives us:

- repeatable experiments;
- identical NAM targets across competing fitting strategies;
- fast optimisation without rerunning NAM inference constantly;
- recorded renderer/model/source provenance;
- clean fit/selection/benchmark separation;
- fair comparison of different EngineV2 philosophies.

The teacher cache is research infrastructure. It is not itself the conversion algorithm.

---

# The GP50 student must be fitted as a whole

The runtime structure is:

```text
PRE -> A128 -> oversampled asymmetric P/K -> POST -> B512
```

This structure is a **hardware constraint**, not a conceptual map of an amplifier.

We must not assume:

```text
A = amp EQ
P/K = all distortion
B = cab/output EQ
```

A128 appears before the nonlinear stage, so it can shape both tone **and what frequencies drive P/K**.

P/K is the nonlinear family available on the GP50.

B512 occurs after the nonlinear stage, so it can correct the final linear response created by the cooperative A/P-K solution.

A valid clean-sheet solution may therefore be:

```text
A128
  frequency-dependent pre-nonlinearity drive shaping
        |
        v
P/K
  fixed GP50 nonlinear mechanism
        |
        v
POST
  fixed hardware response
        |
        v
B512
  post-nonlinearity correction and output level
```

The blocks do not need intuitive amplifier meanings.

The only important question is whether their **combined behaviour** is the closest available approximation of the NAM.

---

# Joint A + P/K search with analytic B

This was the core first-prototype fitting hypothesis.

B512 is large but linear, so it should not normally be treated as 512 independent outer optimiser variables.

Instead, for each proposed upstream configuration:

```text
candidate A + P/K
        |
        v
render GP50 pre-B responses across fitting material
        |
        v
solve ONE shared B512 analytically against NAM teacher outputs
        |
        v
evaluate complete A + P/K + POST + B candidate
```

This is variable projection: search the difficult nonlinear/upstream variables and solve the large linear block directly.

The important part is that **A and P/K are allowed to cooperate**. We should not independently estimate a supposedly "correct P/K" and then freeze it unless experiments demonstrate that doing so is genuinely superior across the corpus.

The first question is not:

> What P/K parameters does this NAM have?

The first question is:

> What A/P-K/B combination makes the constrained GP50 student behave most like this NAM teacher across informative inputs?

---

# What should drive the optimiser

The original philosophy is to give the optimiser **better evidence**, not to keep adding amp-specific penalty terms.

The fitting set should contain enough complementary inputs that direct teacher-vs-student prediction error exposes important mistakes naturally.

Useful derived measurements such as:

- level-response curves;
- harmonic growth;
- nonlinear residual;
- IMD;
- transient metrics;
- spectral differences;

are valuable for understanding failures and may eventually justify principled general objective terms.

But they must not become an ever-growing list of penalties added because one named NAM failed one listening test.

The preferred sequence is:

```text
better / more informative input evidence
        before
more hand-created objective rules
```

---

# Fit, selection, benchmark and sealed really mean different things

## Fit

Fit material is allowed to influence coefficients.

It can include deliberately selected synthetic identification probes and selected real guitar DI.

## Selection

Selection material may choose between candidate rounds or genuinely general fitting strategies.

It should contain different real performances and ideally different pickup/source characteristics from fit.

It must not be mined repeatedly until a particular NAM behaves.

## Benchmark

Benchmark is evaluation only.

It must not influence coefficients, weights, gain rules or algorithm choices during the experiment being evaluated.

Once a benchmark result causes us to alter the algorithm, that material has effectively become development/selection evidence and must no longer be described as held out for the next claim.

## Sealed NAMs

Sealed NAMs are the final model-level generalisation check after the fitting philosophy is frozen.

Do not use them while designing the algorithm.

---

# Output volume is post-nonlinearity

This is a hard project rule.

If a candidate is too quiet, increasing the signal **before** P/K is not an output-volume correction; it changes the nonlinear behaviour.

```text
WRONG as a volume fix:
input -> more pre-drive -> P/K -> more distortion

RIGHT:
input -> same fitted A/P-K behaviour -> post-P/K output gain
```

Final loudness correction belongs after the nonlinear section, currently through B/output.

Pre-nonlinearity gain is allowed only when it is intentionally part of the fitted teacher/student behaviour.

---

# NAM Mixer's role in this research

NAM Mixer is **not part of EngineV2**.

EngineV2 must not require NAM Mixer, its manifests, its modes or its generated NAMs.

We looked at NAM Mixer because work on constructing and retraining NAMs exposed useful concepts:

- teacher/target construction;
- input calibration;
- drive behaviour;
- tone/feel separation;
- post-processing versus pre-drive;
- multi-level validation;
- receptive-field limitations;
- how a NAM is trained from known input/output data.

Those ideas helped us think differently about NamToClo.

They are research inspiration only.

The intended product remains:

```text
arbitrary .nam
    -> standalone NamToClo EngineV2
    -> GP50/GP5 .clo
```

Anything learned from NAM training research must be implemented independently inside NamToClo.

---

# What the current experiments have taught us

The experiments so far are useful evidence, including the failures.

## Clean conversion does not prove the new philosophy

The JC-120 showed that our GP50 renderer, serializer and broad linear fitting path can produce a very close result.

That validated infrastructure.

It did not validate crunch/high-gain fitting.

## Matched level behaviour matters

The same guitar performance independently rendered through the NAM at multiple input levels is highly informative.

But matching the output-level curve by itself is not sufficient. We demonstrated that a GP50 candidate can match compression better while becoming audibly more distorted.

## Better fit/selection metrics can still be wrong

The JCM800 experiments showed that derived metrics can look excellent on fit/selection material while held-out behaviour and hardware listening remain wrong.

That is exactly why the original design included independent real guitar and held-out model families.

## Static P/K identification is a diagnostic, not the engine philosophy

The 1 kHz transfer analyser is useful for understanding the GP50 nonlinear family and validating measurement tools.

It should not turn EngineV2 into a search for the NAM's supposed equivalent P/K values.

The GP50 round-trip control demonstrated that the analyser can recognise known GP50-style behaviour. That makes it a useful diagnostic instrument, not the required conversion path.

---

# What counts as a real improvement

EngineV2 is better only if it improves **generally**.

The acceptance evidence should cover:

### Clean

- waveform/tone fidelity;
- correct output level;
- real hardware listening.

### Crunch

- full-level fidelity;
- input-level response;
- guitar-volume cleanup;
- attack/compression;
- no excess perceived distortion.

### High gain

- low-end retention;
- top-end/fizz control;
- harmonic balance;
- attack/compression;
- correct saturation behaviour.

### Generalisation

- unseen performances;
- different pickups/guitars;
- different amp families;
- same-amp gain progressions;
- held-out benchmark material;
- ultimately sealed NAMs.

### Hardware

Software metrics are engineering tools.

Real GP50 listening is an acceptance gate.

### Existing converter baseline

The decisive comparison is:

```text
NAM teacher
vs
existing/old NamToClo GP50 result
vs
new EngineV2 GP50 result
```

using the same held-out inputs and actual GP50 playback.

A result is not a success merely because it differs from the old converter or wins one internal metric.

---

# Anti-drift rules

## Good reasons to change the fitting strategy

- the same failure appears across multiple NAM families;
- a controlled identification experiment exposes a structural weakness;
- fit, selection and independent benchmark evidence agree;
- the change uses a real degree of freedom in the GP50 architecture;
- hardware listening confirms the improvement;
- the principle would make sense without knowing the failing amp's name.

## Warning signs that we are drifting

Stop and reassess if we find ourselves:

- repeatedly rerunning one NAM while changing weights;
- adding a new metric because one specific amp sounds wrong;
- independently deriving P/K because that resembles the old converter's mental model;
- calling excellent selection performance a win when benchmark performance is poor;
- using pre-P/K drive as a volume correction;
- changing the algorithm because of benchmark data and still calling that benchmark held out;
- assuming lower ESR means better playing behaviour;
- treating clean success as proof of distorted success;
- reconstructing the original converter because its block interpretation is familiar;
- making NAM Mixer a dependency or product stage;
- optimising synthetic probes at the expense of held-out real guitar;
- removing useful synthetic fitting probes because one derived metric overfit;
- inventing named-amp, capture or creator special cases.

A useful anonymous-model test is:

> Would we make the same algorithmic change if the failing NAM had an anonymous filename and we did not know which amp it represented?

If not, the change is probably overfitting.

---

# Experimental hierarchy

When something fails, prefer this order:

```text
1. Verify NAM renderer / GP50 renderer / serializer parity
2. Reproduce the failure on more than one relevant NAM
3. Use controlled probes to understand what behaviour differs
4. Ask whether the fitting corpus gives the whole GP50 student enough information
5. Ask whether A and P/K are being allowed to cooperate appropriately
6. Ask whether the GP50 architecture can express the required behaviour
7. Change the general fitting strategy only when justified
8. Validate on independent real guitar
9. Listen on real GP50 hardware
10. Repeat across other NAM families
11. Only after the philosophy is frozen, use sealed NAMs
```

Do not start by adding a penalty to make one result look better.

---

# Current reusable EngineV2 infrastructure

These pieces support the original experiment and should be retained unless evidence disproves them:

- reproducible real/synthetic audio corpus;
- deliberately varied NAM corpus;
- development / selection / benchmark / sealed intent;
- cached authoritative NAMCore teacher renders;
- exact 44.1 kHz GP50 student DSP model;
- exact compact GP50 CLO serializer;
- analytical B512 solving;
- fast candidate rendering;
- matched real-DI level variants;
- system-identification probes;
- level/harmonic/distortion diagnostics;
- real-guitar previews;
- hardware listening workflow.

Recent level-response penalties, distortion-excess penalties and isolated P/K fitting experiments are **research findings**, not automatically part of the final engine.

---

# The next central experiment

Return to the original hypothesis:

> Can a compact mixture of synthetic identification probes and varied real DI teacher examples drive a jointly optimised A/P-K + analytically solved B GP50 student that generalises better than the existing NamToClo conversion method?

The first implementation should use the deliberately informative prototype set:

```text
SYSTEM IDENTIFICATION
  quiet log sweep
  multisine level ladder
  1 kHz level ladder
  frequency x level matrix
  two-tone IMD
  transient bursts

REAL GUITAR
  ~6 fit clips
  ~4 selection clips
  2-4 held-out benchmark clips
```

The optimiser should primarily learn from the teacher/student input-output evidence across that set.

Then evaluate against independent guitar and hardware.

If this architecture cannot beat the current converter with that information, stop and understand why before adding more data or more penalties.

---

# Final north-star question

Before every substantial EngineV2 change, ask:

> **Does this help the complete constrained GP50 student learn the behaviour of arbitrary NAM teachers more faithfully and generally than the existing NamToClo engine, or are we patching the current experiment so that one example looks more convincing?**

If it is the latter, stop and return to this document.
