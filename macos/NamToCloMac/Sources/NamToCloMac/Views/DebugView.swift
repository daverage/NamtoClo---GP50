import SwiftUI
import AppKit

struct DebugView: View {
    @EnvironmentObject var appState: AppState
    @State private var didCopy = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Backend").font(.headline)
                Spacer()
                Button(didCopy ? "Copied!" : "Copy Log") { copyLog() }
                    .disabled(appState.diagnosticLog.isEmpty)
                Button("Clear Log") { appState.diagnosticLog.removeAll() }
            }
            Text(appState.backend.executableURL.path)
                .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)

            Text("Every namtoclo command this app runs, its progress output, exit code, and result -- plus, when raw MIDI debug logging is enabled on the GP-5/GP-50 tab, protocol-level trace output (off by default). Select text below to copy manually, or use \"Copy Log\" for the whole thing.")
                .font(.caption).foregroundStyle(.secondary)

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 2) {
                        ForEach(Array(appState.diagnosticLog.enumerated()), id: \.offset) { index, line in
                            Text(line)
                                .font(.system(.caption, design: .monospaced))
                                .textSelection(.enabled)
                                .id(index)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .onChange(of: appState.diagnosticLog.count) { newCount in
                    proxy.scrollTo(newCount - 1, anchor: .bottom)
                }
            }
            .background(Color(nsColor: .textBackgroundColor))
            .border(Color.secondary.opacity(0.2))
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    private func copyLog() {
        let text = appState.diagnosticLog.joined(separator: "\n")
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
        didCopy = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { didCopy = false }
    }
}
