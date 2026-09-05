#pragma once

#include "stimulus.hpp"
#include "corrective_ir.hpp"
#include "clo_refiner.hpp"

#include <filesystem>
#include <functional>
#include <string>
#include <vector>

namespace ntc {

namespace fs = std::filesystem;

struct ConversionResult {
    bool ok = false;
    std::string error;
    fs::path inputNam;
    fs::path gp2001024;
    // Populated only when NativeConverterConfig::gp5DirectFit is enabled: a compact
    // 128-tap A / 512-tap B CLO whose Block B was fit directly at the GP-5/GP-50
    // device tap budget, instead of being sliced from the 2048-tap GP-200 fit.
    fs::path gp5gp50Compact;
    double fitLoss = 0.0;        // final fitAB residual for the standard 2048-tap B model
    double gp5DirectFitLoss = 0.0; // final fitAB residual for the direct-fit gp5gp50Compact model
    // The reference WAV actually used for Tone Match, whether resolved automatically
    // (Auto/Clean/Moderate/High/Bass) or user-browsed (Custom). Empty when Tone Match
    // was disabled or used the default stimulus with no named reference.
    fs::path toneMatchReferenceUsed;

    // Fitted PK nonlinearity shaper (pp/pn = positive/negative saturation ceiling,
    // kp/kn = positive/negative saturation steepness), same values
    // QualityExperimentResult::pkPp/etc. expose from the separate
    // runQualityExperiments pipeline -- see classifyGainBucket() in this header.
    // Computed once before any GP-5/GP-50 candidate branching, so the same
    // regardless of which candidate is chosen. 0 if conversion failed before
    // fitPk() ran.
    float pkPp = 0.0f, pkPn = 0.0f, pkKp = 0.0f, pkKn = 0.0f;

    // Which GP-5/GP-50 device-specific Tone Match candidate actually won (see
    // convertNamToClo's GP-5/GP-50 Tone Match block): "none", "correction-IR",
    // "direct B solve", "multi-level B solve", or "Step 2 P/K search". Empty
    // when GP-5/GP-50 Tone Match didn't run at all (no gp5Chosen, or Tone
    // Match disabled).
    std::wstring gp5ToneMatchMethod;
    // The dynamics-aware fitting gate's own measurement (see
    // NativeConverterConfig::dynamicsAwareFitting) of the already-chosen
    // candidate's dynamics-tracking RMS error against Full A2, in dB. -1 when
    // not computed (no reference clip available, or dynamicsAwareFitting was
    // off). Meaningful even when the Step 2 search didn't end up winning --
    // shows whether the gate considered running it and why it did/didn't.
    double gp5MeasuredDynamicsRmsDb = -1.0;
    // Final output-level correction (dB) applied to the winning GP-5/GP-50
    // Block B so its 0dB-reference output RMS matches Full A2's, after
    // whichever Tone Match candidate above was chosen -- see convertNamToClo's
    // "final output-level match" step. Every prior selection metric (spectral
    // ratio loss, zero-anchored dynamics-tracking error) is blind to a
    // constant absolute-gain offset by design, so this step exists
    // specifically to catch and correct one. 0 when not computed (no
    // gp5Chosen, or Tone Match disabled -- no reference target to match).
    double gp5ToneMatchFinalGainDb = 0.0;
};

struct BatchConversionResult {
    bool ok = false;
    std::size_t total = 0;
    std::size_t succeeded = 0;
    std::size_t failed = 0;
    std::vector<ConversionResult> items;
};

using StatusCallback = std::function<void(const std::wstring&)>;

// Coarse amp gain classification derived from the fitted PK nonlinearity shaper
// (kp/kn -- saturation steepness). Validated against 21 real NAM captures spanning
// clean Fender through extreme high-gain djent, including three same-amp
// clean-vs-crunch pairs that classified in the correct relative order. Does NOT
// distinguish bass from guitar -- bass amps can score in the Clean range (e.g. a
// clean-voiced Ampeg SVT) just as a clean guitar amp does, since kp/kn tracks gain
// character, not instrument type. Bass reference selection is therefore always an
// explicit user choice (ToneMatchReferenceMode::Bass), never picked by Auto.
enum class AmpGainBucket { Clean, Moderate, High };
AmpGainBucket classifyGainBucket(float kp, float kn);

// Resolves the bundled reference clip for a gain bucket or ToneMatchReferenceMode::Bass.
// Clips live in resources/reference_clips (next to the exe, same convention as
// nam_input_wav.wav) as clean_*.wav / moderate_*.wav / high_*.wav / bass_*.wav; when
// more than one matches a bucket, one is chosen deterministically (not randomly) so
// repeated conversions of the same NAM are reproducible. Returns an empty path if the
// clips folder or a matching clip isn't found.
fs::path resolveNamedReferenceClip(ToneMatchReferenceMode mode, float kp = 0.0f, float kn = 0.0f);

// Which A2 SlimmableContainer submodel to render/fit from. GP-200.exe itself always
// selects Full (highest max_value); Lite is an experimental option for comparing
// fit quality, since a lower-capacity submodel may leave less residual error once
// squeezed through CLO's fixed FIR+shaper structure than Full does.
enum class A2Submodel { Full, Lite };

struct NativeConverterConfig {
    int blockSize = 1024;
    A2Submodel submodel = A2Submodel::Full;
    // When true, also fit and serialize a 128/512-tap CLO whose Block B is optimized
    // directly at the GP-5/GP-50 device tap budget, rather than truncating the first
    // 512 taps of the 2048-tap GP-200 fit (see gp5gp50Compact / gp5DirectFitLoss).
    // Defaults to true: held-out validation across 5 NAM models (clean through
    // extreme-high-gain djent) found direct-fit never meaningfully worse than
    // truncation and sometimes a large win (~22% lower loss on the highest-gain model
    // tested) -- see test_assets/quality_results/*/quality_experiment_results.csv.
    bool gp5DirectFit = true;

