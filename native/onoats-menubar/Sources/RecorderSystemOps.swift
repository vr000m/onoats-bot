// Process, filesystem, clock and polling boundaries for RecorderModel.
import AppKit
import Darwin
import Foundation

protocol ProcessHandle: AnyObject {
    var pid: Int32 { get }
    var isRunning: Bool { get }
    var terminationStatus: Int32 { get }
    func terminate()
}

protocol PollHandle: AnyObject { func cancel() }

/// Retained authority for one exclusively-created app-owned run log. The URL
/// is presentation metadata only; child output and machine reads stay on the
/// reserved inode through duplicated descriptors.
protocol RunLogSink: AnyObject {
    var url: URL { get }
    func duplicateOutputHandle() throws -> FileHandle?
    func readTail(maxBytes: Int) -> String?
    func closeOwner()
}

/// Compatibility sink for injected SystemOps implementations. Production
/// NativeSystemOps overrides reservation with DescriptorRunLogSink below.
private final class URLRunLogSink: RunLogSink {
    let url: URL
    private let tailReader: (Int) -> String?
    init(url: URL, tailReader: @escaping (Int) -> String?) {
        self.url = url
        self.tailReader = tailReader
    }
    func duplicateOutputHandle() throws -> FileHandle? { nil }
    func readTail(maxBytes: Int) -> String? {
        tailReader(maxBytes)
    }
    func closeOwner() {}
}

struct ProcessRequest {
    let executable: URL
    let arguments: [String]
    let environment: [String: String]?
    let outputSink: RunLogSink?
    var appendOutputTo: URL? { outputSink?.url }

    init(
        executable: URL, arguments: [String], environment: [String: String]?,
        outputSink: RunLogSink?
    ) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
        self.outputSink = outputSink
    }
}

/// Keeps optional Process.arguments construction semantics while process
/// creation itself remains wholly behind SystemOps.
struct ProcessArguments {
    var arguments: [String]?
}

/// Outcome of one bounded process-identity probe. A timeout, overflow or launch
/// failure is `unknown`, never `absent`: a slow `ps` says nothing about whether
/// the recorder is alive, so callers keep their last proven verdict instead of
/// reading it as "dead".
enum CommandLineProbe: Equatable {
    /// The process exists and this is its full command line.
    case command(String)
    /// `ps` proved no such process (exit 1, no output).
    case absent
    /// The probe did not prove either; the caller must not infer liveness.
    case unknown
}

/// All process, signal, file, clock and polling effects used by the model.
@MainActor
protocol SystemOps: AnyObject {
    func spawn(
        _ request: ProcessRequest,
        onExit: @escaping @MainActor (ProcessHandle) -> Void
    ) throws -> ProcessHandle
    func processIsAlive(_ pid: Int32) -> Bool
    func commandLine(_ pid: Int32) -> CommandLineProbe
    /// Every read is bounded and no-follow; there is deliberately no unbounded
    /// read requirement, so a conformer cannot silently regress to one.
    func readData(at url: URL, maxBytes: Int) -> Data?
    func readTail(at url: URL, maxBytes: Int) -> String?
    func write(_ data: Data, to url: URL) throws
    func reserveFile(at url: URL) throws -> Bool
    func reserveRunLogSink(at url: URL) throws -> RunLogSink?
    func deleteRegularFile(at url: URL) throws
    func fileExists(at url: URL) -> Bool
    func isRegularFile(at url: URL) -> Bool
    func isExecutable(at url: URL) -> Bool
    func contentsOfDirectory(at url: URL) -> [URL]
    func open(_ url: URL)
    func terminateApplication()
    func now() -> Date
    func schedule(every interval: TimeInterval, _ action: @escaping @MainActor () -> Void)
        -> PollHandle
}

extension SystemOps {
    func terminateApplication() { NSApp.terminate(nil) }
    func contentsOfDirectory(at url: URL) -> [URL] {
        (try? FileManager.default.contentsOfDirectory(
            at: url, includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles])) ?? []
    }
    func reserveFile(at url: URL) throws -> Bool {
        guard !fileExists(at: url) else { return false }
        try write(Data(), to: url)
        return true
    }
    func reserveRunLogSink(at url: URL) throws -> RunLogSink? {
        guard try reserveFile(at: url) else { return nil }
        return URLRunLogSink(url: url) { [weak self] maxBytes in
            self?.readTail(at: url, maxBytes: maxBytes)
        }
    }
    func open(_ url: URL) { NSWorkspace.shared.open(url) }
}

