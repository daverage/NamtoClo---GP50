#pragma once

#include "gp5_clo_upload.hpp"

#include <filesystem>
#include <functional>
#include <string>
#include <vector>

namespace ntc::gp5 {

struct MidiDetection {
    bool inputFound = false;
    bool outputFound = false;
    unsigned int inputId = 0;
    unsigned int outputId = 0;
    std::wstring inputName;
    std::wstring outputName;
};

MidiDetection detectGp5Midi();
std::wstring describeDetection(const MidiDetection& detection);

struct UploadResult {
    bool ok = false;
    std::wstring message;
};

using UploadProgress = std::function<void(int currentBlock, int totalBlocks, const std::wstring& status)>;

UploadResult uploadCloToGp5(const std::filesystem::path& cloFile,
                            int slot,
                            UploadProgress progress = {});

// One user SnapTone slot's name, as currently stored on the device. Empty
// name means the slot is unoccupied.
struct SnapToneCatalogueEntry {
    int visibleSlot = 0; // 51..80
    std::wstring name;
};

// Read-only: queries the GP-50's SnapTone/amp catalogue (read command,
// selector 0x24) over USB MIDI and returns the 30 user slot names (visible
// SnapTone 51-80). This reads the device's own name table, not the uploaded
// CLO contents, so it reflects whatever is actually on the pedal -- presets
// loaded via Valeton Suite included, not just ones NamToClo uploaded itself.
// No write/commit command is involved.
bool readSnapToneCatalogue(std::vector<SnapToneCatalogueEntry>& entries, std::wstring& error);

} // namespace ntc::gp5
