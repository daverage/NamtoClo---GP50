// namtoclo: cross-platform headless engineering/test CLI wrapping the same
// portable core (src/core) as the Windows GUI. See CLAUDE.md and the macOS
// porting notes in README.md for context. This CLI is also the process
// boundary the macOS SwiftUI app (macos/NamToCloMac) talks to -- see
// "Phase 1: make the CLI a clean GUI backend" in that app's design notes.
//
// JSON contract (--json): structured output goes to stdout ONLY, one JSON
// value per line (newline-delimited JSON / NDJSON) for long-running
// commands so a GUI can consume progress incrementally without waiting for
// the process to exit. Human-readable status/diagnostic text never goes to
// stdout in --json mode; it goes to stderr instead. Callers that just want
// the final result can ignore all but the last line.
//
// Subcommands:
//   convert <input.nam> --output <dir-or-file.clo> [--tone-match]
//                        [--reference clean|moderate|high|bass|auto]
//                        [--recorded-audio <wav>] [--corrective-ir <wav>]
//                        [--no-gp5-direct-fit] [--slot N] [--json]
//   midi-list [--json]
//   slots [--json]
//   upload <file.clo> --slot N [--debug-midi] [--json]
//   clo-info <file.clo> [--json]

#include "native_converter.hpp"
#include "gp5_clo_upload.hpp"
#include "gp5_midi.hpp"
#include "midi_transport.hpp"
#include "common.hpp"
#include "platform.hpp"

#include <cstdio>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

namespace {

namespace fs = ntc::fs;

void printUsage() {
    std::cerr <<
        "namtoclo -- NAM to CLO conversion and GP-5/GP-50/GP-200 upload CLI\n\n"
        "Usage:\n"
        "  namtoclo convert <input.nam> --output <dir-or-file.clo> [--tone-match]\n"
        "                              [--reference clean|moderate|high|bass|auto]\n"
        "                              [--recorded-audio <wav>] [--corrective-ir <wav>]\n"
        "                              [--no-gp5-direct-fit] [--slot N] [--json]\n"
        "  namtoclo midi-list [--json]\n"
        "  namtoclo slots [--json]\n"
        "  namtoclo upload <file.clo> --slot N [--debug-midi] [--json]\n"
        "  namtoclo clo-info <file.clo> [--json]\n";
}

struct Args {
    std::vector<std::string> positional;
    bool json = false;
    bool toneMatch = false;
    bool debugMidi = false;
    bool gp5DirectFit = true;
    std::string output;
    std::string reference;
    std::string recordedAudio;
    std::string correctiveIr;
    std::string device; // accepted; see midi-list for why device selection is limited today
    int slot = -1;
};

Args parseArgs(int argc, char** argv, int startAt) {
    Args a;
    for (int i = startAt; i < argc; ++i) {
        const std::string arg = argv[i];
        auto next = [&](const char* flag) -> std::string {
            if (i + 1 >= argc) {
                std::cerr << "Missing value for " << flag << "\n";
                std::exit(2);
            }
            return argv[++i];
        };
        if (arg == "--json") a.json = true;
        else if (arg == "--tone-match") a.toneMatch = true;
        else if (arg == "--debug-midi") a.debugMidi = true;
        else if (arg == "--no-gp5-direct-fit") a.gp5DirectFit = false;
        else if (arg == "--output" || arg == "-o") a.output = next("--output");
        else if (arg == "--reference") a.reference = next("--reference");
        else if (arg == "--recorded-audio") a.recordedAudio = next("--recorded-audio");
        else if (arg == "--corrective-ir") a.correctiveIr = next("--corrective-ir");
        else if (arg == "--slot") a.slot = std::stoi(next("--slot"));
        else if (arg == "--device") a.device = next("--device");
        else a.positional.push_back(arg);
    }
    return a;
}

std::string jsonEscape(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    for (char c : s) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            default: out.push_back(c);
        }
    }
    return out;
}

