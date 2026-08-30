#pragma once

#include <filesystem>
#include <functional>
#include <string>
#include <vector>

namespace ntc {
namespace fs = std::filesystem;

// How the Tone Match reference audio is chosen:
//  - Default: no reference WAV; the standard conversion stimulus is used (original
//    behavior).
//  - Auto: classify the NAM's fitted PK shaper (kp/kn) into a gain bucket and pick a
//    matching bundled reference clip -- see classifyGainBucket()/resolveNamedReferenceClip()
//    in native_converter.hpp. Falls back to Default if classification or clip resolution
//    fails.
//  - Clean/Moderate/High/Bass: use a specific bundled reference clip directly, bypassing
//    classification.
//  - Custom: use referenceWav verbatim (a user-browsed file).
enum class ToneMatchReferenceMode { Default, Auto, Clean, Moderate, High, Bass, Custom };

struct CloRefineConfig {
    bool enabled = false;
    int passes = 4;
    ToneMatchReferenceMode referenceMode = ToneMatchReferenceMode::Default;
    // Optional refinement test audio, used only when referenceMode == Custom. Its
    // FIRST 20 seconds are adapted to mono PCM16 44.1 kHz and inserted as the
    // 20-second tail of a second, otherwise-identical conversion stimulus. That exact
    // stimulus is rendered through both the verified NAM Full path and the original
    // CLO, so Tone Match compares the same performance through both models.
    fs::path referenceWav;
};

using RefineStatusCallback = std::function<void(const std::wstring&)>;

// Analysis-only: computes the Tone Match minimum-phase correction filter for sourceClo
// against targetWav (stimulusWav locates the final 20-second analysis window), without
// writing or modifying any CLO. parseModel() reads A/B tap counts from the CLO header
// rather than assuming a fixed length, so this works for any VTSI CLO -- the GP-200
// 2048-tap model or the GP-5/GP-50 512-tap model -- letting each be measured and
// corrected against its own actual response instead of one borrowing a correction
// derived from the other's.
bool computeToneMatchCorrectionIr(const fs::path& sourceClo,
                                  const fs::path& stimulusWav,
                                  const fs::path& targetWav,
                                  std::vector<float>& outIr,
                                  std::string& error,
                                  const RefineStatusCallback& status = {});

// CAB Tone Match refinement on the final 20 seconds, for the GP-200 2048-tap CLO.
// The 2048-sample minimum-phase IR stays in memory and is applied directly to Block B.
// Built on computeToneMatchCorrectionIr() above.
// outCorrectionIr, when non-null, receives the same correction filter that was
// convolved into Block B (0 dB post gain).
bool refineCloBOnly(const fs::path& inputClo2048,
                    const fs::path& stimulusWav,
                    const fs::path& targetWav,
                    const fs::path& outputClo2048,
                    const CloRefineConfig& config,
                    std::string& error,
                    const RefineStatusCallback& status = {},
                    std::vector<float>* outCorrectionIr = nullptr);

// Solves directly for the length-matching Block B FIR that minimizes the
// least-squares residual between the rendered model and targetWav, instead
// of computing a correction filter sized for a different tap budget (see
// computeToneMatchCorrectionIr's fixed 2048-sample IR) and convolving +
// truncating it into the existing B. Pre/A/P-K/Post are frozen (read from
// sourceClo, unchanged) -- only B is replaced. Same tail-analysis window as
// computeToneMatchCorrectionIr() (final 20s of stimulusWav/targetWav) so the
// two are directly comparable, but renders preB at unity gain (no CloPlayer
// Gain/Volume wrapper) since the least-squares solve picks its own optimal
// absolute gain and there's no downstream RMS renormalization step to
// correct for an artificial gain mismatch the way applyCorrectiveIrToB44
// has for the correction-IR path.
//
// MEASURED (2026-08-30, see test_assets/quality_results/*_PureCandidate/
// quality_experiment_results_b_solve_*.csv): scored via runQualityExperiments
// against held-out validationClips (real playing content excluded from
// whatever was used as the Tone Match reference), across a clean and an
// extreme-high-gain NAM, both submodels, and both the default synthetic
// stimulus and a real recorded clip as the Tone Match reference -- 8
// combinations total. This consistently beat both the pre-Tone-Match
// baseline and computeToneMatchCorrectionIr's correction-IR approach by a
// large margin: held-out loss reduced 54-87% vs. baseline, and 60-87% vs.
// the correction-IR candidate (which itself sometimes made held-out loss
// *worse* than doing nothing). Unlike gp5_optimizer.hpp's P/K search, this
// is not overfitting -- see the numbers hold up on the real-reference-clip
// runs with a properly excluded held-out set. Not yet wired into
// convertNamToClo's production output; still only computed for comparison
// in runQualityExperiments as of this writing.
bool solveBlockBLeastSquares(const fs::path& sourceClo,
                              const fs::path& stimulusWav,
                              const fs::path& targetWav,
                              std::vector<float>& outB,
                              std::string& error,
                              const RefineStatusCallback& status = {});

// A real-playing-content (dry, Full-A2-rendered target) pair used only to
// gate searchPostAndSolveB()'s round acceptance -- never to fit anything
// directly. Both fields must already be at 44.1kHz (the caller resamples
// native_converter.cpp's GroundTruth::target from its native trainer rate
// before building this -- clo_refiner.cpp has no resampler of its own).
struct Gp5SelectionClip {
    std::vector<float> clip44100;
    std::vector<float> target44100;
};

struct PostSearchResult {
    bool ok = false;
    std::string error;
    double postFreqScale = 1.0; // 1.0 reproduces native_converter.cpp's postForRate() exactly
    std::vector<float> b;       // Block B solved against the winning post
};

// Constrained search over the Post biquad's corner-frequency scale (a small
// fixed grid including 1.0, which reproduces native_converter.cpp's
// postForRate() -- today's fixed, reverse-engineered-from-GP-200.exe value
// -- exactly), re-solving Block B via the same least-squares approach as
// solveBlockBLeastSquares() after each candidate (Pre/A/P-K frozen). Keeps
// whichever (freqScale, B) pair scores the lowest average loss across
// selectionClips. 1.0 is always a candidate, so this can never score worse
// than today's fixed Post *on selectionClips itself* -- but that is not a
// held-out guarantee: the winner can still score worse than freqScale=1.0
// on a disjoint benchmark set, exactly as a small selection set is prone to
// (see MEASURED below). Always score the returned result against a
// benchmark set before trusting it, the same discipline gp5_optimizer.hpp's
// P/K search needed.
//
// selectionClips gates acceptance the same way gp5_optimizer.hpp's P/K
// search gates on its own selection set, for the same reason: this is a
// search over real signal, not a closed-form solve, so grading it against
// its own training/analysis window would let it overfit exactly like the
// P/K search originally did. Pass a benchmark subset disjoint from
// selectionClips to score the final result, as the caller already does for
// the P/K search.
//
// MEASURED (2026-08-30, see test_assets/quality_results/*_PureCandidate/
// quality_experiment_results_post_search.csv): freqScale=2.0 (the top of
// the grid) won on all 4 (NAM, submodel) combinations tested, but the
// benchmark-scored effect was small and inconsistent -- +4.0%/+0.9%/~0%
// (noise) on three, and a -1.6% regression on the fourth (Meshuggah Lite).
// Not wired into convertNamToClo. Always hitting the grid's boundary
// suggests either real headroom beyond 2.0 or (more likely, given the
// regression) that 2-3 selection clips isn't enough signal to trust this
// search's verdict yet -- see CLAUDE.md's "Post-biquad frequency-scale
// search" section.
PostSearchResult searchPostAndSolveB(const fs::path& sourceClo,
                                      const fs::path& stimulusWav,
                                      const fs::path& targetWav,
                                      const std::vector<Gp5SelectionClip>& selectionClips,
                                      std::string& error,
                                      const RefineStatusCallback& status = {});

// One (input, Full-A2-rendered target) pair at a specific gain level, both
// already at 44.1kHz -- the same shape native_converter.cpp's
// measureLevelResponse() builds per LevelResponsePoint, reused here so
// sweepKAndSolveSharedB() can test dynamics response across levels instead
// of a single operating point.
struct MultiLevelClip {
    double levelDb = 0.0;
    std::vector<float> input44100;
    std::vector<float> target44100;
};

struct KSweepCandidate {
    double kMultiplier = 1.0;
    std::vector<float> b;               // Block B solved jointly across all levelClips
    double maxDynamicsErrorDb = 0.0;    // vs Full A2, same relative-anchoring as LevelResponsePoint
    double rmsDynamicsErrorDb = 0.0;
    double spectralHeldOutEsr = 0.0;    // mean ESR across levels -- proxy for "did we break the tone"
};

// Dynamics-aware fitting, Step 1 (see CLAUDE.md's dynamic-range section for
// the dose-response evidence this responds to): sweeps a multiplier on the
// P/K shaper's Kp/Kn steepness -- Pre/A128/Pp/Pn/Post all frozen, read from
// sourceClo -- and for each candidate solves ONE Block B jointly across all
// of levelClips (see solveBlockBMultiLevel's doc comment in the .cpp for
// the weighting), instead of fitting each level separately the way
// solveBlockBLeastSquares() does for a single operating point. This tests
// whether the measured "GP-50 saturates too late" mismatch responds to the
// saturation curve's steepness alone, before committing to a full P/K
// search (which would also vary Pp/Pn, at much higher search cost).
//
// kMultiplier=1.0 should always be included in kMultipliers as the control:
// it must reproduce (very closely -- floating-point differences only) the
// same B solveBlockBLeastSquares would solve for a single level, and the
// same dynamics numbers already measured by measureLevelResponse for the
// unmodified conversion, as a sanity check on this new solve path before
// trusting the other candidates.
bool sweepKAndSolveSharedB(const fs::path& sourceClo,
                            const std::vector<double>& kMultipliers,
                            const std::vector<MultiLevelClip>& levelClips,
                            std::vector<KSweepCandidate>& outCandidates,
                            std::string& error,
                            const RefineStatusCallback& status = {});

// Dynamics-aware fitting, Step 2 (see CLAUDE.md's Step 1 writeup for why a
// single shared Kp/Kn multiplier was rejected but full per-NAM search was
// judged warranted). Full P/K unlock: Pp/Pn/Kp/Kn all free (Pre/A128/Post
// frozen, read from sourceClo), coordinate-descent search where every
// candidate is followed by a fresh sweepKAndSolveSharedB-style joint solve
// of Block B across trainLevelClips. Scored as
// spectralEsr(trainLevelClips) + lambda*rmsDynamicsErrorDb(trainLevelClips),
// but a round is only ACCEPTED if it also improves the same combined score
// re-evaluated on selectionLevelClips (a disjoint clip) using the exact
// (params, B) pair the training round produced -- never re-solving B
// against selection, the same discipline gp5_optimizer.hpp's P/K search and
// searchPostAndSolveB() both use, for the same overfitting reason. A round
// is further rejected if it would let selection maxDynamicsErrorDb grow
// more than 10% past the current best's -- a safety floor so a better RMS
// average can't hide a worse single-level outlier. benchmarkLevelClips (a
// second, different disjoint clip) is scored only once at the end, purely
// for honest reporting -- never used to accept or reject anything.
struct PkDynamicsResult {
    bool ok = false;
    std::string error;
    float pp = 0.0f, pn = 0.0f, kp = 0.0f, kn = 0.0f; // winning P/K
    std::vector<float> b;                             // Block B solved jointly on trainLevelClips for the winner

