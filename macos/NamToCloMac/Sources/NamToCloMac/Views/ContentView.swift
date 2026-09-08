import SwiftUI

private enum AppTab: Hashable {
    case convert, tone3000, gp5, gp200, debug
}

struct ContentView: View {
    @State private var selectedTab: AppTab = .convert

    var body: some View {
        TabView(selection: $selectedTab) {
            ConvertView()
                .tabItem { Label("Convert", systemImage: "waveform") }
                .tag(AppTab.convert)
            Tone3000View()
                .tabItem { Label("Tone3000", systemImage: "magnifyingglass") }
                .tag(AppTab.tone3000)
            Gp5UploadView()
                .tabItem { Label("GP-5 / GP-50", systemImage: "cable.connector") }
                .tag(AppTab.gp5)
            Gp200PlaceholderView()
                .tabItem { Label("GP-200", systemImage: "cable.connector.horizontal") }
                .tag(AppTab.gp200)
            DebugView()
                .tabItem { Label("Debug", systemImage: "ladybug") }
                .tag(AppTab.debug)
        }
        .padding()
        .onReceive(NotificationCenter.default.publisher(for: .switchToUploadTab)) { _ in
            selectedTab = .gp5
        }
        .onReceive(NotificationCenter.default.publisher(for: .switchToConvertTab)) { _ in
            selectedTab = .convert
        }
    }
}

/// GP-200 upload isn't wired into the native `namtoclo` CLI yet (see
/// CLAUDE.md: gp200_midi.cpp/gp200_clo_upload.cpp are portable but not
/// exposed as CLI subcommands) -- shown honestly as not-yet-available rather
/// than faking controls that would silently do nothing.
struct Gp200PlaceholderView: View {
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "wrench.and.screwdriver")
                .font(.system(size: 36))
                .foregroundStyle(.secondary)
            Text("GP-200 upload isn't available yet")
                .font(.headline)
            Text("The GP-200 protocol logic already exists in the shared native core, but it isn't exposed through the namtoclo command-line backend this app drives yet. GP-200 conversion (the 1024-tap CLO) still happens automatically as part of every Convert -- only direct upload to a GP-200's SnapTone slots is unavailable here.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
