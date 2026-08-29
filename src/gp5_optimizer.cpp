#include "gp5_optimizer.hpp"

namespace ntc::gp5 {

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

    r.model = std::move(m);
    r.ok = true;
    return r;
}

} // namespace ntc::gp5
