// Recorder state for the menu bar. PID identity is the sole liveness
// authority; lifecycle status and UI telemetry only decorate that verdict.
import AppKit
import CoreAudio
import Darwin
import Foundation
import SwiftUI

struct StatusRecord: Decodable {
    let schema: Int
    let pid: Int32
    let start_time: Double
    let audio_source: String
    let stt_label: String
    let running: Bool
    let last_rotation_time: Double?
    let last_error: String?
    let exit_reason: String?
    let supervisor_rc: Int?
    let warning: String?
    let mic_device: String?
    let system_device: String?
}

enum RecorderState: Equatable {
    case stopped
    case starting
    case running(ours: Bool)
    case stopping
    case failed(reason: String, detail: String?)
}

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
    private let tailReader: ((Int) -> String?)?
    init(url: URL, tailReader: ((Int) -> String?)? = nil) {
        self.url = url
        self.tailReader = tailReader
    }
    func duplicateOutputHandle() throws -> FileHandle? { nil }
    func readTail(maxBytes: Int) -> String? {
        if let tailReader { return tailReader(maxBytes) }
        guard maxBytes > 0, let data = try? Data(contentsOf: url) else { return nil }
        return String(decoding: data.suffix(maxBytes), as: UTF8.self)
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

    init(
        executable: URL, arguments: [String], environment: [String: String]?,
        appendOutputTo: URL?
    ) {
        self.init(
            executable: executable, arguments: arguments, environment: environment,
            outputSink: appendOutputTo.map { URLRunLogSink(url: $0) })
    }
}

/// Keeps optional Process.arguments construction semantics while process
/// creation itself remains wholly behind SystemOps.
private struct ProcessArguments {
    var arguments: [String]?
}

/// All process, signal, file, clock and polling effects used by the model.
@MainActor
protocol SystemOps: AnyObject {
    func spawn(
        _ request: ProcessRequest,
        onExit: @escaping @MainActor (ProcessHandle) -> Void
    ) throws -> ProcessHandle
    func processIsAlive(_ pid: Int32) -> Bool
    func commandLine(_ pid: Int32) -> String?
    func readData(at url: URL) -> Data?
    func readData(at url: URL, maxBytes: Int) -> Data?
    func readText(at url: URL) -> String?
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
    func readData(at url: URL, maxBytes: Int) -> Data? {
        guard maxBytes > 0, let data = readData(at: url), data.count <= maxBytes else {
            return nil
        }
        return data
    }
    func readTail(at url: URL, maxBytes: Int) -> String? {
        guard maxBytes > 0, let data = readData(at: url) else { return nil }
        return String(decoding: data.suffix(maxBytes), as: UTF8.self)
    }
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

    func commandLine(_ pid: Int32) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-p", "\(pid)", "-o", "command="]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        let value = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return value?.isEmpty == false ? value : nil
    }

    func readData(at url: URL) -> Data? { try? Data(contentsOf: url) }
    private func openRegularFile(at url: URL) -> FileHandle? {
        let descriptor = Darwin.open(
            url.path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW)
        guard descriptor >= 0 else { return nil }
        var metadata = Darwin.stat()
        guard Darwin.fstat(descriptor, &metadata) == 0,
              metadata.st_mode & S_IFMT == S_IFREG else {
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
            return try handle.read(upToCount: maxBytes)
        } catch {
            return nil
        }
    }
    func readText(at url: URL) -> String? { try? String(contentsOf: url, encoding: .utf8) }
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

@MainActor
final class RecorderModel: ObservableObject {
    static let statusSchemaVersion = 2
    static let sttServices = ["whisper", "websocket", "deepgram"]
    static let seminarCategory = "seminars"
    static let dataDirEnvironment = "ONOATS_DATA_DIR"
    static let dataDirArgument = "--data-dir"
    static let transcriptPreviewDefaultsKey = "showTranscriptPreview"
    static let invalidCategoryErrorTag = "ONOATS-ERROR invalid-category"
    static let runLogPrefix = "onoats-bot-"
    static let runLogSuffix = ".log"
    static let maxRunLogs = 10
    static let categoryErrorTailBytes = 64 * 1024
    static let telemetryMaxBytes = UiTelemetryContract.maxSnapshotBytes