private final class NativeProcessHandle: ProcessHandle {
    let process: Process
    private var output: FileHandle?
    init(_ process: Process, output: FileHandle? = nil) {
        self.process = process
        self.output = output
    }
    var pid: Int32 { process.processIdentifier }
    var isRunning: Bool { process.isRunning }
    var terminationStatus: Int32 { process.terminationStatus }
    func terminate() { process.terminate() }
    func closeOutput() {
        try? output?.close()
        output = nil
    }
}

private final class DescriptorRunLogSink: RunLogSink {
    let url: URL
    private var descriptor: Int32

    init(url: URL, descriptor: Int32) {
        self.url = url
        self.descriptor = descriptor
    }

    deinit { closeOwner() }

    func duplicateOutputHandle() throws -> FileHandle? {
        guard descriptor >= 0 else { throw POSIXError(.EBADF) }
        let duplicate = Darwin.fcntl(descriptor, F_DUPFD_CLOEXEC, 0)
        guard duplicate >= 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        return FileHandle(fileDescriptor: duplicate, closeOnDealloc: true)
    }

    func readTail(maxBytes: Int) -> String? {
        guard descriptor >= 0, maxBytes > 0 else { return nil }
        let duplicate = Darwin.dup(descriptor)
        guard duplicate >= 0 else { return nil }
        let handle = FileHandle(fileDescriptor: duplicate, closeOnDealloc: true)
        defer { try? handle.close() }
        guard let size = try? handle.seekToEnd() else { return nil }
        let offset = size > UInt64(maxBytes) ? size - UInt64(maxBytes) : 0
        do {
            try handle.seek(toOffset: offset)
            guard let data = try handle.read(upToCount: maxBytes) else { return nil }
            return String(decoding: data, as: UTF8.self)
        } catch {
            return nil
        }
    }

    func closeOwner() {
        if descriptor >= 0 {
            Darwin.close(descriptor)
            descriptor = -1
        }
    }
}

/// Reads a child's pipe to EOF on a background queue, keeping at most
/// `maxBytes`; `nil` from `wait(until:)` means EOF missed the deadline or the
/// output overflowed the bound.
private final class BoundedPipeDrain: @unchecked Sendable {
    private let lock = NSLock()
    private let finished = DispatchSemaphore(value: 0)
    private var data = Data()
    private var overflowed = false

    init(_ handle: FileHandle, maxBytes: Int) {
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                lock.lock()
                if data.count + chunk.count > maxBytes {
                    overflowed = true
                } else {
                    data.append(chunk)
                }
                lock.unlock()
            }
            finished.signal()
        }
    }

    func wait(until deadline: DispatchTime) -> Data? {
        guard finished.wait(timeout: deadline) == .success else { return nil }
        lock.lock()
        defer { lock.unlock() }
        return overflowed ? nil : data
    }
}

private final class NativePollHandle: PollHandle {
    let timer: Timer
    init(_ timer: Timer) { self.timer = timer }
    func cancel() { timer.invalidate() }
}

@MainActor
final class NativeSystemOps: SystemOps {
    func spawn(
        _ request: ProcessRequest,
        onExit: @escaping @MainActor (ProcessHandle) -> Void
    ) throws -> ProcessHandle {
        let process = Process()
        process.executableURL = request.executable
        process.arguments = request.arguments
        process.environment = request.environment
        var capturedOutput: FileHandle?
        if let sink = request.outputSink,
           let output = try sink.duplicateOutputHandle() {
            capturedOutput = output
            process.standardOutput = output
            process.standardError = output
        }
        let handle = NativeProcessHandle(process, output: capturedOutput)
        process.terminationHandler = { [weak handle] _ in
            guard let handle else { return }
            handle.closeOutput()
            Task { @MainActor in onExit(handle) }
        }
        do {
            try process.run()
        } catch {
            process.terminationHandler = nil
            handle.closeOutput()
            throw error
        }
        return handle
    }

