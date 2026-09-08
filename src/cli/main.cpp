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
//   tone3000 login [--publishable-key t3k_pub_...] [--json]   (macOS only)
//   tone3000 logout [--json]                                  (macOS only)
//   tone3000 search <query> [--page N] [--sort ...] [--json]  (macOS only)
//   tone3000 models <tone-id> [--json]                        (macOS only)
//   tone3000 download <model-id> --tone <tone-id>
//                      --output <dir> [--json]                (macOS only)
//   tone3000 preview <nam-file> --input <wav>
//                     [--output <wav>] [--no-play] [--json]   (macOS only)

#include "native_converter.hpp"
#include "gp5_clo_upload.hpp"
#include "gp5_midi.hpp"
#include "midi_transport.hpp"
#include "common.hpp"
#include "platform.hpp"
#if defined(__APPLE__)
#include "net_client.hpp"
#include "tone3000_client.hpp"
#include "tone3000_preview.hpp"
#endif

#include <algorithm>
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
        "  namtoclo clo-info <file.clo> [--json]\n"
#if defined(__APPLE__)
        "  namtoclo tone3000 login [--publishable-key t3k_pub_...] [--json]\n"
        "  namtoclo tone3000 logout [--json]\n"
        "  namtoclo tone3000 search <query> [--page N] [--sort best-match|...] [--json]\n"
        "  namtoclo tone3000 models <tone-id> [--json]\n"
        "  namtoclo tone3000 download <model-id> --tone <tone-id> --output <dir> [--json]\n"
        "  namtoclo tone3000 preview <nam-file> --input <wav> [--output <wav>] [--no-play] [--json]\n"
