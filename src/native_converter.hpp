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

    // Fitted PK nonlinearity shaper (pp/pn = positive/negative saturation ceiling,
    // kp/kn = positive/negative saturation steepness) -- a cheap, already-computed
    // proxy for how "hot"/high-gain the amp is, independent of any filename or
    // metadata guess. Same values shared by both GP-5/GP-50 candidates (PK doesn't
    // depend on B tap count).
    float pkPp = 0.0f, pkPn = 0.0f, pkKp = 0.0f, pkKn = 0.0f;
};

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

} // namespace ntc
