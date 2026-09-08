import Foundation
import SwiftUI

/// Session-lifetime shared state. Only non-sensitive, reasonable preferences
/// are persisted via @AppStorage (conversion toggles, last-used folder) --
/// selections tied to hardware/files present right now (device, slot,
/// generated CLO) live only in memory, per this app's design notes.
@MainActor
final class AppState: ObservableObject {
    let backend: CLIBackend

    // Convert screen
    @Published var selectedNam: URL?
    @Published var lastConvertResult: ConvertOutcome?
    @Published var isConverting = false
    @Published var convertProgressMessage = ""
    @Published var convertError: BackendError?
    /// When the current (or most recent) conversion started -- Tone Match
    /// conversions genuinely take 30-60+ seconds (full 2048-tap fit, a
    /// separate 512-tap direct fit, then a 6-level Tone Match gain sweep),
    /// with only a one-line status message to show for it. Surfacing elapsed
    /// time in the UI (see ConvertView) avoids that being mistaken for a
    /// hang, which is what prompted adding this.
    @Published var conversionStartedAt: Date?

    @AppStorage("toneMatchEnabled") var toneMatchEnabled = false
    @AppStorage("toneMatchReference") var toneMatchReferenceRaw = ToneMatchReference.auto.rawValue
    @AppStorage("gp5DirectFit") var gp5DirectFit = true
    @AppStorage("lastOutputDirectory") var lastOutputDirectoryPath = ""
    @Published var recordedAudioURL: URL?
    @Published var correctiveIrURL: URL?

    var toneMatchReference: ToneMatchReference {
        get { ToneMatchReference(rawValue: toneMatchReferenceRaw) ?? .auto }
        set { toneMatchReferenceRaw = newValue.rawValue }
    }

    // GP-5/GP-50 screen
    @Published var midiList: MidiListResult?
    @Published var isRefreshingDevices = false
    @Published var slots: [SnapToneSlot] = []
    @Published var isRefreshingSlots = false
    @Published var selectedSlot: Int?
    @Published var uploadSourceURL: URL?
    @Published var isUploading = false
    @Published var uploadProgress: (current: Int, total: Int, message: String)?
    @Published var uploadError: BackendError?
    @Published var lastUploadOutcome: UploadOutcome?
    @AppStorage("debugMidiEnabled") var debugMidiEnabled = false

    // Debug
    @Published var diagnosticLog: [String] = []

    init(backend: CLIBackend) {
        self.backend = backend
        backend.onDiagnostic = { [weak self] text in
            Task { @MainActor in
                self?.appendDiagnostic(text)
            }
        }
    }

    func appendDiagnostic(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        diagnosticLog.append(trimmed)
        if diagnosticLog.count > 500 { diagnosticLog.removeFirst(diagnosticLog.count - 500) }
    }

    var outputDirectory: URL {
        if !lastOutputDirectoryPath.isEmpty {
            return URL(fileURLWithPath: lastOutputDirectoryPath)
        }
        return selectedNam?.deletingLastPathComponent() ?? FileManager.default.homeDirectoryForCurrentUser
    }

    func rememberOutputDirectory(_ url: URL) {
        lastOutputDirectoryPath = url.path
    }

    // MARK: Convert

    func runConversion() async {
        guard let selectedNam else { return }
        isConverting = true
        convertError = nil
        convertProgressMessage = "Starting..."
        conversionStartedAt = Date()
        defer { isConverting = false }
        do {
            let result = try await backend.convert(
                inputNam: selectedNam,
                outputDirectory: outputDirectory,
                toneMatch: toneMatchEnabled,
                reference: toneMatchReference,
                recordedAudio: recordedAudioURL,
                correctiveIr: correctiveIrURL,
                gp5DirectFit: gp5DirectFit,
                onProgress: { [weak self] message in
                    self?.convertProgressMessage = message
                    self?.appendDiagnostic("[convert] " + message)
                }
            )
            lastConvertResult = result
            appendDiagnostic("[convert] complete: ok=\(result.ok) gp200=\(result.gp200Path) gp5=\(result.gp5Path)")
            if let candidate = result.bestUploadCandidate {
                uploadSourceURL = URL(fileURLWithPath: candidate)
            }
        } catch let error as BackendError {
            convertError = error
            appendDiagnostic("[convert] failed: \(error.summary) -- \(error.technicalDetails)")
        } catch {
            convertError = BackendError(summary: "Conversion failed unexpectedly.", technicalDetails: error.localizedDescription)
            appendDiagnostic("[convert] failed unexpectedly: \(error.localizedDescription)")
        }
    }

    // MARK: GP-5/GP-50

    func refreshDevices() async {
        isRefreshingDevices = true
        defer { isRefreshingDevices = false }
        do {
            midiList = try await backend.listMidiDevices()
        } catch {
            appendDiagnostic("midi-list failed: \(error.localizedDescription)")
        }
    }

    func refreshSlots() async {
        isRefreshingSlots = true
        uploadError = nil
        defer { isRefreshingSlots = false }
        do {
            slots = try await backend.readSlots()
        } catch let error as BackendError {
            uploadError = error
            slots = []
        } catch {
            uploadError = BackendError(summary: "Failed to read slots.", technicalDetails: error.localizedDescription)
            slots = []
        }
    }

    func runUpload() async {
        guard let uploadSourceURL, let selectedSlot else { return }
        isUploading = true
        uploadError = nil
        uploadProgress = (0, 0, "Starting upload...")
        defer { isUploading = false }
        do {
            let outcome = try await backend.upload(
                cloFile: uploadSourceURL,
                slot: selectedSlot,
                debugMidi: debugMidiEnabled,
                onProgress: { [weak self] current, total, message in
                    self?.uploadProgress = (current, total, message)
                    self?.appendDiagnostic("[upload] [\(current)/\(total)] " + message)
                }
            )
            lastUploadOutcome = outcome
            appendDiagnostic("[upload] complete: ok=\(outcome.ok) slot=\(outcome.slot) message=\(outcome.message)")
            await refreshSlots()
        } catch let error as BackendError {
            uploadError = error
            appendDiagnostic("[upload] failed: \(error.summary) -- \(error.technicalDetails)")
        } catch {
            uploadError = BackendError(summary: "Upload failed unexpectedly.", technicalDetails: error.localizedDescription)
            appendDiagnostic("[upload] failed unexpectedly: \(error.localizedDescription)")
        }
    }
}