#endif
        ;
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
    // tone3000 (macOS only -- see net_client.hpp)
    std::string publishableKey;
    std::string input;
    std::int64_t toneId = -1;
    int page = 1;
    std::string sort;
    bool noPlay = false;
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
        else if (arg == "--publishable-key") a.publishableKey = next("--publishable-key");
        else if (arg == "--input") a.input = next("--input");
        else if (arg == "--tone") a.toneId = std::stoll(next("--tone"));
        else if (arg == "--page") a.page = std::stoi(next("--page"));
        else if (arg == "--sort") a.sort = next("--sort");
        else if (arg == "--no-play") a.noPlay = true;
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
    JsonObj& num(const std::string& k, std::int64_t v) {
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

#if defined(__APPLE__)

constexpr const char* kT3kPublishableKeySecret = "tone3000.publishableKey";
constexpr const char* kT3kRefreshTokenSecret = "tone3000.refreshToken";

void printJsonError(const std::string& operation, const std::string& error) {
    JsonObj o;
    o.boolean("ok", false).str("operation", operation).str("error", error);
    std::cout << o.build() << "\n";
}

bool loadOrUseGivenPublishableKey(const Args& a, std::string& key, std::string& error) {
    if (!a.publishableKey.empty()) { key = a.publishableKey; return true; }
    if (ntc::net::loadSecret(kT3kPublishableKeySecret, key) && !key.empty()) return true;
    error = "No Tone3000 publishable key saved. Pass --publishable-key t3k_pub_... once.";
    return false;
}

// Loads the saved publishable key (or the one just supplied) and the saved
// refresh token, then restores the session. On success the (possibly
// rotated) refresh token is written back to the Keychain.
bool connectTone3000Client(const Args& a, ntc::tone3000::Client& client, std::string& error) {
    std::string key;
    if (!loadOrUseGivenPublishableKey(a, key, error)) return false;
    client.setPublishableKey(key);
    std::string refreshToken;
    if (!ntc::net::loadSecret(kT3kRefreshTokenSecret, refreshToken) || refreshToken.empty()) {
        error = "Not connected to Tone3000. Run 'namtoclo tone3000 login' first.";
        return false;
    }
    if (!client.restoreSession(refreshToken, error)) return false;
    ntc::net::saveSecret(kT3kRefreshTokenSecret, client.refreshToken());
    return true;
}

int cmdTone3000Login(const Args& a) {
    std::string key, error;
    if (!loadOrUseGivenPublishableKey(a, key, error)) {
        if (a.json) printJsonError("tone3000-login", error); else std::cerr << error << "\n";
        return 2;
    }
    ntc::net::saveSecret(kT3kPublishableKeySecret, key);
    ntc::tone3000::Client client(key);
    if (!client.authenticateInteractive(error)) {
        if (a.json) printJsonError("tone3000-login", error);
        else std::cerr << "Tone3000 login failed: " << error << "\n";
        return 1;
    }
    ntc::net::saveSecret(kT3kRefreshTokenSecret, client.refreshToken());
    if (a.json) { JsonObj o; o.boolean("ok", true).str("operation", "tone3000-login"); std::cout << o.build() << "\n"; }
    else std::cout << "Connected to Tone3000.\n";
    return 0;
}

int cmdTone3000Logout(const Args& a) {
    ntc::net::deleteSecret(kT3kRefreshTokenSecret);
    if (a.json) { JsonObj o; o.boolean("ok", true).str("operation", "tone3000-logout"); std::cout << o.build() << "\n"; }
    else std::cout << "Disconnected from Tone3000.\n";
    return 0;
}

int cmdTone3000Search(const Args& a) {
    if (a.positional.empty()) { std::cerr << "tone3000 search: missing <query>\n"; return 2; }
    ntc::tone3000::Client client;
    std::string error;
    if (!connectTone3000Client(a, client, error)) {
        if (a.json) printJsonError("tone3000-search", error); else std::cerr << error << "\n";
        return 1;
    }
    std::vector<ntc::tone3000::Tone> tones;
    int totalPages = 1, totalResults = 0;
    if (!client.searchNamTones(a.positional.front(), a.page, a.sort, tones, totalPages, totalResults, error)) {
        if (a.json) printJsonError("tone3000-search", error);
        else std::cerr << "Tone3000 search failed: " << error << "\n";
        return 1;
    }
    if (a.json) {
        std::string arr = "[";
        for (std::size_t i = 0; i < tones.size(); ++i) {
            if (i) arr += ",";
            const auto& t = tones[i];
            JsonObj o;
            o.num("id", t.id).str("title", t.title).str("creator", t.creator).str("gear", t.gear)
             .str("license", t.license).num("models_count", t.modelsCount)
             .num("downloads_count", t.downloadsCount).num("favorites_count", t.favoritesCount);
            arr += o.build();
        }
        arr += "]";
        JsonObj o;
        o.boolean("ok", true).num("page", a.page).num("total_pages", totalPages)
         .num("total_results", totalResults).raw("tones", arr);
        std::cout << "{\"operation\":\"tone3000-search\"," << o.build().substr(1, o.build().size() - 2) << "}\n";
    } else {
        for (const auto& t : tones) std::cout << t.id << "  " << t.title << "  (" << t.creator << ")  " << t.gear << "\n";
        std::cout << "Page " << a.page << "/" << totalPages << ", " << totalResults << " total\n";
    }
    return 0;
}

int cmdTone3000Models(const Args& a) {
    if (a.positional.empty()) { std::cerr << "tone3000 models: missing <tone-id>\n"; return 2; }
    ntc::tone3000::Client client;
    std::string error;
    if (!connectTone3000Client(a, client, error)) {
        if (a.json) printJsonError("tone3000-models", error); else std::cerr << error << "\n";
        return 1;
    }
    const std::int64_t toneId = std::stoll(a.positional.front());
    std::vector<ntc::tone3000::Model> models;
    if (!client.listModels(toneId, models, error)) {
        if (a.json) printJsonError("tone3000-models", error);
        else std::cerr << "Tone3000 model list failed: " << error << "\n";
        return 1;
    }
    if (a.json) {
        std::string arr = "[";
        for (std::size_t i = 0; i < models.size(); ++i) {
            if (i) arr += ",";
            const auto& m = models[i];
            JsonObj o;
            o.num("id", m.id).num("tone_id", m.toneId).str("name", m.name).str("size", m.size)
             .str("architecture_version", m.architectureVersion).str("model_url", m.modelUrl);
            arr += o.build();
        }
        arr += "]";
        JsonObj o;
        o.boolean("ok", true).raw("models", arr);
        std::cout << "{\"operation\":\"tone3000-models\"," << o.build().substr(1, o.build().size() - 2) << "}\n";
    } else {
        for (const auto& m : models) std::cout << m.id << "  " << m.name << "  " << m.size << "\n";
    }
    return 0;
}

int cmdTone3000Download(const Args& a) {
    if (a.positional.empty()) { std::cerr << "tone3000 download: missing <model-id>\n"; return 2; }
    if (a.toneId < 0) { std::cerr << "tone3000 download: --tone <tone-id> is required\n"; return 2; }
    if (a.output.empty()) { std::cerr << "tone3000 download: --output <dir> is required\n"; return 2; }
    ntc::tone3000::Client client;
    std::string error;
    if (!connectTone3000Client(a, client, error)) {
        if (a.json) printJsonError("tone3000-download", error); else std::cerr << error << "\n";
        return 1;
    }
    const std::int64_t modelId = std::stoll(a.positional.front());
    std::vector<ntc::tone3000::Model> models;
    if (!client.listModels(a.toneId, models, error)) {
        if (a.json) printJsonError("tone3000-download", error);
        else std::cerr << "Tone3000 model list failed: " << error << "\n";
        return 1;
    }
    const auto it = std::find_if(models.begin(), models.end(), [&](const auto& m) { return m.id == modelId; });
    if (it == models.end()) {
        error = "Model " + std::to_string(modelId) + " not found under tone " + std::to_string(a.toneId);
        if (a.json) printJsonError("tone3000-download", error); else std::cerr << error << "\n";
        return 1;
    }
    const fs::path destination = fs::path(a.output) / ((it->name.empty() ? std::to_string(it->id) : it->name) + ".nam");
    if (!client.downloadModel(*it, destination, error)) {
        if (a.json) printJsonError("tone3000-download", error);
        else std::cerr << "Tone3000 download failed: " << error << "\n";
        return 1;
    }
    if (a.json) {
        JsonObj o;
        o.boolean("ok", true).str("operation", "tone3000-download").str("output", destination.string());
        std::cout << o.build() << "\n";
    } else {
        std::cout << "Downloaded: " << destination.string() << "\n";
    }
    return 0;
}

int cmdTone3000Preview(const Args& a) {
    if (a.positional.empty()) { std::cerr << "tone3000 preview: missing <nam-file>\n"; return 2; }
    if (a.input.empty()) { std::cerr << "tone3000 preview: --input <wav> is required\n"; return 2; }
    const fs::path namPath = a.positional.front();
    const fs::path outputWav = a.output.empty()
        ? fs::temp_directory_path() / "namtoclo_tone3000_preview.wav" : fs::path(a.output);
    std::string error;
    if (a.json) emitEventFields("start", "\"operation\":\"tone3000-preview\"");
    if (!ntc::renderNamPreview(namPath, a.input, outputWav, error)) {
        if (a.json) {
            JsonObj o;
            o.boolean("ok", false).str("error", error);
            emitEventFields("complete", o.build().substr(1, o.build().size() - 2));
        } else {
            std::cerr << "Preview render failed: " << error << "\n";
        }
        return 1;
    }
    bool played = false;
    if (!a.noPlay) {
        played = ntc::playAudioFileBlocking(outputWav, error);
        if (!played && !a.json) std::cerr << "Could not play preview: " << error << "\n";
    }
    if (a.json) {
        JsonObj o;
        o.boolean("ok", true).str("output", outputWav.string()).boolean("played", played);
        emitEventFields("complete", o.build().substr(1, o.build().size() - 2));
    } else {
        std::cout << "Rendered: " << outputWav.string() << "\n";
    }
    return 0;
}

int cmdTone3000(const std::string& sub, const Args& a) {
    if (sub == "login") return cmdTone3000Login(a);
    if (sub == "logout") return cmdTone3000Logout(a);
    if (sub == "search") return cmdTone3000Search(a);
    if (sub == "models") return cmdTone3000Models(a);
    if (sub == "download") return cmdTone3000Download(a);
    if (sub == "preview") return cmdTone3000Preview(a);
    std::cerr << "Unknown tone3000 subcommand: " << sub << "\n";
    return 2;
}

#endif // defined(__APPLE__)

} // namespace

int main(int argc, char** argv) {
    if (argc < 2) {
        printUsage();
        return 2;
    }
    const std::string command = argv[1];

    if (command == "tone3000") {
#if defined(__APPLE__)
        if (argc < 3) {
            std::cerr << "tone3000: missing subcommand (login|logout|search|models|download|preview)\n";
            return 2;
        }
        const std::string sub = argv[2];
        const Args a = parseArgs(argc, argv, 3);
        return cmdTone3000(sub, a);
#else
        std::cerr << "tone3000: not available on this build (macOS only)\n";
        return 2;
#endif
    }

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
