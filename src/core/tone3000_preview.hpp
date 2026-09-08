#pragma once

// Offline "preview" renderer: runs a guitar/DI WAV through a NAM model
// (nam::get_dsp) and writes a plain 44.1kHz mono WAV of the result. This is
// a deliberately simple substitute for upstream's ~800-line real-time
// WinMM streaming preview player (nam_preview_player.cpp) -- see the
// tone3000 integration plan. The CLI plays the rendered file with the OS's
// own player (afplay on macOS via platform.hpp's playAudioFileBlocking)
// instead of building a CoreAudio real-time engine.

#include <filesystem>
#include <string>

namespace ntc {

namespace fs = std::filesystem;

bool renderNamPreview(const fs::path& namPath, const fs::path& inputWav, const fs::path& outputWav,
                      std::string& error);

} // namespace ntc
