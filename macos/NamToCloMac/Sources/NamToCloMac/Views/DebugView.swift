import SwiftUI

struct DebugView: View {
    @EnvironmentObject var appState: AppState

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Backend").font(.headline)
                Spacer()
                Button("Clear Log") { appState.diagnosticLog.removeAll() }
            }
            Text(appState.backend.executableURL.path)
                .font(.caption).foregroundStyle(.secondary).textSelection(.enabled)

            Text("namtoclo diagnostics (stderr) and, when raw MIDI debug logging is enabled on the GP-5/GP-50 tab, protocol-level trace output. Off by default.")
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
}
