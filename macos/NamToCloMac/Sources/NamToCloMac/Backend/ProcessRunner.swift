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

                // Only ever called while scheduled on `self.queue`, from
                // either the readabilityHandler hop below or the guaranteed
                // final drain in terminationHandler -- both routes funnel
                // through here so stdoutBuffer/collected are never touched
                // from two places at once.
                func consumeStdout(_ data: Data) {
                    guard !data.isEmpty else { return }
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

                stdoutPipe.fileHandleForReading.readabilityHandler = { handle in
                    let data = handle.availableData
                    guard !data.isEmpty else {
                        handle.readabilityHandler = nil
                        return
                    }
                    self.queue.async { consumeStdout(data) }
                }
                stderrPipe.fileHandleForReading.readabilityHandler = { handle in
                    let data = handle.availableData
                    guard !data.isEmpty else {
                        handle.readabilityHandler = nil
                        return
                    }
                    if let text = String(data: data, encoding: .utf8) {
                        DispatchQueue.main.async { onStderr(text) }
                    }
                }

                process.terminationHandler = { proc in
                    let exitCode = proc.terminationStatus
                    self.queue.async {
                        // Guaranteed final drain. `readabilityHandler`'s
                        // async EOF notification can lag or, for a
                        // fast-closing process whose last write and pipe
                        // close happen essentially simultaneously, be missed
                        // entirely for that final chunk -- a known
                        // Process/Pipe/readabilityHandler quirk. This
                        // silently dropped namtoclo's final "complete" NDJSON
                        // event on some runs even though the process had
                        // exited cleanly (exit code 0) and had genuinely
                        // written and flushed that line. Once
                        // terminationHandler fires the process has
                        // definitely exited, so both pipes' write ends are
                        // guaranteed closed and this synchronous read can
                        // never block -- it just returns whatever is left,
                        // which a plain wait-for-EOF approach could lose.
                        stdoutPipe.fileHandleForReading.readabilityHandler = nil
                        let restStdout = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
                        consumeStdout(restStdout)

                        stderrPipe.fileHandleForReading.readabilityHandler = nil
                        let restStderr = stderrPipe.fileHandleForReading.readDataToEndOfFile()
                        if !restStderr.isEmpty, let text = String(data: restStderr, encoding: .utf8) {
                            DispatchQueue.main.async { onStderr(text) }
                        }

                        continuation.resume(returning: (collected, exitCode))
                    }
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
