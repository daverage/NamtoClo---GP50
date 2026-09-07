#pragma once

// Portable USB-MIDI transport interface. The GP-5/GP-50 and GP-200 protocol
// implementations (gp5_midi.cpp, gp200_midi.cpp) are written entirely against
// this interface -- nibble encoding, CRC-8, ACK/completion matching, retry
// policy, and SnapTone catalogue parsing all live in those portable files
// and never change between platforms. Only the mechanics of talking to the
// OS's MIDI stack differ:
//   src/platform/windows/midi_transport_winmm.cpp    (WinMM)
//   src/platform/macos/midi_transport_coremidi.cpp   (CoreMIDI)
//
// Threading contract: onMessage (registered via openInput) is invoked from a
// platform-owned thread (the WinMM callback thread on Windows, a CoreMIDI
// dispatch/read thread on macOS) and must not block. Implementations must
// deliver each received SysEx message exactly once and must not split or
// coalesce message boundaries -- callers rely on receiving each 0xF0..0xF7
// frame intact in a single callback invocation.

#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace ntc {

struct MidiDeviceDescriptor {
    std::string name; // UTF-8, as reported by the OS
    // Opaque, platform-specific identifier (a WinMM device index on Windows,
    // a CoreMIDI endpoint ref on macOS). Only meaningful to the transport
    // implementation that produced it.
    std::uint64_t nativeId = 0;
};

using MidiMessageCallback = std::function<void(const std::uint8_t* data, std::size_t size)>;

class MidiTransport {
public:
    virtual ~MidiTransport() = default;

    virtual std::vector<MidiDeviceDescriptor> listInputs() = 0;
    virtual std::vector<MidiDeviceDescriptor> listOutputs() = 0;

    // Opens the input endpoint and starts delivering received messages to
    // onMessage. onMessage must remain valid until close() returns.
    virtual bool openInput(const MidiDeviceDescriptor& device,
                           MidiMessageCallback onMessage,
                           std::string& error) = 0;

    virtual bool openOutput(const MidiDeviceDescriptor& device, std::string& error) = 0;

    // Blocking send of one complete message (typically a SysEx frame,
    // 0xF0 ... 0xF7). Returns once the platform confirms the message has
    // gone out, or after an internal ~2s send timeout.
    virtual bool sendMessage(const std::vector<std::uint8_t>& bytes, std::string& error) = 0;

    virtual void close() = 0;
};

// Returns the platform's MIDI transport implementation.
std::unique_ptr<MidiTransport> createMidiTransport();

} // namespace ntc
