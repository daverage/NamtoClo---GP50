import Foundation

/// Everything the SwiftUI app needs from the native backend. Presentation
/// code (Views/) talks only to this protocol -- never to Process directly
/// (see CLAUDE.md-equivalent design note in this app's README: "Do NOT
/// scatter Process() calls throughout individual SwiftUI views").
protocol NamToCloBackend: AnyObject {
    func listMidiDevices() async throws -> MidiListResult
    func readSlots() async throws -> [SnapToneSlot]

    func convert(
        inputNam: URL,
        outputDirectory: URL,
        toneMatch: Bool,
        reference: ToneMatchReference,
        recordedAudio: URL?,
        correctiveIr: URL?,
        gp5DirectFit: Bool,
        onProgress: @escaping (String) -> Void
    ) async throws -> ConvertOutcome

    func upload(
        cloFile: URL,
        slot: Int,
        debugMidi: Bool,
        onProgress: @escaping (Int, Int, String) -> Void
    ) async throws -> UploadOutcome

    func cloInfo(path: URL) async throws -> CloInfoResult

    func cancelCurrentOperation()
}

/// Errors surfaced up through the backend, translated into user-facing text
/// by callers (see Views/ErrorPresentation.swift-equivalent helpers below).
struct BackendError: Error, LocalizedError {
    let summary: String
    let technicalDetails: String
    var errorDescription: String? { summary }
}

/// Locates and drives the bundled `namtoclo` executable. This is the ONLY
/// place in the app that knows the CLI's argument/JSON conventions.
final class CLIBackend: NamToCloBackend {
    private let runner = ProcessRunner()
    let executableURL: URL
    /// Appended to every backend error/log so the Debug view has something
    /// concrete to show without polling.
    var onDiagnostic: ((String) -> Void)?

    init(executableURL: URL) {
        self.executableURL = executableURL
    }

    /// Resolves `namtoclo` next to this app's own executable inside the
    /// bundle (Contents/MacOS/namtoclo, alongside Contents/MacOS/NamToCloMac)
    /// so the app works after being moved anywhere -- never relies on $PATH.
    /// Falls back to the CMake-built CLI for `swift run`/`swift build`
    /// development outside a packaged .app.
    static func resolveBundled() -> URL {
        let bundledCandidate = Bundle.main.bundleURL
            .appendingPathComponent("Contents/MacOS/namtoclo")
        if FileManager.default.isExecutableFile(atPath: bundledCandidate.path) {
            return bundledCandidate
        }
        // Development fallback. Outside a packaged .app, Bundle.main.bundleURL
        // just points at the raw executable's own directory, and the
        // process's current working directory is NOT a reliable place to
        // resolve from -- launching via Xcode, `open`, or a terminal opened
        // elsewhere can leave it at $HOME or anywhere else, which silently
        // breaks a cwd-relative "../../build-macos/namtoclo" guess. Resolve
        // relative to THIS SOURCE FILE's own on-disk location instead
        // (stable for anyone building this exact repo checkout, regardless
        // of how/where the resulting binary is launched from):
        // <repo>/macos/NamToCloMac/Sources/NamToCloMac/Backend/NamToCloBackend.swift
        // -> <repo>/build-macos/namtoclo
        let repoRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // strips NamToCloBackend.swift -> .../Backend
            .deletingLastPathComponent() // strips Backend -> .../Sources/NamToCloMac
            .deletingLastPathComponent() // strips inner NamToCloMac -> .../Sources
            .deletingLastPathComponent() // strips Sources -> .../macos/NamToCloMac (package root)
            .deletingLastPathComponent() // strips NamToCloMac -> .../macos
            .deletingLastPathComponent() // strips macos -> repo root
        let devCandidate = repoRoot.appendingPathComponent("build-macos/namtoclo")
        if FileManager.default.isExecutableFile(atPath: devCandidate.path) {
            return devCandidate
        }
        return URL(fileURLWithPath: "/usr/local/bin/namtoclo")
    }

    func cancelCurrentOperation() {
        runner.cancel()
    }