    // Dynamics-aware fitting (CLAUDE.md's "Dynamics-aware fitting, Step 2" --
    // full P/K coordinate-descent search). EXPERIMENTAL / DIAGNOSTIC ONLY --
    // defaults to false as of the 2026-09-01 "CoreRevert" pass. Real hardware
    // listening found that letting this gate reopen and move Pp/Pn/Kp/Kn away
    // from the frozen Valeton-style fitPk() result produces a behavioral
    // regression: converted patches end up substantially quieter than the
    // official SnapTone conversion, and rolling off guitar volume mostly just
    // makes them quieter rather than cleaning up the distortion the way the
    // official conversion does. The six-level zero-anchored RMS metric this
    // gate optimizes against can improve while that metric is blind to
    // exactly this failure mode. Kept in the codebase as an experiment/
    // diagnostic tool (see the headless --pk-dynamics-search /
    // --pk-dynamics-audition CLI flags and searchPkForDynamics() itself,
    // clo_refiner.hpp) but must not run by default. When true and a Tone
    // Match reference clip is available (refine.enabled &&
    // refine.referenceWav non-empty, i.e. Tone Match reference mode isn't
    // Default/Custom-without-a-clip), convertNamToClo first cheaply measures
    // the winning GP-5/GP-50 Tone Match candidate's own dynamics-tracking
    // error against Full A2 (reusing the six-level sweep already built for
    // the multi-level B-solve candidate -- no extra Full A2 renders). Only
    // when that measured RMS relative error exceeds dynamicsSearchThresholdDb
    // does it run the full P/K search and ship the result if it beats the
    // already-chosen candidate on a disjoint selection clip. The threshold
    // calibration notes below are retained as historical context from when
    // this was production behavior, not a recommendation to re-enable it.
    bool dynamicsAwareFitting = false;
    // Threshold recalibrated (2026-08-31) against the ACTUAL measurement
    // this gate performs in production: the already-chosen candidate (i.e.
    // AFTER the cheap multi-level B solve above has already run and
    // possibly helped), scored against whichever bundled reference_clips
    // WAV the fitted gain bucket resolves to -- not the pre-correction
    // baseline, and not the same DI clip CLAUDE.md's other measurements
    // used, so this number is not directly comparable to those. 4 real
    // production-path data points measured at this exact methodology:
    // Fender Super Reverb (clean) 0.134dB -- correctly skips; Fortin
    // Meshuggah 0.516dB -- borderline, chosen to trigger; Bogner Uberschall
    // 1.344dB and JCM800 HighGain 2.504dB -- both clearly trigger and both
    // verified to find and ship a large genuine dynamics improvement
    // (Bogner selection rms 3.48->1.21dB, JCM800 3.64->0.80dB). 0.4dB
    // catches Meshuggah while keeping clear margin under Bogner/JCM800 and
    // over Fender. Still a small sample (4 amps) -- revisit if a larger
    // corpus measured this same way shows a different boundary is needed.
    double dynamicsSearchThresholdDb = 0.4;
};

ConversionResult convertNamToClo(const fs::path& inputNam,
                                 const fs::path& outputDirectory,
                                 StimulusConfig stimulus = {},
                                 CorrectiveIrConfig correction = {},
                                 CloRefineConfig refine = {},
                                 NativeConverterConfig converter = {},
                                 const StatusCallback& status = {});

BatchConversionResult convertNamFolderToClo(const fs::path& inputDirectory,
                                            const fs::path& outputDirectory,
                                            StimulusConfig stimulus = {},
                                            CorrectiveIrConfig correction = {},
                                            CloRefineConfig refine = {},
                                            NativeConverterConfig converter = {},
                                            const StatusCallback& status = {});

struct QualityExperimentResult {
    std::wstring label;          // "Full" or "Lite"
    A2Submodel submodel = A2Submodel::Full;
    ConversionResult conversion; // gp2001024 / gp5gp50Compact for this submodel
    double gp5TruncatedLoss = 0.0;  // loss if GP-5/GP-50 truncates the 2048-tap B fit
    double gp5DirectFitLoss = 0.0;  // loss from fitting B directly at the device budget
    // Mean loss of both GP-5/GP-50 candidates against held-out validationClips (real
    // playing content never used for fitting), rendered through the Full A2 submodel
    // as ground truth. -1 when validationClips was empty (not computed). This is the
    // metric to trust over the two above when they disagree, since the in-sample loss
    // above is scored on the same synthetic stimulus the model was fit against and can
    // favor a candidate that doesn't generalize to real playing.
    double gp5TruncatedHeldOutLoss = -1.0;
    double gp5DirectFitHeldOutLoss = -1.0;

    // Which candidate the dynamic pick chose ("direct-fit" or "truncated"), and how
    // that chosen candidate scores against held-out validationClips before and after
    // applying the GP-5/GP-50 Tone Match correction (the same correction reuse
    // convertNamToClo ships when Tone Match is enabled). Computed only when
    // validationClips is non-empty; -1 otherwise. Scored in the 44.1kHz device-storage
    // domain (matching what actually ships), so these two are only comparable to each
    // other, not to gp5TruncatedHeldOutLoss/gp5DirectFitHeldOutLoss above, which are
    // scored in the NAM trainer-rate domain.
    std::wstring gp5ChosenStrategy;
    double gp5ChosenDeviceHeldOutLoss = -1.0;
    double gp5PostToneMatchHeldOutLoss = -1.0;

    // Roadmap item 5: the SAME gp5Chosen model (A/B/P-K/pre/post, same B*4.0
    // scaling gp5ChosenDeviceHeldOutLoss's build44 applies before
    // resampling), rendered at its native trainer rate (exercising the
    // real, unresampled A/B coefficients) with only the rendered OUTPUT
    // resampled to the common 44.1kHz comparison rate before scoring --
    // see runQualityExperiments's heldOutLossAtCommonRateFor. Compare
    // directly against gp5ChosenDeviceHeldOutLoss (same held-out clips,
    // same model, same scoring rate, only the render rate differs) to
    // isolate how much quality the trainer-rate -> 44.1kHz resample step
    // itself costs, independent of fitting quality -- gates roadmap item 6
    // (rearchitect the optimizer to work natively at 44.1kHz), which should
    // only be pursued if this gap is actually meaningful.
    //
    // A naive same-rate comparison (heldOutLossFor(candidate, candidate's
    // own native rate) at each rate, no common-rate resampling of the
    // output) was tried first and found NOT valid: it scored the
    // trainer-rate domain consistently *worse* than the 44.1kHz domain on
    // every NAM tested, which isn't physically plausible for a resampling
    // step (resampling can't make a model more accurate) -- ratioSpectrumF/
    // lossFromRatioF isn't comparable across different native rates
    // (different FFT bin count/frequency resolution), so that version's
    // numbers were an artifact of the metric, not a real quality
    // difference. -1 when not computed.
    double gp5ChosenTrainerDomainHeldOutLoss = -1.0;

    // Alternative to gp5PostToneMatchHeldOutLoss: instead of computing a
    // correction filter sized for a different tap budget and
    // convolving+truncating it into B (the gp5PostToneMatchHeldOutLoss
    // approach), solve directly for the 512 B coefficients that minimize the
    // residual against the same Tone Match target (see
    // ntc::solveBlockBLeastSquares, clo_refiner.hpp). Scored the same way,
    // against the same held-out validationClips, so directly comparable to
    // gp5ChosenDeviceHeldOutLoss (before) and gp5PostToneMatchHeldOutLoss
    // (the existing correction-IR approach). -1 when not computed.
    //
    // Measured a large, consistent held-out win over both -- see
    // solveBlockBLeastSquares's doc comment (clo_refiner.hpp) for the full
    // numbers. Not yet wired into convertNamToClo's production output.
    double gp5DirectBSolveHeldOutLoss = -1.0;

    // Same candidate as gp5DirectBSolveHeldOutLoss (today's fixed Post,
    // freqScale=1.0 implicitly), scored on the benchmark-only subset
    // instead of the full validationClips set -- the fair baseline to
    // compare gp5PostSearchHeldOutLoss against, since that candidate's own
    // fitting saw the selection subset and can't be scored on the full set
    // without leaking. -1 when not computed.
    double gp5DirectBSolveBenchmarkHeldOutLoss = -1.0;

    // Constrained Post-biquad-frequency-scale search (see
    // clo_refiner.hpp's searchPostAndSolveB), alternating a fresh direct B
    // solve with each Post candidate. gp5PostSearchFreqScale==1.0 means the
    // search kept today's fixed postForRate() value (either no improving
    // candidate was found, or no selection clips were available to search
    // against); any other value means a different corner-frequency scale
    // won. gp5PostSearchHeldOutLoss is scored against the same disjoint
    // benchmark subset as gp5PureHeldOutLoss, for the same reason (this
    // candidate's own fitting saw the selection subset). Compare against
    // gp5DirectBSolveBenchmarkHeldOutLoss specifically (same subset, fair
    // freqScale=1.0 baseline) -- not gp5DirectBSolveHeldOutLoss, which uses
    // the full validation set and isn't comparable. -1 when not computed.
    //
    // Measured (see clo_refiner.hpp's searchPostAndSolveB doc comment):
    // freqScale=2.0 won every case tested but the benchmark effect was
    // small and inconsistent, including one regression. Not a verified win;
    // not wired into convertNamToClo.
    double gp5PostSearchFreqScale = 1.0;
    double gp5PostSearchHeldOutLoss = -1.0;

    // "Pure" candidate: a GP-5/GP-50 model fit directly at the device tap
    // budget from a neutral seed, including a bounded local P/K search (see
    // gp5_optimizer.hpp), with no dependency on the GP-200 2048-tap fit at
    // all -- not even as a seed. Purely comparative for now; not wired into
    // convertNamToClo's shipped output. -1 when the fit failed to run.
    //
    // See gp5_optimizer.hpp's fitPureFromRender doc comment and
    // test_assets/quality_results/*_PureCandidate/ for the full measured
    // history. gp5PureHeldOutLoss is now scored against a benchmark subset
    // of validationClips disjoint from whatever subset gated the P/K
    // search's round-acceptance (see runQualityExperiments's
    // gp5SelectionTruths/gp5BenchmarkTruths split) -- fixed the earlier
    // overfitting, but as of the fix landing, the search has not yet found
    // a single improvement that survives that gate: gp5PureLoss stays
    // identical to the pre-search baseline on every case tested. Not a bug;
    // just means P/K search isn't a productive lever with the current small
    // real-playing validation corpus.
    fs::path gp5PureCompact;
    double gp5PureLoss = -1.0;
    double gp5PureHeldOutLoss = -1.0;

