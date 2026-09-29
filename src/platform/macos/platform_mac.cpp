#include "platform.hpp"

#include <mach-o/dyld.h>
#include <sys/wait.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#include <cstdint>
#include <vector>

namespace ntc {

// wchar_t is 32-bit (UCS-4) on macOS/Clang, so wstring here is a UTF-32
// container. Every caller in this codebase treats wstring as an opaque
// Unicode string (round-tripped through toUtf8/fromUtf8, or literal ASCII),
// never as UTF-16 code units, so UTF-8 <-> UTF-32 is a safe substitution for
// the Windows UTF-8 <-> UTF-16 behavior these functions used to provide.
// Invalid input yields an empty string, matching the Windows implementation.

std::string toUtf8(const std::wstring& value) {
    std::string out;
    out.reserve(value.size());
    for (const wchar_t wc : value) {
        const std::uint32_t cp = static_cast<std::uint32_t>(wc);
        if (cp > 0x10FFFFu || (cp >= 0xD800u && cp <= 0xDFFFu)) return {};
        if (cp < 0x80u) {
            out.push_back(static_cast<char>(cp));
        } else if (cp < 0x800u) {
            out.push_back(static_cast<char>(0xC0u | (cp >> 6)));
            out.push_back(static_cast<char>(0x80u | (cp & 0x3Fu)));
        } else if (cp < 0x10000u) {
            out.push_back(static_cast<char>(0xE0u | (cp >> 12)));
            out.push_back(static_cast<char>(0x80u | ((cp >> 6) & 0x3Fu)));
            out.push_back(static_cast<char>(0x80u | (cp & 0x3Fu)));
        } else {
            out.push_back(static_cast<char>(0xF0u | (cp >> 18)));
            out.push_back(static_cast<char>(0x80u | ((cp >> 12) & 0x3Fu)));
            out.push_back(static_cast<char>(0x80u | ((cp >> 6) & 0x3Fu)));
            out.push_back(static_cast<char>(0x80u | (cp & 0x3Fu)));
        }
    }
    return out;
}

std::wstring fromUtf8(const std::string& value) {
    std::wstring out;
    out.reserve(value.size());
    std::size_t i = 0;
    const std::size_t n = value.size();
    while (i < n) {
        const std::uint32_t b0 = static_cast<unsigned char>(value[i]);
        std::uint32_t cp = 0;
        std::size_t extra = 0;
        std::uint32_t minCp = 0;
        if (b0 < 0x80u) { cp = b0; }
        else if ((b0 & 0xE0u) == 0xC0u) { cp = b0 & 0x1Fu; extra = 1; minCp = 0x80u; }
        else if ((b0 & 0xF0u) == 0xE0u) { cp = b0 & 0x0Fu; extra = 2; minCp = 0x800u; }
        else if ((b0 & 0xF8u) == 0xF0u) { cp = b0 & 0x07u; extra = 3; minCp = 0x10000u; }
        else return {};
        if (i + extra >= n) return {};
        for (std::size_t k = 1; k <= extra; ++k) {
            const std::uint32_t bk = static_cast<unsigned char>(value[i + k]);
            if ((bk & 0xC0u) != 0x80u) return {};
            cp = (cp << 6) | (bk & 0x3Fu);
        }
        if (extra != 0 && cp < minCp) return {};
        if (cp > 0x10FFFFu || (cp >= 0xD800u && cp <= 0xDFFFu)) return {};
        out.push_back(static_cast<wchar_t>(cp));
        i += extra + 1;
    }
    return out;
}

fs::path executablePath() {
    std::uint32_t size = 0;
    _NSGetExecutablePath(nullptr, &size);
    if (size == 0) return {};
    std::vector<char> buffer(size);
    if (_NSGetExecutablePath(buffer.data(), &size) != 0) return {};

    std::error_code ec;
    fs::path resolved = fs::canonical(fs::path(buffer.data()), ec);
    if (ec) return fs::path(buffer.data());
    return resolved;
}

std::string platformErrorMessage(const std::uint32_t code) {
    return std::strerror(static_cast<int>(code));
}

// "System player" preview playback (see tone3000_preview.hpp / the tone3000
// integration plan): shells out to afplay (ships with every macOS install,
// no new dependency) and blocks until it exits, rather than building a
// CoreAudio real-time engine.
bool playAudioFileBlocking(const fs::path& wav, std::string& error) {
    const pid_t pid = fork();
    if (pid < 0) { error = std::string("fork() failed: ") + std::strerror(errno); return false; }
    if (pid == 0) {
        execlp("afplay", "afplay", wav.c_str(), static_cast<char*>(nullptr));
        _exit(127); // execlp only returns on failure
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) { error = std::string("waitpid() failed: ") + std::strerror(errno); return false; }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) { error = "afplay could not play the rendered preview WAV"; return false; }
    return true;
}

} // namespace ntc
