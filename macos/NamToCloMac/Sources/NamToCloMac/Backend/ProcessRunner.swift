import Foundation

/// Thin wrapper around `Process` for talking to the `namtoclo` CLI, which is
/// the sole boundary this app crosses into the portable C++ core (see
/// CLAUDE.md's "Cross-platform architecture" and the macOS porting notes).
/// This file contains NO conversion, CLO, CRC, SysEx, or MIDI logic -- only
/// process launch/stream/cancel plumbing. All CLI subcommands are invoked
/// with `--json`, which makes `namtoclo` emit newline-delimited JSON (NDJSON)
/// on stdout and free-form diagnostics on stderr (see src/cli/main.cpp's file
/// header comment for the exact contract).
final class ProcessRunner {
    struct RunError: Error, LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// One parsed NDJSON line from stdout.
    typealias JSONLine = [String: Any]

    private var process: Process?
    private let queue = DispatchQueue(label: "com.namtoclo.mac.processrunner")

    /// Launches `executable` with `arguments`, calling `onLine` on the main
    /// actor for every NDJSON object printed to stdout as it arrives (so a
    /// GUI can show incremental progress rather than waiting for exit), and
    /// `onStderr` for raw stderr text (diagnostics only -- see the debug log
    /// view). Resolves with the full stdout line list and exit code once the
    /// process exits.
    @discardableResult
    func run(
        executable: URL,
        arguments: [String],
        onLine: @escaping (JSONLine) -> Void,
        onStderr: @escaping (String) -> Void = { _ in }
    ) async throws -> (lines: [JSONLine], exitCode: Int32) {
        try await withCheckedThrowingContinuation { continuation in
            queue.async { [weak self] in
                guard let self else { return }
                let process = Process()
                process.executableURL = executable
                process.arguments = arguments

                let stdoutPipe = Pipe()
                let stderrPipe = Pipe()
                process.standardOutput = stdoutPipe
                process.standardError = stderrPipe

                var collected: [JSONLine] = []
                var stdoutBuffer = Data()
                var exitCode: Int32 = -1

                // `Process.terminationHandler` and a pipe's readability
                // notifications are driven by independent mechanisms
                // (waitpid vs. kqueue) and are NOT guaranteed to fire in any
                // particular order. Resolving as soon as terminationHandler
                // fires -- as this used to do -- can race ahead of the pipe
                // actually delivering its last buffered chunk (e.g. the
                // final NDJSON "complete" line, flushed right before the
                // child exits), making a successful run look like it "did
                // not complete" even though the CLI produced correct output.
                // A DispatchGroup that only resolves once BOTH pipes have
                // hit EOF AND the process has terminated closes that race.
                let group = DispatchGroup()
                group.enter() // stdout EOF
                group.enter() // stderr EOF
                group.enter() // process termination

                stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
                    let data = handle.availableData
                    guard !data.isEmpty else {
                        handle.readabilityHandler = nil
                        group.leave()
                        return
                    }
                    stdoutBuffer.append(data)
                    while let newlineRange = stdoutBuffer.firstRange(of: Data([0x0A])) {
                        let lineData = stdoutBuffer.subdata(in: stdoutBuffer.startIndex..<newlineRange.lowerBound)
                        stdoutBuffer.removeSubrange(stdoutBuffer.startIndex..<newlineRange.upperBound)
                        guard !lineData.isEmpty,
                              let obj = try? JSONSerialization.jsonObject(with: lineData) as? JSONLine
                        else { continue }
                        collected.append(obj)
                        DispatchQueue.main.async { onLine(obj) }
                    }
                }
                stderrPipe.fileHandleForReading.readabilityHandler = { handle in
                    let data = handle.availableData
                    guard !data.isEmpty else {
                        handle.readabilityHandler = nil
                        group.leave()
                        return
                    }
                    if let text = String(data: data, encoding: .utf8) {
                        DispatchQueue.main.async { onStderr(text) }
                    }
                }

                process.terminationHandler = { proc in
                    exitCode = proc.terminationStatus
                    group.leave()
                }

                group.notify(queue: self.queue) {
                    continuation.resume(returning: (collected, exitCode))
                }

                self.process = process
                do {
                    try process.run()
                } catch {
                    stdoutPipe.fileHandleForReading.readabilityHandler = nil
                    stderrPipe.fileHandleForReading.readabilityHandler = nil
                    continuation.resume(throwing: RunError(message: "Failed to launch \(executable.lastPathComponent): \(error.localizedDescription)"))
                }
            }
        }
    }

    /// Terminates the in-flight process, if any (best-effort cancellation).
    func cancel() {
        queue.async { [weak self] in
            self?.process?.terminate()
        }
    }
}