    // Fitted PK nonlinearity shaper (pp/pn = positive/negative saturation ceiling,
    // kp/kn = positive/negative saturation steepness) -- a cheap, already-computed
    // proxy for how "hot"/high-gain the amp is, independent of any filename or
    // metadata guess. Same values shared by both GP-5/GP-50 candidates (PK doesn't
    // depend on B tap count).
    float pkPp = 0.0f, pkPn = 0.0f, pkKp = 0.0f, pkKn = 0.0f;
};

// Diagnostic utility: renders an arbitrary WAV clip (any encoding/sample rate/channel
// count) through a NAM model's Full A2 submodel and writes the result as a 44.1kHz
// mono float32 WAV. Not part of any conversion path -- useful for verifying reference
// clip content (e.g. checking for palm-mute/chug character, which is far more evident
// after amp compression/distortion than in the raw DI) by listening to or analyzing
// the rendered output instead of the source clip.
bool renderClipThroughNam(const fs::path& namPath,
                          const fs::path& inputWav,
                          const fs::path& outputWav,
                          std::string& error);

// Loops the conversion over both A2 submodels (Full, Lite) and, for each, scores both
// GP-5/GP-50 Block-B strategies (truncated vs. directly fit) using the same
// frequency-domain loss the internal A/B fitter already optimizes against. Writes
// quality_experiment_results.csv into outputDirectory so results can be compared
// across runs. Reuses converter.blockSize but ignores converter.submodel/gp5DirectFit
// (both are looped over internally) and does not apply Corrective IR / Tone Match.
//
// validationClips, when non-empty, are WAV files of real playing content (any mono/
// stereo PCM or float encoding, any sample rate) never used to fit the model. Each is
// rendered through the Full A2 submodel once as ground truth, then every GP-5/GP-50
// candidate is scored against that same ground truth and the mean loss is reported as
// gp5*HeldOutLoss -- a held-out check that the in-sample fit loss alone cannot give,
// since a candidate can fit the synthetic conversion stimulus well without generalizing
// to real playing.
//
// toneMatchReferenceWav, when non-empty, replaces the default Tone Match target: its
// first 20 seconds become the tail of the conversion stimulus (mirroring
// CloRefineConfig::referenceWav in convertNamToClo), so the Tone Match before/after
// columns reflect analysis against real playing content instead of the same synthetic
// stimulus the model was already fit against.
std::vector<QualityExperimentResult> runQualityExperiments(const fs::path& inputNam,
                                                            const fs::path& outputDirectory,
                                                            StimulusConfig stimulus = {},
                                                            NativeConverterConfig converter = {},
                                                            const std::vector<fs::path>& validationClips = {},
                                                            const fs::path& toneMatchReferenceWav = {},
                                                            const StatusCallback& status = {});

// Roadmap item 7 (measurement phase): one dry-clip level's worth of the
// level-response table -- how a fixed input gain scale renders through
// Full A2 (ground truth) vs. the actual shipped GP-5/GP-50 CLO.
struct LevelResponsePoint {
    double levelDb = 0.0;           // requested gain scale applied to the dry clip
    double inputRmsDb = 0.0;        // measured actual dry RMS after scaling
    double fullA2OutputRmsDb = 0.0;
    double gp5OutputRmsDb = 0.0;
    double waveformErrorEsr = 0.0;  // error-to-signal ratio between the two outputs at this level