// Minimal JSON value builder -- this CLI's JSON shapes are small and fixed,
// so a dependency-free string builder is simpler than pulling in a JSON
// library for it. All string fields go through jsonEscape.
class JsonObj {
public:
    JsonObj& str(const std::string& k, const std::string& v) {
        field(k, "\"" + jsonEscape(v) + "\"");
        return *this;
    }
    JsonObj& boolean(const std::string& k, bool v) {
        field(k, v ? "true" : "false");
        return *this;
    }
    JsonObj& num(const std::string& k, double v) {
        field(k, std::to_string(v));
        return *this;
    }
    JsonObj& num(const std::string& k, int v) {
        field(k, std::to_string(v));
        return *this;
    }
    JsonObj& num(const std::string& k, std::uint64_t v) {
        field(k, std::to_string(v));
        return *this;
    }
    // Raw pre-built JSON (object/array literal) for a field.
    JsonObj& raw(const std::string& k, const std::string& rawJson) {
        field(k, rawJson);
        return *this;
    }
    std::string build() const { return "{" + body + "}"; }

private:
    void field(const std::string& k, const std::string& v) {
        if (!body.empty()) body += ",";
        body += "\"" + jsonEscape(k) + "\":" + v;
    }
    std::string body;
};

// Emits {"event":"<name>",...fieldsJsonBody} as one NDJSON line to stdout.
// fieldsJsonBody is a pre-built comma-free-leading JSON object body (as
// returned by JsonObj::build() with its outer braces stripped), or empty.
void emitEventFields(const std::string& event, const std::string& fieldsJsonBody) {
    std::cout << "{\"event\":\"" << jsonEscape(event) << "\"";
    if (!fieldsJsonBody.empty()) std::cout << "," << fieldsJsonBody;
    std::cout << "}\n" << std::flush;
}

ntc::ToneMatchReferenceMode parseReferenceMode(const std::string& s) {
    if (s == "clean") return ntc::ToneMatchReferenceMode::Clean;
    if (s == "moderate") return ntc::ToneMatchReferenceMode::Moderate;
    if (s == "high") return ntc::ToneMatchReferenceMode::High;
    if (s == "bass") return ntc::ToneMatchReferenceMode::Bass;
    return ntc::ToneMatchReferenceMode::Auto;
}

int cmdConvert(const Args& a) {
    if (a.positional.empty()) {
        std::cerr << "convert: missing <input.nam>\n";
        return 2;
    }
    const fs::path inputNam = a.positional.front();
    fs::path outputDir = a.output.empty() ? inputNam.parent_path() : fs::path(a.output);
    // If --output looks like a file (has a .clo extension), use its parent
    // directory as the conversion output directory -- convertNamToClo always
    // derives the output filename itself from the input NAM's name.
    if (outputDir.has_extension() && outputDir.extension() == ".clo") {
        outputDir = outputDir.parent_path();
    }
    if (outputDir.empty()) outputDir = fs::current_path();

    ntc::StimulusConfig stimulus;
    if (!a.recordedAudio.empty()) {
        stimulus.tailMode = ntc::TailMode::RecordedAudio;
        stimulus.recordedAudio = fs::path(a.recordedAudio);
    }

    ntc::CorrectiveIrConfig correction;
    if (!a.correctiveIr.empty()) {
        correction.enabled = true;
        correction.wav = fs::path(a.correctiveIr);
    }

    ntc::CloRefineConfig refine;
    refine.enabled = a.toneMatch;
    if (!a.reference.empty()) refine.referenceMode = parseReferenceMode(a.reference);

    ntc::NativeConverterConfig converter;
    converter.gp5DirectFit = a.gp5DirectFit;

    if (a.json) emitEventFields("start", "\"operation\":\"convert\"");

    const auto result = ntc::convertNamToClo(inputNam, outputDir, stimulus, correction, refine, converter,
        [&](const std::wstring& status) {
            if (a.json) {
                JsonObj o;
                o.str("message", ntc::toUtf8(status));
                emitEventFields("progress", o.build().substr(1, o.build().size() - 2));
            } else {
                std::wcout << status << L"\n";
            }
        });

    if (a.json) {
        JsonObj o;
        o.boolean("ok", result.ok)
         .str("input", inputNam.string())
         .str("output_gp200", result.gp2001024.string())
         .str("output_gp5gp50", result.gp5gp50Compact.string())
         .str("error", result.error);
        emitEventFields("complete", o.build().substr(1, o.build().size() - 2));
    } else if (!result.ok) {
        std::cerr << "Conversion failed: " << result.error << "\n";
    } else {
        std::cout << "GP-200 CLO:    " << result.gp2001024.string() << "\n";
        if (!result.gp5gp50Compact.empty())
            std::cout << "GP-5/GP-50 CLO: " << result.gp5gp50Compact.string() << "\n";
    }
    if (!result.ok) return 1;

    if (a.slot >= 0) {
        const fs::path uploadSource = !result.gp5gp50Compact.empty() ? result.gp5gp50Compact : result.gp2001024;
        ntc::gp5::UploadResult uploadResult = ntc::gp5::uploadCloToGp5(uploadSource, a.slot - 1,
            [&](int cur, int total, const std::wstring& status) {
                if (a.json) {
                    JsonObj o;
                    o.num("current", cur).num("total", total).str("message", ntc::toUtf8(status));
                    emitEventFields("progress", o.build().substr(1, o.build().size() - 2));
                } else {
                    std::wcout << L"[" << cur << L"/" << total << L"] " << status << L"\n";
                }
            });
        if (a.json) {
            JsonObj o;
            o.boolean("ok", uploadResult.ok).num("slot", a.slot).str("message", ntc::toUtf8(uploadResult.message));
            emitEventFields("complete", o.build().substr(1, o.build().size() - 2));
        } else {
            std::wcout << uploadResult.message << L"\n";
        }
        if (!uploadResult.ok) return 1;
    }
    return 0;
}