    float initialPp = 0.0f, initialPn = 0.0f, initialKp = 0.0f, initialKn = 0.0f;
    double initialMaxDynamicsErrorDb = 0.0, initialRmsDynamicsErrorDb = 0.0, initialSpectralEsr = 0.0;         // sourceClo as-shipped, scored on selectionLevelClips
    double optimizedMaxDynamicsErrorDb = 0.0, optimizedRmsDynamicsErrorDb = 0.0, optimizedSpectralEsr = 0.0;   // winner, scored on selectionLevelClips (the acceptance signal)
    double benchmarkMaxDynamicsErrorDb = 0.0, benchmarkRmsDynamicsErrorDb = 0.0, benchmarkSpectralEsr = 0.0;   // winner, scored on benchmarkLevelClips (never seen during search)
    double benchmarkInitialMaxDynamicsErrorDb = 0.0, benchmarkInitialRmsDynamicsErrorDb = 0.0, benchmarkInitialSpectralEsr = 0.0; // sourceClo as-shipped, scored on benchmarkLevelClips, for a fair benchmark-vs-benchmark comparison
};

PkDynamicsResult searchPkForDynamics(const fs::path& sourceClo,
                                      const std::vector<MultiLevelClip>& trainLevelClips,
                                      const std::vector<MultiLevelClip>& selectionLevelClips,
                                      const std::vector<MultiLevelClip>& benchmarkLevelClips,
                                      double lambda,
                                      std::string& error,
                                      const RefineStatusCallback& status = {});

// Parses sourceClo and renders inputSignal44100 through it end-to-end
// (Pre -> A -> P/K shaper -> Post -> B), at unity gain (no CloPlayer
// Gain/Volume wrapper -- same convention as solveBlockBLeastSquares, for
// the same reason: this is meant to reflect the actual device signal path,
// not the CloPlayer analysis convention computeToneMatchCorrectionIr uses).
// Diagnostic utility, not part of any conversion or refinement path.
bool renderCloOnSignal(const fs::path& sourceClo,
                        const std::vector<float>& inputSignal44100,
                        std::vector<float>& outputSignal44100,
                        std::string& error);

// Same render chain as renderCloOnSignal (Pre -> A -> P/K shaper -> Post ->
// B, unity gain), except the shaper (pp/pn/kp/kn) and B are overridden --
// Pre/A/Post still come from sourceClo, unmodified. Lets an unshipped
// candidate (e.g. searchPkForDynamics's winning P/K + Block B) be auditioned
// on real audio without writing a new CLO file first.
bool renderCloWithOverrideOnSignal(const fs::path& sourceClo,
                                    float pp, float pn, float kp, float kn,
                                    const std::vector<float>& b,
                                    const std::vector<float>& inputSignal44100,
                                    std::vector<float>& outputSignal44100,
                                    std::string& error);

// Writes samples as mono PCM16 44.1kHz WAV -- for exporting comparison
// renders (e.g. runPkDynamicsAudition below) to listen to directly, not
// used by any conversion or refinement path.
bool writeMono44100Wav(const fs::path& path, const std::vector<float>& samples, std::string& error);

} // namespace ntc
