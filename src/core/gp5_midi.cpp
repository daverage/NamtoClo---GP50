#include "gp5_midi.hpp"

#include "midi_transport.hpp"
#include "platform.hpp"

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <condition_variable>
#include <iomanip>
#include <map>
#include <mutex>
#include <sstream>
#include <thread>
#include <utility>
#include <vector>

namespace ntc::gp5 {
namespace {

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return s;
}

// GP-50 shares the GP-5 SnapTone file/model format and write protocol, so a
// single detector accepts both. Note "gp-5"/"gp5" already match "gp-50"/
// "gp50" as substrings; the explicit checks below just make that intent
// visible rather than relying on the coincidence.
bool looksLikeSupportedSnapToneDevice(const std::string& name) {
    const auto n = lower(name);
    return n.find("gp-5") != std::string::npos
        || n.find("gp5") != std::string::npos
        || n.find("gp-50") != std::string::npos
        || n.find("gp50") != std::string::npos;
}

bool nibbleDecodeSysEx(const std::uint8_t* data,
                       std::size_t size,
                       std::vector<std::uint8_t>& decoded) {
    decoded.clear();
    if (!data || size < 4 || data[0] != 0xF0 || data[size - 1] != 0xF7) return false;
    const std::size_t encodedSize = size - 2;
    if ((encodedSize & 1u) != 0u) return false;
    decoded.reserve(encodedSize / 2);
    for (std::size_t i = 1; i + 1 < size - 1; i += 2) {
        if (data[i] > 0x0Fu || data[i + 1] > 0x0Fu) return false;
        decoded.push_back(static_cast<std::uint8_t>((data[i] << 4) | data[i + 1]));
    }
    return true;
}

// Same CRC-8 (poly 0x07) used by the upload path's makeTransferFrame
// (gp5_clo_upload.cpp) -- duplicated locally rather than shared across
// translation units, matching this file's existing style.
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

std::vector<std::uint8_t> nibbleEncodeSysEx(const std::uint8_t* body, std::size_t size) {
    std::vector<std::uint8_t> sysex;
    sysex.reserve(2 + size * 2);
    sysex.push_back(0xF0);
    for (std::size_t i = 0; i < size; ++i) {
        sysex.push_back(static_cast<std::uint8_t>((body[i] >> 4) & 0x0Fu));
        sysex.push_back(static_cast<std::uint8_t>(body[i] & 0x0Fu));
    }
    sysex.push_back(0xF7);
    return sysex;
}

// Latin-1: each byte maps directly to the same Unicode code point, matching
// how the device's 16-byte name records are encoded (confirmed against a
// real SnapTone catalogue capture -- ASCII/Latin-1, null padded).
std::wstring latin1ToWide(const std::string& s) {
    std::wstring w;
    w.reserve(s.size());
    for (unsigned char c : s) w.push_back(static_cast<wchar_t>(c));
    return w;
}

std::vector<std::uint8_t> wideToLatin1(const std::wstring& s) {
    std::vector<std::uint8_t> bytes;
    bytes.reserve(s.size());
    for (wchar_t c : s) bytes.push_back(static_cast<std::uint8_t>(c & 0xFF));
    return bytes;
}

// Finds the (first) MIDI input/output pair whose name looks like a GP-5 or
// GP-50 SnapTone port. All device enumeration and the actual byte-level I/O
// are delegated to the platform MidiTransport; this file only ever deals in
// portable MidiDeviceDescriptor values and raw bytes.
struct DetectedPorts {
    bool inputFound = false;
    bool outputFound = false;
    MidiDeviceDescriptor input;
    MidiDeviceDescriptor output;
};

DetectedPorts detectPorts(MidiTransport& transport) {
    DetectedPorts d;
    for (const auto& dev : transport.listInputs()) {
        if (looksLikeSupportedSnapToneDevice(dev.name)) {
            d.inputFound = true;
            d.input = dev;
            break;
        }
    }
    for (const auto& dev : transport.listOutputs()) {
        if (looksLikeSupportedSnapToneDevice(dev.name)) {
            d.outputFound = true;
            d.output = dev;
            break;
        }
    }
    return d;
}

class Session {
public:
    ~Session() { close(); }

    bool open(MidiTransport& transport, const DetectedPorts& ports, std::wstring& error) {
        close();
        {
            std::lock_guard<std::mutex> lock(rxMutex_);
            ackReceived_ = false;
            completionReceived_ = false;
        }

        std::string err;
        auto onMessage = [this](const std::uint8_t* data, std::size_t size) { handleMessage(data, size); };
        if (!transport.openInput(ports.input, onMessage, err)) {
            error = ntc::fromUtf8("Cannot open SnapTone MIDI input: " + err);
            return false;
        }
        if (!transport.openOutput(ports.output, err)) {
            error = ntc::fromUtf8("Cannot open SnapTone MIDI output: " + err);
            transport.close();
            return false;
        }
        transport_ = &transport;
        return true;
    }

