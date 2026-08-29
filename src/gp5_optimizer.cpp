#include "gp5_optimizer.hpp"

#include <sstream>

namespace ntc::gp5 {

namespace {

struct PkSearchResult { PK pk; double loss; };

// Coordinate/pattern search over pp/pn/kp/kn, minimizing the real end-to-end
// model loss (not fitPk's internal shaper-only SSE). Same spirit as fitPk's
// own 0.80..1.20 multiplier sweep, but (a) scores the complete
// Pre->A->PK->Post->B render against the NAM target, and (b) coordinate-
// descends over all four parameters instead of a fixed two-stage
// positive/negative search.
PkSearchResult searchPkLocal(const Model& m, const std::vector<float>& input,
                              const std::vector<float>& target, double sr,
                              const StatusCallback& status) {
    Model probe = m;
    PK bestPk = m.pk;
    double bestLoss = evaluateModelLoss(probe, input, target, sr);

    // Shrinking-step coordinate descent: each pass tries pp/pn/kp/kn scaled
    // by (1+step) and (1-step) in turn, with the other three held at their
    // current best; a candidate is kept only if it strictly improves loss.
    // Step halves after a pass with no improvement. 4 passes,
    // step 0.20 -> 0.10 -> 0.05 -> 0.025.
    float step = 0.20f;
    float PK::* const fields[4] = {&PK::pp, &PK::pn, &PK::kp, &PK::kn};
    for (int pass = 0; pass < 4; ++pass) {
        bool improved = false;
        for (float PK::* field : fields) {
            for (float mul : {1.0f + step, 1.0f - step}) {
                PK candidate = bestPk;
                candidate.*field *= mul;
                if (candidate.*field <= 0.0f) continue; // pp/pn/kp/kn must stay positive
                probe.pk = candidate;
                const double loss = evaluateModelLoss(probe, input, target, sr);
                if (loss < bestLoss) { bestLoss = loss; bestPk = candidate; improved = true; }
            }
        }
        if (status) {
            std::wostringstream os;
            os << L"GP-5/GP-50 pure: P/K search pass " << (pass + 1) << L"/4 (step=" << step
               << L") -- loss=" << bestLoss << L", P/K=" << bestPk.pp << L"/" << bestPk.pn
               << L"/" << bestPk.kp << L"/" << bestPk.kn;
            status(os.str());
        }
        if (!improved) step *= 0.5f;
    }
    return {bestPk, bestLoss};
}

} // namespace

PureFit fitPureFromRender(const std::vector<float>& input, const std::vector<float>& target,
                           double sr, const StatusCallback& status) {
    PureFit r;
    if (input.empty() || target.empty()) {
        r.error = "Empty NAM render passed to GP-5/GP-50 pure fit.";
        return r;
    }

    // Device tap budget for Block B, same rule the current direct-fit path
    // uses (see native_converter.cpp's gp5TrainerTapsFor) -- ensures every
    // one of the 512 stored device taps ends up populated with real content
    // after the 44.1kHz storage-domain resample.
    r.bTaps = gp5TrainerTapsFor(sr);

    // Neutral seed: flat (identity) FIRs, default biquads, P/K estimated
    // directly from this render via the general extrema/slope heuristic --
    // never touches the GP-200 2048-tap fit or anything derived from it.
    constexpr std::size_t kDeviceATaps = 128; // CLAUDE.md: compact CLO FIR A = 128 taps
    Model m;
    m.A.assign(kDeviceATaps, 0.0f);
    if (!m.A.empty()) m.A[0] = 1.0f;
    m.B.assign(r.bTaps, 0.0f);
    if (!m.B.empty()) m.B[0] = 1.0f;
    m.pk = fitPk(input, target, sr);
    m.pre = Biquad{};
    m.post = postForRate(sr);

    fitAB(m, input, target, sr, status, r.bTaps, &r.fitLoss);
    refineB(m, input, target, sr, status, r.bTaps);

    // Alternate: search P/K against the real end-to-end loss, refit A/B
    // around the improved P/K, repeat until a round fails to clear a small
    // relative-improvement threshold. searchPkLocal()'s own result is scored
    // with the *current* A/B held frozen, so it can look better than it
    // turns out to be once fitAB/refineB re-fit A/B around the new P/K --
    // that re-fit is not guaranteed to preserve or improve on the isolated
    // measurement. So each round's candidate is scored again AFTER the
    // refit and only kept if it still clears the threshold; otherwise the
    // candidate is discarded and the loop stops, leaving m at its previous
    // (better or equal) state. This is what actually makes the loop
    // monotonically non-increasing in loss, not searchPkLocal() alone.
    constexpr int kMaxPkRounds = 3;
    constexpr double kMinRelativeImprovement = 0.002; // 0.2%
    // Must be evaluateModelLoss(), not r.fitLoss: fitAB's own internally-
    // reported phase loss is not guaranteed to be on the same scale as the
    // full end-to-end evaluateModelLoss() score searchPkLocal() optimizes
    // against, and comparing across the two caused this loop to break
    // immediately even when the P/K search found a real improvement.
    double currentLoss = evaluateModelLoss(m, input, target, sr);
    for (int round = 0; round < kMaxPkRounds; ++round) {
        const auto pkResult = searchPkLocal(m, input, target, sr, status);
        if (pkResult.loss >= currentLoss * (1.0 - kMinRelativeImprovement)) break;

        Model candidate = m;
        candidate.pk = pkResult.pk;
        double candidateFitLoss = 0.0;
        fitAB(candidate, input, target, sr, status, r.bTaps, &candidateFitLoss);
        refineB(candidate, input, target, sr, status, r.bTaps);
        const double candidateLoss = evaluateModelLoss(candidate, input, target, sr);
        if (candidateLoss >= currentLoss * (1.0 - kMinRelativeImprovement)) break;

        m = std::move(candidate);
        r.fitLoss = candidateFitLoss;
        currentLoss = candidateLoss;
    }
    r.fitLoss = currentLoss;

    r.model = std::move(m);
    r.ok = true;
    return r;
}

} // namespace ntc::gp5