    /// Stop/Flush helpers need only a deterministic executable search path and
    /// basic user/locale context. Never forward the app's API keys, recorder
    /// configuration, or dynamic-loader injection variables to these helpers.
    static func auxiliaryEnvironment(
        from source: [String: String] = ProcessInfo.processInfo.environment
    ) -> [String: String] {
        var environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin"]
        for key in ["HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"] {
            if let value = source[key], !value.contains("\0") { environment[key] = value }
        }
        return environment
    }

    @Published var state: RecorderState = .stopped
    @Published var micDevice = "—"
    @Published var outputDevice = "—"
    @Published var inputDevices: [AudioInputDevice] = []
    @Published var defaultInputID = AudioObjectID(kAudioObjectUnknown)
    @Published var sttLabel: String?
    @Published var audioSource: String?
    @Published var startTime: Date?
    @Published var warning: String?
    @Published var schemaDrift = false
    @Published var sttService = "whisper"
    @Published var seminarMode = false
    @Published var dataDirDisplay = ""
    @Published private(set) var activeDataDir: URL?
    @Published private(set) var nextDataDir: URL
    @Published private(set) var activeDataDirDisplay: String?
    @Published private(set) var nextDataDirDisplay = ""
    @Published var flushNote: String?
    @Published var stopRequested = false
    @Published private(set) var prestartDetail: String?
    @Published private(set) var seminarConfigurationFailure = false
    @Published private(set) var lastFailureWasAppOwned = false
    @Published private(set) var mostRecentRunLogURL: URL?
    @Published private var mostRecentRunLogSink: RunLogSink?
    private var runLogFinishing = false
    private var runLogReservationAttempted = false
    @Published private(set) var mostRecentRunStartedAt: Date?

    @Published private(set) var meActivity: UiTelemetryActivity?
    @Published private(set) var themActivity: UiTelemetryActivity?
    @Published private(set) var meStt: UiTelemetrySttBranch?
    @Published private(set) var themStt: UiTelemetrySttBranch?
    @Published private(set) var aggregateStt: UiSttState?
    @Published private(set) var telemetryCategory: String?
    @Published private(set) var transcriptPreview: UiTelemetryTranscript?
    @Published private(set) var telemetryAvailable = false
    @Published private(set) var transcriptPreviewEnabled: Bool

    var categoryForDisplay: String? {
        if proc != nil, seminarMode { return Self.seminarCategory }
        return telemetryCategory == Self.seminarCategory ? Self.seminarCategory : nil
    }
    var categoryUnavailable: Bool {
        if case .running(ours: false) = state { return categoryForDisplay == nil }
        return false
    }

    private let ops: SystemOps
    private let defaults: UserDefaults
    private let dataDirResolver: () -> URL
    private var proc: ProcessHandle?
    private var auxiliary: [ObjectIdentifier: ProcessHandle] = [:]
    private var quitRequestedAt: Date?
    private var quitPoll: PollHandle?
    private var userRequestedStop = false
    private var spawnedAt: Date?
    private var statusPoll: PollHandle?
    private var telemetryPoll: PollHandle?
    private var stopRequestedForPid: Int32?
    private var stopRequestedForStartEpoch: Double?
    private var fingerprintCache: [Int32: (match: Bool, at: Date)] = [:]
    private let fingerprintTTL: TimeInterval = 30

    private struct TelemetryAuthority: Equatable {
        let directory: URL
        let pid: Int32
        let startEpoch: Double
    }
    private var telemetryAuthority: TelemetryAuthority?

    var cliPath: String {
        if let override = defaults.string(forKey: "cliPath"), !override.isEmpty {
            return (override as NSString).expandingTildeInPath
        }
        return NSHomeDirectory() + "/.local/bin/onoats"
    }
    var cliAvailable: Bool { ops.isExecutable(at: URL(fileURLWithPath: cliPath)) }
    var capturerPath: String { Bundle.main.bundlePath + "/Contents/MacOS/onoats-capturer" }
    var dataDir: URL { nextDataDir }
    var logsDirectory: URL {
        URL(fileURLWithPath: NSHomeDirectory() + "/Library/Logs/Onoats", isDirectory: true)
    }