    bool sendSysEx(const std::vector<std::uint8_t>& bytes, std::wstring& error) {
        if (!transport_) {
            error = L"SnapTone MIDI output is not open.";
            return false;
        }
        std::string err;
        if (!transport_->sendMessage(bytes, err)) {
            error = ntc::fromUtf8(err);
            return false;
        }
        return true;
    }

    void prepareForBlock() {
        std::lock_guard<std::mutex> lock(rxMutex_);
        ackReceived_ = false;
    }

    bool waitForAck(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(rxMutex_);
        return rxCv_.wait_for(lock, timeout, [this] { return ackReceived_; });
    }

    void prepareForCompletion() {
        std::lock_guard<std::mutex> lock(rxMutex_);
        completionReceived_ = false;
    }

    bool waitForCompletion(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(rxMutex_);
        return rxCv_.wait_for(lock, timeout, [this] { return completionReceived_; });
    }

    // General-purpose capture of every successfully decoded SysEx message,
    // used by readSnapToneCatalogue below (a multi-packet read response,
    // unlike the single-purpose ack/completion tracking above which only
    // covers the fixed upload ACK/completion byte sequences). Off by default
    // so the upload path's behavior/memory use is unchanged.
    void beginCapture() {
        std::lock_guard<std::mutex> lock(rxMutex_);
        capturedMessages_.clear();
        capturing_ = true;
    }

    std::vector<std::vector<std::uint8_t>> endCapture() {
        std::lock_guard<std::mutex> lock(rxMutex_);
        capturing_ = false;
        return std::move(capturedMessages_);
    }

    std::size_t capturedCountSnapshot() {
        std::lock_guard<std::mutex> lock(rxMutex_);
        return capturedMessages_.size();
    }

    // Diagnostic only: the most recent successfully nibble-decoded SysEx
    // message, regardless of whether it matched the known ACK/completion
    // messages. Lets a completion timeout report what the device actually
    // sent last, which is useful while confirming whether GP-50 uses the
    // same final completion message as GP-5.
    std::wstring lastMessageHex() {
        std::lock_guard<std::mutex> lock(rxMutex_);
        if (lastDecoded_.empty()) return L"(none received)";
        std::wstringstream ss;
        ss << std::hex << std::uppercase << std::setfill(L'0');
        const std::size_t shown = std::min<std::size_t>(lastDecoded_.size(), 32);
        for (std::size_t i = 0; i < shown; ++i) {
            if (i) ss << L' ';
            ss << std::setw(2) << static_cast<unsigned>(lastDecoded_[i]);
        }
        if (shown < lastDecoded_.size()) ss << L" ...";
        return ss.str();
    }

private:
    void handleMessage(const std::uint8_t* bytes, std::size_t size) {
        std::vector<std::uint8_t> decoded;
        if (!nibbleDecodeSysEx(bytes, size, decoded)) return;

        static constexpr std::array<std::uint8_t, 7> ack { 0xB2,0x01,0x00,0x03,0x14,0x08,0x00 };
        static constexpr std::array<std::uint8_t, 10> completion { 0xCE,0x01,0x00,0x06,0x12,0x1B,0x03,0x00,0x00,0x00 };
        bool notify = false;
        {
            std::lock_guard<std::mutex> lock(rxMutex_);
            lastDecoded_ = decoded;
            if (capturing_) capturedMessages_.push_back(decoded);
            if (decoded.size() == ack.size() && std::equal(decoded.begin(), decoded.end(), ack.begin())) {
                ackReceived_ = true;
                notify = true;
            }
            if (decoded.size() == completion.size() && std::equal(decoded.begin(), decoded.end(), completion.begin())) {
                completionReceived_ = true;
                notify = true;
            }
        }
        if (notify) rxCv_.notify_all();
    }

    void close() {
        if (transport_) {
            transport_->close();
            transport_ = nullptr;
        }
    }

