#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace ntc {

namespace fs = std::filesystem;

struct CorrectiveIrConfig {
    bool enabled = false;
    fs::path wav;
};

struct CorrectiveIrStats {
    double originalRms = 0.0;
    double convolvedRms = 0.0;
    double rmsGain = 1.0;
    double rmsGainDb = 0.0;
    double postGainDb = -6.0;
    double totalGainDb = -6.0;
};


// Decode the 44.1 kHz mono corrective IR using the same WAV rules as the CLO
// correction path. The returned samples are not normalized or gain-scaled.
bool loadCorrectiveIrSamples(const fs::path& correctiveWav,
                             std::vector<float>& correctiveIr,
                             std::string& error);

bool applyCorrectiveIrToClo(const fs::path& sourceClo,
                            const std::vector<float>& correctiveIr,
                            const fs::path& destinationClo,
                            CorrectiveIrStats& stats,
                            std::string& error,
                            double postCorrectionDb = -6.0);

bool applyCorrectiveIrToClo(const fs::path& sourceClo,
                            const fs::path& correctiveWav,
                            const fs::path& destinationClo,
                            CorrectiveIrStats& stats,
                            std::string& error,
                            double postCorrectionDb = -6.0);

// Multiplies an existing 2048-tap GP-200 CLO's Block B by a plain linear
// gain, in place -- no filter-energy renormalization (unlike
// applyCorrectiveIrToClo's automatic rmsGain, which deliberately forces the
// convolved result back to the ORIGINAL Block B's RMS and would silently
// cancel out any gain this function tries to apply). Used to correct Tone
// Match's Block B to the actual measured acoustic target level after
// refineCloBOnly, since the correction IR's own broadband gain is exactly
// what applyCorrectiveIrToClo's normalization throws away.
bool scaleClo2048BlockB(const fs::path& cloPath, double gainLinear, std::string& error);

} // namespace ntc
