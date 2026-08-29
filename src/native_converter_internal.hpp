#pragma once

// Private implementation-sharing header between native_converter.cpp and
// gp5_optimizer.cpp. Not included by gui.cpp / native_converter.hpp -- the
// public API surface for the rest of the app is native_converter.hpp only.
//
// Exposes the minimal fitting primitives + the Model/PK/Biquad device
// representation with external linkage so a second translation unit
// (gp5_optimizer.cpp) can drive the same NAM-target fit used by the GP-200
// path without depending on GP-200's own 2048-tap fit ever running.

#include "native_converter.hpp"

#include <cstddef>
#include <string>
#include <vector>

namespace ntc {

// GP-200.exe amp-shaper nonlinearity: positive/negative saturation ceiling
// (pp/pn) and steepness (kp/kn). Estimated by fitPk() from extrema/slope
// statistics over an already-rendered NAM signal -- this estimator is
// NAM-target-generic, not specific to the GP-200 2048-tap CLO fit.
struct PK {
    float pp = .1f, pn = .1f, kp = 1, kn = 1;
};

// Trainer biquad numerical path from GP-200.exe (0x557900 et al.).
// The official trainer uses direct-form II state in double, scales the input
// by 1000, rounds the internal output to float, then applies 0.001 and rounds
// to float again. The reciprocal scales cancel mathematically but the
// intermediate float conversion is observable and is therefore retained.
struct Biquad {
    double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0;
    double w1 = 0, w2 = 0;
    float p(float x) {
        const double w0 = static_cast<double>(x) * 1000.0 - a1 * w1 - a2 * w2;
        const double yd = b0 * w0 + b1 * w1 + b2 * w2;
        w2 = w1; w1 = w0;
        const float rounded = static_cast<float>(yd);
        return static_cast<float>(static_cast<double>(rounded) * 0.001);
    }
};

// Device model: Pre biquad -> A (FIR) -> PK shaper -> Post biquad -> B (FIR).
// Exactly the representation both the GP-200 2048-tap CLO and the GP-5/GP-50
// compact CLO serialize -- only A/B tap counts differ between the two.
struct Model {
    std::vector<float> A, B;
    PK pk;
    Biquad pre, post;
};

PK fitPk(const std::vector<float>& in, const std::vector<float>& out, double sr);
Biquad postForRate(double fs);

// How many trainer-rate-domain B taps are needed so that, after the official
// resampleFirOfficial() SRC down to the 44.1 kHz storage domain, all 512
// GP-5/GP-50 device taps end up populated with real content instead of
// trailing zero-padding.
std::size_t gp5TrainerTapsFor(double sr);

// Joint A/B optimizer (sweep / low-level / multi-level phases) against an
// already-rendered NAM input/target pair, at the given B tap budget.
void fitAB(Model& m, const std::vector<float>& input, const std::vector<float>& target,
           double sr, const StatusCallback& status, std::size_t bTaps, double* outLoss);

// Final Block B tail refinement pass, run after fitAB.
void refineB(Model& m, const std::vector<float>& input, const std::vector<float>& target,
             double sr, const StatusCallback& status, std::size_t bTaps);

} // namespace ntc