    MidiTransport* transport_ = nullptr;
    std::mutex rxMutex_;
    std::condition_variable rxCv_;
    bool ackReceived_ = false;
    bool completionReceived_ = false;
    std::vector<std::uint8_t> lastDecoded_;
    std::vector<std::vector<std::uint8_t>> capturedMessages_;
    bool capturing_ = false;
};

// Builds and sends the SnapTone delete (selector 0x27) write command,
// reverse-engineered from a real USB-MIDI capture of Valeton Suite on macOS
// against a GP-50 (2026-09-15). Decoded (post nibble-decode) layout:
//   [CRC][0x01][0x00][LEN][0x11][0x27][SLOT][0x00][0x00][0x0F]
// SLOT is the zero-based global slot index (0..79), same addressing as
// uploadCloToGp5/readSnapToneCatalogue. LEN is the count of decoded bytes
// following the LEN byte itself. Confirmed against real hardware: the
// target slot reverts to its "Empty N" placeholder name in a subsequent
// readSnapToneCatalogue call.
//
// The equivalent-looking selector 0x26 "rename" write (same shape, with a
// name payload appended) was also captured and initially assumed to be a
// working rename command, but real-hardware testing showed it does NOT
// change the name table readSnapToneCatalogue reads -- it appears to write
// to a separate, still-unidentified data structure. Renaming a SnapTone slot
// for real requires reading back the slot's live patch body and rewriting
// the whole patch with an edited name field; see renameSnapTone below, which
// ports the approach (and the 0x1D patch-write opcode) from the independent,
// hardware-verified github.com/drewmerc302/valeton-gp50 project rather than
// this selector-0x26 dead end.
bool sendSnapToneDelete(Session& session, int visibleSlot, std::wstring& error) {
    if (visibleSlot < 51 || visibleSlot > 80) {
        error = L"SnapTone slot must be in range 51-80.";
        return false;
    }
    const auto slotByte = static_cast<std::uint8_t>(visibleSlot - 1);

    std::array<std::uint8_t, 10> body{ 0x00, 0x01, 0x00, 0x00, 0x11, 0x27, slotByte, 0x00, 0x00, 0x0F };
    body[3] = static_cast<std::uint8_t>(body.size() - 4);
    body[0] = crc8Poly07(body.data() + 1, body.size() - 1);

    const auto request = nibbleEncodeSysEx(body.data(), body.size());
    return session.sendSysEx(request, error);
}

// -- Rename, via live-patch read + edit + full rewrite ----------------------
//
// Ported from github.com/drewmerc302/valeton-gp50 (webmidi_device.js /
// webmidi_write.js / prst.js), an independent project whose GP-50 write
// protocol is described there as verified against real Suite captures.
// Constants below are named the same as that project's for easy
// cross-reference. GP-50 only -- that project explicitly leaves the GP-5
// patch-write protocol unverified, and this port hasn't been tested against
// a GP-5 either, so it refuses on anything that doesn't look like a GP-50.
constexpr std::uint8_t kCatSel = 0x12;      // read-request command byte
constexpr std::uint8_t kSelBody = 0x41;     // selector: currently active patch body
constexpr std::uint8_t kPatchWriteCmd = 0x1D;
constexpr std::size_t kPatchBlockSize = 19; // payload bytes per write block
constexpr std::array<std::uint8_t, 2> kPatchHdr{ 0x11, 0x4F };
constexpr std::size_t kNameLen = 16;        // patch name field length
// GP-50 .prst layout constants (prst.js): name starts right after the fixed
// header+sentinel preamble, body right after the name.
constexpr std::size_t kPrstLen = 552;
constexpr std::size_t kBodyOff = 0x29;
constexpr std::size_t kBodyLen = kPrstLen - kBodyOff; // 511

std::vector<std::uint8_t> buildReadRequest(std::uint8_t selector) {
    std::array<std::uint8_t, 6> body{ 0x00, 0x01, 0x00, 0x02, kCatSel, selector };
    body[0] = crc8Poly07(body.data() + 1, body.size() - 1);
    return nibbleEncodeSysEx(body.data(), body.size());
}

// Sends `request`, collects every decoded reply until the stream goes idle,
// groups replies by their own decoded[1] ("cmd") byte, and returns the
// longest reassembled [4:]-payload concatenation (sorted by decoded[2],
// "index") -- port of webmidi_device.js's exchange()+reassemble(), which
// picks the longest bank rather than assuming a specific response cmd byte.
std::vector<std::uint8_t> exchangeAndReassembleLongest(Session& session,
                                                        const std::vector<std::uint8_t>& request,
                                                        std::wstring& error) {
    session.beginCapture();
    if (!session.sendSysEx(request, error)) {
        session.endCapture();
        return {};
    }
    const auto start = std::chrono::steady_clock::now();
    auto lastGrowth = start;
    std::size_t lastCount = 0;
    for (;;) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        const auto now = std::chrono::steady_clock::now();
        const std::size_t count = session.capturedCountSnapshot();
        if (count > lastCount) { lastCount = count; lastGrowth = now; }
        if (count > 0 && now - lastGrowth > std::chrono::milliseconds(400)) break;
        if (now - start > std::chrono::seconds(3)) break;
    }
    const auto messages = session.endCapture();