    var isOwnedSession: Bool {
        if case .running(ours: true) = state { return true }
        return proc != nil
    }

    var canOpenLogForCurrentSurface: Bool {
        if isOwnedSession { return true }
        if case .failed = state { return lastFailureWasAppOwned }
        return false
    }

    static func resolveDataDir() -> URL {
        if let value = ConfigStore.readValue(section: "storage", key: "data_dir") {
            return URL(fileURLWithPath: (value as NSString).expandingTildeInPath)
        }
        return URL(fileURLWithPath: NSHomeDirectory() + "/.local/share/onoats")
    }

    init(
        systemOps: SystemOps? = nil,
        userDefaults: UserDefaults = .standard,
        resolveDataDir: (() -> URL)? = nil,
        startPolling: Bool = true
    ) {
        ops = systemOps ?? NativeSystemOps()
        defaults = userDefaults
        dataDirResolver = resolveDataDir ?? Self.resolveDataDir
        nextDataDir = dataDirResolver()
        transcriptPreviewEnabled = userDefaults.bool(forKey: Self.transcriptPreviewDefaultsKey)
        refresh()
        if startPolling {
            statusPoll = ops.schedule(every: 1.0) { [weak self] in self?.refresh() }
            telemetryPoll = ops.schedule(every: 0.25) { [weak self] in self?.refreshTelemetry() }
        }
    }

    deinit {
        statusPoll?.cancel()
        telemetryPoll?.cancel()
        quitPoll?.cancel()
    }

    private func displayPath(_ url: URL) -> String {
        url.path.replacingOccurrences(of: NSHomeDirectory(), with: "~")
    }
    private func refreshDirectoryDisplays(configured: URL) {
        nextDataDir = configured
        nextDataDirDisplay = displayPath(configured)
        dataDirDisplay = nextDataDirDisplay
        activeDataDirDisplay = activeDataDir.map(displayPath)
    }

    private func readStatus(under directory: URL) -> StatusRecord? {
        let url = directory.appendingPathComponent(".active/onoats.status.json")
        guard let data = ops.readData(at: url) else { return nil }
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let schema = object["schema"] as? Int,
           schema != Self.statusSchemaVersion {
            schemaDrift = true
            return nil
        }
        schemaDrift = false
        return try? JSONDecoder().decode(StatusRecord.self, from: data)
    }

    private func readPid(under directory: URL) -> (pid: Int32, cmdline: String, startEpoch: Double)? {
        let url = directory.appendingPathComponent(".active/onoats.pid")
        guard let text = ops.readText(at: url) else { return nil }
        let lines = text.trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
        guard lines.count >= 2, lines[1] == "onoats-bot", let pid = Int32(lines[0]) else {
            return nil
        }
        return (pid, lines.count >= 3 ? lines[2] : "",
                lines.count >= 4 ? (Double(lines[3]) ?? 0) : 0)
    }

    private func processAlive(_ pid: Int32, storedCmdline: String) -> Bool {
        guard ops.processIsAlive(pid) else {
            fingerprintCache[pid] = nil
            return false
        }
        if storedCmdline.isEmpty { return true }
        if let cached = fingerprintCache[pid],
           ops.now().timeIntervalSince(cached.at) < fingerprintTTL { return cached.match }
        let match = ops.commandLine(pid) == storedCmdline
        fingerprintCache[pid] = (match, ops.now())
        return match
    }

    private func setTelemetryAuthority(_ authority: TelemetryAuthority?) {
        if telemetryAuthority != authority { clearTelemetry() }
        telemetryAuthority = authority
        if authority == nil { clearTelemetry() }
    }