    // Each output's RMS relative to THIS NAM's own 0dB point (fullA2OutputRmsDb/
    // gp5OutputRmsDb at levelDb==0), and the difference between them. Anchoring
    // to each model's own 0dB result -- rather than comparing absolute RMS --
    // isolates the *shape* of the level-response curve so amps with different
    // absolute output levels are still comparable across a NAM sweep.
    // relativeErrorDb near 0 at every level means the GP-5/GP-50 conversion
    // tracks Full A2's dynamic response; a growing magnitude means it doesn't.
    double fullA2RelativeDb = 0.0;
    double gp5RelativeDb = 0.0;
    double relativeErrorDb = 0.0;   // gp5RelativeDb - fullA2RelativeDb
};

// Roadmap item 7 (measurement phase): full level-response result for one
// (NAM, DI clip) pair, including the fitted P/K (for correlating error
// against gain character) and summary error stats derived from points.
struct LevelResponseResult {
    bool ok = false;
    std::string error;
    float pkPp = 0.0f, pkPn = 0.0f, pkKp = 0.0f, pkKn = 0.0f;
    double fullA2SweepDb = 0.0;        // total output swing across tested levels (last - first)
    double gp5SweepDb = 0.0;
    double maxRelativeErrorDb = 0.0;   // max |relativeErrorDb| across points
    double rmsRelativeErrorDb = 0.0;   // sqrt(mean(relativeErrorDb^2))
    std::vector<LevelResponsePoint> points;
};

// Renders diClipWav at several gain levels ({-24,-18,-12,-6,0,+6} dB) through
// both Full A2 and inputNam's actual shipped GP-5/GP-50 conversion
// (convertNamToClo with default/production settings, Tone Match enabled --
// this measures the real output users get, not a synthetic candidate), to
// check whether the shipped conversion tracks a player's dynamic range
// consistently. See CLAUDE.md's dynamic-range section for what was found.
bool measureLevelResponse(const fs::path& inputNam,
                          const fs::path& diClipWav,
                          LevelResponseResult& out,
                          std::string& error,
                          const StatusCallback& status = {});

// Dynamics-aware fitting, Step 1 (see CLAUDE.md's dynamic-range section):
// one K-multiplier candidate's dynamics/fidelity numbers, from
// runKSweepExperiment below. maxDynamicsErrorDb/rmsDynamicsErrorDb use the
// exact same relative-anchoring formula as LevelResponsePoint, so
// kMultiplier=1.0's row is directly comparable to a prior
// measureLevelResponse() run on the same (NAM, DI clip) pair -- the sanity
// check this experiment's plan calls for before trusting the other rows.
struct KSweepResult {
    double kMultiplier = 0.0;
    double maxDynamicsErrorDb = 0.0;
    double rmsDynamicsErrorDb = 0.0;
    double spectralHeldOutEsr = 0.0;
};

// Converts inputNam via convertNamToClo (default/production settings, same
// as measureLevelResponse -- this tests the real shipped weakness, not a
// synthetic candidate), builds the same 6-level ({-24,-18,-12,-6,0,+6} dB)
// set of (input, Full-A2-target) clips from diClipWav, and sweeps
// kMultiplier in {0.75,1.0,1.25,1.5,2.0,3.0,4.0} via
// ntc::sweepKAndSolveSharedB (clo_refiner.hpp) -- one shared Block B solved
// jointly across all six levels per candidate, instead of the production
// conversion's single-operating-point solve. Comparative/measurement only:
// does not modify or replace inputNam's actual shipped conversion. See
// CLAUDE.md's dynamic-range section for the result.
bool runKSweepExperiment(const fs::path& inputNam,
                         const fs::path& diClipWav,
                         std::vector<KSweepResult>& outResults,
                         std::string& error,
                         const StatusCallback& status = {});

// Dynamics-aware fitting, Step 2 (see CLAUDE.md -- Step 1 confirmed causality
// but rejected a single shared Kp/Kn multiplier; this is the per-NAM full
// P/K search that follows from it). Converts inputNam via convertNamToClo
// (production settings), builds three DISJOINT 6-level clip sets from
// trainDiClipWav/selectionDiClipWav/benchmarkDiClipWav, and runs
// ntc::searchPkForDynamics (clo_refiner.hpp) with the given lambda
// (weight on rmsDynamicsErrorDb relative to spectral ESR in the combined
// score). Comparative/measurement only -- does not modify or replace
// inputNam's actual shipped conversion.
bool runPkDynamicsSearchExperiment(const fs::path& inputNam,
                                   const fs::path& trainDiClipWav,
                                   const fs::path& selectionDiClipWav,
                                   const fs::path& benchmarkDiClipWav,
                                   double lambda,
                                   PkDynamicsResult& outResult,
                                   std::string& error,
                                   const StatusCallback& status = {});

// Listening-test export (not a measurement): runs the same production
// conversion + Step 2 P/K dynamics search as runPkDynamicsSearchExperiment
// above, then renders playingClipWav -- a real musical clip with its own
// natural dynamics, not the synthetic {-24..+6}dB staircase the search
// itself trains/scores on -- through three paths: Full A2 (ground truth),
// the as-shipped GP-5/GP-50 conversion, and the Step 2 P/K-optimized
// candidate (unshipped). Writes full_a2.wav / baseline_gp5.wav /
// optimized_gp5.wav to outputDirectory so the actual audible difference (if
// any) can be checked by ear -- see CLAUDE.md's Step 2 section for why the
// measured numbers alone aren't sufficient proof of an audible improvement.
bool runPkDynamicsAudition(const fs::path& inputNam,
                           const fs::path& playingClipWav,
                           const fs::path& trainDiClipWav,
                           const fs::path& selectionDiClipWav,
                           const fs::path& benchmarkDiClipWav,
                           double lambda,
                           const fs::path& outputDirectory,
                           std::string& error,
                           const StatusCallback& status = {});

// Direct verification of the multi-level B-solve Tone Match candidate wired
// into convertNamToClo's GP-5/GP-50 (true 512-tap compact) path -- every
// official-vs-ours benchmark result above was inadvertently scored against
// the GP-200 1024/2048-tap candidate (all user-supplied "official SnapTone"
// files turned out to be 8840-byte GP-200-format files, not the 2696-byte
// GP-5/GP-50 compact format), so that evidence never actually exercised the
// real GP-5/GP-50 code path this candidate was wired into. This compares
// TWO real conversions of the SAME NAM -- Default Tone Match reference mode
// (multi-level candidate never fires, refine.referenceWav stays empty) vs.
// Auto (the GUI's actual default, which does supply a reference clip) --
// against Full A2 directly, sidestepping the official-file mislabeling
// entirely. "Candidate" is the Auto-mode gp5gp50Compact; "baseline" is the
// Default-mode one.
struct Gp5MultiLevelVerifyResult {
    bool ok = false;
    std::string error;
    double baselineMaxRelativeErrorDb = 0.0, baselineRmsRelativeErrorDb = 0.0, baselineMeanHeldOutEsr = 0.0;
    double candidateMaxRelativeErrorDb = 0.0, candidateRmsRelativeErrorDb = 0.0, candidateMeanHeldOutEsr = 0.0;
    // Raw (pre-anchoring) output RMS-dB at 0dB input, on the TRUE gp5gp50Compact
    // 512-tap path -- lets a caller check whether our shipped GP-5/GP-50 output is
    // actually quieter/louder than the NAM's own true output (Full A2), independent
    // of the shape-only relative-error numbers above.
    double fullA2AbsoluteZeroDb = 0.0, baselineAbsoluteZeroDb = 0.0, candidateAbsoluteZeroDb = 0.0;
};
bool verifyGp5MultiLevelWiring(const fs::path& inputNam,
                               const fs::path& diClipWav,
                               const std::vector<fs::path>& heldOutClips,
                               Gp5MultiLevelVerifyResult& out,
                               std::string& error,
                               const StatusCallback& status = {});

// Definitive official-vs-ours benchmark (resources/GP50_SnapTone_Conversion_
// Benchmark_Plan_v2.md, section 14): given a real official SnapTone CLO
// captured/converted by Valeton's own tooling for inputNam, compares it
// against our own conversion of the SAME NAM, both rendered through the
// SAME software renderer (renderCloOnSignal -- parseModel() is A/B-tap-
// count-agnostic, so this works whether the official file is GP-200's
// 128/2048-tap layout or GP-5/GP-50's 128/512-tap compact layout; whichever
// it is, "ours" is built via the matching architecture: gp2001024 for a
// 128/2048-tap official file, gp5gp50Compact for a 128/512-tap one), with
// Full A2 (NAMCore) as the ground truth both are judged against. Removes
// hardware/capture confounds from the comparison, per the plan's section 15
// -- a one-time physical-pedal check that the renderer matches real
// hardware is a separate, manual validation step, not part of this
// function.
struct BenchmarkLevelPoint {
    double levelDb = 0.0;
    double fullA2RelativeDb = 0.0, officialRelativeDb = 0.0, oursRelativeDb = 0.0;
    double officialRelativeErrorDb = 0.0; // officialRelativeDb - fullA2RelativeDb
    double oursRelativeErrorDb = 0.0;     // oursRelativeDb - fullA2RelativeDb
    // Populated only when optimization (trainDiClipWav/selectionDiClipWav) was requested and succeeded.
    double optimizedRelativeDb = 0.0, optimizedRelativeErrorDb = 0.0;
    // Populated only when optimization succeeded: a B-only candidate (P/K frozen at the
    // shipped values, Block B solved jointly across trainDiClipWav's levels the same way
    // Step 2's search solves B per P/K candidate) -- isolates how much of "optimized"'s
    // win is the direct B-solve mechanism alone, vs. the P/K search on top of it.
    double bOnlyRelativeDb = 0.0, bOnlyRelativeErrorDb = 0.0;
};
struct BenchmarkHeldOutPoint {
    std::wstring clipName;
    double officialEsr = 0.0; // waveform error-to-signal ratio vs Full A2 (levelResponseEsr)
    double oursEsr = 0.0;
    double optimizedEsr = 0.0; // populated only when optimization succeeded
    double bOnlyEsr = 0.0;     // populated only when optimization succeeded
};
struct BenchmarkResult {
    bool ok = false;
    std::string error;
    float pkPp = 0.0f, pkPn = 0.0f, pkKp = 0.0f, pkKn = 0.0f; // ours (shipped), for gain-character context
    fs::path oursClo;

    // Six-level ({-24,-18,-12,-6,0,+6} dB) dynamics/compression-curve sweep,
    // same relative-anchoring convention as LevelResponsePoint.
    std::vector<BenchmarkLevelPoint> levels;
    double officialMaxRelativeErrorDb = 0.0, officialRmsRelativeErrorDb = 0.0;
    double oursMaxRelativeErrorDb = 0.0, oursRmsRelativeErrorDb = 0.0;

    // Raw (pre-anchoring) output RMS-dB at the 0dB input level, for official vs.
    // ours vs. Full A2 -- everything else in `levels` is deliberately zero-anchored
    // per-candidate (see runOfficialSnaptoneBenchmark's zeroing pass), which makes
    // dynamics/compression-curve *shape* comparable but throws away any absolute
    // loudness offset between candidates. These three let a caller check whether
    // our conversion's overall output level actually matches the official file's,
    // independent of the shape comparison above.
    double fullA2AbsoluteZeroDb = 0.0, officialAbsoluteZeroDb = 0.0, oursAbsoluteZeroDb = 0.0;
    double oursVsOfficialAbsoluteOffsetDb = 0.0; // oursAbsoluteZeroDb - officialAbsoluteZeroDb

    // Held-out real playing material (never used to fit either model),
    // spectral/waveform fidelity per clip plus the mean across clips.
    std::vector<BenchmarkHeldOutPoint> heldOut;
    double officialMeanHeldOutEsr = 0.0, oursMeanHeldOutEsr = 0.0;

    // Step 2 dynamics-aware P/K search (see CLAUDE.md's Step 2 section),
    // run against our own conversion and scored the SAME way as official/
    // ours above -- true only when trainDiClipWav/selectionDiClipWav were
    // both supplied to runOfficialSnaptoneBenchmark and the search
    // succeeded. Answers the question the plain official-vs-shipped
    // comparison above cannot: does our unshipped dynamics-aware candidate
    // actually beat the official file, not just our own shipped baseline?
    bool optimizedComputed = false;
    float optimizedPp = 0.0f, optimizedPn = 0.0f, optimizedKp = 0.0f, optimizedKn = 0.0f;
    double optimizedMaxRelativeErrorDb = 0.0, optimizedRmsRelativeErrorDb = 0.0;
    double optimizedMeanHeldOutEsr = 0.0;

