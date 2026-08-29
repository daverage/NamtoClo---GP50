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
// phases the GP-200 path uses at the true device tap budget from the start,
// then alternates a bounded local P/K search (see searchPkLocal() in the
// .cpp) with re-running fitAB()+refineB() around the improved P/K, stopping
// once a round fails to clear a small relative-improvement threshold.
//
// MEASURED PHASE 1 (2026-08-29, before P/K search existed, see
// test_assets/quality_results/*_PureCandidate/quality_experiment_results.csv,
// one clean and one extreme-high-gain NAM): with P/K left untouched, this
// produced a byte-identical .clo to the existing direct-fit candidate
// (native_converter.cpp's m5, seeded from the GP-200 2048-tap fit's converged
// A) -- fitAB()'s A/B search is seed-independent given identical P/K/pre/post.
//
// MEASURED PHASE 2 (2026-08-29, P/K search added, see the same directory's
// *_pk_search.csv): the search finds and keeps genuine in-sample-loss
// improvements on 2 of 4 (NAM, submodel) combinations tested (e.g. clean
// Fender Full: -32% in-sample), and the monotonicity guard correctly
// rejects it on the other 2 (where a naive accept would have regressed
// in-sample loss). But on EVERY combination where an improvement was kept,
// held-out loss (scored against real playing content the search never sees)
// got WORSE, not better -- e.g. clean Fender Full: held-out +23% despite the
// in-sample win. This is overfitting to the 70s synthetic conversion
// stimulus, not a bug: searchPkLocal() and the round-acceptance check both
// only ever see the same signal fitAB() was fit against. Do NOT wire this
// candidate into convertNamToClo, and do NOT consider "the search accepted
// an improvement" as evidence of a quality win, until P/K search is scored
// against held-out content too (would need validation clips threaded into
// fitPureFromRender, which it does not currently take) -- see CLAUDE.md's
// "GP-5/GP-50 pure candidate" section.
PureFit fitPureFromRender(const std::vector<float>& input, const std::vector<float>& target,
                           double sr, const StatusCallback& status);

} // namespace ntc::gp5
