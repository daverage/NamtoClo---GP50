import SwiftUI
import UniformTypeIdentifiers
import AppKit

private let namType = UTType(filenameExtension: "nam") ?? .data
private let wavType = UTType(filenameExtension: "wav") ?? .audio

/// Which file picker is currently active. SwiftUI does not reliably support
/// stacking several independent `.fileImporter` modifiers on the same view
/// -- only one of them ends up wired to its button in practice -- so every
/// "Choose..." button in this screen shares a single `.fileImporter`, keyed
/// by this enum, instead of each getting its own `@State`/modifier pair.
private enum ConvertPicker: Identifiable {
    case nam, recordedAudio, correctiveIr, output
    var id: Self { self }

    var allowedContentTypes: [UTType] {
        switch self {
        case .nam: return [namType, .item]
        case .recordedAudio, .correctiveIr: return [wavType]
        case .output: return [.folder]
        }
    }
}

struct ConvertView: View {
    @EnvironmentObject var appState: AppState
    // Deliberately two separate pieces of state, not one Optional: SwiftUI
    // toggles `isPickerPresented` back to false as part of dismissing the
    // panel, which can happen before the `.fileImporter` completion closure
    // below runs. If `activePicker` were cleared by that same isPresented
    // binding (as it originally was), the completion closure could read it
    // as nil and silently drop the picked file. Keeping `activePicker`
    // independent means it's still valid whenever the completion fires.
    @State private var activePicker: ConvertPicker?
    @State private var isPickerPresented = false
    @State private var isDropTargeted = false

    private func presentPicker(_ picker: ConvertPicker) {
        activePicker = picker
        isPickerPresented = true
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                inputSection
                targetSection
                settingsSection
                convertButtonSection
                if let result = appState.lastConvertResult, result.ok {
                    resultSection(result)
                }
            }
            .padding()
        }
        .onReceive(NotificationCenter.default.publisher(for: .requestOpenNam)) { _ in
            presentPicker(.nam)
        }
        .fileImporter(
            isPresented: $isPickerPresented,
            allowedContentTypes: activePicker?.allowedContentTypes ?? [.item]
        ) { result in
            guard case let .success(url) = result, let picker = activePicker else { return }
            switch picker {
            case .nam: appState.selectedNam = url
            case .recordedAudio: appState.recordedAudioURL = url
            case .correctiveIr: appState.correctiveIrURL = url
            case .output: appState.rememberOutputDirectory(url)
            }
        }
        .sheet(isPresented: errorBinding) {
            if let error = appState.convertError {
                ErrorSheetView(title: "Conversion Failed", error: error) {
                    appState.convertError = nil
                }
            }
        }
    }

    private var errorBinding: Binding<Bool> {
        Binding(get: { appState.convertError != nil }, set: { if !$0 { appState.convertError = nil } })
    }

    // MARK: Sections

    private var inputSection: some View {
        GroupBox("NAM Input") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Image(systemName: "doc.badge.gearshape")
                    if let nam = appState.selectedNam {
                        Text(nam.lastPathComponent).font(.body)
                    } else {
                        Text("No file selected").foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Choose...") { presentPicker(.nam) }
                }
                if let nam = appState.selectedNam {
                    Text(nam.path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(isDropTargeted ? Color.accentColor : Color.secondary.opacity(0.3), style: StrokeStyle(lineWidth: 1.5, dash: [5]))
                    .frame(height: 44)
                    .overlay(Text("or drop a .nam file here").font(.caption).foregroundStyle(.secondary))
                    .onDrop(of: [.fileURL], isTargeted: $isDropTargeted, perform: handleDrop)
            }
        }
    }

    private func handleDrop(providers: [NSItemProvider]) -> Bool {
        guard let provider = providers.first else { return false }
        _ = provider.loadObject(ofClass: URL.self) { url, _ in
            guard let url else { return }
            DispatchQueue.main.async { appState.selectedNam = url }
        }
        return true
    }

    private var targetSection: some View {
        GroupBox("Target") {
            HStack {
                Label("GP-5 / GP-50", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Spacer()
                Text("GP-200 CLO is always produced too").font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var settingsSection: some View {
        GroupBox("Conversion Settings") {
            VStack(alignment: .leading, spacing: 10) {
                Toggle("Tone Match", isOn: $appState.toneMatchEnabled)
                if appState.toneMatchEnabled {
                    Picker("Reference", selection: Binding(
                        get: { appState.toneMatchReference },
                        set: { appState.toneMatchReference = $0 }
                    )) {
                        ForEach(ToneMatchReference.allCases) { ref in
                            Text(ref.displayName).tag(ref)
                        }
                    }
                    .pickerStyle(.menu)
                    .padding(.leading, 20)
                }

                Divider()

                HStack {
                    Text("Recorded Audio (last 20s tail)").frame(width: 220, alignment: .leading)
                    if let url = appState.recordedAudioURL {
                        Text(url.lastPathComponent).lineLimit(1).truncationMode(.middle)
                        Button("Clear") { appState.recordedAudioURL = nil }
                    } else {
                        Text("Original preset audio").foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Choose...") { presentPicker(.recordedAudio) }
                }

                HStack {
                    Text("Corrective IR").frame(width: 220, alignment: .leading)
                    if let url = appState.correctiveIrURL {
                        Text(url.lastPathComponent).lineLimit(1).truncationMode(.middle)
                        Button("Clear") { appState.correctiveIrURL = nil }
                    } else {
                        Text("None").foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Choose...") { presentPicker(.correctiveIr) }
                }

                Divider()

                Toggle("GP-5/GP-50 direct Block-B fit (recommended)", isOn: $appState.gp5DirectFit)

                Divider()

                HStack {
                    Text("Output Folder").frame(width: 220, alignment: .leading)
                    Text(appState.outputDirectory.path).font(.caption).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Button("Choose...") { presentPicker(.output) }
                }
            }
        }
    }

    private var convertButtonSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button(action: { Task { await appState.runConversion() } }) {
                    Label("Convert", systemImage: "arrow.triangle.2.circlepath")
                        .frame(minWidth: 100)
                }
                .buttonStyle(.borderedProminent)
                .disabled(appState.selectedNam == nil || appState.isConverting)

                if appState.isConverting {
                    ProgressView().controlSize(.small)
                    Text(appState.convertProgressMessage).font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func resultSection(_ result: ConvertOutcome) -> some View {
        GroupBox("Result") {
            VStack(alignment: .leading, spacing: 8) {
                if !result.gp200Path.isEmpty {
                    fileRow(label: "GP-200 CLO", path: result.gp200Path)
                }
                if !result.gp5Path.isEmpty {
                    fileRow(label: "GP-5/GP-50 CLO", path: result.gp5Path)
                }
                HStack {
                    Button("Upload to GP-5/GP-50") {
                        NotificationCenter.default.post(name: .switchToUploadTab, object: nil)
                    }
                    .disabled(result.bestUploadCandidate == nil)
                }
            }
        }
    }

    private func fileRow(label: String, path: String) -> some View {
        HStack {
            Text(label).frame(width: 110, alignment: .leading).font(.caption).foregroundStyle(.secondary)
            Text((path as NSString).lastPathComponent).lineLimit(1).truncationMode(.middle)
            Spacer()
            Button("Reveal in Finder") {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            }
        }
    }
}

extension Notification.Name {
    static let switchToUploadTab = Notification.Name("switchToUploadTab")
}