    func refresh() {
        micDevice = AudioDevices.defaultInputName()
        outputDevice = AudioDevices.defaultOutputName()
        inputDevices = AudioDevices.inputDevices()
        defaultInputID = AudioDevices.defaultInputID()
        sttService = ConfigStore.readValue(section: "stt", key: "service") ?? "whisper"

        let configured = dataDirResolver()
        let directory = activeDataDir ?? configured
        refreshDirectoryDisplays(configured: configured)
        let status = readStatus(under: directory)
        let pid = readPid(under: directory)
        let alive = pid.map { processAlive($0.pid, storedCmdline: $0.cmdline) } ?? false
        if proc == nil, alive, activeDataDir == nil {
            activeDataDir = directory
            activeDataDirDisplay = displayPath(directory)
        }

        let ownedPublication: Bool = {
            guard let handle = proc, handle.isRunning, alive, let pid, let attempt = spawnedAt
            else { return false }
            return pid.pid == handle.pid && pid.startEpoch >= attempt.timeIntervalSince1970
        }()
        let externalPublication = proc == nil && alive
        let authority: TelemetryAuthority? = {
            guard (ownedPublication || externalPublication), let pid else { return nil }
            return TelemetryAuthority(
                directory: directory, pid: pid.pid, startEpoch: pid.startEpoch)
        }()
        setTelemetryAuthority(authority)

        let matchingStatus: StatusRecord? = {
            guard let authority, let status,
                  status.pid == authority.pid, status.start_time == authority.startEpoch
            else { return nil }
            return status
        }()
        sttLabel = matchingStatus.flatMap { $0.stt_label.isEmpty ? nil : $0.stt_label }
        audioSource = matchingStatus.flatMap { $0.audio_source.isEmpty ? nil : $0.audio_source }
        startTime = matchingStatus.map { Date(timeIntervalSince1970: $0.start_time) }
        warning = matchingStatus?.warning

        if let handle = proc, handle.isRunning {
            if ownedPublication {
                ownedRecorderPidWasPublished = true
                prestartDetail = nil
                if case .stopping = state {} else { state = .running(ours: true) }
            } else {
                if let status, let attempt = spawnedAt,
                   status.pid == handle.pid,
                   status.start_time >= attempt.timeIntervalSince1970,
                   status.running {
                    prestartDetail = status.warning
                } else { prestartDetail = nil }
                if case .stopping = state {} else { state = .starting }
            }
            return
        }
        if proc != nil { return }

        let stoppedTargetGone = stopRequestedForPid != nil
            && (pid?.pid != stopRequestedForPid || pid?.startEpoch != stopRequestedForStartEpoch)
        if !alive || stoppedTargetGone {
            stopRequested = false
            stopRequestedForPid = nil
            stopRequestedForStartEpoch = nil
        }
        if alive {
            state = .running(ours: false)
        } else {
            prestartDetail = nil
            clearTelemetry()
            activeDataDir = nil
            activeDataDirDisplay = nil
            if case .failed = state {} else { state = .stopped }
        }
    }

    /// Four-hertz display poll. This method deliberately never assigns state.
    func refreshTelemetry() {
        guard let authority = telemetryAuthority,
              let data = ops.readData(
                at: authority.directory.appendingPathComponent(
                    ".active/\(UiTelemetryContract.fileName)"),
                maxBytes: Self.telemetryMaxBytes),
              let snapshot = UiTelemetryDecoder.decode(
                data, expectedPid: authority.pid, expectedStartEpoch: authority.startEpoch,
                now: ops.now())
        else {
            clearTelemetry()
            return
        }
        telemetryAvailable = true
        meActivity = snapshot.me
        themActivity = snapshot.them
        meStt = snapshot.meStt
        themStt = snapshot.themStt
        aggregateStt = snapshot.aggregateStt
        telemetryCategory = snapshot.lockedCategory
        transcriptPreview = transcriptPreviewEnabled ? snapshot.transcript : nil
    }

    private func clearTelemetry() {
        telemetryAvailable = false
        meActivity = nil
        themActivity = nil
        meStt = nil
        themStt = nil
        aggregateStt = nil
        telemetryCategory = nil
        transcriptPreview = nil
    }

    func setTranscriptPreviewEnabled(_ enabled: Bool) {
        transcriptPreviewEnabled = enabled
        defaults.set(enabled, forKey: Self.transcriptPreviewDefaultsKey)
        if enabled { refreshTelemetry() } else { transcriptPreview = nil }
    }