    // B-only candidate: shipped P/K frozen, Block B solved jointly across
    // trainDiClipWav's six levels (ntc::sweepKAndSolveSharedB with
    // kMultiplier=1.0 -- P/K unchanged, same solve mechanism Step 2 uses
    // per P/K candidate). Isolates how much of optimizedComputed's win is
    // the direct B-solve alone vs. the P/K search on top of it. True only
    // when optimizedComputed is also true (built from the same train clips).
    bool bOnlyComputed = false;
    double bOnlyMaxRelativeErrorDb = 0.0, bOnlyRmsRelativeErrorDb = 0.0;
    double bOnlyMeanHeldOutEsr = 0.0;
};

// trainDiClipWav/selectionDiClipWav are optional (pass empty paths to skip):
// when both are supplied, also runs Step 2's searchPkForDynamics against our
// own conversion (using diClipWav itself as the disjoint benchmark clip for
// the search's round-acceptance gate, matching the discipline every other
// search in this codebase uses) and scores the resulting optimized candidate
// against the SAME official file and Full A2 reference as the shipped
// candidate -- see BenchmarkResult::optimizedComputed.
bool runOfficialSnaptoneBenchmark(const fs::path& inputNam,
                                  const fs::path& officialSnapClo,
                                  const fs::path& diClipWav,
                                  const std::vector<fs::path>& heldOutClips,
                                  BenchmarkResult& out,
                                  std::string& error,
                                  const StatusCallback& status = {},
                                  const fs::path& trainDiClipWav = {},
                                  const fs::path& selectionDiClipWav = {},
                                  double optimizationLambda = 0.3);

// "CoreRevert" pass (2026-09-01, see CLAUDE.md): compares three internal
// GP-5/GP-50 candidates built from the SAME NAM, to check whether restoring
// Valeton-style P/K/A fitting -- while keeping only the direct B512 solve --
// recovers correct guitar-volume cleanup without losing the B512 win.
//   "valeton"      -- Tone Match disabled entirely, so none of Tone Match's B
//                      candidates (including our direct B512 least-squares
//                      solve) or the dynamics-aware P/K search ever run. What
//                      ships is exactly convertNamToClo's always-on fitPk()/
//                      fitAB() result, with B taken from its existing dynamic
//                      pick between direct-at-512-taps and truncating the
//                      2048-tap fit (whichever scores lower for this NAM --
//                      that pick predates and is independent of the Tone
//                      Match-driven B512 solve this pass is about, so it's
//                      left on for every candidate here). The closest
//                      approximation this codebase can produce to "Valeton's
//                      own B for the 512-tap budget" without a real captured
//                      512-tap official SnapTone file to fit against.
//   "production"    -- Tone Match enabled (Auto reference mode, matching the
//                      GUI's default), so the B512 solve competes and can
//                      ship; dynamicsAwareFitting=false so P/K stays frozen
//                      at the Valeton-style fitPk() result. Production AFTER
//                      this pass.
//   "experimental"  -- same as "production" but dynamicsAwareFitting=true, so
//                      the P/K search can reopen and move P/K. Production
//                      BEFORE this pass -- comparison only, not shipped.
struct ValetonComparisonLevelPoint {
    double levelDb = 0.0;
    double fullA2AbsoluteRmsDb = 0.0;
    double candidateAbsoluteRmsDb = 0.0;
    double absoluteGainErrorDb = 0.0; // candidateAbsoluteRmsDb - fullA2AbsoluteRmsDb, NOT anchored --
                                       // the first-class loudness metric the user asked for.
    double fullA2RelativeDb = 0.0;    // anchored to this candidate's own 0dB point (compression shape only)
    double candidateRelativeDb = 0.0;
    double relativeErrorDb = 0.0;     // candidateRelativeDb - fullA2RelativeDb

