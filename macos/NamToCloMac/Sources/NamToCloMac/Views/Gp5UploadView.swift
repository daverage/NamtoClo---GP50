import SwiftUI
import UniformTypeIdentifiers

private let cloType = UTType(filenameExtension: "clo") ?? .data

struct Gp5UploadView: View {
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
            if case let .success(url) = result { appState.uploadSourceURL = url }
        }
        .sheet(isPresented: errorBinding) {
            if let error = appState.uploadError {
                ErrorSheetView(title: "Upload Failed", error: error) {
                    appState.uploadError = nil
                }
            }
        }
    }

    private var errorBinding: Binding<Bool> {
        Binding(get: { appState.uploadError != nil }, set: { if !$0 { appState.uploadError = nil } })
    }

    private var deviceSection: some View {
        GroupBox("Connected Device") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    if appState.isRefreshingDevices {
                        ProgressView().controlSize(.small)
                        Text("Scanning...")
                    } else if let list = appState.midiList, list.gp5Gp50Ready {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                        Text("GP-5/GP-50 connected (\(list.gp5Gp50InputName))")
                    } else {
                        Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
                        Text("No GP-5/GP-50 detected")
                    }
                    Spacer()
                    Button("Refresh") { Task { await appState.refreshDevices() } }
                }
                if let list = appState.midiList, !list.devices.isEmpty {
                    DisclosureGroup("All MIDI devices (\(list.devices.count))") {
                        ForEach(list.devices) { device in
                            HStack {
                                Text(device.name)
                                Spacer()
                                if device.input { Text("in").font(.caption).foregroundStyle(.secondary) }
                                if device.output { Text("out").font(.caption).foregroundStyle(.secondary) }
                            }
                        }
                    }
                    .font(.caption)
                }
            }
        }
    }

    private var slotSection: some View {
        GroupBox("SnapTone Catalogue") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("Destination slot (51-80)")
                    Spacer()
                    if appState.isRefreshingSlots {
                        ProgressView().controlSize(.small)
                    }
                    Button("Rescan") { Task { await appState.refreshSlots() } }
                }
                if appState.slots.isEmpty {
                    Text("No catalogue loaded yet. Rescan with a GP-5/GP-50 connected.")
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    List(appState.slots, selection: Binding(
                        get: { appState.selectedSlot },
                        set: { appState.selectedSlot = $0 }
                    )) { slot in
                        HStack {
                            Text(String(format: "%03d", slot.slot)).monospaced()
                            Text(slot.isEmpty ? "(empty)" : slot.name)
                                .foregroundStyle(slot.isEmpty ? .secondary : .primary)
                        }
                    }
                    .frame(height: 180)
                    if let selectedSlot = appState.selectedSlot {
                        Text("Uploading will REPLACE slot \(selectedSlot).")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
            }
        }
    }

    private var sourceSection: some View {
        GroupBox("CLO Source") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    if let url = appState.uploadSourceURL {
                        Text(url.lastPathComponent).lineLimit(1).truncationMode(.middle)
                    } else {
                        Text("No file selected").foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Use Latest Conversion") {
                        if let candidate = appState.lastConvertResult?.bestUploadCandidate {
                            appState.uploadSourceURL = URL(fileURLWithPath: candidate)
                        }
                    }
                    .disabled(appState.lastConvertResult?.bestUploadCandidate == nil)
                    Button("Browse...") { showingCloPicker = true }
                }
            }
        }
    }

    private var uploadSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button(action: { Task { await appState.runUpload() } }) {
                    Label("Upload", systemImage: "arrow.up.circle")
                        .frame(minWidth: 100)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canUpload)

                if appState.isUploading, let progress = appState.uploadProgress {
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
            if let outcome = appState.lastUploadOutcome, outcome.ok {
                Label("Uploaded to slot \(outcome.slot): \(outcome.message)", systemImage: "checkmark.seal.fill")
                    .foregroundStyle(.green)
                    .font(.caption)
            }
        }
    }

    private var canUpload: Bool {
        appState.uploadSourceURL != nil
            && appState.selectedSlot != nil
            && !appState.isUploading
            && (appState.midiList?.gp5Gp50Ready ?? false)
    }
}
