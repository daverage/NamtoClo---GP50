import Foundation

/// Result/model types mirroring the JSON shapes `namtoclo --json` prints.
/// See src/cli/main.cpp for the authoritative field set -- these structs
/// are a thin, hand-written mapping, not a codegen artifact, so keep them
/// in sync manually if the CLI's JSON shape changes.

struct MidiDevice: Identifiable, Hashable {
    let id: String // "coremidi:<nativeId>"
    let name: String
    let input: Bool
    let output: Bool
}

struct MidiListResult {
    let ok: Bool
    let devices: [MidiDevice]
    let gp5Gp50InputFound: Bool
    let gp5Gp50OutputFound: Bool
    let gp5Gp50InputName: String
    let gp5Gp50OutputName: String

    /// Whether namtoclo's protocol layer has actually recognized a GP-5/GP-50
    /// on both directions -- this, not the raw device list, is what gates
    /// whether Slots/Upload can succeed (see CLAUDE.md: device selection by
    /// id isn't wired into the protocol layer yet, so only the auto-detected
    /// pair is usable).
    var gp5Gp50Ready: Bool { gp5Gp50InputFound && gp5Gp50OutputFound }
}

struct SnapToneSlot: Identifiable, Hashable {
    var id: Int { slot }
    let slot: Int
    let name: String
    var isEmpty: Bool { name.isEmpty }
}

struct ConvertOutcome {
    let ok: Bool
    let inputPath: String
    let gp200Path: String
    let gp5Path: String
    let error: String

    var bestUploadCandidate: String? {
        if !gp5Path.isEmpty { return gp5Path }
        if !gp200Path.isEmpty { return gp200Path }
        return nil
    }
}

struct UploadOutcome {
    let ok: Bool
    let slot: Int
    let message: String
}

struct CloInfoResult {
    let ok: Bool
    let path: String
    let exists: Bool
    let size: Int
    let magic: String
    let declaredSize: String
    let payloadSize: String
    let modelField: String
}

enum ToneMatchReference: String, CaseIterable, Identifiable {
    case auto, clean, moderate, high, bass
    var id: String { rawValue }
    var displayName: String {
        switch self {
        case .auto: return "Auto"
        case .clean: return "Clean"
        case .moderate: return "Moderate"
        case .high: return "High Gain"
        case .bass: return "Bass"
        }
    }
}

/// Progress callback shapes surfaced to SwiftUI views.
enum BackendProgress {
    case message(String)
    case blockProgress(current: Int, total: Int, message: String)
}