    var canStart: Bool { proc == nil && mostRecentRunLogSink == nil && cliAvailable }

    func start() {
        guard canStart else { return }
        userRequestedStop = false
        flushNote = nil
        prestartDetail = nil
        seminarConfigurationFailure = false
        lastFailureWasAppOwned = false
        ownedRecorderPidWasPublished = false
        setTelemetryAuthority(nil)
        let pinned = dataDirResolver()
        activeDataDir = pinned
        refreshDirectoryDisplays(configured: pinned)
        let attempt = ops.now()
        spawnedAt = attempt
        runLogReservationAttempted = true
        mostRecentRunLogURL = nil
        mostRecentRunStartedAt = nil
        runLogFinishing = false
        let initialSpawnEpoch = Int64((attempt.timeIntervalSince1970 * 1000).rounded(.down))
        let runLog: RunLogSink
        do {
            runLog = try reserveRunLog(startingAt: initialSpawnEpoch)
        } catch {
            spawnedAt = nil
            activeDataDir = nil
            activeDataDirDisplay = nil
            lastFailureWasAppOwned = true
            state = .failed(reason: "log-reservation-failed", detail: error.localizedDescription)
            return
        }
        mostRecentRunLogSink = runLog
        mostRecentRunLogURL = runLog.url
        mostRecentRunStartedAt = attempt
        var environment = ProcessInfo.processInfo.environment
        environment["AUDIO_SOURCE"] = "socket"
        environment["ONOATS_CAPTURER_BIN"] = capturerPath
        environment[Self.dataDirEnvironment] = pinned.path
        var p = ProcessArguments(arguments: ["bot"])
        if seminarMode { p.arguments? += ["--category", Self.seminarCategory] }
        let request = ProcessRequest(
            executable: URL(fileURLWithPath: cliPath), arguments: p.arguments ?? ["bot"],
            environment: environment, outputSink: runLog)
        do {
            let handle = try ops.spawn(request) { [weak self] handle in self?.handleExit(handle) }
            proc = handle
            state = .starting
            pruneRunLogs()
        } catch {
            runLog.closeOwner()
            mostRecentRunLogSink = nil
            spawnedAt = nil
            activeDataDir = nil
            activeDataDirDisplay = nil
            lastFailureWasAppOwned = true
            state = .failed(reason: "spawn-failed", detail: error.localizedDescription)
            pruneRunLogs()
        }
    }

    func stop() {
        guard let handle = proc, handle.isRunning else { return }
        userRequestedStop = true
        state = .stopping
        handle.terminate()
    }

    func quitApp() {
        guard quitRequestedAt == nil else { return }
        quitRequestedAt = ops.now()
        if let handle = proc, handle.isRunning, !userRequestedStop { stop() }
        finishQuitIfReady()
        guard quitRequestedAt != nil else { return }
        quitPoll = ops.schedule(every: 0.25) { [weak self] in self?.finishQuitIfReady() }
    }

    private func finishQuitIfReady() {
        guard let requested = quitRequestedAt else { return }
        // Only retained children are ours. An external recorder keeps running.
        if proc?.isRunning != true && !auxiliary.values.contains(where: { $0.isRunning }) {
            quitPoll?.cancel()
            quitPoll = nil
            quitRequestedAt = nil
            ops.terminateApplication()
        } else if ops.now().timeIntervalSince(requested) >= 15 {
            // Killing the supervisor would bypass its capturer-group teardown.
            // Keep the app/handles alive and surface a bounded failure instead.
            quitPoll?.cancel()
            quitPoll = nil
            quitRequestedAt = nil
            flushNote = "Quit timed out waiting for recorder cleanup. Check the run log and retry."
        }
    }

    func flush() {
        flushNote = nil
        guard let directory = activeDataDir else {
            flushNote = "Flush failed: no active recorder directory"
            return
        }
        guard let target = telemetryAuthority, target.directory == directory,
              target.startEpoch > 0 else {
            flushNote = "Flush failed: recorder identity is unavailable"
            return
        }
        spawnAuxiliary(
            arguments: [
                "flush", "--data-dir", directory.path,
                "--expected-pid", String(target.pid),
                "--expected-start-epoch", String(target.startEpoch),
            ],
            logOwnedOutput: proc?.isRunning == true, failureAction: "Flush")
    }