    std::map<std::uint8_t, std::vector<std::pair<std::uint8_t, std::vector<std::uint8_t>>>> byCmd;
    for (const auto& m : messages) {
        if (m.size() < 4) continue;
        byCmd[m[1]].emplace_back(m[2], std::vector<std::uint8_t>(m.begin() + 4, m.end()));
    }
    std::vector<std::uint8_t> best;
    for (auto& [cmd, chunks] : byCmd) {
        std::sort(chunks.begin(), chunks.end(),
                  [](const auto& a, const auto& b) { return a.first < b.first; });
        std::vector<std::uint8_t> blob;
        for (const auto& [index, payload] : chunks) blob.insert(blob.end(), payload.begin(), payload.end());
        if (blob.size() > best.size()) best = std::move(blob);
    }
    return best;
}

std::vector<std::uint8_t> buildPatchWritePacket(std::uint8_t index, const std::uint8_t* payload, std::size_t len) {
    std::vector<std::uint8_t> buf;
    buf.reserve(4 + len);
    buf.push_back(0x00); // CRC placeholder
    buf.push_back(kPatchWriteCmd);
    buf.push_back(index);
    buf.push_back(static_cast<std::uint8_t>(len));
    buf.insert(buf.end(), payload, payload + len);
    buf[0] = crc8Poly07(buf.data() + 1, buf.size() - 1);
    return nibbleEncodeSysEx(buf.data(), buf.size());
}

} // namespace

MidiDetection detectGp5Midi() {
    auto transport = createMidiTransport();
    const auto ports = detectPorts(*transport);
    MidiDetection d;
    d.inputFound = ports.inputFound;
    d.outputFound = ports.outputFound;
    d.inputId = static_cast<unsigned int>(ports.input.nativeId);
    d.outputId = static_cast<unsigned int>(ports.output.nativeId);
    d.inputName = ntc::fromUtf8(ports.input.name);
    d.outputName = ntc::fromUtf8(ports.output.name);
    return d;
}

std::wstring describeDetection(const MidiDetection& d) {
    if (d.inputFound && d.outputFound) {
        if (d.inputName == d.outputName) return L"Detected SnapTone device: " + d.outputName;
        return L"Detected IN: " + d.inputName + L" | OUT: " + d.outputName;
    }
    if (d.inputFound) return L"SnapTone MIDI input found, but MIDI output is missing.";
    if (d.outputFound) return L"SnapTone MIDI output found, but MIDI input is missing.";
    return L"GP-5 / GP-50 MIDI not detected. Connect the pedal and press Rescan.";
}

UploadResult uploadCloToGp5(const std::filesystem::path& cloFile,
                            int slot,
                            UploadProgress progress) {
    CloUploadData data;
    std::wstring error;
    if (!buildCloUpload(cloFile, slot, data, error))
        return { false, L"Upload failed: " + error };

    auto transport = createMidiTransport();
    const auto ports = detectPorts(*transport);
    if (!ports.inputFound || !ports.outputFound) {
        MidiDetection detection;
        detection.inputFound = ports.inputFound;
        detection.outputFound = ports.outputFound;
        detection.inputName = ntc::fromUtf8(ports.input.name);
        detection.outputName = ntc::fromUtf8(ports.output.name);
        return { false, L"Upload failed: " + describeDetection(detection) };
    }

    Session session;
    if (!session.open(*transport, ports, error))
        return { false, L"Upload failed: " + error };

    const int total = static_cast<int>(data.chunks.size());
    session.prepareForCompletion();

    for (int i = 0; i < total; ++i) {
        constexpr int maxAttempts = 3;
        bool acknowledged = false;
        for (int attempt = 1; attempt <= maxAttempts && !acknowledged; ++attempt) {
            session.prepareForBlock();
            if (!session.sendSysEx(data.chunks[static_cast<std::size_t>(i)], error))
                return { false, L"Upload failed: " + error };

            acknowledged = session.waitForAck(std::chrono::milliseconds(800));
            if (!acknowledged && progress) {
                std::wstringstream ss;
                ss << L"No ACK for block " << (i + 1) << L"; retry " << attempt << L" / " << maxAttempts << L"...";
                progress(i, total, ss.str());
            }
        }
        if (!acknowledged) {
            std::wstringstream ss;
            ss << L"Upload failed: SnapTone ACK timeout at block " << (i + 1) << L" / " << total << L".";
            return { false, ss.str() };
        }

        if (progress) {
            std::wstringstream ss;
            ss << L"Uploading SnapTone block " << (i + 1) << L" / " << total << L"...";
            progress(i + 1, total, ss.str());
        }
        // Captures advance after the ACK with only a very small gap.
        if (i + 1 < total) std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }

    if (progress) progress(total, total, L"Waiting for SnapTone final confirmation...");
    if (!session.waitForCompletion(std::chrono::milliseconds(2000))) {
        std::wstringstream ss;
        ss << L"All blocks were acknowledged, but the SnapTone final confirmation timed out. "
           << L"Last received message: " << session.lastMessageHex();
        return { false, ss.str() };
    }

    return { true, L"SnapTone upload completed successfully." };
}