    // Level-normalized timbre: candidate RMS-matched to Full A2 at THIS level (a pure
    // scalar gain, so it cannot change spectral/waveform shape) before comparison --
    // removes loudness as a variable so this isolates whether the candidate still
    // sounds like Full A2 at this drive level, independent of absoluteGainErrorDb above.
    double normalizedSpectralLoss = 0.0; // ratioSpectrumF/lossFromRatioF after RMS-matching
    double alignedEsr = 0.0;             // waveform ESR at the best-aligning lag (removes group-delay confound)
    double correlation = 0.0;            // waveform correlation coefficient at that same lag
};
// Diagnostic-only spectral-BALANCE ("EQ") snapshot: % of total STFT energy per
// frequency band. Distinct from ValetonComparisonLevelPoint::normalizedSpectralLoss's
// magnitude-RATIO loss -- a candidate can score well on ratio loss while still skewing
// audibly bassier/duller than Full A2, which this makes directly visible per band. See
// CLAUDE.md's "CoreRevert" pass and the manual EQ audit that motivated adding this.
struct ValetonComparisonBandEnergy {
    double subBassPercent = 0.0;  // <120Hz
    double lowMidPercent = 0.0;   // 120-500Hz
    double midPercent = 0.0;      // 500-2000Hz
    double presencePercent = 0.0; // 2000-5000Hz
    double highPercent = 0.0;     // 5000-12000Hz
    double airPercent = 0.0;      // >12000Hz
};
struct ValetonComparisonResult {
    bool ok = false;
    std::string error;
    std::wstring label; // "valeton" | "production" | "experimental"
    float pkPp = 0.0f, pkPn = 0.0f, pkKp = 0.0f, pkKn = 0.0f;
    std::vector<ValetonComparisonLevelPoint> levels; // {0,-3,-6,-9,-12,-18,-24} dB
    double meanAbsoluteGainErrorDb = 0.0;
    double maxRelativeErrorDb = 0.0, rmsRelativeErrorDb = 0.0;
    double meanNormalizedSpectralLoss = 0.0, meanAlignedEsr = 0.0, meanCorrelation = 0.0;
    // Held-out real playing clips (never used to fit anything): mean spectral fidelity
    // via the same levelResponseEsr metric used elsewhere in this file, plus the mean
    // per-band energy split for this candidate and for Full A2 on the SAME clips (both
    // RMS-matched and lag-aligned first) -- see ValetonComparisonBandEnergy above.
    double meanHeldOutEsr = 0.0;
    ValetonComparisonBandEnergy meanBandEnergyPercent;
    ValetonComparisonBandEnergy fullA2MeanBandEnergyPercent;
};

// Builds and scores four candidates for inputNam against diClipWav (rendered at the seven
// levels) and heldOutClips (spectral fidelity only, at 0dB): "valeton" (Tone Match off,
// the Valeton-style baseline), "generic" (Tone Match on, Default reference -- the plain
// synthetic stimulus tail, not a real playing clip), "production" (Tone Match on, Auto
// reference -- the gain-bucket-matched bundled clip production actually ships, added
// 2026-09-02 to directly answer whether genre/character-matched Tone Match reference audio
// beats a generic one), and "experimental" (same as production but with the dynamics-aware
// P/K search re-enabled, comparison only). Always writes exactly 4 entries into out, in
// that order -- check .ok per entry, since one candidate's conversion can fail
// independently of the others.
bool runValetonComparisonExperiment(const fs::path& inputNam,
                                    const fs::path& diClipWav,
                                    const std::vector<fs::path>& heldOutClips,
                                    std::vector<ValetonComparisonResult>& out,
                                    std::string& error,
                                    const StatusCallback& status = {});

// Scores a real official SnapTone capture (any VTSI file -- renderCloOnSignal is
// tap-count-agnostic) against Full A2 with the exact same methodology
// runValetonComparisonExperiment's candidates use (absolute gain error, relative
// dynamics, normalized spectral loss, aligned ESR/correlation at 7 levels, plus mean
// held-out ESR and 6-band EQ energy split) -- closing the gap where official was only
// ever compared against Full A2 at the individual-harmonic level (runHarmonicProfile),
// never on gain/dynamics/EQ-bands. Result label is always "official". See CLAUDE.md's
// "full comparison" section for why this exists alongside runHarmonicProfile rather than
// replacing it -- harmonic balance and broadband EQ/gain are different questions.
//
// inputGainLinear/outputGainLinear (2026-09-02, see CLAUDE.md): every prior official
// comparison rendered the official CLO at unity gain (clo_refiner.cpp's documented
// convention -- "reflects the actual device signal path, not the CloPlayer Gain/Volume
// analysis wrapper"). But CloPlayer's own default operating point is NOT unity: it applies
// a Gain stage BEFORE the CLO core (kToneMatchGainControl=50 -> ~2.19x/+6.83dB linear,
// clo_refiner.cpp's cloPlayerGainControlToLinear) and a Volume stage after (control=50 ->
// ~1.0x/0dB). Gain sits before the nonlinearity, so testing an official file at unity
// under-drives its shaper relative to what it may have actually been fit/calibrated
// against -- a plausible explanation for both part of the ~10dB absolute-gain offset found
// in the "full comparison" and some of official's surprisingly low correlation numbers on
// several amps. Default 1.0/1.0 reproduces every existing call site's unity-gain behavior
// exactly; pass cloPlayerGainControlToLinear(50.0f)/cloPlayerVolumeControlToLinear(50.0f)
// (exposed below) to test the CloPlayer-default operating point instead.
bool runOfficialCandidateComparison(const fs::path& inputNam,
                                    const fs::path& officialClo,
                                    const fs::path& diClipWav,
                                    const std::vector<fs::path>& heldOutClips,
                                    ValetonComparisonResult& out,
                                    std::string& error,
                                    const StatusCallback& status = {},
                                    float inputGainLinear = 1.0f,
                                    float outputGainLinear = 1.0f);

// Listening-test export (not a measurement): renders playingClipWav -- a real musical clip,
// not the synthetic level sweep runValetonComparisonExperiment scores against -- through
// three paths: Full A2 (ground truth), a GP-5/GP-50 conversion with Tone Match disabled
// entirely (the "valeton" candidate above -- no B512 solve, no dynamics search), and the
// actual shipped production conversion (Tone Match enabled, Auto reference mode, dynamics
// search off by default). Writes full_a2.wav / no_tonematch.wav / production.wav to
// outputDirectory so the measured differences documented in CLAUDE.md's "CoreRevert" pass
// can be checked by ear.
bool runValetonAudition(const fs::path& inputNam,
                        const fs::path& playingClipWav,
                        const fs::path& outputDirectory,
                        std::string& error,
                        const StatusCallback& status = {});

// EQ-match follow-up to the CoreRevert pass (2026-09-01, see CLAUDE.md): production's
// direct B512 solve (solveBlockBLeastSquares, clo_refiner.cpp) is a regularized Wiener
// deconvolution fit against a SINGLE Tone Match reference clip's 20s tail -- at
// frequencies weak in that one clip, the fitted H(f) is poorly conditioned and doesn't
// generalize to other real playing content, which the per-band EQ diagnostic above found
// produces amp-dependent (not consistent-direction) spectral drift on held-out clips.
// This tests the fix `sweepKAndSolveSharedB`'s existing multi-clip machinery already
// supports but has only ever been fed gain-scaled copies of ONE clip for: build two
// candidates for the SAME NAM -- "production" (via convertNamToClo, identical to
// runValetonComparisonExperiment's production candidate) and "multiclip" (production's
// OWN shipped CLO, Pre/A/P-K/Post frozen exactly as shipped, but Block B re-solved
// jointly across fitClips -- several spectrally-diverse real clips, kMultiplier=1.0 so
// P/K never moves -- instead of the single Tone Match reference) -- then scores both with
// the exact same scoreGp5CandidateAgainstFullA2 methodology (7-level sweep on diClipWav +
// held-out ESR/EQ on heldOutClips) runValetonComparisonExperiment uses. fitClips and
// heldOutClips must be disjoint from each other (and ideally from diClipWav) to keep the
// same train/eval discipline every other search in this codebase follows. Comparative/
// measurement only -- does not modify or replace inputNam's actual shipped conversion.
bool runMultiClipB512Experiment(const fs::path& inputNam,
                                const std::vector<fs::path>& fitClips,
                                const fs::path& diClipWav,
                                const std::vector<fs::path>& heldOutClips,
                                std::vector<ValetonComparisonResult>& out,
                                std::string& error,
                                const StatusCallback& status = {});

// Second EQ-match follow-up (2026-09-01, see CLAUDE.md): isolates ONE lever --
// frequency-dependent regularization in the B512 solve (ntc::solveBlockBWeighted,
// clo_refiner.hpp) -- from the multi-clip generalization fix above, so the two aren't
// conflated. Builds a "production" candidate (identical to runValetonComparisonExperiment's)
// plus one "weighted_<N>db" candidate per entry in presenceHighBoostDb: each re-solves
// production's OWN shipped Block B against the SAME single fitClip (not the multi-clip
// set), but with regularization reduced by that many dB in the presence/high band
// (2000-12000Hz) -- a positive value lets the solve apply a larger corrective gain there
// instead of falling back toward unity gain just because that band happens to be quiet in
// the fit clip, which the per-band EQ diagnostic found is a likely cause of the recurring
// presence-band deficit. 0.0 in presenceHighBoostDb reproduces the plain (unweighted) B512
// solve exactly, for a same-fit-clip baseline distinct from "production"'s real
// Tone-Match-clip fit. Scored identically to every other candidate here
// (scoreGp5CandidateAgainstFullA2). fitClip and heldOutClips must be disjoint.
// Comparative/measurement only -- does not modify or replace inputNam's actual shipped
// conversion.
bool runFrequencyWeightedB512Experiment(const fs::path& inputNam,
                                        const fs::path& fitClip,
                                        const std::vector<double>& presenceHighBoostDb,
                                        const fs::path& diClipWav,
                                        const std::vector<fs::path>& heldOutClips,
                                        std::vector<ValetonComparisonResult>& out,
                                        std::string& error,
                                        const StatusCallback& status = {});

// "EQ Match" (2026-09-01, see CLAUDE.md's CoreRevert follow-up): a deliberately gentle,
// separate correction, unlike the two failed levers above (multi-clip content diversity
// redistributed the EQ error rather than closing it; reducing regularization in the
// presence/high band had no measurable effect at all). Runs on top of the SAME
// "production" candidate the other CoreRevert experiments use (Tone Match on, dynamics
// search off) -- NOT a re-derivation of the Valeton-style no-Tone-Match baseline, and NOT
// gated on/entangled with CloRefineConfig::enabled, so it can never re-trigger Tone
// Match's own aggressive B-replacement candidates (correction-IR, solveBlockBLeastSquares,
// the multi-level solve, the dynamics search). Derives a broad, heavily 1/2-octave-smoothed,
// hard-clamped (+-maxCorrectionDb), band-limited (lowHz-highHz, fading to 0dB outside)
// magnitude correction between production's own output and Full A2 on fitClip, then
// convolves it into production's EXISTING shipped B512 (never replaces it wholesale) via
// applyCorrectiveIrToB44, which also RMS-renormalizes the result back to the original B's
// energy -- the primary gain-neutrality guarantee, on top of the EQ correction's own
// dB-domain zero-mean adjustment. fitClip and heldOutClips must be disjoint. Comparative/
// measurement only -- not wired into convertNamToClo.
struct EqMatchConfig {
    // Correction is clamped to +-maxCorrectionDb at every frequency -- deliberately small
    // (a gentle nudge on an already-good candidate, not a re-fit).
    float maxCorrectionDb = 2.0f;
    // The measured magnitude ratio (already an average over many overlapping analysis
    // windows -- see ratioSpectrumF) is further smoothed over this many octaves before
    // use, so the correction stays broad/general rather than fitting narrow-band
    // idiosyncrasies of whatever single clip it's derived from.
    float smoothingOctaves = 0.5f;
    // The correction fades smoothly to 0dB (over a half-octave transition) below lowHz
    // and above highHz -- outside this range production's existing B is untouched.
    float lowHz = 100.0f;
    float highHz = 8000.0f;
};

// Builds "production" and one "eqmatch" candidate (production's own B512 plus the gentle
// correction above, derived from fitClip) and scores both via scoreGp5CandidateAgainstFullA2
// against diClipWav (7-level sweep) and heldOutClips (spectral/EQ fidelity).
bool runEqMatchExperiment(const fs::path& inputNam,
                          const fs::path& fitClip,
                          const EqMatchConfig& eqMatch,
                          const fs::path& diClipWav,
                          const std::vector<fs::path>& heldOutClips,
                          std::vector<ValetonComparisonResult>& out,
                          std::string& error,
                          const StatusCallback& status = {});

// Harmonic-content diagnostic (2026-09-01, see CLAUDE.md's CoreRevert follow-up): three
// different linear corrections to Block B (a multi-clip solve, reduced regularization in
// the presence/high band, and a gentle smoothed EQ touch-up) all failed to close the
// recurring presence-band deficit -- one redistributed the error, one had no effect, one
// made it worse. That's converging evidence the shortfall isn't a B-side (linear,
// post-nonlinearity) problem at all: it may be that the pre-B signal itself -- shaped by
// A128, the single memoryless P/K exponential nonlinearity, and the fixed Post biquad --
// simply doesn't carry enough presence/high-band energy in the first place, which no
// linear correction on B can manufacture. This tests that directly: drives inputNam's
// Full A2 and the SAME NAM's actual production GP-5/GP-50 conversion (both its pre-B
// signal via renderPreBOnSignal, and its full shipped post-B output via renderCloOnSignal)
// with pure sine tones at several guitar-range fundamentals, and measures each harmonic's
// magnitude relative to that signal's OWN fundamental -- i.e. the harmonic falloff shape,
// independent of overall level. If pre-B's harmonic falloff is already steeper than Full
// A2's (drops off faster, so upper harmonics -- which land in presence/high for a guitar
// fundamental -- are already weaker before B ever sees the signal), that's direct evidence
// of a nonlinearity/A128 shortfall B cannot fix. If pre-B's falloff already matches Full
// A2's and only the post-B (candidate) column diverges, the problem re-implicates B after
// all, contrary to what the three failed B-side experiments suggest. Comparative/
// measurement only -- does not modify or replace inputNam's actual shipped conversion.
struct HarmonicDiagnosticPoint {
    double fundamentalHz = 0.0;
    int harmonicNumber = 0;      // 1 = fundamental
    double harmonicHz = 0.0;
    // Each column's magnitude at this harmonic, in dB relative to THAT SAME signal's own
    // fundamental (harmonic 1) magnitude -- isolates falloff SHAPE from absolute level.
    double fullA2RelativeDb = 0.0;
    double preBRelativeDb = 0.0;        // candidate's pre-B signal (Pre->A->P/K->Post, unity gain)
    double candidateRelativeDb = 0.0;   // candidate's full shipped output (post-B)
    double preBErrorDb = 0.0;           // preBRelativeDb - fullA2RelativeDb
    double candidateErrorDb = 0.0;      // candidateRelativeDb - fullA2RelativeDb
};
struct HarmonicDiagnosticResult {
    bool ok = false;
    std::string error;
    std::vector<HarmonicDiagnosticPoint> points;
};
bool runHarmonicDiagnostic(const fs::path& inputNam,
                           const std::vector<double>& fundamentalsHz,
                           HarmonicDiagnosticResult& out,
                           std::string& error,
                           const StatusCallback& status = {});

// Standalone harmonic profile of a SINGLE already-built CLO (any VTSI file --
// renderCloOnSignal is tap-count-agnostic, so this works on a real official SnapTone
// capture as much as on one of our own conversions). Reuses the exact same tone
// generation/analysis as runHarmonicDiagnostic (same fundamentals convention, same
// relative-to-own-fundamental dB convention) so its output is directly mergeable with an
// existing HarmonicDiagnosticResult/CSV for that NAM without re-deriving the Full A2 or
// "ours" columns already on hand. Added to let an official SnapTone capture be added as a
// third comparison column against already-computed Full A2/ours harmonic data, without
// re-running those.
struct HarmonicProfilePoint {
    double fundamentalHz = 0.0;
    int harmonicNumber = 0;
    double harmonicHz = 0.0;
    double relativeDb = 0.0; // relative to THIS clo's own fundamental (harmonic 1) magnitude
};
bool runHarmonicProfile(const fs::path& sourceClo,
                        const std::vector<double>& fundamentalsHz,
                        std::vector<HarmonicProfilePoint>& out,
                        std::string& error,
                        const StatusCallback& status = {});

// THD-per-level sweep (2026-09-02): extends runHarmonicProfile's sine-tone harmonic
// analysis with a gain dimension -- reports total harmonic distortion (RMS of harmonics
// 2..N over the fundamental, as a percent) at several drive levels for both Full A2 and
// a candidate CLO (any VTSI file, tap-count-agnostic), so distortion CHARACTER across the
// gain range can be compared directly instead of only at one fixed operating point --
// answers "does the conversion's distortion grow/saturate the same way the real NAM's
// does as you dig in," distinct from the existing harmonic-falloff-SHAPE diagnostics above.
struct ThdLevelPoint {
    double fundamentalHz = 0.0;
    double levelDb = 0.0; // relative to the sweep's own 0dB reference amplitude
    double fullA2ThdPercent = 0.0;
    double candidateThdPercent = 0.0;
    double thdErrorPercent = 0.0; // candidateThdPercent - fullA2ThdPercent
};
struct ThdLevelSweepResult {
    bool ok = false;
    std::string error;
    std::vector<ThdLevelPoint> points;
};
bool runThdLevelSweep(const fs::path& inputNam,
                      const fs::path& candidateClo,
                      const std::vector<double>& fundamentalsHz,
                      ThdLevelSweepResult& out,
                      std::string& error,
                      const StatusCallback& status = {});

// Envelope/crest-factor diagnostic (2026-09-02): renders a real musical clip (not a
// synthetic tone/stimulus) through Full A2 and a candidate CLO, lag-aligns them
// (bestAlignmentLag's same methodology), and compares a short-window RMS envelope over
// time plus whole-clip crest factor (peak/RMS, dB). Targets what correlation/ESR are known
// to be insensitive to: attack-transient and dynamic-envelope divergence -- e.g. a
// candidate that matches Full A2's static tone but attacks/releases differently, or
// compresses transients more/less than the real model.
struct EnvelopePoint {
    double timeSec = 0.0;
    double fullA2EnvelopeDb = 0.0;
    double candidateEnvelopeDb = 0.0;
    double envelopeErrorDb = 0.0; // candidateEnvelopeDb - fullA2EnvelopeDb
};
struct EnvelopeDiagnosticResult {
    bool ok = false;
    std::string error;
    double fullA2CrestFactorDb = 0.0;
    double candidateCrestFactorDb = 0.0;
    double crestFactorErrorDb = 0.0;
    std::vector<EnvelopePoint> points;
    double meanAbsEnvelopeErrorDb = 0.0;
    double rmsEnvelopeErrorDb = 0.0;
};
bool runEnvelopeDiagnostic(const fs::path& inputNam,
                           const fs::path& candidateClo,
                           const fs::path& playingClipWav,
                           EnvelopeDiagnosticResult& out,
                           std::string& error,
                           const StatusCallback& status = {});

// Time-varying ("spectrogram") per-band energy diff (2026-09-02): unlike
// ValetonComparisonBandEnergy's single clip-averaged band split, this reports each of the
// same 6 bands (sub-bass/low-mid/mid/presence/high/air) PER SHORT-TIME FRAME across a real
// musical clip, lag-aligned first -- reveals whether an EQ mismatch is constant throughout
// the clip or concentrated in specific passages (e.g. only under sustained/driven content,
// not palm mutes), which a single averaged number cannot show.
struct SpectrogramDiffPoint {
    double timeSec = 0.0;
    std::wstring bandLabel; // "subBass","lowMid","mid","presence","high","air"
    double fullA2Db = 0.0;
    double candidateDb = 0.0;
    double diffDb = 0.0; // candidateDb - fullA2Db
};
struct SpectrogramDiffResult {
    bool ok = false;
    std::string error;
    std::vector<SpectrogramDiffPoint> points;
};
bool runSpectrogramDiff(const fs::path& inputNam,
                        const fs::path& candidateClo,
                        const fs::path& playingClipWav,
                        SpectrogramDiffResult& out,
                        std::string& error,
                        const StatusCallback& status = {});

// Aliasing fingerprint test (2026-09-02, see CLAUDE.md's CoreRevert follow-up): isolates
// the 4x-oversampled polyphase-allpass P/K shaper chain (native_converter.cpp's AP/Poly
// structs, U1/U2/D1/D2) from every other variable -- A/B are built as exact 44.1kHz-domain
// impulses (serializeGp5Compact's own identity-resample shortcut when trainerRate==44100.0
// guarantees this exactly, no fitting involved), Pre is identity, Post is the real fixed
// biquad, and Pp/Pn/Kp/Kn are set symmetric (a purely-odd-harmonic nonlinearity by
// construction) so any measured EVEN-harmonic energy or any energy at a frequency that
// isn't an in-band (< Nyquist) harmonic of the drive tone is entirely attributable to the
// oversampling/decimation chain, not to A/B fitting or P/K asymmetry. Drives this
// synthetic CLO with sine tones at several frequencies/levels and measures, for every
// harmonic whose TRUE frequency exceeds the 22050Hz output Nyquist, how much energy shows
// up at that harmonic's predicted aliased (folded) frequency -- see CLAUDE.md's harmonic
// diagnostic section for why a growing high-frequency odd-harmonic excess is the expected
// fingerprint of inadequate anti-aliasing before decimation. This is the software half of
// the test only: it cannot compare against real GP-50/GP-5 hardware, only characterize our
// own renderer's aliasing behavior in isolation. Comparative/measurement only.
struct AliasingDiagnosticPoint {
    double toneHz = 0.0;
    double levelDb = 0.0;
    int harmonicNumber = 0;
    double trueHarmonicHz = 0.0;     // n * toneHz, may exceed Nyquist
    double aliasedHz = 0.0;          // trueHarmonicHz folded into [0, 22050]
    double aliasedMagnitudeDb = 0.0; // magnitude at aliasedHz, relative to the tone's own fundamental
};
bool runAliasingDiagnostic(const std::vector<double>& toneFrequenciesHz,
                           const std::vector<double>& levelsDb,
                           std::vector<AliasingDiagnosticPoint>& out,
                           std::string& error,
                           const StatusCallback& status = {});

// Isolated-stage aliasing test (2026-09-02, see CLAUDE.md): the full-chain aliasing test
// above is trustworthy ground truth but doesn't pinpoint which stage is responsible. A
// follow-up symbolic (z-domain) analysis of the D1/D2 allpass-halfband coefficients
// suggested near-zero stopband attenuation until right at the edge of the band -- but that
// analysis modeled the two branches' combination in isolation, not the full multirate
// round-trip through the paired up()/down() calls, so it carries real risk of a subtle
// multirate-DSP modeling error and should not be trusted on its own. This settles it
// empirically instead: drives D1 (nominally 176.4kHz -> 88.2kHz) and D2 (nominally
// 88.2kHz -> 44.1kHz) directly and ONLY -- no shaper, no A/B, no other stage -- by
// generating a sine tone at the stage's OWN input rate, splitting it into even/odd samples
// exactly the way the real up()/down() pairing does, feeding those through down() with
// persistent AP state (matching real usage), and measuring the decimated output's
// spectrum. A tone below the stage's output Nyquist should pass through near-full-strength
// at its own frequency; a tone above it can only appear (if at all) at its aliased/folded
// frequency, and how much energy shows up there is a direct, non-symbolic measurement of
// that stage's actual anti-aliasing performance.
struct StageAliasingPoint {
    std::wstring stage;    // "D1" | "D2"
    double toneHz = 0.0;   // true frequency at the stage's OWN input rate
    double inputRateHz = 0.0, outputRateHz = 0.0;
    bool inBand = false;           // toneHz < outputRateHz/2
    double aliasedHz = 0.0;        // toneHz folded into [0, outputRateHz/2]
    double magnitudeDb = 0.0;      // magnitude at toneHz (if inBand) or at aliasedHz (if not), relative to a calibration in-band tone at the same amplitude
};
bool runStageAliasingDiagnostic(const std::vector<double>& toneFractionsOfInputNyquist,
                                std::vector<StageAliasingPoint>& out,
                                std::string& error,
                                const StatusCallback& status = {});

// Isolated-stage IMAGING test for U1/U2 (2026-09-02, see CLAUDE.md): the interpolation-side
// counterpart to runStageAliasingDiagnostic -- U1 (44.1kHz -> 88.2kHz) and U2 (88.2kHz ->
// 176.4kHz) are the up()-side twins of D1/D2, responsible for suppressing spectral IMAGES
// (mirror copies of the baseband signal above the original Nyquist) before the shaper sees
// them, the same way D1/D2 suppress aliasing after it. Drives U1/U2 directly and ONLY --
// no shaper, no A/B, no other stage -- with a sine tone at the stage's own (low) input
// rate, splits the stage's up() output into an interleaved high-rate stream exactly as the
// real chain does, and measures energy at both the tone's own true frequency (should pass
// near full-strength) and its predicted image frequency (inputRateHz - toneHz, the mirror
// a naive/imperfect interpolator would leak into the new high-rate band). Completes the
// elimination the D1/D2 test started: if U1/U2 also come back clean, the full-chain
// aliasing leakage found earlier is not attributable to any single isolated stage, pointing
// toward a cumulative multi-harmonic effect instead.
struct StageImagingPoint {
    std::wstring stage;    // "U1" | "U2"
    double toneHz = 0.0;   // true frequency at the stage's OWN (low) input rate
    double inputRateHz = 0.0, outputRateHz = 0.0;
    double imageHz = 0.0;         // inputRateHz - toneHz -- the predicted image/mirror frequency
    double passthroughDb = 0.0;   // magnitude at toneHz, relative to a calibration in-band tone at the same amplitude
    double imageMagnitudeDb = 0.0;// magnitude at imageHz, same reference
};
bool runStageImagingDiagnostic(const std::vector<double>& toneFractionsOfInputNyquist,
                               std::vector<StageImagingPoint>& out,
                               std::string& error,
                               const StatusCallback& status = {});

// Standard-EQ-range frequency response comparison (2026-09-02, see CLAUDE.md): the 6-band
// split (ValetonComparisonBandEnergy) is coarse and non-standard. This instead reports
// absolute magnitude (dB) in the 10 ISO standard octave bands (31.5Hz-16kHz, the usual
// graphic-EQ/RTA range) for Full A2, our production conversion (Tone Match on, Auto
// reference), our conversion with Tone Match OFF entirely (the Valeton-style baseline --
// added so the EQ curve can show what Tone Match's B512 solve actually changes, not just
// what the shipped result looks like), and (optionally) a real official SnapTone capture,
// all on the SAME real playing clip(s) -- a proper frequency-response curve rather than a
// coarse energy-share summary. Reuses averagedMagnitudeSpectrum (native_converter.cpp) for
// the underlying spectral estimate.
struct FrequencyResponseBand {
    double centerHz = 0.0;
    double fullA2Db = 0.0;
    double candidateDb = 0.0;       // Tone Match on (production)
    double noToneMatchDb = 0.0;     // Tone Match off (Valeton-style baseline)
    bool hasOfficial = false;
    double officialDb = 0.0; // only valid when hasOfficial
};
struct FrequencyResponseResult {
    bool ok = false;
    std::string error;
    std::vector<FrequencyResponseBand> bands; // 10 entries, ISO octave centers
};
// officialClo may be empty to skip the official column.
bool runFrequencyResponseComparison(const fs::path& inputNam,
                                    const fs::path& officialClo,
                                    const std::vector<fs::path>& clips,
                                    FrequencyResponseResult& out,
                                    std::string& error,
                                    const StatusCallback& status = {});

} // namespace ntc
