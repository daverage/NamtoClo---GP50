// swift-tools-version: 5.10
import PackageDescription

let package = Package(
    name: "NamToCloMac",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "NamToCloMac",
            path: "Sources/NamToCloMac"
        )
    ]
)
