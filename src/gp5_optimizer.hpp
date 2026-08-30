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

// A real-playing-content (input, target) pair, already rendered through Full
// A2 at the same trainer rate as fitPureFromRender's own input/target (see
// native_converter.cpp's runQualityExperiments -- GroundTruth::input/target).
// Used only as a "selection" set: gates the P/K search's round-acceptance
// check, never used to fit A/B/P-K directly. Keep disjoint from whatever set
// is used to report a candidate's final held-out loss -- see fitPureFromRender's
// doc comment below for why.
struct SelectionClip {
    std::vector<float> input;
    std::vector<float> target;
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
// selectionClips (may be empty) are a disjoint-from-benchmark set of real
// playing content: searchPkLocal()'s own per-move coordinate descent still
// fits against input/target (that's the "fit" role), but each round's
// post-refit acceptance check is gated on the average loss across
// selectionClips instead, when non-empty -- see MEASURED PHASE 3 below for
// why. Falls back to the training-loss-gated behavior (PHASE 2) when empty.
//
// MEASURED PHASE 1 (2026-08-29, before P/K search existed, see
// test_assets/quality_results/*_PureCandidate/quality_experiment_results.csv,
// one clean and one extreme-high-gain NAM): with P/K left untouched, this
// produced a byte-identical .clo to the existing direct-fit candidate
// (native_converter.cpp's m5, seeded from the GP-200 2048-tap fit's converged
// A) -- fitAB()'s A/B search is seed-independent given identical P/K/pre/post.
//
// MEASURED PHASE 2 (2026-08-29, P/K search added, no selection set yet, see
// the same directory's *_pk_search.csv): the search finds and keeps genuine
// in-sample-loss improvements on 2 of 4 (NAM, submodel) combinations tested
// (e.g. clean Fender Full: -32% in-sample), and the monotonicity guard
// correctly rejects it on the other 2. But on EVERY combination where an
// improvement was kept, held-out loss (scored against real playing content
// the search never saw at all) got WORSE, not better -- e.g. clean Fender
// Full: held-out +23% despite the in-sample win. Overfitting to the 70s
// synthetic conversion stimulus: both the search and its acceptance check
// only ever saw that one signal.
//
// PHASE 3 (this parameter): adds the missing middle tier from the
// train/selection/held-out-benchmark split -- round-acceptance now gates on
// selectionClips (disjoint from whatever benchmark set reports the final
// held-out number) instead of training loss alone. See CLAUDE.md's
// "GP-5/GP-50 pure candidate" section for whether this actually fixed the
// overfitting once measured -- update that section, don't assume from this
// comment alone.
PureFit fitPureFromRender(const std::vector<float>& input, const std::vector<float>& target,
                           double sr, const std::vector<SelectionClip>& selectionClips,
                           const StatusCallback& status);

} // namespace ntc::gp5