    func stopExternal() {
        guard proc == nil, let directory = activeDataDir else { return }
        guard let stopping = telemetryAuthority, stopping.directory == directory,
              stopping.startEpoch > 0 else {
            flushNote = "Stop failed: recorder identity is unavailable"
            return
        }
        stopRequested = true
        stopRequestedForPid = stopping.pid
        stopRequestedForStartEpoch = stopping.startEpoch
        flushNote = nil
        spawnAuxiliary(
            arguments: [
                "stop", "--data-dir", directory.path,
                "--expected-pid", String(stopping.pid),
                "--expected-start-epoch", String(stopping.startEpoch),
            ],
            logOwnedOutput: false, failureAction: "Stop", clearExternalStopOnFailure: true)
    }

    private func spawnAuxiliary(
        arguments: [String], logOwnedOutput: Bool, failureAction: String,
        clearExternalStopOnFailure: Bool = false
    ) {
        let request = ProcessRequest(
            executable: URL(fileURLWithPath: cliPath), arguments: arguments,
            environment: Self.auxiliaryEnvironment(),
            outputSink: logOwnedOutput ? mostRecentRunLogSink : nil)
        do {
            let handle = try ops.spawn(request) { [weak self] handle in
                guard let self else { return }
                self.auxiliary[ObjectIdentifier(handle)] = nil
                defer { self.finishQuitIfReady() }
                self.releaseRunLogSinkIfIdle()
                if handle.terminationStatus != 0 {
                    if clearExternalStopOnFailure { self.clearExternalStopRequest() }
                    self.flushNote = "\(failureAction) failed (rc \(handle.terminationStatus))"
                }
            }
            auxiliary[ObjectIdentifier(handle)] = handle
        } catch {
            if clearExternalStopOnFailure { clearExternalStopRequest() }
            flushNote = "\(failureAction) spawn failed: \(error.localizedDescription)"
        }
    }

    private func clearExternalStopRequest() {
        stopRequested = false
        stopRequestedForPid = nil
        stopRequestedForStartEpoch = nil
    }

    private func handleExit(_ handle: ProcessHandle) {
        guard let owned = proc, owned === handle else { return }
        defer { finishQuitIfReady() }
        let directory = activeDataDir
        let knownEpoch = telemetryAuthority?.startEpoch
        prestartDetail = nil
        setTelemetryAuthority(nil)
        if userRequestedStop || handle.terminationStatus == 0 {
            state = .stopped
        } else {
            var status = directory.flatMap { readStatus(under: $0) }
            if let value = status {
                if value.pid != handle.pid || spawnedAt == nil
                    || value.start_time < spawnedAt!.timeIntervalSince1970
                    || (knownEpoch != nil && value.start_time != knownEpoch!) {
                    status = nil
                }
            }
            let pidWasPublished = ownedRecorderPidWasPublished
                || currentAttemptPublicationEvidence(handle: handle, directory: directory)
            state = .failed(
                reason: status?.exit_reason ?? "exit code \(handle.terminationStatus)",
                detail: status?.last_error)
            lastFailureWasAppOwned = true
            seminarConfigurationFailure = !pidWasPublished
                && runLogTailContainsInvalidCategoryTag()
        }
        userRequestedStop = false
        seminarMode = false
        proc = nil
        runLogFinishing = true
        releaseRunLogSinkIfIdle()
        activeDataDir = nil
        activeDataDirDisplay = nil
        spawnedAt = nil
        refreshDirectoryDisplays(configured: dataDirResolver())
    }

    private var ownedRecorderPidWasPublished = false

    private func currentAttemptPublicationEvidence(
        handle: ProcessHandle, directory: URL?
    ) -> Bool {
        guard let directory, let attempt = spawnedAt,
              let status = readStatus(under: directory)
        else { return false }
        return status.pid == handle.pid
            && status.start_time >= attempt.timeIntervalSince1970
            && !status.stt_label.isEmpty
    }

