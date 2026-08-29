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
//
// MEASURED (2026-08-29, see test_assets/quality_results/*_PureCandidate/
// quality_experiment_results.csv, one clean and one extreme-high-gain NAM):
// this produces a byte-identical .clo to the existing direct-fit candidate
// (native_converter.cpp's m5, seeded from the GP-200 2048-tap fit's converged
// A). fitAB()'s sweep/low-level/multi-level search is seed-independent for
// A/B given identical P/K/pre/post -- so removing the GP-200 A-seed alone
// buys nothing. Do not expect a quality win from this candidate on its own;
// it exists as scaffolding for a real P/K (and pre/post) re-optimization
// pass, which is the part fitPureFromRender does NOT yet do differently from
// the direct-fit path.
PureFit fitPureFromRender(const std::vector<float>& input, const std::vector<float>& target,
                           double sr, const StatusCallback& status);

} // namespace ntc::gp5
