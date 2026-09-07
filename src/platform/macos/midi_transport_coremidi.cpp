#include "midi_transport.hpp"

#include <CoreMIDI/CoreMIDI.h>
#include <CoreFoundation/CoreFoundation.h>

#include <chrono>
#include <cstring>
#include <mutex>
#include <thread>
#include <vector>

namespace ntc {
namespace {

std::string cfStringToUtf8(CFStringRef str) {
    if (!str) return {};
    const CFIndex length = CFStringGetLength(str);
    const CFIndex maxSize = CFStringGetMaximumSizeForEncoding(length, kCFStringEncodingUTF8) + 1;
    std::vector<char> buffer(static_cast<std::size_t>(maxSize));
    if (!CFStringGetCString(str, buffer.data(), maxSize, kCFStringEncodingUTF8)) return {};
    return std::string(buffer.data());
}

std::string endpointName(MIDIEndpointRef endpoint) {
    CFStringRef name = nullptr;
    // MIDIObjectGetStringProperty with kMIDIPropertyDisplayName gives the
    // same human-readable name Audio MIDI Setup shows (falls back to the
    // plain name property if display name is unavailable).
    if (MIDIObjectGetStringProperty(endpoint, kMIDIPropertyDisplayName, &name) != noErr || !name) {
        if (MIDIObjectGetStringProperty(endpoint, kMIDIPropertyName, &name) != noErr || !name) {
            return {};
        }
    }
    const std::string result = cfStringToUtf8(name);
    CFRelease(name);
    return result;
}

// CoreMIDI SysEx messages arrive via MIDIReadProc as one or more MIDIPacket
// entries in a MIDIPacketList; a single logical SysEx (0xF0 ... 0xF7) may be
// split across multiple 256-byte packets by the driver. This reassembles
// packets into complete 0xF0..0xF7 frames before handing them to the
// portable protocol layer, matching the "deliver each message exactly once,
// intact" contract in midi_transport.hpp.
class SysExReassembler {
public:
    void feed(const MIDIPacket* packet, const MidiMessageCallback& onMessage) {
        for (UInt16 i = 0; i < packet->length; ++i) {
            const std::uint8_t byte = packet->data[i];
            if (byte == 0xF0) {
                buffer_.clear();
                buffer_.push_back(byte);
            } else if (!buffer_.empty()) {
                buffer_.push_back(byte);
                if (byte == 0xF7) {
                    if (onMessage) onMessage(buffer_.data(), buffer_.size());
                    buffer_.clear();
                }
            }
            // Bytes received outside of an 0xF0..0xF7 span (stray realtime
            // bytes, etc.) are ignored -- this transport only carries SysEx
            // for the GP protocols.
        }
    }

private:
    std::vector<std::uint8_t> buffer_;
};

class CoreMidiTransport final : public MidiTransport {
public:
    ~CoreMidiTransport() override { close(); }

    std::vector<MidiDeviceDescriptor> listInputs() override {
        return listEndpoints(/*sources=*/true);
    }

    std::vector<MidiDeviceDescriptor> listOutputs() override {
        return listEndpoints(/*sources=*/false);
    }

    bool openInput(const MidiDeviceDescriptor& device, MidiMessageCallback onMessage, std::string& error) override {
        if (!ensureClient(error)) return false;

        onMessage_ = std::move(onMessage);
        reassembler_ = std::make_unique<SysExReassembler>();

        const OSStatus st = MIDIInputPortCreate(client_, CFSTR("NamToClo Input"), &readProc, this, &inputPort_);
        if (st != noErr) {
            error = "MIDIInputPortCreate failed (OSStatus " + std::to_string(st) + ")";
            return false;
        }

        source_ = static_cast<MIDIEndpointRef>(device.nativeId);
        const OSStatus connectSt = MIDIPortConnectSource(inputPort_, source_, nullptr);
        if (connectSt != noErr) {
            error = "MIDIPortConnectSource failed (OSStatus " + std::to_string(connectSt) + ")";
            MIDIPortDispose(inputPort_);
            inputPort_ = 0;
            return false;
        }
        return true;
    }

    bool openOutput(const MidiDeviceDescriptor& device, std::string& error) override {
        if (!ensureClient(error)) return false;

        const OSStatus st = MIDIOutputPortCreate(client_, CFSTR("NamToClo Output"), &outputPort_);
        if (st != noErr) {
            error = "MIDIOutputPortCreate failed (OSStatus " + std::to_string(st) + ")";
            return false;
        }
        destination_ = static_cast<MIDIEndpointRef>(device.nativeId);
        return true;
    }