    func processIsAlive(_ pid: Int32) -> Bool { kill(pid, 0) == 0 || errno == EPERM }

    /// Upper bound on the main-actor wait for one `ps` identity probe. A stalled
    /// `ps` is killed and yields `.unknown` (identity unproven: the model keeps
    /// its last proven verdict for that generation) instead of freezing the
    /// menu bar; the next lifecycle refresh probes again.
    var commandLineTimeout: TimeInterval = 1.0
    /// Test seam: the identity probe executable (production: `/bin/ps`).
    var commandLineExecutable = URL(fileURLWithPath: "/bin/ps")
    static let commandLineMaxBytes = 64 * 1024

    func commandLine(_ pid: Int32) -> CommandLineProbe {
        let process = Process()
        process.executableURL = commandLineExecutable
        process.arguments = ["-p", "\(pid)", "-o", "command="]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }
        do { try process.run() } catch { return .unknown }
        // Drain off the main actor so a full pipe can never stall `ps`, and so
        // neither the read nor the exit wait is unbounded on the main actor.
        let output = BoundedPipeDrain(
            pipe.fileHandleForReading, maxBytes: Self.commandLineMaxBytes)
        let deadline = DispatchTime.now() + commandLineTimeout
        let exitedInTime = exited.wait(timeout: deadline) == .success
        if !exitedInTime { stopStalledProbe(process, exited: exited) }
        guard exitedInTime, let data = output.wait(until: deadline) else {
            return .unknown
        }
        let value = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        switch process.terminationStatus {
        case 0:
            return value.isEmpty ? .unknown : .command(value)
        case 1 where value.isEmpty:
            return .absent
        default:
            return .unknown
        }
    }

    /// Terminate, then kill, then reap a probe that missed its deadline, each
    /// step bounded: a `ps` that ignores SIGTERM must not outlive the probe.
    private func stopStalledProbe(_ process: Process, exited: DispatchSemaphore) {
        process.terminate()
        if exited.wait(timeout: .now() + 0.2) == .success { return }
        kill(process.processIdentifier, SIGKILL)
        _ = exited.wait(timeout: .now() + 0.5)
    }

    private func openRegularFile(at url: URL) -> FileHandle? {
        let descriptor = Darwin.open(
            url.path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW)
        guard descriptor >= 0 else { return nil }
        var metadata = Darwin.stat()
        guard Darwin.fstat(descriptor, &metadata) == 0,
              metadata.st_mode & S_IFMT == S_IFREG,
              metadata.st_uid == Darwin.geteuid(),
              metadata.st_mode & (S_IWGRP | S_IWOTH) == 0 else {
            Darwin.close(descriptor)
            return nil
        }
        return FileHandle(fileDescriptor: descriptor, closeOnDealloc: true)
    }
    func readData(at url: URL, maxBytes: Int) -> Data? {
        guard maxBytes > 0,
              let handle = openRegularFile(at: url) else { return nil }
        defer { try? handle.close() }
        guard let size = try? handle.seekToEnd(), size <= UInt64(maxBytes) else { return nil }
        do {
            try handle.seek(toOffset: 0)
            guard let data = try handle.read(upToCount: maxBytes) else { return nil }
            var opened = Darwin.stat()
            var current = Darwin.stat()
            guard Darwin.fstat(handle.fileDescriptor, &opened) == 0,
                  opened.st_size <= off_t(maxBytes),
                  opened.st_uid == Darwin.geteuid(),
                  opened.st_mode & (S_IWGRP | S_IWOTH) == 0,
                  Darwin.lstat(url.path, &current) == 0,
                  current.st_mode & S_IFMT == S_IFREG,
                  current.st_uid == Darwin.geteuid(),
                  current.st_mode & (S_IWGRP | S_IWOTH) == 0,
                  opened.st_dev == current.st_dev,
                  opened.st_ino == current.st_ino else { return nil }
            return data
        } catch {
            return nil
        }
    }
    func readTail(at url: URL, maxBytes: Int) -> String? {
        guard maxBytes > 0,
              let handle = openRegularFile(at: url) else { return nil }
        defer { try? handle.close() }
        guard let size = try? handle.seekToEnd() else { return nil }
        let offset = size > UInt64(maxBytes) ? size - UInt64(maxBytes) : 0
        do {
            try handle.seek(toOffset: offset)
            guard let data = try handle.read(upToCount: maxBytes) else { return nil }
            return String(decoding: data, as: UTF8.self)
        } catch {
            return nil
        }
    }
    func write(_ data: Data, to url: URL) throws { try data.write(to: url, options: .atomic) }
    func reserveFile(at url: URL) throws -> Bool {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let descriptor = Darwin.open(
            url.path, O_WRONLY | O_CREAT | O_EXCL | O_APPEND | O_CLOEXEC,
            S_IRUSR | S_IWUSR)
        if descriptor >= 0 {
            Darwin.close(descriptor)
            return true
        }
        if errno == EEXIST { return false }
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
    func reserveRunLogSink(at url: URL) throws -> RunLogSink? {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let descriptor = Darwin.open(
            url.path, O_RDWR | O_CREAT | O_EXCL | O_APPEND | O_CLOEXEC | O_NOFOLLOW,
            S_IRUSR | S_IWUSR)
        if descriptor >= 0 {
            return DescriptorRunLogSink(url: url, descriptor: descriptor)
        }
        if errno == EEXIST { return nil }
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
    // Test seam at the pathname replacement boundary, before atomic quarantine.
    var beforeLogQuarantine: ((URL) -> Void)?
    var afterLogQuarantine: ((URL) -> Void)?
    func deleteRegularFile(at url: URL) throws {
        guard let handle = openRegularFile(at: url) else { throw POSIXError(.EINVAL) }
        defer { try? handle.close() }
        var opened = Darwin.stat()
        guard Darwin.fstat(handle.fileDescriptor, &opened) == 0 else {
            throw POSIXError(.EINVAL)
        }
        let quarantine = url.deletingLastPathComponent().appendingPathComponent(
            ".onoats-prune-\(UUID().uuidString)", isDirectory: true)
        guard Darwin.mkdir(quarantine.path, S_IRWXU) == 0 else {
            throw POSIXError(.EIO)
        }
        defer { _ = Darwin.rmdir(quarantine.path) }
        let moved = quarantine.appendingPathComponent("candidate")
        beforeLogQuarantine?(url)
        guard Darwin.rename(url.path, moved.path) == 0 else { throw POSIXError(.EIO) }
        afterLogQuarantine?(url)
        var current = Darwin.stat()
        guard Darwin.lstat(moved.path, &current) == 0,
              opened.st_dev == current.st_dev, opened.st_ino == current.st_ino,
              current.st_mode & S_IFMT == S_IFREG else {
            // Never replace a new arrival while restoring a raced candidate.
            // If occupied, retain the candidate in quarantine rather than lose it.
            _ = Darwin.renamex_np(moved.path, url.path, UInt32(RENAME_EXCL))
            throw POSIXError(.EINVAL)
        }
        guard Darwin.unlink(moved.path) == 0 else { throw POSIXError(.EIO) }
    }
    func fileExists(at url: URL) -> Bool { FileManager.default.fileExists(atPath: url.path) }
    func isRegularFile(at url: URL) -> Bool {
        guard let values = try? url.resourceValues(forKeys: [
            .isRegularFileKey, .isSymbolicLinkKey,
        ]) else { return false }
        return values.isRegularFile == true && values.isSymbolicLink != true
    }
    func isExecutable(at url: URL) -> Bool {
        FileManager.default.isExecutableFile(atPath: url.path)
    }
    func contentsOfDirectory(at url: URL) -> [URL] {
        (try? FileManager.default.contentsOfDirectory(
            at: url, includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles])) ?? []
    }
    func open(_ url: URL) { NSWorkspace.shared.open(url) }
    func now() -> Date { Date() }
    func schedule(every interval: TimeInterval, _ action: @escaping @MainActor () -> Void)
        -> PollHandle {
        let timer = Timer(timeInterval: interval, repeats: true) { _ in
            Task { @MainActor in action() }
        }
        RunLoop.main.add(timer, forMode: .common)
        return NativePollHandle(timer)
    }
}