int cmdMidiList(const Args& a) {
    // Generic device enumeration (every CoreMIDI/WinMM endpoint the OS
    // reports), plus GP-5/GP-50-specific detection layered on top -- a GUI
    // wants to show "here's every MIDI device" and separately know which
    // one (if any) namtoclo has recognized as a GP-5/GP-50, since slots/
    // upload only ever talk to that recognized device today (see
    // CLAUDE.md's GP-5/GP-50 slot range section; multi-device disambiguation
    // by --device id is not yet wired into the protocol layer).
    const auto transport = ntc::createMidiTransport();
    const auto inputs = transport->listInputs();
    const auto outputs = transport->listOutputs();
    const auto detection = ntc::gp5::detectGp5Midi();

    if (a.json) {
        std::string devicesArray = "[";
        bool first = true;
        auto appendDevice = [&](const ntc::MidiDeviceDescriptor& d, bool isInput, bool isOutput) {
            if (!first) devicesArray += ",";
            first = false;
            JsonObj o;
            o.str("id", "coremidi:" + std::to_string(d.nativeId))
             .str("name", d.name)
             .boolean("input", isInput)
             .boolean("output", isOutput);
            devicesArray += o.build();
        };
        for (const auto& d : inputs) appendDevice(d, true, false);
        for (const auto& d : outputs) appendDevice(d, false, true);
        devicesArray += "]";

        JsonObj o;
        o.boolean("ok", true)
         .raw("devices", devicesArray)
         .boolean("gp5_gp50_input_found", detection.inputFound)
         .boolean("gp5_gp50_output_found", detection.outputFound)
         .str("gp5_gp50_input", ntc::toUtf8(detection.inputName))
         .str("gp5_gp50_output", ntc::toUtf8(detection.outputName));
        std::cout << "{\"operation\":\"midi-list\"," << o.build().substr(1, o.build().size() - 2) << "}\n";
    } else {
        std::wcout << L"GP-5/GP-50: " << ntc::gp5::describeDetection(detection) << L"\n";
        std::cout << "\nAll MIDI inputs:\n";
        for (const auto& d : inputs) std::cout << "  " << d.name << "\n";
        std::cout << "All MIDI outputs:\n";
        for (const auto& d : outputs) std::cout << "  " << d.name << "\n";
    }
    return 0;
}

