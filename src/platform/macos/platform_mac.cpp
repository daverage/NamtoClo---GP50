#include "platform.hpp"

#include <mach-o/dyld.h>

#include <cerrno>
#include <cstring>
#include <codecvt>
#include <locale>
#include <vector>

namespace ntc {

// wchar_t is 32-bit (UCS-4) on macOS/Clang, so wstring here is a UTF-32
// container. Every caller in this codebase treats wstring as an opaque
// Unicode string (round-tripped through toUtf8/fromUtf8, or literal ASCII),
// never as UTF-16 code units, so UTF-8 <-> UTF-32 is a safe substitution for
// the Windows UTF-8 <-> UTF-16 behavior these functions used to provide.
//
// std::wstring_convert is deprecated in C++17 but remains available in both
// libc++ and libstdc++ with no replacement in the standard library; using it
// here (rather than hand-rolling UTF-8/UTF-32 conversion) keeps this file
// small and matches how the codebase already tolerates deprecated-but-only
// option APIs (see r8brain-free-src usage elsewhere).
namespace {
using Utf8Utf32Cvt = std::wstring_convert<std::codecvt_utf8<wchar_t>, wchar_t>;
} // namespace

std::string toUtf8(const std::wstring& value) {
    if (value.empty()) return {};
    try {
        return Utf8Utf32Cvt().to_bytes(value);
    } catch (const std::range_error&) {
        return {};
    }
}

std::wstring fromUtf8(const std::string& value) {
    if (value.empty()) return {};
    try {
        return Utf8Utf32Cvt().from_bytes(value);
    } catch (const std::range_error&) {
        return {};
    }
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

} // namespace ntc