bool readSnapToneCatalogue(std::vector<SnapToneCatalogueEntry>& entries, std::wstring& error) {
    entries.clear();
    error.clear();

    auto transport = createMidiTransport();
    const auto ports = detectPorts(*transport);
    if (!ports.inputFound || !ports.outputFound) {
        MidiDetection detection;
        detection.inputFound = ports.inputFound;
        detection.outputFound = ports.outputFound;
        detection.inputName = ntc::fromUtf8(ports.input.name);
        detection.outputName = ntc::fromUtf8(ports.output.name);
        error = describeDetection(detection);
        return false;
    }

    Session session;
    if (!session.open(*transport, ports, error)) return false;

    // Decoded request: [CRC][0x01][0x00][0x02][0x12][0x24] -- the existing
    // read envelope [CRC, 0x01, 0x00, length, 0x12, selector], selector 0x24
    // for the SnapTone/amp catalogue. CRC is computed over the 5 bytes after
    // the placeholder, same convention as the upload path's transfer frames.
    std::array<std::uint8_t, 6> body{ 0x00, 0x01, 0x00, 0x02, 0x12, 0x24 };
    body[0] = crc8Poly07(body.data() + 1, body.size() - 1);
    const auto request = nibbleEncodeSysEx(body.data(), body.size());

    session.beginCapture();
    if (!session.sendSysEx(request, error)) {
        session.endCapture();
        return false;
    }

    // The catalogue arrives as several packets; collect until the response
    // stream goes idle rather than assuming a fixed packet count -- mirrors
    // the timing this command was captured/verified against: up to 3s total,
    // stop 400ms after the last new packet once at least one has arrived.
    const auto start = std::chrono::steady_clock::now();
    auto lastGrowth = start;
    std::size_t lastCount = 0;
    for (;;) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        const auto now = std::chrono::steady_clock::now();
        const std::size_t count = session.capturedCountSnapshot();
        if (count > lastCount) {
            lastCount = count;
            lastGrowth = now;
        }
        if (count > 0 && now - lastGrowth > std::chrono::milliseconds(400)) break;
        if (now - start > std::chrono::seconds(3)) break;
    }
    const auto messages = session.endCapture();

    // Decoded response packets are [CRC][CMD][INDEX][LENGTH][PAYLOAD...].
    // The catalogue response uses CMD=0x48; sort by INDEX and concatenate
    // payloads to reassemble the full blob.
    constexpr std::uint8_t catalogueCmd = 0x48;
    std::vector<std::pair<std::uint8_t, std::vector<std::uint8_t>>> chunks;
    for (const auto& m : messages) {
        if (m.size() < 4 || m[1] != catalogueCmd) continue;
        chunks.emplace_back(m[2], std::vector<std::uint8_t>(m.begin() + 4, m.end()));
    }
    if (chunks.empty()) {
        error = L"No SnapTone catalogue response from the device (command 0x48). "
                L"Make sure a GP-5/GP-50 is connected and not mid-transfer.";
        return false;
    }
    std::sort(chunks.begin(), chunks.end(),
              [](const auto& a, const auto& b) { return a.first < b.first; });

    std::vector<std::uint8_t> blob;
    for (const auto& [index, payload] : chunks) blob.insert(blob.end(), payload.begin(), payload.end());

    // offset 0..1: selector/header, offset 2..81: 80-byte occupancy table,
    // offset 82..: 80 x 16-byte null-padded name records.
    constexpr std::size_t nameStart = 82;
    constexpr std::size_t recordSize = 16;
    constexpr std::size_t expectedBytes = nameStart + 80 * recordSize;
    if (blob.size() < expectedBytes) {
        std::wstringstream ss;
        ss << L"SnapTone catalogue response was incomplete (" << blob.size()
           << L" of " << expectedBytes << L" bytes). Try Rescan again.";
        error = ss.str();
        return false;
    }

    // Only the 30 user slots (index 50..79 => visible SnapTone 51..80) are
    // exposed, matching the range this app's uploader already targets.
    for (int index = 50; index < 80; ++index) {
        const std::size_t off = nameStart + static_cast<std::size_t>(index) * recordSize;
        std::string raw(blob.begin() + static_cast<std::ptrdiff_t>(off),
                        blob.begin() + static_cast<std::ptrdiff_t>(off + recordSize));
        const auto nul = raw.find('\0');
        if (nul != std::string::npos) raw.resize(nul);
        while (!raw.empty() && (raw.back() == ' ' || raw.back() == '\t')) raw.pop_back();

        SnapToneCatalogueEntry entry;
        entry.visibleSlot = index + 1;
        entry.name = latin1ToWide(raw);
        entries.push_back(entry);
    }

    return true;
}