int cmdSlots(const Args& a) {
    std::vector<ntc::gp5::SnapToneCatalogueEntry> entries;
    std::wstring error;
    if (!ntc::gp5::readSnapToneCatalogue(entries, error)) {
        if (a.json) {
            JsonObj o;
            o.boolean("ok", false).str("operation", "slots").str("error", ntc::toUtf8(error));
            std::cout << o.build() << "\n";
        } else {
            std::wcerr << L"Failed to read SnapTone catalogue: " << error << L"\n";
        }
        return 1;
    }

    if (a.json) {
        std::string slotsArray = "[";
        for (std::size_t i = 0; i < entries.size(); ++i) {
            if (i) slotsArray += ",";
            JsonObj s;
            s.num("slot", entries[i].visibleSlot).str("name", ntc::toUtf8(entries[i].name));
            slotsArray += s.build();
        }
        slotsArray += "]";
        std::cout << "{\"ok\":true,\"operation\":\"slots\",\"slots\":" << slotsArray << "}\n";
    } else {
        for (const auto& entry : entries) {
            std::wcout << entry.visibleSlot << L": " << (entry.name.empty() ? L"(empty)" : entry.name) << L"\n";
        }
    }
    return 0;
}

int cmdUpload(const Args& a) {
    if (a.positional.empty()) {
        std::cerr << "upload: missing <file.clo>\n";
        return 2;
    }
    if (a.slot < 0) {
        std::cerr << "upload: --slot N is required (visible SnapTone slot 51..80)\n";
        return 2;
    }
    const fs::path cloFile = a.positional.front();

    if (a.json) emitEventFields("start", "\"operation\":\"upload\"");

    const auto result = ntc::gp5::uploadCloToGp5(cloFile, a.slot - 1,
        [&](int cur, int total, const std::wstring& status) {
            if (a.debugMidi) std::wcerr << L"[debug] " << status << L"\n";
            if (a.json) {
                JsonObj o;
                o.num("current", cur).num("total", total).str("message", ntc::toUtf8(status));
                emitEventFields("progress", o.build().substr(1, o.build().size() - 2));
            } else {
                std::wcout << L"[" << cur << L"/" << total << L"] " << status << L"\n";
            }
        });

    if (a.json) {
        JsonObj o;
        o.boolean("ok", result.ok)
         .str("device", "Valeton GP-5/GP-50")
         .num("slot", a.slot)
         .str("message", ntc::toUtf8(result.message));
        emitEventFields("complete", o.build().substr(1, o.build().size() - 2));
    } else {
        std::wcout << result.message << L"\n";
    }
    return result.ok ? 0 : 1;
}

int cmdCloInfo(const Args& a) {
    if (a.positional.empty()) {
        std::cerr << "clo-info: missing <file.clo>\n";
        return 2;
    }
    const fs::path path = a.positional.front();
    const auto info = ntc::inspectClo(path, 32);
    if (a.json) {
        JsonObj o;
        o.boolean("ok", info.exists)
         .str("path", path.string())
         .boolean("exists", info.exists)
         .num("size", info.size)
         .str("magic", info.magic)
         .str("declared_size", ntc::hex32(info.declaredSize))
         .str("payload_size", ntc::hex32(info.payloadSize))
         .str("model_field", ntc::hex32(info.modelField));
        std::cout << o.build() << "\n";
    } else {
        ntc::printCloInfo(path, info);
    }
    return info.exists ? 0 : 1;
}

} // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        printUsage();
        return 2;
    }
    const std::string command = argv[1];
    const Args a = parseArgs(argc, argv, 2);

    if (command == "convert") return cmdConvert(a);
    if (command == "midi-list") return cmdMidiList(a);
    if (command == "slots") return cmdSlots(a);
    if (command == "upload") return cmdUpload(a);
    if (command == "clo-info") return cmdCloInfo(a);
    if (command == "--help" || command == "-h" || command == "help") {
        printUsage();
        return 0;
    }

    std::cerr << "Unknown command: " << command << "\n\n";
    printUsage();
    return 2;
}
