import SwiftUI
import UniformTypeIdentifiers

private let cloType = UTType(filenameExtension: "clo") ?? .data

/// GP-200 SnapTone upload. Simpler than Gp5UploadView by design: GP-200 has
/// no on-device catalogue readback, just a fixed 10-slot list (AMP 1-5,
/// DIST 1-5 -- see Gp200Slot in BackendTypes.swift and CLAUDE.md's GP-200
/// upload section), so there's no rescan/delete/name-list UI here.
struct Gp200UploadView: View {
    @EnvironmentObject var appState: AppState
    @State private var showingCloPicker = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                deviceSection
                slotSection
                sourceSection
                uploadSection
            }
            .padding()
        }
        .task { await appState.refreshDevices() }
        .fileImporter(isPresented: $showingCloPicker, allowedContentTypes: [cloType]) { result in
            if case let .success(url) = result { appState.gp200UploadSourceURL = url }
        }
        .sheet(isPresented: errorBinding) {
            if let error = appState.gp200UploadError {
                ErrorSheetView(title: "Upload Failed", error: error) {
                    appState.gp200UploadError = nil
                }
            }
        }
    }

    private var errorBinding: Binding<Bool> {
        Binding(get: { appState.gp200UploadError != nil }, set: { if !$0 { appState.gp200UploadError = nil } })
    }

    private var deviceSection: some View {
        GroupBox("Connected Device") {
            HStack {
                if appState.isRefreshingDevices {
                    ProgressView().controlSize(.small)
                    Text("Scanning...")
                } else if let list = appState.midiList, list.gp5Gp50Ready {
                    // GP-5/GP-50 auto-detection is what namtoclo's MIDI layer
                    // reports today; a real GP-200 shows up in "All MIDI
                    // devices" below even though it isn't a recognized
                    // GP-5/GP-50, since GP-200 has no dedicated detection
                    // probe in midi-list yet -- gp200-upload connects
                    // directly by name at upload time regardless.
                    Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                    Text("MIDI device detected (\(list.gp5Gp50InputName))")
                } else {
                    Image(systemName: "questionmark.circle").foregroundStyle(.secondary)
                    Text("No recognized MIDI device -- connect your GP-200 and Refresh")
                }
                Spacer()
                Button("Refresh") { Task { await appState.refreshDevices() } }
            }
        }
    }

    private var slotSection: some View {
        GroupBox("Destination Slot") {
            VStack(alignment: .leading, spacing: 8) {
                Text("GP-200 has 10 fixed SnapTone slots (AMP 1-5, DIST 1-5) -- no on-device name readback.")
                    .font(.caption).foregroundStyle(.secondary)
                Picker("Slot", selection: Binding(
                    get: { appState.gp200SelectedSlot },
                    set: { appState.gp200SelectedSlot = $0 }
                )) {
                    Text("Select a slot...").tag(Gp200Slot?.none)
                    ForEach(Gp200Slot.allCases) { slot in
                        Text(slot.displayName).tag(Gp200Slot?.some(slot))
                    }
                }
                .pickerStyle(.menu)
                .frame(maxWidth: 220)
                if appState.gp200SelectedSlot != nil {
                    Text("Uploading will REPLACE this SnapTone slot on the device.")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }
            }
        }
    }

    private var sourceSection: some View {
        GroupBox("CLO Source") {
            HStack {
                if let url = appState.gp200UploadSourceURL {
                    Text(url.lastPathComponent).lineLimit(1).truncationMode(.middle)
                } else {
                    Text("No file selected").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Use Latest Conversion") {
                    if let candidate = appState.lastConvertResult, !candidate.gp200Path.isEmpty {
                        appState.gp200UploadSourceURL = URL(fileURLWithPath: candidate.gp200Path)
                    }
                }
                .disabled((appState.lastConvertResult?.gp200Path ?? "").isEmpty)
                Button("Browse...") { showingCloPicker = true }
            }
        }
    }

    private var uploadSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button(action: { Task { await appState.runGp200Upload() } }) {
                    Label("Upload", systemImage: "arrow.up.circle")
                        .frame(minWidth: 100)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canUpload)

                if appState.gp200IsUploading, let progress = appState.gp200UploadProgress {
                    if progress.total > 0 {
                        ProgressView(value: Double(progress.current), total: Double(progress.total))
                            .frame(width: 140)
                        Text("\(progress.current)/\(progress.total)").font(.caption)
                    } else {
                        ProgressView().controlSize(.small)
                    }
                    Text(progress.message).font(.caption).foregroundStyle(.secondary)
                }
            }
            Toggle("Enable raw MIDI debug logging", isOn: $appState.debugMidiEnabled)
                .font(.caption)
            if let outcome = appState.gp200LastUploadOutcome, outcome.ok {
                Label("Uploaded to \(appState.gp200SelectedSlot?.displayName ?? "slot"): \(outcome.message)", systemImage: "checkmark.seal.fill")
                    .foregroundStyle(.green)
                    .font(.caption)
            }
        }
    }

    private var canUpload: Bool {
        appState.gp200UploadSourceURL != nil
            && appState.gp200SelectedSlot != nil
            && !appState.gp200IsUploading
    }
}