    private func run(_ arguments: [String], onLine: @escaping (ProcessRunner.JSONLine) -> Void = { _ in }) async throws -> (lines: [ProcessRunner.JSONLine], exitCode: Int32) {
        guard FileManager.default.isExecutableFile(atPath: executableURL.path) else {
            let message = "The namtoclo engine could not be found. Expected executable at \(executableURL.path)."
            onDiagnostic?(message)
            throw BackendError(
                summary: "The namtoclo engine could not be found.",
                technicalDetails: message + " Reinstall the app or rebuild it with scripts/build_macos_app.sh."
            )
        }
        // Log the exact invocation and its result -- namtoclo's real
        // diagnostics go through stdout as NDJSON progress events (routed to
        // the Debug tab separately by callers via onLine), not stderr, so
        // stderr alone is usually empty even on a normal run. Without this,
        // the Debug tab had nothing useful to show for most sessions.
        onDiagnostic?("$ \(executableURL.path) \(arguments.joined(separator: " "))")
        do {
            let result = try await runner.run(executable: executableURL, arguments: arguments, onLine: onLine) { [weak self] text in
                self?.onDiagnostic?(text)
            }
            onDiagnostic?("(exit code \(result.exitCode))")
            return result
        } catch {
            onDiagnostic?("Process failed to run: \(error.localizedDescription)")
            throw BackendError(summary: "Failed to run the namtoclo engine.", technicalDetails: error.localizedDescription)
        }
    }

    func listMidiDevices() async throws -> MidiListResult {
        let (lines, exitCode) = try await run(["midi-list", "--json"])
        guard exitCode == 0, let obj = lines.first else {
            throw BackendError(summary: "Could not list MIDI devices.", technicalDetails: "namtoclo midi-list exited with code \(exitCode).")
        }
        let devicesRaw = obj["devices"] as? [[String: Any]] ?? []
        let devices = devicesRaw.compactMap { d -> MidiDevice? in
            guard let id = d["id"] as? String, let name = d["name"] as? String else { return nil }
            return MidiDevice(id: id, name: name, input: d["input"] as? Bool ?? false, output: d["output"] as? Bool ?? false)
        }
        return MidiListResult(
            ok: obj["ok"] as? Bool ?? false,
            devices: devices,
            gp5Gp50InputFound: obj["gp5_gp50_input_found"] as? Bool ?? false,
            gp5Gp50OutputFound: obj["gp5_gp50_output_found"] as? Bool ?? false,
            gp5Gp50InputName: obj["gp5_gp50_input"] as? String ?? "",
            gp5Gp50OutputName: obj["gp5_gp50_output"] as? String ?? ""
        )
    }

    func readSlots() async throws -> [SnapToneSlot] {
        let (lines, exitCode) = try await run(["slots", "--json"])
        guard let obj = lines.first else {
            throw BackendError(summary: "No response reading SnapTone slots.", technicalDetails: "namtoclo slots produced no output (exit \(exitCode)).")
        }
        guard obj["ok"] as? Bool == true else {
            let error = obj["error"] as? String ?? "Unknown error"
            throw slotReadError(for: error)
        }
        let slotsRaw = obj["slots"] as? [[String: Any]] ?? []
        return slotsRaw.compactMap { s -> SnapToneSlot? in
            guard let slot = s["slot"] as? Int else { return nil }
            return SnapToneSlot(slot: slot, name: s["name"] as? String ?? "")
        }.sorted { $0.slot < $1.slot }
    }

    private func slotReadError(for raw: String) -> BackendError {
        if raw.localizedCaseInsensitiveContains("not found") || raw.localizedCaseInsensitiveContains("no gp") {
            return BackendError(summary: "No GP-5/GP-50 is connected.", technicalDetails: raw)
        }
        if raw.localizedCaseInsensitiveContains("timeout") {
            return BackendError(summary: "The GP-5/GP-50 didn't respond in time reading its SnapTone catalogue.", technicalDetails: raw)
        }
        return BackendError(summary: "Failed to read the SnapTone catalogue.", technicalDetails: raw)
    }

    func convert(
        inputNam: URL,
        outputDirectory: URL,
        toneMatch: Bool,
        reference: ToneMatchReference,
        recordedAudio: URL?,
        correctiveIr: URL?,
        gp5DirectFit: Bool,
        onProgress: @escaping (String) -> Void
    ) async throws -> ConvertOutcome {
        var args = ["convert", inputNam.path, "--output", outputDirectory.path, "--json"]
        if toneMatch {
            args.append("--tone-match")
            args.append(contentsOf: ["--reference", reference.rawValue])
        }
        if let recordedAudio { args.append(contentsOf: ["--recorded-audio", recordedAudio.path]) }
        if let correctiveIr { args.append(contentsOf: ["--corrective-ir", correctiveIr.path]) }
        if !gp5DirectFit { args.append("--no-gp5-direct-fit") }

        // Read the "complete" event back out of `lines` (run()'s own
        // synchronously-populated return value) rather than tracking it via
        // a variable mutated inside the onLine callback. onLine is
        // dispatched to the main queue asynchronously for live progress
        // UI -- that dispatch is not guaranteed to have run yet by the time
        // `run()` itself returns, so a captured "did we see complete yet"
        // variable can still read as unset here even though the line was
        // genuinely received. `lines` has no such race: it is fully built
        // before run() resumes.
        let (lines, exitCode) = try await run(args) { line in
            if line["event"] as? String == "progress" {
                onProgress(line["message"] as? String ?? "Working...")
            }
        }
        guard let completeLine = lines.last(where: { ($0["event"] as? String) == "complete" }) else {
            throw BackendError(summary: "Conversion did not complete.", technicalDetails: "namtoclo convert exited (code \(exitCode)) without a completion event.")
        }
        let ok = completeLine["ok"] as? Bool ?? false
        let outcome = ConvertOutcome(
            ok: ok,
            inputPath: completeLine["input"] as? String ?? inputNam.path,
            gp200Path: completeLine["output_gp200"] as? String ?? "",
            gp5Path: completeLine["output_gp5gp50"] as? String ?? "",
            error: completeLine["error"] as? String ?? ""
        )
        if !ok {
            throw convertError(for: outcome.error)
        }
        return outcome
    }