// -- EXPERIMENTAL: selector 0x26 write + a suspected commit/flush companion -
//
// Every captured selector-0x26 "rename" write (both Suite's own automatic
// ones and manual tests) is immediately followed by this exact 16-byte
// message, which prior analysis dismissed as an unrelated reconnect
// handshake -- it never appears after a plain catalogue read, only after a
// 0x26 write:
//   F0 00 0A 00 02 00 01 00 03 00 00 00 00 00 00 F7
// Decoded: [CRC=0x0A][0x02][0x01][LEN=0x03][0x00,0x00,0x00] -- a distinct
// shape from both the read-request envelope and the write envelope used
// elsewhere in this file, with famByte 0x02 matching the rename write's own
// famByte. Worth testing as a possible commit/flush step the 0x26 write
// needs before the device applies it to the table readSnapToneCatalogue
// reads. UNCONFIRMED -- test on an unused slot before trusting this.
bool sendSnapToneRenameWithCommitAttempt(int visibleSlot, const std::wstring& newName, std::wstring& error) {
    error.clear();
    if (newName.empty() || newName.size() > 13) {
        error = L"SnapTone name must be 1-13 characters for this experimental path.";
        return false;
    }
    if (visibleSlot < 51 || visibleSlot > 80) {
        error = L"SnapTone slot must be in range 51-80.";
        return false;
    }
    const auto slotByte = static_cast<std::uint8_t>(visibleSlot - 1);

    auto transport = createMidiTransport();
    const auto ports = detectPorts(*transport);
    if (!ports.inputFound || !ports.outputFound) {
        MidiDetection detection;
        detection.inputFound = ports.inputFound;
        detection.outputFound = ports.outputFound;
        detection.inputName = ntc::fromUtf8(ports.input.name);
        detection.outputName = ntc::fromUtf8(ports.output.name);
        error = describeDetection(detection);
        return false;
    }

    Session session;
    if (!session.open(*transport, ports, error)) return false;

    // Replay the exact read-request sequence Suite always does before a
    // rename write, in case the device needs this context established
    // first: selectors 0x30, 0x40, 0x41, 0x20, 0x24, 0x1A, 0x1C, captured
    // verbatim from a real Suite session (2026-09-15/16). An isolated
    // write+commit pair (no preceding reads) was tested and confirmed NOT
    // to change the catalogue, even read back within the same session --
    // this sequence is the next thing to rule in or out.
    for (std::uint8_t selector : { 0x30u, 0x40u, 0x41u, 0x20u, 0x24u, 0x1Au, 0x1Cu }) {
        std::wstring ignoredError;
        session.sendSysEx(buildReadRequest(selector), ignoredError);
        std::this_thread::sleep_for(std::chrono::milliseconds(150));
    }

    // The 0x26 write itself -- same shape as sendSnapToneDelete's 0x27, with
    // a fixed 13-byte name field (confirmed via two real captures of
    // different name lengths that both totaled exactly 13 name-field bytes).
    auto nameBytes = wideToLatin1(newName);
    nameBytes.resize(13, 0x00);

    std::vector<std::uint8_t> body{ 0x00, 0x02, 0x00, 0x00, 0x11, 0x26, slotByte, 0x00, 0x00, 0x0F };
    body.insert(body.end(), nameBytes.begin(), nameBytes.end());
    body[3] = static_cast<std::uint8_t>(body.size() - 4);
    body[0] = crc8Poly07(body.data() + 1, body.size() - 1);
    if (!session.sendSysEx(nibbleEncodeSysEx(body.data(), body.size()), error)) return false;

    std::this_thread::sleep_for(std::chrono::milliseconds(50));

    // The suspected commit/flush companion.
    std::array<std::uint8_t, 7> commit{ 0x00, 0x02, 0x01, 0x03, 0x00, 0x00, 0x00 };
    commit[0] = crc8Poly07(commit.data() + 1, commit.size() - 1);
    if (!session.sendSysEx(nibbleEncodeSysEx(commit.data(), commit.size()), error)) return false;

    std::this_thread::sleep_for(std::chrono::milliseconds(300));

    // Diagnostic: re-read the catalogue in this SAME session (matching how
    // Suite's own write+read happen in one continuous connection) rather
    // than a fresh reconnect, to isolate whether the earlier "no change"
    // result was a same-session-vs-reconnect timing issue.
    const auto catalogueRequest = buildReadRequest(0x24);
    const auto blob = exchangeAndReassembleLongest(session, catalogueRequest, error);
    constexpr std::size_t nameStart = 82;
    constexpr std::size_t recordSize = 16;
    const std::size_t off = nameStart + static_cast<std::size_t>(slotByte) * recordSize;
    if (blob.size() >= off + recordSize) {
        std::string raw(blob.begin() + static_cast<std::ptrdiff_t>(off),
                        blob.begin() + static_cast<std::ptrdiff_t>(off + recordSize));
        const auto nul = raw.find('\0');
        if (nul != std::string::npos) raw.resize(nul);
        error = L"[diagnostic] same-session catalogue re-read for this slot: \"" + latin1ToWide(raw) + L"\"";
    } else {
        error = L"[diagnostic] same-session catalogue re-read came back too short to check.";
    }
    return true;
}

