#pragma once

#include <filesystem>
#include <functional>
#include <string>

namespace ntc {
namespace fs = std::filesystem;

struct CloRefineConfig {
    bool enabled = false;
    int passes = 4;
    // Optional refinement test audio. When provided, its FIRST 20 seconds are
    // adapted to mono PCM16 44.1 kHz and inserted as the 20-second tail of a
    // second, otherwise-identical conversion stimulus. That exact stimulus is
    // rendered through both the verified NAM Full path and the original CLO,
    // so Tone Match compares the same performance through both models.
    fs::path referenceWav;
};

using RefineStatusCallback = std::function<void(const std::wstring&)>;

// CAB Tone Match refinement on the final 20 seconds.
// The 2048-sample minimum-phase IR stays in memory and is applied directly to Block B.
// The analysis/solver itself is unchanged by the diagnostic cleanup.
// outCorrectionIr, when non-null, receives the 2048-sample minimum-phase Tone Match
// correction filter that was convolved into Block B (0 dB post gain) -- the same
// correction can be reapplied to a different-length Block B (e.g. the GP-5/GP-50
// 512-tap fit) via applyCorrectiveIrToClo()/its 44.1kHz-array equivalent, so a second
// full Tone Match analysis pass isn't required to approximate the same correction there.
bool refineCloBOnly(const fs::path& inputClo2048,
                    const fs::path& stimulusWav,
                    const fs::path& targetWav,
                    const fs::path& outputClo2048,
                    const CloRefineConfig& config,
                    std::string& error,
                    const RefineStatusCallback& status = {},
                    std::vector<float>* outCorrectionIr = nullptr);

} // namespace ntc
