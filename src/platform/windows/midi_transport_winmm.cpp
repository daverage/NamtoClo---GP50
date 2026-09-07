#include "midi_transport.hpp"

#include <windows.h>
#include <mmsystem.h>

#include <array>
#include <chrono>
#include <cstring>
#include <thread>
#include <vector>

namespace ntc {
namespace {

std::string mmError(MMRESULT code, bool input) {
    wchar_t text[256]{};
    const MMRESULT r = input
        ? midiInGetErrorTextW(code, text, static_cast<UINT>(std::size(text)))
        : midiOutGetErrorTextW(code, text, static_cast<UINT>(std::size(text)));
    std::wstring w = (r == MMSYSERR_NOERROR) ? std::wstring(text) : (L"MIDI error " + std::to_wstring(code));
    // Best-effort ASCII fold; WinMM device error strings are ASCII in
    // practice, and this class only needs to surface something readable.
    std::string out;
    out.reserve(w.size());
    for (wchar_t c : w) out.push_back(c < 128 ? static_cast<char>(c) : '?');
    return out;
}

class WinMmMidiTransport final : public MidiTransport {
public:
    ~WinMmMidiTransport() override { close(); }

    std::vector<MidiDeviceDescriptor> listInputs() override {
        std::vector<MidiDeviceDescriptor> out;
        for (UINT i = 0; i < midiInGetNumDevs(); ++i) {
            MIDIINCAPSW caps{};
            if (midiInGetDevCapsW(i, &caps, sizeof(caps)) != MMSYSERR_NOERROR) continue;
            out.push_back(descriptorFromWide(caps.szPname, i));
        }
        return out;
    }

    std::vector<MidiDeviceDescriptor> listOutputs() override {
        std::vector<MidiDeviceDescriptor> out;
        for (UINT i = 0; i < midiOutGetNumDevs(); ++i) {
            MIDIOUTCAPSW caps{};
            if (midiOutGetDevCapsW(i, &caps, sizeof(caps)) != MMSYSERR_NOERROR) continue;
            out.push_back(descriptorFromWide(caps.szPname, i));
        }
        return out;
    }

    bool openInput(const MidiDeviceDescriptor& device, MidiMessageCallback onMessage, std::string& error) override {
        onMessage_ = std::move(onMessage);
        MMRESULT r = midiInOpen(&midiIn_, static_cast<UINT>(device.nativeId),
                                reinterpret_cast<DWORD_PTR>(&WinMmMidiTransport::midiInCallback),
                                reinterpret_cast<DWORD_PTR>(this), CALLBACK_FUNCTION);
        if (r != MMSYSERR_NOERROR) {
            error = "Cannot open MIDI input: " + mmError(r, true);
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
                error = "Cannot prepare MIDI input buffer: " + mmError(r, true);
                closeInput();
                return false;
            }
            preparedInputs_ = i + 1;
            r = midiInAddBuffer(midiIn_, &h, sizeof(h));
            if (r != MMSYSERR_NOERROR) {
                error = "Cannot queue MIDI input buffer: " + mmError(r, true);
                closeInput();
                return false;
            }
        }

        r = midiInStart(midiIn_);
        if (r != MMSYSERR_NOERROR) {
            error = "Cannot start MIDI input: " + mmError(r, true);
            closeInput();
            return false;
        }
        return true;
    }

    bool openOutput(const MidiDeviceDescriptor& device, std::string& error) override {
        MMRESULT r = midiOutOpen(&midiOut_, static_cast<UINT>(device.nativeId), 0, 0, CALLBACK_NULL);
        if (r != MMSYSERR_NOERROR) {
            error = "Cannot open MIDI output: " + mmError(r, false);
            midiOut_ = nullptr;
            return false;
        }
        return true;
    }