bool renameSnapTone(int visibleSlot, const std::wstring& newName, std::wstring& error) {
    error.clear();
    // DISABLED (2026-09-15): real-hardware testing traced this all the way
    // through -- Program Change addressing fixed, slot-byte addressing fixed
    // (see writeSlotByte below) -- and confirmed the 0x1D command writes to a
    // SEPARATE "Patch" storage area (1-80), NOT the SnapTone storage (51-80)
    // that readSnapToneCatalogue/uploadCloToGp5 (0x92) read and write. A
    // corrected-addressing test wrote "SafeTest" to Patch slot 80
    // successfully (visible on-device), while SnapTone slot 80's own name
    // was completely unaffected. So this whole 0x41-read + 0x1D-write
    // mechanism, ported from github.com/drewmerc302/valeton-gp50, can only
    // ever rename Patches, not SnapTones, on this hardware -- it is not a
    // dead end from a bug, it is the wrong target entirely.
    //
    // A real SnapTone rename needs one of:
    //  (a) a dedicated SnapTone name-write opcode, still unidentified (0x26
    //      was an early guess, also confirmed wrong -- see sendSnapToneDelete
    //      above for what *is* confirmed: 0x27 deletes a SnapTone correctly);
    //  (b) a "read SnapTone tone binary" command (readSnapToneCatalogue only
    //      reads names/occupancy, not tone data) paired with the
    //      already-working uploadCloToGp5 (0x92) to re-upload the same tone
    //      under a new name.
    // Needs a fresh, SnapTone-specific capture (not a Patch-list capture) to
    // find either. Do not re-enable by repurposing the code below -- it is
    // kept only as a reference for the (functioning, just wrong-target)
    // Patch-write mechanism, in case that's useful for a future Patch-list
    // feature.
    error = L"SnapTone rename is not implemented yet -- see the comment on "
             L"this function for what's confirmed and what's still needed.";
    return false;
    if (newName.empty() || newName.size() > kNameLen) {
        error = L"SnapTone name must be 1-16 characters.";
        return false;
    }
    if (visibleSlot < 51 || visibleSlot > 80) {
        error = L"SnapTone slot must be in range 51-80.";
        return false;
    }
    // Program Change follows ordinary 0-based MIDI addressing (confirmed
    // against the catalogue/0x92-upload convention used elsewhere in this
    // file). The 0x1D write's own slot field is a DIFFERENT, direct 1:1
    // mapping to the on-screen patch/slot number -- confirmed the hard way
    // on 2026-09-15: visibleSlot-1 (matching every other command here) put
    // "diagtest" on the device's actual displayed patch 50, not SnapTone 51,
    // when 51-1=50 was sent as the write's slot byte. So the write's slot
    // byte must be visibleSlot itself, not visibleSlot-1.
    const auto programChangeSlot = static_cast<std::uint8_t>(visibleSlot - 1);
    const auto writeSlotByte = static_cast<std::uint8_t>(visibleSlot);

    auto transport = createMidiTransport();
    const auto ports = detectPorts(*transport);
    if (!ports.inputFound || !ports.outputFound) {
        MidiDetection detection;
        detection.inputFound = ports.inputFound;
        detection.outputFound = ports.outputFound;
        detection.inputName = ntc::fromUtf8(ports.input.name);
        detection.outputName = ntc::fromUtf8(ports.output.name);
        error = describeDetection(detection);
        return false;
    }
    // Only GP-50's patch-write protocol is verified (by the ported project);
    // refuse on anything else rather than risk wedging an unverified device.
    if (lower(ports.output.name).find("50") == std::string::npos) {
        error = L"Rename requires a GP-50 -- the live patch read/rewrite this "
                 L"uses is only verified for GP-50, not GP-5.";
        return false;
    }

    Session session;
    if (!session.open(*transport, ports, error)) return false;

    // 1. Select the slot with a plain Program Change so the device's "active
    // patch" (what selector 0x41 reads) is the one we're about to rename.
    if (!session.sendSysEx({ 0xC0, static_cast<std::uint8_t>(programChangeSlot & 0x7F) }, error)) return false;
    std::this_thread::sleep_for(std::chrono::milliseconds(300));

    // 2. Read the live patch body back so the rewrite carries the slot's
    // actual current tone data -- this command only edits the name field.
    const auto bodyRequest = buildReadRequest(kSelBody);
    auto body = exchangeAndReassembleLongest(session, bodyRequest, error);
    // The reassembled blob leads with a 2-byte [CATSEL, selector] echo before
    // the actual body bytes; strip it, mirroring webmidi_device.js's strip().
    if (body.size() >= 2 && body[0] == kCatSel && body[1] == kSelBody) {
        body.erase(body.begin(), body.begin() + 2);
    }
    if (body.size() != kBodyLen) {
        std::wstringstream ss;
        ss << L"Could not read the slot's current patch body (" << body.size()
           << L" of " << kBodyLen << L" bytes) -- refusing to rewrite it blind.";
        error = ss.str();
        return false;
    }

    // 3. Rebuild [name(16, edited) + body] and send it back as a patch write
    // (cmd 0x1D), 19 payload bytes per block, same CRC-8/nibble framing as
    // the rest of this file.
    std::vector<std::uint8_t> nameBytes = wideToLatin1(newName);
    nameBytes.resize(kNameLen, 0x00);

    std::vector<std::uint8_t> payload;
    payload.reserve(kPatchHdr.size() + 4 + nameBytes.size() + body.size());
    payload.insert(payload.end(), kPatchHdr.begin(), kPatchHdr.end());
    payload.push_back(writeSlotByte);
    payload.push_back(0x00);
    payload.push_back(0x00);
    payload.push_back(0x00);
    payload.insert(payload.end(), nameBytes.begin(), nameBytes.end());
    payload.insert(payload.end(), body.begin(), body.end());

    const int totalBlocks = static_cast<int>((payload.size() + kPatchBlockSize - 1) / kPatchBlockSize);
    for (int i = 0; i < totalBlocks; ++i) {
        const std::size_t offset = static_cast<std::size_t>(i) * kPatchBlockSize;
        const std::size_t len = std::min(kPatchBlockSize, payload.size() - offset);
        const auto packet = buildPatchWritePacket(static_cast<std::uint8_t>(i), payload.data() + offset, len);

        if (!session.sendSysEx(packet, error)) return false;
        // No confirmed ACK byte pattern for this command family (unlike the
        // 0x92 CLO-transfer path's ack/completion bytes) -- a fixed pacing
        // delay per block, mirroring the ported project's own best-effort
        // approach, rather than a real ack wait.
        std::this_thread::sleep_for(std::chrono::milliseconds(150));
    }

    std::this_thread::sleep_for(std::chrono::milliseconds(300));
    return true;
}

bool deleteSnapTone(int visibleSlot, std::wstring& error) {
    error.clear();

    auto transport = createMidiTransport();
    const auto ports = detectPorts(*transport);
    if (!ports.inputFound || !ports.outputFound) {
        MidiDetection detection;
        detection.inputFound = ports.inputFound;
        detection.outputFound = ports.outputFound;
        detection.inputName = ntc::fromUtf8(ports.input.name);
        detection.outputName = ntc::fromUtf8(ports.output.name);
        error = describeDetection(detection);
        return false;
    }

    Session session;
    if (!session.open(*transport, ports, error)) return false;

    if (!sendSnapToneDelete(session, visibleSlot, error)) return false;

    std::this_thread::sleep_for(std::chrono::milliseconds(150));
    return true;
}

} // namespace ntc::gp5
