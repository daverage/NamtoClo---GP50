#pragma once

// Small platform boundary used by the portable core. Each function here has
// exactly one implementation per OS:
//   src/platform/windows/platform_win.cpp   (Win32)
//   src/platform/macos/platform_mac.cpp     (POSIX / Foundation)
// Nothing in src/core may include <windows.h> or any macOS system header
// directly -- if new platform-specific behavior is needed, extend this file
// and add it to both implementations rather than spreading #ifdefs through
// the core.

#include <cstdint>
#include <filesystem>
#include <string>

namespace ntc {

namespace fs = std::filesystem;

// UTF-8 <-> UTF-16 (Windows-style "wide string") conversion. On Windows this
// is exactly what previously lived inline in common.cpp (WideCharToMultiByte
// / MultiByteToWideChar). On macOS wchar_t is 32-bit, so these convert
// UTF-8 <-> UTF-32 instead; every existing caller only uses wstring as an
// opaque Unicode container (never assumes UTF-16 code units), so this is a
// transparent substitution.
std::string toUtf8(const std::wstring& value);
std::wstring fromUtf8(const std::string& value);

// Absolute path to the running executable, or an empty path on failure.
// Used to locate resources shipped next to the binary (nam_input_wav.wav,
// resources/reference_clips).
fs::path executablePath();

// Human-readable description of a platform error code (Win32 error code on
// Windows, errno on macOS). Currently unused by any shipped path but kept
// available for diagnostics.
std::string platformErrorMessage(std::uint32_t code);

// Plays an audio file through the OS's own default player and blocks until
// playback finishes. macOS-only for now (shells out to afplay -- see the
// tone3000 integration plan); used by `namtoclo tone3000 preview`, which is
// itself gated APPLE-only in CMakeLists.txt. Not implemented on Windows.
bool playAudioFileBlocking(const fs::path& wav, std::string& error);

} // namespace ntc
