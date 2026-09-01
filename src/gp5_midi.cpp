#include "gp5_midi.hpp"

#include <windows.h>
#include <mmsystem.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cwctype>
#include <iomanip>
#include <mutex>
#include <sstream>
#include <thread>
#include <utility>
#include <vector>

namespace ntc::gp5 {
namespace {

std::wstring lower(std::wstring s) {
    std::transform(s.begin(), s.end(), s.begin(), [](wchar_t c) {
        return static_cast<wchar_t>(std::towlower(c));
    });
    return s;
}

// GP-50 shares the GP-5 SnapTone file/model format and write protocol, so a
// single detector accepts both. Note "gp-5"/"gp5" already match "gp-50"/
// "gp50" as substrings; the explicit checks below just make that intent
// visible rather than relying on the coincidence.
bool looksLikeSupportedSnapToneDevice(const std::wstring& name) {
    const auto n = lower(name);
    return n.find(L"gp-5") != std::wstring::npos
        || n.find(L"gp5") != std::wstring::npos
        || n.find(L"gp-50") != std::wstring::npos
        || n.find(L"gp50") != std::wstring::npos;
}

std::wstring mmError(MMRESULT code, bool input) {
    wchar_t text[256]{};
    const MMRESULT r = input
        ? midiInGetErrorTextW(code, text, static_cast<UINT>(std::size(text)))
        : midiOutGetErrorTextW(code, text, static_cast<UINT>(std::size(text)));
    if (r == MMSYSERR_NOERROR) return text;
    return L"MIDI error " + std::to_wstring(code);
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

class MidiSession {
public:
    ~MidiSession() { close(); }

    bool open(const MidiDetection& d, std::wstring& error) {
        close();
        closing_.store(false);
        {
            std::lock_guard<std::mutex> lock(rxMutex_);
            ackReceived_ = false;
            completionReceived_ = false;
        }

        MMRESULT r = midiInOpen(&midiIn_, d.inputId,
                                reinterpret_cast<DWORD_PTR>(&MidiSession::midiInCallback),
                                reinterpret_cast<DWORD_PTR>(this), CALLBACK_FUNCTION);
        if (r != MMSYSERR_NOERROR) {
            error = L"Cannot open SnapTone MIDI input: " + mmError(r, true);
            midiIn_ = nullptr;
            return false;
        }

        for (auto& b : inputBuffers_) b.resize(2048);
        for (std::size_t i = 0; i < inputHeaders_.size(); ++i) {
            auto& h = inputHeaders_[i];
            h = {};
            h.lpData = reinterpret_cast<LPSTR>(inputBuffers_[i].data());
            h.dwBufferLength = static_cast<DWORD>(inputBuffers_[i].size());
            r = midiInPrepareHeader(midiIn_, &h, sizeof(h));
            if (r != MMSYSERR_NOERROR) {
                error = L"Cannot prepare SnapTone MIDI input buffer: " + mmError(r, true);
                close();
                return false;
            }
            preparedInputs_ = i + 1;
            r = midiInAddBuffer(midiIn_, &h, sizeof(h));
            if (r != MMSYSERR_NOERROR) {
                error = L"Cannot queue SnapTone MIDI input buffer: " + mmError(r, true);
                close();
                return false;
            }
        }

        r = midiInStart(midiIn_);
        if (r != MMSYSERR_NOERROR) {
            error = L"Cannot start SnapTone MIDI input: " + mmError(r, true);
            close();
            return false;
        }

        r = midiOutOpen(&midiOut_, d.outputId, 0, 0, CALLBACK_NULL);
        if (r != MMSYSERR_NOERROR) {
            error = L"Cannot open SnapTone MIDI output: " + mmError(r, false);
            midiOut_ = nullptr;
            close();
            return false;
        }
        return true;
    }

    bool sendSysEx(const std::vector<std::uint8_t>& bytes, std::wstring& error) {
        if (!midiOut_ || bytes.empty()) {
            error = L"SnapTone MIDI output is not open.";
            return false;
        }

        MIDIHDR hdr{};
        hdr.lpData = reinterpret_cast<LPSTR>(const_cast<std::uint8_t*>(bytes.data()));
        hdr.dwBufferLength = static_cast<DWORD>(bytes.size());

        MMRESULT r = midiOutPrepareHeader(midiOut_, &hdr, sizeof(hdr));
        if (r != MMSYSERR_NOERROR) {
            error = L"Cannot prepare SnapTone MIDI SysEx: " + mmError(r, false);
            return false;
        }

        r = midiOutLongMsg(midiOut_, &hdr, sizeof(hdr));
        if (r != MMSYSERR_NOERROR) {
            midiOutUnprepareHeader(midiOut_, &hdr, sizeof(hdr));
            error = L"Cannot send SnapTone MIDI SysEx: " + mmError(r, false);
            return false;
        }

        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
        while ((hdr.dwFlags & MHDR_DONE) == 0) {
            if (std::chrono::steady_clock::now() >= deadline) {
                midiOutReset(midiOut_);
                midiOutUnprepareHeader(midiOut_, &hdr, sizeof(hdr));
                error = L"Timed out while sending SnapTone MIDI SysEx.";
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }

        midiOutUnprepareHeader(midiOut_, &hdr, sizeof(hdr));
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
    static void CALLBACK midiInCallback(HMIDIIN, UINT msg, DWORD_PTR instance, DWORD_PTR param1, DWORD_PTR) {
        if (msg != MIM_LONGDATA || instance == 0 || param1 == 0) return;
        auto* self = reinterpret_cast<MidiSession*>(instance);
        self->handleLongData(reinterpret_cast<MIDIHDR*>(param1));
    }

    void handleLongData(MIDIHDR* hdr) {
        if (!hdr) return;
        if (hdr->dwBytesRecorded > 0) {
            const auto* bytes = reinterpret_cast<const std::uint8_t*>(hdr->lpData);
            std::vector<std::uint8_t> decoded;
            if (nibbleDecodeSysEx(bytes, static_cast<std::size_t>(hdr->dwBytesRecorded), decoded)) {
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
        }

        if (midiIn_ && !closing_.load()) {
            hdr->dwBytesRecorded = 0;
            midiInAddBuffer(midiIn_, hdr, sizeof(*hdr));
        }
    }

    void close() {
        closing_.store(true);
        if (midiOut_) {
            midiOutReset(midiOut_);
            midiOutClose(midiOut_);
            midiOut_ = nullptr;
        }
        if (midiIn_) {
            midiInStop(midiIn_);
            midiInReset(midiIn_);
            for (std::size_t i = 0; i < preparedInputs_; ++i)
                midiInUnprepareHeader(midiIn_, &inputHeaders_[i], sizeof(MIDIHDR));
            midiInClose(midiIn_);
            midiIn_ = nullptr;
            preparedInputs_ = 0;
        }
    }

    HMIDIIN midiIn_ = nullptr;
    HMIDIOUT midiOut_ = nullptr;
    std::array<std::vector<std::uint8_t>, 4> inputBuffers_;
    std::array<MIDIHDR, 4> inputHeaders_{};
    std::size_t preparedInputs_ = 0;
    std::mutex rxMutex_;
    std::condition_variable rxCv_;
    bool ackReceived_ = false;
    bool completionReceived_ = false;
    std::vector<std::uint8_t> lastDecoded_;
    std::vector<std::vector<std::uint8_t>> capturedMessages_;
    bool capturing_ = false;
    std::atomic<bool> closing_{false};
};

} // namespace

MidiDetection detectGp5Midi() {
    MidiDetection d;
    for (UINT i = 0; i < midiInGetNumDevs(); ++i) {
        MIDIINCAPSW caps{};
        if (midiInGetDevCapsW(i, &caps, sizeof(caps)) == MMSYSERR_NOERROR && looksLikeSupportedSnapToneDevice(caps.szPname)) {
            d.inputFound = true;
            d.inputId = i;
            d.inputName = caps.szPname;
            break;
        }
    }
    for (UINT i = 0; i < midiOutGetNumDevs(); ++i) {
        MIDIOUTCAPSW caps{};
        if (midiOutGetDevCapsW(i, &caps, sizeof(caps)) == MMSYSERR_NOERROR && looksLikeSupportedSnapToneDevice(caps.szPname)) {
            d.outputFound = true;
            d.outputId = i;
            d.outputName = caps.szPname;
            break;
        }
    }
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

    const auto detection = detectGp5Midi();
    if (!detection.inputFound || !detection.outputFound)
        return { false, L"Upload failed: " + describeDetection(detection) };

    MidiSession session;
    if (!session.open(detection, error))
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

    const auto detection = detectGp5Midi();
    if (!detection.inputFound || !detection.outputFound) {
        error = describeDetection(detection);
        return false;
    }

    MidiSession session;
    if (!session.open(detection, error)) return false;

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

} // namespace ntc::gp5
