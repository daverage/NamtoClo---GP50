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

    // Tone3000 (macOS-only -- see net_client.hpp / tone3000_client.cpp).
    // `tone3000Connected == nil` means "not checked yet this launch"; the
    // publishable key/refresh token themselves live in the macOS Keychain
    // (namtoclo tone3000 login/status), not here -- this is UI state only.
    @Published var tone3000Connected: Bool?
    @Published var tone3000IsBusy = false // covers status/login/logout
    @Published var tone3000Error: BackendError?
    @Published var tone3000PublishableKeyInput = ""

    @Published var tone3000Query = ""
    @Published var tone3000Tones: [Tone3000Tone] = []
    @Published var tone3000Page = 1
    @Published var tone3000TotalPages = 1
    @Published var tone3000IsSearching = false

    @Published var tone3000SelectedTone: Tone3000Tone?
    @Published var tone3000Models: [Tone3000Model] = []
    @Published var tone3000IsLoadingModels = false

    @AppStorage("tone3000DownloadDirectory") var tone3000DownloadDirectoryPath = ""
    @Published var tone3000IsDownloading = false
    @Published var tone3000DownloadedNamURL: URL?

    @Published var tone3000PreviewInputWav: URL?
    @Published var tone3000IsPreviewing = false
    @Published var tone3000LastPreviewOutcome: Tone3000PreviewOutcome?

    var tone3000DownloadDirectory: URL {
        tone3000DownloadDirectoryPath.isEmpty
            ? FileManager.default.urls(for: .musicDirectory, in: .userDomainMask).first ?? FileManager.default.homeDirectoryForCurrentUser
            : URL(fileURLWithPath: tone3000DownloadDirectoryPath)
    }

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

    // MARK: Tone3000

    /// Cheap connection check -- call on tab appear, not on every keystroke.
    func checkTone3000Status() async {
        tone3000IsBusy = true
        defer { tone3000IsBusy = false }
        do {
            tone3000Connected = try await backend.tone3000Status()
        } catch {
            // A failed status check just means "assume not connected", not a
            // user-facing error -- login will surface the real reason.
            tone3000Connected = false
            appendDiagnostic("[tone3000] status check failed: \(error.localizedDescription)")
        }
    }

    func tone3000Login() async {
        guard !tone3000PublishableKeyInput.isEmpty else { return }
        tone3000IsBusy = true
        tone3000Error = nil
        defer { tone3000IsBusy = false }
        do {
            try await backend.tone3000Login(publishableKey: tone3000PublishableKeyInput)
            tone3000Connected = true
            tone3000PublishableKeyInput = ""
            appendDiagnostic("[tone3000] login: connected")
        } catch let error as BackendError {
            tone3000Error = error
            appendDiagnostic("[tone3000] login failed: \(error.summary) -- \(error.technicalDetails)")
        } catch {
            tone3000Error = BackendError(summary: "Tone3000 login failed unexpectedly.", technicalDetails: error.localizedDescription)
        }
    }

    func tone3000Logout() async {
        tone3000IsBusy = true
        defer { tone3000IsBusy = false }
        do {
            try await backend.tone3000Logout()
        } catch {
            appendDiagnostic("[tone3000] logout failed: \(error.localizedDescription)")
        }
        tone3000Connected = false
        tone3000Tones = []
        tone3000Models = []
        tone3000SelectedTone = nil
    }

    func tone3000RunSearch(page: Int = 1) async {
        guard !tone3000Query.isEmpty else { return }
        tone3000IsSearching = true
        tone3000Error = nil
        tone3000SelectedTone = nil
        tone3000Models = []
        defer { tone3000IsSearching = false }
        do {
            let result = try await backend.tone3000Search(query: tone3000Query, page: page, sort: "")
            tone3000Tones = result.tones
            tone3000Page = result.page
            tone3000TotalPages = result.totalPages
        } catch let error as BackendError {
            tone3000Error = error
            tone3000Tones = []
        } catch {
            tone3000Error = BackendError(summary: "Tone3000 search failed unexpectedly.", technicalDetails: error.localizedDescription)
            tone3000Tones = []
        }
    }

    func tone3000SelectTone(_ tone: Tone3000Tone) async {
        tone3000SelectedTone = tone
        tone3000IsLoadingModels = true
        tone3000Error = nil
        defer { tone3000IsLoadingModels = false }
        do {
            tone3000Models = try await backend.tone3000Models(toneId: tone.id)
        } catch let error as BackendError {
            tone3000Error = error
            tone3000Models = []
        } catch {
            tone3000Error = BackendError(summary: "Tone3000 model list failed unexpectedly.", technicalDetails: error.localizedDescription)
            tone3000Models = []
        }
    }

    func tone3000DownloadModel(_ model: Tone3000Model) async {
        tone3000IsDownloading = true
        tone3000Error = nil
        defer { tone3000IsDownloading = false }
        do {
            let url = try await backend.tone3000Download(modelId: model.id, toneId: model.toneId, outputDirectory: tone3000DownloadDirectory)
            tone3000DownloadedNamURL = url
            appendDiagnostic("[tone3000] downloaded: \(url.path)")
        } catch let error as BackendError {
            tone3000Error = error
        } catch {
            tone3000Error = BackendError(summary: "Tone3000 download failed unexpectedly.", technicalDetails: error.localizedDescription)
        }
    }

    func tone3000RunPreview(play: Bool) async {
        guard let namURL = tone3000DownloadedNamURL, let inputWav = tone3000PreviewInputWav else { return }
        tone3000IsPreviewing = true
        tone3000Error = nil
        defer { tone3000IsPreviewing = false }
        do {
            let outcome = try await backend.tone3000Preview(namPath: namURL, inputWav: inputWav, outputWav: nil, play: play)
            tone3000LastPreviewOutcome = outcome
            appendDiagnostic("[tone3000] preview rendered: \(outcome.outputPath) played=\(outcome.played)")
        } catch let error as BackendError {
            tone3000Error = error
            appendDiagnostic("[tone3000] preview failed: \(error.summary) -- \(error.technicalDetails)")
        } catch {
            tone3000Error = BackendError(summary: "Preview failed unexpectedly.", technicalDetails: error.localizedDescription)
        }
    }
}
