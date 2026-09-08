import SwiftUI
import AppKit

@main
struct NamToCloMacApp: App {
    @StateObject private var appState = AppState(backend: CLIBackend(executableURL: CLIBackend.resolveBundled()))
    @State private var showingOpenPanel = false
    @NSApplicationDelegateAdaptor(OffscreenWindowRecoveryDelegate.self) private var appDelegate

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

/// macOS/AppKit persists a plain SwiftUI WindowGroup's frame across launches.
/// If the window was last shown on an external display that's since been
/// disconnected, it reopens at that same (now off-screen) position -- the
/// app looks like it launched with "nothing rendering" when really the
/// window just isn't visible on any connected screen. Re-centers any window
/// that doesn't intersect a currently connected screen, once, shortly after
/// launch.
final class OffscreenWindowRecoveryDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
            for window in NSApp.windows {
                let onAnyScreen = NSScreen.screens.contains { $0.frame.intersects(window.frame) }
                if !onAnyScreen {
                    window.center()
                }
            }
        }
    }
}
