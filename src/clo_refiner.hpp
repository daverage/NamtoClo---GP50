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

} // namespace ntc
