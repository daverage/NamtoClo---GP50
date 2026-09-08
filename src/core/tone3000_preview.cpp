#include "tone3000_preview.hpp"
#include "corrective_ir.hpp"
#include "clo_refiner.hpp"
#include "native_converter_internal.hpp"

#include <NAM/get_dsp.h>

#include <algorithm>

namespace ntc {

bool renderNamPreview(const fs::path& namPath, const fs::path& inputWav, const fs::path& outputWav,
                      std::string& error) {
    // loadCorrectiveIrSamples decodes an arbitrary WAV and resamples it to
    // 44.1kHz mono float -- exactly the decode this preview needs too, so
    // reuse it rather than duplicating a WAV reader.
    std::vector<float> input44100;
    if (!loadCorrectiveIrSamples(inputWav, input44100, error)) return false;
    if (input44100.empty()) { error = "Input WAV is empty."; return false; }

    try {
        auto dsp = nam::get_dsp(namPath);
        if (!dsp) { error = "NeuralAmpModelerCore could not load the NAM."; return false; }

        double rate = dsp->GetExpectedSampleRate();
        if (!(rate > 1000.0 && rate < 384000.0)) rate = 48000.0;

        std::vector<float> renderInput = resampleForCorrectiveIr(input44100, 44100.0, rate);

        constexpr int kBlock = 1024;
        dsp->Reset(rate, kBlock);
        std::vector<NAM_SAMPLE> ib(kBlock), ob(kBlock);
        NAM_SAMPLE* ip[1] = {ib.data()};
        NAM_SAMPLE* op[1] = {ob.data()};
        std::vector<float> renderOutput(renderInput.size(), 0.0f);
        for (std::size_t pos = 0; pos < renderInput.size(); pos += static_cast<std::size_t>(kBlock)) {
            const int n = static_cast<int>(std::min<std::size_t>(static_cast<std::size_t>(kBlock), renderInput.size() - pos));
            for (int i = 0; i < n; ++i) ib[static_cast<std::size_t>(i)] = static_cast<NAM_SAMPLE>(renderInput[pos + static_cast<std::size_t>(i)]);
            dsp->process(ip, op, n);
            for (int i = 0; i < n; ++i) renderOutput[pos + static_cast<std::size_t>(i)] = static_cast<float>(ob[static_cast<std::size_t>(i)]);
        }

        const std::vector<float> output44100 = resampleForCorrectiveIr(renderOutput, rate, 44100.0);
        return writeMono44100Wav(outputWav, output44100, error);
    } catch (const std::exception& e) {
        error = std::string("NAM preview renderer: ") + e.what();
        return false;
    }
}

} // namespace ntc