    private func reserveRunLog(startingAt initialEpoch: Int64) throws -> RunLogSink {
        var epoch = initialEpoch
        while true {
            let candidate = logsDirectory.appendingPathComponent(
                "\(Self.runLogPrefix)\(epoch)\(Self.runLogSuffix)")
            if let sink = try ops.reserveRunLogSink(at: candidate) { return sink }
            guard epoch < Int64.max else { throw CocoaError(.fileWriteFileExists) }
            epoch += 1
        }
    }

    private func runLogEpoch(_ url: URL) -> Int64? {
        let name = url.lastPathComponent
        guard name.hasPrefix(Self.runLogPrefix), name.hasSuffix(Self.runLogSuffix) else {
            return nil
        }
        let start = name.index(name.startIndex, offsetBy: Self.runLogPrefix.count)
        let end = name.index(name.endIndex, offsetBy: -Self.runLogSuffix.count)
        return Int64(name[start..<end])
    }

    private func sortedRunLogsNewestFirst() -> [URL] {
        ops.contentsOfDirectory(at: logsDirectory)
            .compactMap { url -> (URL, Int64)? in
                guard ops.isRegularFile(at: url), let epoch = runLogEpoch(url) else {
                    return nil
                }
                return (url, epoch)
            }
            .sorted {
                if $0.1 != $1.1 { return $0.1 > $1.1 }
                return $0.0.lastPathComponent > $1.0.lastPathComponent
            }
            .map(\.0)
    }

    private func pruneRunLogs() {
        let active = mostRecentRunLogURL?.standardizedFileURL
        var remainingHistorical = Self.maxRunLogs - (active == nil ? 0 : 1)
        for url in sortedRunLogsNewestFirst() {
            if url.standardizedFileURL == active { continue }
            if remainingHistorical > 0 {
                remainingHistorical -= 1
            } else {
                try? ops.deleteRegularFile(at: url)
            }
        }
    }

    /// Resolves the one canonical Open Log target without opening it. When this
    /// process has attempted a run, its in-memory path remains authoritative:
    /// deletion falls back to the folder and never silently selects an older
    /// run. A fresh launch may select the newest surviving run log.
    func resolvedOpenLogURL() -> URL {
        if let remembered = mostRecentRunLogURL {
            return ops.isRegularFile(at: remembered) ? remembered : logsDirectory
        }
        if runLogReservationAttempted { return logsDirectory }
        return sortedRunLogsNewestFirst().first ?? logsDirectory
    }

    func openLog() {
        ops.open(resolvedOpenLogURL())
    }

    func showLogsFolder() { ops.open(logsDirectory) }

    private func runLogTailContainsInvalidCategoryTag() -> Bool {
        guard let sink = mostRecentRunLogSink,
              let text = sink.readTail(maxBytes: Self.categoryErrorTailBytes) else {
            return false
        }
        return text.components(separatedBy: .newlines).contains(Self.invalidCategoryErrorTag)
    }

    private func releaseRunLogSinkIfIdle() {
        guard runLogFinishing, auxiliary.isEmpty, let sink = mostRecentRunLogSink else {
            return
        }
        sink.closeOwner()
        mostRecentRunLogSink = nil
        runLogFinishing = false
    }

    func setSTTService(_ service: String) {
        try? ConfigStore.writeValue(section: "stt", key: "service", value: service)
        refresh()
    }

    func chooseDataDir() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.directoryURL = nextDataDir
        panel.message = "Choose the onoats data directory (config.toml [storage].data_dir)"
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try ConfigStore.writeValue(section: "storage", key: "data_dir", value: url.path)
        } catch {
            let alert = NSAlert()
            alert.messageText = "Could not save data directory"
            alert.informativeText = "\(error)"
            alert.runModal()
        }
        refresh()
    }

    func openConfig() { NSWorkspace.shared.open(ConfigStore.configURL) }
    func setMicDevice(_ device: AudioInputDevice) {
        AudioDevices.setDefaultInput(device.id)
        refresh()
    }
}
