#pragma once

// GP-5/GP-50 "pure" model optimizer: fits a device model (Pre biquad -> A128 ->
// P/K shaper -> Post biquad -> B-at-device-taps) directly against an
// already-rendered Full A2 NAM target, from a neutral seed. Never runs, and
// never seeds from, the GP-200 2048-tap fit -- see native_converter.cpp's
// convertNamToClo/runQualityExperiments for how the GP-200-derived and
// current-direct-fit candidates are produced, and CLAUDE.md's "GP-5/GP-50
// pure pipeline" note for why this is a separate candidate rather than a
// production swap.

#include "native_converter_internal.hpp"

#include <cstddef>
#include <string>
#include <vector>

namespace ntc::gp5 {

struct PureFit {
    bool ok = false;
    std::string error;
    Model model;          // Pre/A128/PK/Post/B-at-device-budget, fully independent
    double fitLoss = 0.0; // final fitAB residual
    std::size_t bTaps = 0;
};

// Fits a GP-5/GP-50 model directly against an already-rendered NAM
// input/target pair (see native_converter.cpp's renderNam), from a neutral
// seed: flat A128/B (device tap budget from gp5TrainerTapsFor(sr)),
// fitPk()-estimated P/K (a general amp-shaper heuristic, not GP-200-specific),
// and default pre/post biquads. Runs the same joint A/B fitAB()+refineB()
// phases the GP-200 path uses, just at the true device tap budget from the
// start -- no 2048-tap pass, no truncation.
PureFit fitPureFromRender(const std::vector<float>& input, const std::vector<float>& target,
                           double sr, const StatusCallback& status);

} // namespace ntc::gp5
