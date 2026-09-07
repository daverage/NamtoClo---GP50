import SwiftUI

@main
struct NamToCloMacApp: App {
    @StateObject private var appState = AppState(backend: CLIBackend(executableURL: CLIBackend.resolveBundled()))
    @State private var showingOpenPanel = false

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(appState)
                .frame(minWidth: 720, minHeight: 520)
        }
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Open NAM File...") {
                    NotificationCenter.default.post(name: .requestOpenNam, object: nil)
                }
                .keyboardShortcut("o", modifiers: .command)
            }
        }
    }
}

extension Notification.Name {
    static let requestOpenNam = Notification.Name("requestOpenNam")
}
