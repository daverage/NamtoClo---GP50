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

// NOT YET IMPLEMENTED -- always fails with an explanatory error. Renaming a
// SnapTone slot in place turns out to need either a still-unidentified
// SnapTone name-write opcode, or a "read SnapTone tone binary" command
// paired with the existing working uploadCloToGp5 (0x92) re-upload path.
// Two other mechanisms were tried and ruled out on real GP-50 hardware
// (2026-09-15): a guessed "0x26 selector" write (silently writes to some
// other, still-unidentified structure -- readSnapToneCatalogue never
// reflects it) and a full live-patch read+edit+rewrite via the 0x1D "Patch"
// write command ported from github.com/drewmerc302/valeton-gp50 (correctly
// renames an on-device *Patch* slot, confirmed working end-to-end, but
// Patches are a separate 1-80 storage area from SnapTones 51-80 -- it never
// touches SnapTone data at all). See gp5_midi.cpp's renameSnapTone for the
// full writeup; that Patch-write code is left in place as a reference/base
// for a possible future Patch-list feature, not as dead code to delete.
bool renameSnapTone(int visibleSlot, const std::wstring& newName, std::wstring& error);

// EXPERIMENTAL, for hardware testing only -- see the large comment on its
// definition in gp5_midi.cpp. Not wired into any stable CLI/GUI surface
// beyond a debug flag; do not build features on top of this until it's
// confirmed against a real readSnapToneCatalogue re-read.
bool sendSnapToneRenameWithCommitAttempt(int visibleSlot, const std::wstring& newName, std::wstring& error);

// Deletes/clears an on-device SnapTone slot. Reverse-engineered the same way
// as renameSnapTone, from the same capture session.
bool deleteSnapTone(int visibleSlot, std::wstring& error);

} // namespace ntc::gp5