    private func convertError(for raw: String) -> BackendError {
        if raw.localizedCaseInsensitiveContains("nam_input_wav") || raw.localizedCaseInsensitiveContains("stimulus") {
            return BackendError(summary: "A required resource file is missing.", technicalDetails: raw)
        }
        if raw.isEmpty {
            return BackendError(summary: "Conversion failed for an unknown reason.", technicalDetails: "namtoclo reported ok=false with no error message.")
        }
        return BackendError(summary: "Conversion failed.", technicalDetails: raw)
    }

    func upload(
        cloFile: URL,
        slot: Int,
        debugMidi: Bool,
        onProgress: @escaping (Int, Int, String) -> Void
    ) async throws -> UploadOutcome {
        var args = ["upload", cloFile.path, "--slot", String(slot), "--json"]
        if debugMidi { args.append("--debug-midi") }

        // See convert()'s comment above: read "complete" back out of the
        // synchronously-returned `lines`, not a variable set from inside
        // the (asynchronously-dispatched) onLine callback.
        let (lines, exitCode) = try await run(args) { line in
            if line["event"] as? String == "progress" {
                onProgress(line["current"] as? Int ?? 0, line["total"] as? Int ?? 0, line["message"] as? String ?? "")
            }
        }
        guard let completeLine = lines.last(where: { ($0["event"] as? String) == "complete" }) else {
            throw BackendError(summary: "Upload did not complete.", technicalDetails: "namtoclo upload exited (code \(exitCode)) without a completion event.")
        }
        let ok = completeLine["ok"] as? Bool ?? false
        let message = completeLine["message"] as? String ?? ""
        if !ok {
            throw uploadError(for: message)
        }
        return UploadOutcome(ok: ok, slot: slot, message: message)
    }

    private func uploadError(for raw: String) -> BackendError {
        let lower = raw.lowercased()
        if lower.contains("timeout") {
            return BackendError(summary: "The GP-5/GP-50 stopped responding during upload (timeout).", technicalDetails: raw)
        }
        if lower.contains("ack") {
            return BackendError(summary: "The GP-5/GP-50 rejected a data block (no ACK).", technicalDetails: raw)
        }
        if lower.contains("retr") {
            return BackendError(summary: "Upload failed after exhausting retries.", technicalDetails: raw)
        }
        if lower.contains("not found") || lower.contains("no gp") {
            return BackendError(summary: "No GP-5/GP-50 is connected.", technicalDetails: raw)
        }
        if raw.isEmpty {
            return BackendError(summary: "Upload failed for an unknown reason.", technicalDetails: "namtoclo reported ok=false with no message.")
        }
        return BackendError(summary: "Upload failed.", technicalDetails: raw)
    }

    func cloInfo(path: URL) async throws -> CloInfoResult {
        let (lines, exitCode) = try await run(["clo-info", path.path, "--json"])
        guard let obj = lines.first else {
            throw BackendError(summary: "Could not inspect the CLO file.", technicalDetails: "namtoclo clo-info exited with code \(exitCode).")
        }
        return CloInfoResult(
            ok: obj["ok"] as? Bool ?? false,
            path: obj["path"] as? String ?? path.path,
            exists: obj["exists"] as? Bool ?? false,
            size: obj["size"] as? Int ?? 0,
            magic: obj["magic"] as? String ?? "",
            declaredSize: obj["declared_size"] as? String ?? "",
            payloadSize: obj["payload_size"] as? String ?? "",
            modelField: obj["model_field"] as? String ?? ""
        )
    }
}