    bool sendMessage(const std::vector<std::uint8_t>& bytes, std::string& error) override {
        if (!midiOut_ || bytes.empty()) {
            error = "MIDI output is not open.";
            return false;
        }

        MIDIHDR hdr{};
        hdr.lpData = reinterpret_cast<LPSTR>(const_cast<std::uint8_t*>(bytes.data()));
        hdr.dwBufferLength = static_cast<DWORD>(bytes.size());

        MMRESULT r = midiOutPrepareHeader(midiOut_, &hdr, sizeof(hdr));
        if (r != MMSYSERR_NOERROR) {
            error = "Cannot prepare MIDI SysEx: " + mmError(r, false);
            return false;
        }

        r = midiOutLongMsg(midiOut_, &hdr, sizeof(hdr));
        if (r != MMSYSERR_NOERROR) {
            midiOutUnprepareHeader(midiOut_, &hdr, sizeof(hdr));
            error = "Cannot send MIDI SysEx: " + mmError(r, false);
            return false;
        }

        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
        while ((hdr.dwFlags & MHDR_DONE) == 0) {
            if (std::chrono::steady_clock::now() >= deadline) {
                midiOutReset(midiOut_);
                midiOutUnprepareHeader(midiOut_, &hdr, sizeof(hdr));
                error = "Timed out while sending MIDI SysEx.";
                return false;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }

        midiOutUnprepareHeader(midiOut_, &hdr, sizeof(hdr));
        return true;
    }

    void close() override {
        closeOutput();
        closeInput();
    }

private:
    static MidiDeviceDescriptor descriptorFromWide(const wchar_t* name, UINT id) {
        MidiDeviceDescriptor d;
        d.nativeId = id;
        // WinMM device caps names are already narrow-representable ASCII in
        // practice on the devices this app targets; a full UTF-16 -> UTF-8
        // conversion would need platform.hpp's toUtf8, which is fine to use
        // here since this file already links against Win32.
        std::wstring w(name);
        d.name.reserve(w.size());
        for (wchar_t c : w) {
            if (c == 0) break;
            d.name.push_back(c < 128 ? static_cast<char>(c) : '?');
        }
        return d;
    }

    static void CALLBACK midiInCallback(HMIDIIN, UINT msg, DWORD_PTR instance, DWORD_PTR param1, DWORD_PTR) {
        if (msg != MIM_LONGDATA || instance == 0 || param1 == 0) return;
        auto* self = reinterpret_cast<WinMmMidiTransport*>(instance);
        self->handleLongData(reinterpret_cast<MIDIHDR*>(param1));
    }

    void handleLongData(MIDIHDR* hdr) {
        if (!hdr) return;
        if (hdr->dwBytesRecorded > 0 && onMessage_) {
            onMessage_(reinterpret_cast<const std::uint8_t*>(hdr->lpData),
                      static_cast<std::size_t>(hdr->dwBytesRecorded));
        }
        if (midiIn_ && !closing_) {
            hdr->dwBytesRecorded = 0;
            midiInAddBuffer(midiIn_, hdr, sizeof(*hdr));
        }
    }

    void closeOutput() {
        if (midiOut_) {
            midiOutReset(midiOut_);
            midiOutClose(midiOut_);
            midiOut_ = nullptr;
        }
    }

    void closeInput() {
        closing_ = true;
        if (midiIn_) {
            midiInStop(midiIn_);
            midiInReset(midiIn_);
            for (std::size_t i = 0; i < preparedInputs_; ++i)
                midiInUnprepareHeader(midiIn_, &inputHeaders_[i], sizeof(MIDIHDR));
            midiInClose(midiIn_);
            midiIn_ = nullptr;
            preparedInputs_ = 0;
        }
        closing_ = false;
    }

    HMIDIIN midiIn_ = nullptr;
    HMIDIOUT midiOut_ = nullptr;
    std::array<std::vector<std::uint8_t>, 4> inputBuffers_;
    std::array<MIDIHDR, 4> inputHeaders_{};
    std::size_t preparedInputs_ = 0;
    MidiMessageCallback onMessage_;
    volatile bool closing_ = false;
};

} // namespace

std::unique_ptr<MidiTransport> createMidiTransport() {
    return std::make_unique<WinMmMidiTransport>();
}

} // namespace ntc
