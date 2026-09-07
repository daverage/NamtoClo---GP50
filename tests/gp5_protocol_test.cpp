// Hardware-free regression test for the GP-5/GP-50 upload payload
// construction (gp5_clo_upload.cpp): chunk framing, CRC-8, nibble encoding,
// and slot placement. This exercises the exact bytes that would go out over
// CoreMIDI/WinMM without needing a real device -- see CLAUDE.md's macOS
// port notes ("Testing").
//
// No test framework dependency: plain asserts, non-zero exit on failure,
// registered with CTest.

#include "gp5_clo_upload.hpp"

#include <cassert>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <vector>

namespace {

namespace fs = std::filesystem;

std::uint8_t crc8Poly07(const std::uint8_t* data, std::size_t size) {
    std::uint8_t crc = 0x00u;
    for (std::size_t i = 0; i < size; ++i) {
        crc ^= data[i];
        for (int bit = 0; bit < 8; ++bit)
            crc = (crc & 0x80u) != 0u ? static_cast<std::uint8_t>((crc << 1) ^ 0x07u)
                                      : static_cast<std::uint8_t>(crc << 1);
    }
    return crc;
}

bool nibbleDecodeSysEx(const std::vector<std::uint8_t>& sysex, std::vector<std::uint8_t>& decoded) {
    decoded.clear();
    if (sysex.size() < 4 || sysex.front() != 0xF0 || sysex.back() != 0xF7) return false;
    const std::size_t encoded = sysex.size() - 2;
    if (encoded % 2 != 0) return false;
    for (std::size_t i = 1; i + 1 < sysex.size() - 1; i += 2) {
        if (sysex[i] > 0x0Fu || sysex[i + 1] > 0x0Fu) return false;
        decoded.push_back(static_cast<std::uint8_t>((sysex[i] << 4) | sysex[i + 1]));
    }
    return true;
}

void writeLe32(std::vector<std::uint8_t>& data, std::size_t off, std::uint32_t value) {
    data[off] = static_cast<std::uint8_t>(value & 0xFFu);
    data[off + 1] = static_cast<std::uint8_t>((value >> 8) & 0xFFu);
    data[off + 2] = static_cast<std::uint8_t>((value >> 16) & 0xFFu);
    data[off + 3] = static_cast<std::uint8_t>((value >> 24) & 0xFFu);
}

// Builds a minimal, structurally valid synthetic GP-200-shaped VTSI CLO
// (header + 128-tap A + >=512-tap B) -- enough for makeGp5CompactClo to
// accept it, without depending on a real conversion or test_assets (which
// is gitignored local research material, not something CI can rely on).
fs::path writeSyntheticClo(const fs::path& path) {
    constexpr std::size_t headerBytes = 0x88;
    constexpr std::size_t blockATaps = 128;
    constexpr std::size_t blockBTaps = 512;
    std::vector<std::uint8_t> data(headerBytes + (blockATaps + blockBTaps) * sizeof(float), 0);
    data[0] = 'V'; data[1] = 'T'; data[2] = 'S'; data[3] = 'I';
    writeLe32(data, 0x78, 0);                                   // startA
    writeLe32(data, 0x7C, static_cast<std::uint32_t>(blockATaps)); // countA
    writeLe32(data, 0x80, static_cast<std::uint32_t>(blockATaps)); // startB
    writeLe32(data, 0x84, static_cast<std::uint32_t>(blockBTaps)); // countB

    std::ofstream out(path, std::ios::binary | std::ios::trunc);
    out.write(reinterpret_cast<const char*>(data.data()), static_cast<std::streamsize>(data.size()));
    return path;
}

#define NTC_CHECK(cond) do { \
        if (!(cond)) { \
            std::fprintf(stderr, "FAILED: %s (%s:%d)\n", #cond, __FILE__, __LINE__); \
            std::exit(1); \
        } \
    } while (0)

} // namespace

int main() {
    const fs::path tmpDir = fs::temp_directory_path() / "namtoclo_gp5_protocol_test";
    std::error_code ec;
    fs::create_directories(tmpDir, ec);
    const fs::path cloPath = writeSyntheticClo(tmpDir / "My Test Amp.clo");

    // --- Rejects out-of-range slots (only visible SnapTone 51-80 exposed) ---
    {
        ntc::gp5::CloUploadData data;
        std::wstring error;
        NTC_CHECK(!ntc::gp5::buildCloUpload(cloPath, 49, data, error));
        NTC_CHECK(!ntc::gp5::buildCloUpload(cloPath, 80, data, error));
    }

    // --- Builds a well-formed transfer for a valid slot ---
    ntc::gp5::CloUploadData data;
    std::wstring error;
    NTC_CHECK(ntc::gp5::buildCloUpload(cloPath, 55, data, error)); // visible 56
    NTC_CHECK(data.slot == 55);
    NTC_CHECK(data.displayName == "My Test Amp");
    // 2770-byte transfer (74-byte wrapper + 2696-byte compact CLO) chunked
    // into 19-byte payloads: 145 full + 1 partial = 146 chunks.
    NTC_CHECK(data.chunks.size() == 146u);

    std::vector<std::uint8_t> reassembled;
    for (std::size_t i = 0; i < data.chunks.size(); ++i) {
        const auto& sysex = data.chunks[i];
        NTC_CHECK(sysex.front() == 0xF0);
        NTC_CHECK(sysex.back() == 0xF7);

        std::vector<std::uint8_t> decoded;
        NTC_CHECK(nibbleDecodeSysEx(sysex, decoded));
        // [CRC8][0x92][sequence][payload length][payload...]
        NTC_CHECK(decoded.size() >= 4);
        NTC_CHECK(decoded[1] == 0x92);
        NTC_CHECK(decoded[2] == static_cast<std::uint8_t>(i));
        const std::uint8_t payloadLen = decoded[3];
        NTC_CHECK(decoded.size() == static_cast<std::size_t>(4 + payloadLen));
        const std::uint8_t expectedCrc = crc8Poly07(decoded.data() + 1, decoded.size() - 1);
        NTC_CHECK(decoded[0] == expectedCrc);

        reassembled.insert(reassembled.end(), decoded.begin() + 4, decoded.end());

        if (i == 0) {
            // Wrapper's destination-slot byte (offset 6) is the zero-based
            // protocol slot, i.e. visible SnapTone - 1.
            NTC_CHECK(decoded[4 + 6] == 55);
        }
    }

    // Wrapper (74 bytes) + compact CLO (0xA88 bytes) = 2770 bytes total.
    NTC_CHECK(reassembled.size() == 74 + 0xA88);
    NTC_CHECK(data.compactCloBytes == 0xA88);
    // Reassembled compact CLO retains VTSI magic after the wrapper.
    NTC_CHECK(std::memcmp(reassembled.data() + 74, "VTSI", 4) == 0);

    fs::remove_all(tmpDir, ec);
    std::printf("gp5_protocol_test: OK\n");
    return 0;
}
