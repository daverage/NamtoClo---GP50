#!/usr/bin/env bash
# Builds NamToClo.app: the native C++ backend (namtoclo CLI, via CMake) plus
# the SwiftUI shell (NamToCloMac, via SwiftPM), bundled into one relocatable
# .app the user can double-click from Finder. No manual copying required.
#
# Usage: scripts/build_macos_app.sh [--debug]
#   --debug   build the Swift app in debug configuration (CMake side is
#             always Release; the CLI has no debug/release quality difference
#             a user would want to trade off).
#
# Output: build-macos-app/NamToClo.app

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CMAKE_BUILD_DIR="$REPO_ROOT/build-macos"
SWIFT_DIR="$REPO_ROOT/macos/NamToCloMac"
APP_OUT_DIR="$REPO_ROOT/build-macos-app"
APP_BUNDLE="$APP_OUT_DIR/NamToClo.app"

SWIFT_CONFIG="release"
if [[ "${1:-}" == "--debug" ]]; then
    SWIFT_CONFIG="debug"
fi

echo "==> [1/4] Building native backend (namtoclo CLI) with CMake"
cmake --preset macos-arm64 -S "$REPO_ROOT" -B "$CMAKE_BUILD_DIR" >/dev/null
cmake --build "$CMAKE_BUILD_DIR" --target namtoclo --parallel

NAMTOCLO_BIN="$CMAKE_BUILD_DIR/namtoclo"
if [[ ! -x "$NAMTOCLO_BIN" ]]; then
    echo "error: $NAMTOCLO_BIN not found or not executable after build" >&2
    exit 1
fi

echo "==> [2/4] Building SwiftUI app ($SWIFT_CONFIG) with SwiftPM"
(cd "$SWIFT_DIR" && swift build -c "$SWIFT_CONFIG")
SWIFT_BIN="$SWIFT_DIR/.build/$SWIFT_CONFIG/NamToCloMac"
if [[ ! -x "$SWIFT_BIN" ]]; then
    echo "error: $SWIFT_BIN not found or not executable after build" >&2
    exit 1
fi

echo "==> [3/4] Assembling NamToClo.app"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS" "$APP_BUNDLE/Contents/Resources"

cp "$SWIFT_BIN" "$APP_BUNDLE/Contents/MacOS/NamToCloMac"
cp "$NAMTOCLO_BIN" "$APP_BUNDLE/Contents/MacOS/namtoclo"

# Resources namtoclo needs at runtime (same convention as the Windows build's
# post-build copy step -- see CLAUDE.md's "Build" section).
if [[ -f "$CMAKE_BUILD_DIR/nam_input_wav.wav" ]]; then
    cp "$CMAKE_BUILD_DIR/nam_input_wav.wav" "$APP_BUNDLE/Contents/MacOS/nam_input_wav.wav"
fi
if [[ -d "$CMAKE_BUILD_DIR/resources" ]]; then
    cp -R "$CMAKE_BUILD_DIR/resources" "$APP_BUNDLE/Contents/MacOS/resources"
fi

cat > "$APP_BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>NamToClo</string>
    <key>CFBundleDisplayName</key>
    <string>NamToClo</string>
    <key>CFBundleIdentifier</key>
    <string>com.namtoclo.mac</string>
    <key>CFBundleVersion</key>
    <string>1.0</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>NamToCloMac</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>LSMinimumSystemVersion</key>
    <string>13.0</string>
    <key>NSHighResolutionCapable</key>
    <true/>
    <key>LSApplicationCategoryType</key>
    <string>public.app-category.music</string>
    <key>NSHumanReadableCopyright</key>
    <string>Independent research/reimplementation project. Not affiliated with or endorsed by Valeton or Hotone.</string>
</dict>
</plist>
PLIST

if [[ -f "$REPO_ROOT/macos/NamToCloMac/Resources/AppIcon.icns" ]]; then
    cp "$REPO_ROOT/macos/NamToCloMac/Resources/AppIcon.icns" "$APP_BUNDLE/Contents/Resources/AppIcon.icns"
fi

echo "==> [4/4] Ad-hoc code-signing (local use only)"
codesign --force --deep --sign - "$APP_BUNDLE"

echo ""
echo "Built: $APP_BUNDLE"
echo "Run it with: open \"$APP_BUNDLE\""
