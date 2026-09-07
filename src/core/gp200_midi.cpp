#include "gp200_midi.hpp"

#include "midi_transport.hpp"
#include "platform.hpp"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <sstream>
#include <thread>

namespace ntc::gp200 {
namespace {

std::string lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

bool looksLikeGp200(const std::string& name) {
    const auto n = lower(name);
    // Keep the historical Valeton fallback, but never claim a GP-5 port in
    // the GP-200 tab now that both devices are supported by the same app.
    if (n.find("gp-5") != std::string::npos || n.find("gp5") != std::string::npos)
        return false;
    return n.find("gp-200") != std::string::npos
        || n.find("gp200") != std::string::npos
        || n.find("valeton") != std::string::npos;
}

struct DetectedPorts {
    bool inputFound = false;
    bool outputFound = false;
    MidiDeviceDescriptor input;
    MidiDeviceDescriptor output;
};

DetectedPorts detectPorts(MidiTransport& transport) {
    DetectedPorts d;
    for (const auto& dev : transport.listInputs()) {
        if (looksLikeGp200(dev.name)) {
            d.inputFound = true;
            d.input = dev;
            break;
        }
    }
    for (const auto& dev : transport.listOutputs()) {
        if (looksLikeGp200(dev.name)) {
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
        expectedSlot_ = -1;
        ackReceived_ = false;
        identityReceived_ = false;

        std::string err;
        auto onMessage = [this](const std::uint8_t* data, std::size_t size) { handleMessage(data, size); };
        if (!transport.openInput(ports.input, onMessage, err)) {
            error = ntc::fromUtf8("Cannot open GP-200 MIDI input: " + err);
            return false;
        }
        if (!transport.openOutput(ports.output, err)) {
            error = ntc::fromUtf8("Cannot open GP-200 MIDI output: " + err);
            transport.close();
            return false;
        }
        transport_ = &transport;
        return true;
    }

    bool sendSysEx(const std::vector<std::uint8_t>& bytes, std::wstring& error) {
        if (!transport_) {
            error = L"GP-200 MIDI output is not open.";
            return false;
        }
        std::string err;
        if (!transport_->sendMessage(bytes, err)) {
            error = ntc::fromUtf8(err);
            return false;
        }
        return true;
    }

    void expectIdentity() {
        std::lock_guard<std::mutex> lock(ackMutex_);
        identityReceived_ = false;
    }

    bool waitForIdentity(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(ackMutex_);
        return ackCv_.wait_for(lock, timeout, [this] { return identityReceived_; });
    }

    void expectAck(int slot) {
        std::lock_guard<std::mutex> lock(ackMutex_);
        expectedSlot_ = slot;
        ackReceived_ = false;
    }

    bool waitForAck(std::chrono::milliseconds timeout) {
        std::unique_lock<std::mutex> lock(ackMutex_);
        return ackCv_.wait_for(lock, timeout, [this] { return ackReceived_; });
    }

private:
    void handleMessage(const std::uint8_t* data, std::size_t size) {
        const bool gpHeader = size >= 10 &&
            data[0] == 0xF0 && data[1] == 0x21 && data[2] == 0x25 && data[3] == 0x7E &&
            data[4] == 0x47 && data[5] == 0x50 && data[6] == 0x2D && data[7] == 0x32;
        if (gpHeader && data[8] == 0x12 && data[9] == 0x08) {
            {
                std::lock_guard<std::mutex> lock(ackMutex_);
                identityReceived_ = true;
            }
            ackCv_.notify_all();
        }

        int expected = -1;
        {
            std::lock_guard<std::mutex> lock(ackMutex_);
            expected = expectedSlot_;
        }

        if (expected >= 0 && size >= 38) {
            const bool matchesObservedAck =
                data[8] == 0x12 && data[9] == 0x0c &&
                data[13] == 0x01 && data[14] == 0x04 && data[15] == 0x01 &&
                data[18] == 0x08 && data[26] == 0x01 &&
                static_cast<int>(data[22]) == expected;
            if (matchesObservedAck) {
                {
                    std::lock_guard<std::mutex> lock(ackMutex_);
                    ackReceived_ = true;
                }
                ackCv_.notify_all();
            }
        }
    }

    void close() {
        if (transport_) {
            transport_->close();
            transport_ = nullptr;
        }
    }

    MidiTransport* transport_ = nullptr;
    std::mutex ackMutex_;
    std::condition_variable ackCv_;
    int expectedSlot_ = -1;
    bool ackReceived_ = false;
    bool identityReceived_ = false;
};

} // namespace

MidiDetection detectGp200Midi() {
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
        if (d.inputName == d.outputName) return L"Detected: " + d.outputName;
        return L"Detected IN: " + d.inputName + L" | OUT: " + d.outputName;
    }
    if (d.inputFound) return L"GP-200 MIDI input found, but MIDI output is missing.";
    if (d.outputFound) return L"GP-200 MIDI output found, but MIDI input is missing.";
    return L"GP-200 MIDI not detected. Connect the pedal and press Rescan.";
}

UploadResult uploadCloToGp200(const std::filesystem::path& cloFile,
                              int globalSlot,
                              UploadProgress progress) {
    CloUploadData data;
    std::wstring error;
    if (!buildCloUpload(cloFile, globalSlot, data, error))
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

    // Mirror the startup sequence already confirmed in the VST before any
    // editor transaction: Identity -> Enter Editor Mode -> 100 ms.
    // The standalone uploader does not need the VST's subsequent state dump.
    const std::vector<std::uint8_t> identity {
        0xF0,0x21,0x25,0x7E,0x47,0x50,0x2D,0x32,0x11,0x04,0x00,
        0x00,0x00,0x00,0x01,0x02,0x00,0x00,0x00,0x00,0x00,0xF7
    };
    const std::vector<std::uint8_t> enterEditor {
        0xF0,0x21,0x25,0x7E,0x47,0x50,0x2D,0x32,0x11,0x12,0x00,
        0x00,0x00,0xF7
    };
    if (progress) progress(0, static_cast<int>(data.chunks.size()), L"Identifying GP-200...");
    session.expectIdentity();
    if (!session.sendSysEx(identity, error))
        return { false, L"Upload failed: " + error };
    if (!session.waitForIdentity(std::chrono::milliseconds(1200)))
        return { false, L"Upload failed: GP-200 identity response timeout." };
    if (progress) progress(0, static_cast<int>(data.chunks.size()), L"Entering GP-200 editor mode...");
    if (!session.sendSysEx(enterEditor, error))
        return { false, L"Upload failed: " + error };
    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    if (progress) progress(0, static_cast<int>(data.chunks.size()), L"Preparing destination slot...");
    session.expectAck(globalSlot);
    if (!session.sendSysEx(data.prepareMessage, error))
        return { false, L"Upload failed: " + error };

    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    const int total = static_cast<int>(data.chunks.size());
    for (int i = 0; i < total; ++i) {
        if (!session.sendSysEx(data.chunks[static_cast<std::size_t>(i)], error))
            return { false, L"Upload failed: " + error };
        if (progress) {
            std::wstringstream ss;
            ss << L"Uploading block " << (i + 1) << L" / " << total << L"...";
            progress(i + 1, total, ss.str());
        }
        if (i + 1 < total)
            std::this_thread::sleep_for(std::chrono::milliseconds(30));
    }

    if (progress) progress(total, total, L"Waiting for GP-200 confirmation...");
    if (!session.waitForAck(std::chrono::milliseconds(2000)))
        return { false, L"Upload failed: GP-200 confirmation timeout." };

    return { true, L"Sound Clone upload completed successfully." };
}

} // namespace ntc::gp200