    bool sendMessage(const std::vector<std::uint8_t>& bytes, std::string& error) override {
        if (!outputPort_ || destination_ == 0 || bytes.empty()) {
            error = "MIDI output is not open.";
            return false;
        }

        // A GP-5/GP-50 SysEx transfer chunk is a small, fixed 19-byte
        // payload nibble-encoded to well under CoreMIDI's single-packet
        // limit (256 bytes), so this always fits in one MIDIPacket -- no
        // chunking logic is needed on the send side.
        if (bytes.size() > 65535) {
            error = "SysEx message too large for a single CoreMIDI packet.";
            return false;
        }

        Byte packetBuffer[65536 + 128];
        MIDIPacketList* packetList = reinterpret_cast<MIDIPacketList*>(packetBuffer);
        MIDIPacket* packet = MIDIPacketListInit(packetList);
        packet = MIDIPacketListAdd(packetList, sizeof(packetBuffer), packet, 0,
                                   bytes.size(), bytes.data());
        if (!packet) {
            error = "Failed to build CoreMIDI SysEx packet list.";
            return false;
        }

        const OSStatus st = MIDISend(outputPort_, destination_, packetList);
        if (st != noErr) {
            error = "MIDISend failed (OSStatus " + std::to_string(st) + ")";
            return false;
        }

        // CoreMIDI's MIDISend is asynchronous at the driver level for
        // virtual/IAC destinations but effectively synchronous (blocks
        // until queued to the USB stack) for hardware endpoints, which is
        // all this transport targets. A short pacing delay matches the
        // WinMM path's inter-message spacing without needing a
        // completion callback that CoreMIDI's C API does not expose here.
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        return true;
    }

    void close() override {
        if (inputPort_) {
            if (source_) MIDIPortDisconnectSource(inputPort_, source_);
            MIDIPortDispose(inputPort_);
            inputPort_ = 0;
        }
        if (outputPort_) {
            MIDIPortDispose(outputPort_);
            outputPort_ = 0;
        }
        source_ = 0;
        destination_ = 0;
        reassembler_.reset();
    }

private:
    bool ensureClient(std::string& error) {
        if (client_) return true;
        const OSStatus st = MIDIClientCreate(CFSTR("NamToClo"), nullptr, nullptr, &client_);
        if (st != noErr) {
            error = "MIDIClientCreate failed (OSStatus " + std::to_string(st) + ")";
            return false;
        }
        return true;
    }

    static std::vector<MidiDeviceDescriptor> listEndpoints(bool sources) {
        std::vector<MidiDeviceDescriptor> out;
        const ItemCount count = sources ? MIDIGetNumberOfSources() : MIDIGetNumberOfDestinations();
        for (ItemCount i = 0; i < count; ++i) {
            const MIDIEndpointRef endpoint = sources ? MIDIGetSource(i) : MIDIGetDestination(i);
            if (!endpoint) continue;
            MidiDeviceDescriptor d;
            d.name = endpointName(endpoint);
            d.nativeId = static_cast<std::uint64_t>(endpoint);
            out.push_back(std::move(d));
        }
        return out;
    }

    static void readProc(const MIDIPacketList* packetList, void* readProcRefCon, void* /*srcConnRefCon*/) {
        auto* self = static_cast<CoreMidiTransport*>(readProcRefCon);
        if (!self || !self->reassembler_) return;
        const MIDIPacket* packet = &packetList->packet[0];
        for (UInt32 i = 0; i < packetList->numPackets; ++i) {
            self->reassembler_->feed(packet, self->onMessage_);
            packet = MIDIPacketNext(packet);
        }
    }

    MIDIClientRef client_ = 0;
    MIDIPortRef inputPort_ = 0;
    MIDIPortRef outputPort_ = 0;
    MIDIEndpointRef source_ = 0;
    MIDIEndpointRef destination_ = 0;
    MidiMessageCallback onMessage_;
    std::unique_ptr<SysExReassembler> reassembler_;
};

} // namespace

std::unique_ptr<MidiTransport> createMidiTransport() {
    return std::make_unique<CoreMidiTransport>();
}

} // namespace ntc
