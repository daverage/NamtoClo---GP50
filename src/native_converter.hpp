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

} // namespace ntc
