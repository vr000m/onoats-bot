// Recorder state for the menu bar. PID identity is the sole liveness
// authority; lifecycle status and UI telemetry only decorate that verdict.
import AppKit
import CoreAudio
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

struct ProcessRequest {
    let executable: URL
    let arguments: [String]
    let environment: [String: String]?
    let appendOutputTo: URL?
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
    func readText(at url: URL) -> String?
    func write(_ data: Data, to url: URL) throws
    func delete(at url: URL) throws
    func fileExists(at url: URL) -> Bool
    func isExecutable(at url: URL) -> Bool
    func now() -> Date
    func schedule(every interval: TimeInterval, _ action: @escaping @MainActor () -> Void)
        -> PollHandle
}

private final class NativeProcessHandle: ProcessHandle {
    let process: Process
    init(_ process: Process) { self.process = process }
    var pid: Int32 { process.processIdentifier }
    var isRunning: Bool { process.isRunning }
    var terminationStatus: Int32 { process.terminationStatus }
    func terminate() { process.terminate() }
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
        let handle = NativeProcessHandle(process)
        process.executableURL = request.executable
        process.arguments = request.arguments
        process.environment = request.environment
        if let log = request.appendOutputTo {
            let manager = FileManager.default
            try manager.createDirectory(
                at: log.deletingLastPathComponent(), withIntermediateDirectories: true)
            if !manager.fileExists(atPath: log.path) {
                guard manager.createFile(atPath: log.path, contents: nil) else {
                    throw CocoaError(.fileWriteUnknown)
                }
            }
            let output = try FileHandle(forWritingTo: log)
            try output.seekToEnd()
            process.standardOutput = output
            process.standardError = output
        }
        process.terminationHandler = { [weak handle] _ in
            guard let handle else { return }
            Task { @MainActor in onExit(handle) }
        }
        do {
            try process.run()
        } catch {
            process.terminationHandler = nil
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
    func readText(at url: URL) -> String? { try? String(contentsOf: url, encoding: .utf8) }
    func write(_ data: Data, to url: URL) throws { try data.write(to: url, options: .atomic) }
    func delete(at url: URL) throws { try FileManager.default.removeItem(at: url) }
    func fileExists(at url: URL) -> Bool { FileManager.default.fileExists(atPath: url.path) }
    func isExecutable(at url: URL) -> Bool {
        FileManager.default.isExecutableFile(atPath: url.path)
    }
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
    private var logURL: URL {
        URL(fileURLWithPath: NSHomeDirectory() + "/Library/Logs/Onoats/onoats-bot.log")
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
              let data = ops.readData(at: authority.directory.appendingPathComponent(
                ".active/\(UiTelemetryContract.fileName)")),
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

    func start() {
        guard proc == nil, cliAvailable else { return }
        userRequestedStop = false
        flushNote = nil
        prestartDetail = nil
        setTelemetryAuthority(nil)
        let pinned = dataDirResolver()
        activeDataDir = pinned
        refreshDirectoryDisplays(configured: pinned)
        let attempt = ops.now()
        spawnedAt = attempt
        var environment = ProcessInfo.processInfo.environment
        environment["AUDIO_SOURCE"] = "socket"
        environment["ONOATS_CAPTURER_BIN"] = capturerPath
        environment[Self.dataDirEnvironment] = pinned.path
        var p = ProcessArguments(arguments: ["bot"])
        if seminarMode { p.arguments? += ["--category", Self.seminarCategory] }
        let request = ProcessRequest(
            executable: URL(fileURLWithPath: cliPath), arguments: p.arguments ?? ["bot"],
            environment: environment, appendOutputTo: logURL)
        do {
            let handle = try ops.spawn(request) { [weak self] handle in self?.handleExit(handle) }
            proc = handle
            state = .starting
        } catch {
            spawnedAt = nil
            activeDataDir = nil
            activeDataDirDisplay = nil
            state = .failed(reason: "spawn-failed", detail: error.localizedDescription)
        }
    }

    func stop() {
        guard let handle = proc, handle.isRunning else { return }
        userRequestedStop = true
        state = .stopping
        handle.terminate()
    }

    func quitApp() {
        if let handle = proc, handle.isRunning { handle.terminate() }
        NSApp.terminate(nil)
    }

    func flush() {
        flushNote = nil
        guard let directory = activeDataDir else {
            flushNote = "Flush failed: no active recorder directory"
            return
        }
        spawnAuxiliary(
            arguments: ["flush", "--data-dir", directory.path],
            logOwnedOutput: proc?.isRunning == true, failureAction: "Flush")
    }

    func stopExternal() {
        guard proc == nil, let directory = activeDataDir else { return }
        stopRequested = true
        let stopping = readPid(under: directory)
        stopRequestedForPid = stopping?.pid
        stopRequestedForStartEpoch = stopping?.startEpoch
        flushNote = nil
        spawnAuxiliary(
            arguments: ["stop", "--data-dir", directory.path],
            logOwnedOutput: false, failureAction: "Stop", clearExternalStopOnFailure: true)
    }

    private func spawnAuxiliary(
        arguments: [String], logOwnedOutput: Bool, failureAction: String,
        clearExternalStopOnFailure: Bool = false
    ) {
        let request = ProcessRequest(
            executable: URL(fileURLWithPath: cliPath), arguments: arguments,
            environment: nil, appendOutputTo: logOwnedOutput ? logURL : nil)
        do {
            let handle = try ops.spawn(request) { [weak self] handle in
                guard let self else { return }
                self.auxiliary[ObjectIdentifier(handle)] = nil
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
        let directory = activeDataDir
        prestartDetail = nil
        setTelemetryAuthority(nil)
        if userRequestedStop || handle.terminationStatus == 0 {
            state = .stopped
        } else {
            var status = directory.flatMap { readStatus(under: $0) }
            if let value = status, let attempt = spawnedAt,
               value.start_time < attempt.timeIntervalSince1970 { status = nil }
            state = .failed(
                reason: status?.exit_reason ?? "exit code \(handle.terminationStatus)",
                detail: status?.last_error)
        }
        userRequestedStop = false
        seminarMode = false
        proc = nil
        activeDataDir = nil
        activeDataDirDisplay = nil
        spawnedAt = nil
        refreshDirectoryDisplays(configured: dataDirResolver())
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
