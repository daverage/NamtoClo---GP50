import SwiftUI
import UniformTypeIdentifiers

private let namType = UTType(filenameExtension: "nam") ?? .data
private let wavType = UTType(filenameExtension: "wav") ?? .audio

struct Tone3000View: View {
    @EnvironmentObject var appState: AppState
    @State private var showingWavPicker = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                accountSection
                if appState.tone3000Connected == true {
                    searchSection
                    if appState.tone3000SelectedTone != nil {
                        modelsSection
                    }
                    if appState.tone3000DownloadedNamURL != nil {
                        previewSection
                    }
                }
            }
            .padding()
        }
        .task { await appState.checkTone3000Status() }
        .fileImporter(isPresented: $showingWavPicker, allowedContentTypes: [wavType]) { result in
            if case let .success(url) = result { appState.tone3000PreviewInputWav = url }
        }
        .sheet(isPresented: errorBinding) {
            if let error = appState.tone3000Error {
                ErrorSheetView(title: "Tone3000", error: error) {
                    appState.tone3000Error = nil
                }
            }
        }
    }

    private var errorBinding: Binding<Bool> {
        Binding(get: { appState.tone3000Error != nil }, set: { if !$0 { appState.tone3000Error = nil } })
    }

    // MARK: Account

    private var accountSection: some View {
        GroupBox("Tone3000 Account") {
            VStack(alignment: .leading, spacing: 8) {
                if appState.tone3000Connected == true {
                    HStack {
                        Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                        Text("Connected")
                        Spacer()
                        Button("Log Out") { Task { await appState.tone3000Logout() } }
                            .disabled(appState.tone3000IsBusy)
                    }
                } else if appState.tone3000IsBusy {
                    HStack {
                        ProgressView().controlSize(.small)
                        Text(appState.tone3000Connected == nil ? "Checking..." : "Waiting for browser authorization...")
                        Spacer()
                        if appState.tone3000Connected != nil {
                            Button("Cancel") { appState.backend.cancelCurrentOperation() }
                        }
                    }
                } else {
                    Text("Enter your Tone3000 publishable key to connect. Find it in your Tone3000 account API settings.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    HStack {
                        TextField("t3k_pub_...", text: $appState.tone3000PublishableKeyInput)
                            .textFieldStyle(.roundedBorder)
                            .onSubmit { Task { await appState.tone3000Login() } }
                        Button("Connect") { Task { await appState.tone3000Login() } }
                            .disabled(appState.tone3000PublishableKeyInput.isEmpty)
                    }
                }
            }
        }
    }

    // MARK: Search

    private var searchSection: some View {
        GroupBox("Search") {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField("Search NAM captures (e.g. \"5150\")", text: $appState.tone3000Query)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit { Task { await appState.tone3000RunSearch() } }
                    if appState.tone3000IsSearching {
                        ProgressView().controlSize(.small)
                    }
                    Button("Search") { Task { await appState.tone3000RunSearch() } }
                        .disabled(appState.tone3000Query.isEmpty || appState.tone3000IsSearching)
                }
                if !appState.tone3000Tones.isEmpty {
                    List(appState.tone3000Tones, selection: selectedToneBinding) { tone in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(tone.title).fontWeight(.medium)
                            Text("\(tone.creator) · \(tone.gear) · \(tone.modelsCount) model\(tone.modelsCount == 1 ? "" : "s")")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        .tag(tone)
                    }
                    .frame(height: 200)
                    HStack {
                        Text("Page \(appState.tone3000Page) of \(appState.tone3000TotalPages) (\(totalResultsLabel))")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button("Previous") { Task { await appState.tone3000RunSearch(page: appState.tone3000Page - 1) } }
                            .disabled(appState.tone3000Page <= 1 || appState.tone3000IsSearching)
                        Button("Next") { Task { await appState.tone3000RunSearch(page: appState.tone3000Page + 1) } }
                            .disabled(appState.tone3000Page >= appState.tone3000TotalPages || appState.tone3000IsSearching)
                    }
                }
            }
        }
    }

    private var totalResultsLabel: String {
        let count = appState.tone3000Tones.count
        return "\(count) shown"
    }

    private var selectedToneBinding: Binding<Tone3000Tone?> {
        Binding(
            get: { appState.tone3000SelectedTone },
            set: { newValue in
                if let tone = newValue { Task { await appState.tone3000SelectTone(tone) } }
            }
        )
    }

    // MARK: Models

    private var modelsSection: some View {
        GroupBox("Models — \(appState.tone3000SelectedTone?.title ?? "")") {
            VStack(alignment: .leading, spacing: 8) {
                if appState.tone3000IsLoadingModels {
                    HStack { ProgressView().controlSize(.small); Text("Loading models...") }
                } else if appState.tone3000Models.isEmpty {
                    Text("No downloadable NAM models for this tone.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(appState.tone3000Models) { model in
                        HStack {
                            Text(model.name).lineLimit(1)
                            Text(model.size).font(.caption).foregroundStyle(.secondary)
                            Spacer()
                            Button("Download") { Task { await appState.tone3000DownloadModel(model) } }
                                .disabled(appState.tone3000IsDownloading)
                        }
                    }
                    if appState.tone3000IsDownloading {
                        HStack { ProgressView().controlSize(.small); Text("Downloading...") }
                    }
                    if let downloaded = appState.tone3000DownloadedNamURL {
                        Label(downloaded.lastPathComponent, systemImage: "checkmark.seal.fill")
                            .foregroundStyle(.green)
                            .font(.caption)
                    }
                }
            }
        }
    }

    // MARK: Preview

    private var previewSection: some View {
        GroupBox("Preview") {
            VStack(alignment: .leading, spacing: 8) {
                Text(appState.tone3000DownloadedNamURL?.lastPathComponent ?? "")
                    .font(.callout)
                HStack {
                    if let wav = appState.tone3000PreviewInputWav {
                        Text(wav.lastPathComponent).lineLimit(1).truncationMode(.middle)
                    } else {
                        Text("No guitar/DI WAV selected").foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Browse...") { showingWavPicker = true }
                }
                HStack {
                    Button(action: { Task { await appState.tone3000RunPreview(play: true) } }) {
                        Label("Render & Play", systemImage: "play.circle")
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!canPreview)

                    Button("Render Only") { Task { await appState.tone3000RunPreview(play: false) } }
                        .disabled(!canPreview)

                    Button("Use in Convert Tab") {
                        appState.selectedNam = appState.tone3000DownloadedNamURL
                        NotificationCenter.default.post(name: .switchToConvertTab, object: nil)
                    }
                    .disabled(appState.tone3000DownloadedNamURL == nil)

                    if appState.tone3000IsPreviewing {
                        ProgressView().controlSize(.small)
                        Text("Rendering...").font(.caption).foregroundStyle(.secondary)
                    }
                }
                if let outcome = appState.tone3000LastPreviewOutcome {
                    Label(
                        outcome.played ? "Played: \(outcome.outputPath)" : "Rendered: \(outcome.outputPath)",
                        systemImage: "checkmark.seal.fill"
                    )
                    .foregroundStyle(.green)
                    .font(.caption)
                }
            }
        }
    }

    private var canPreview: Bool {
        appState.tone3000DownloadedNamURL != nil && appState.tone3000PreviewInputWav != nil && !appState.tone3000IsPreviewing
    }
}
