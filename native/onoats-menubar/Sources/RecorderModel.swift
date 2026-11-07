// Recorder state for the menu bar. PID identity is the sole liveness
// authority; lifecycle status and UI telemetry only decorate that verdict.
import AppKit
import CoreAudio
import Foundation
import SwiftUI

enum RecorderState: Equatable {
    case stopped
    case starting
    case running(ours: Bool)
    case stopping
    case failed(reason: String, detail: String?)
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
    private struct RecorderGeneration: Hashable {
        let pid: Int32
        let command: String
        let startEpoch: Double
    }
    private var ownedRecorderGeneration: RecorderGeneration?

    private struct ControlAuthority: Equatable {
        let directory: URL
        let pid: Int32
        let startEpoch: Double
    }
    private var controlAuthority: ControlAuthority?

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
        if configured.standardizedFileURL != nextDataDir.standardizedFileURL {
            schemaDrift = false
        }
        nextDataDir = configured
        nextDataDirDisplay = displayPath(configured)
        dataDirDisplay = nextDataDirDisplay
        activeDataDirDisplay = activeDataDir.map(displayPath)
    }

    private func readStatus(under directory: URL) -> StatusRecord? {
        switch RecorderRecordReader.status(
            under: directory, expectedSchema: Self.statusSchemaVersion, using: ops
        ) {
        case .missingOrInvalid:
            schemaDrift = false
            return nil
        case .unsupportedSchema:
            schemaDrift = true
            return nil
        case .record(let record):
            schemaDrift = false
            return record
        }
    }

    private func readPid(under directory: URL) -> RecorderPidRecord? {
        RecorderRecordReader.pid(under: directory, using: ops)
    }

    private func processAlive(
        _ pid: Int32, storedCmdline: String, startEpoch: Double
    ) -> Bool {
        guard !storedCmdline.isEmpty, startEpoch.isFinite, startEpoch > 0 else {
            return false
        }
        guard ops.processIsAlive(pid) else {
            identityVerdict = nil
            return false
        }
        // A PID may be recycled while a cached fingerprint is still fresh.
        // Revalidate the live command on every lifecycle refresh; the PID file's
        // positive epoch still keys downstream status and telemetry authority.
        let generation = IdentityVerdict(
            pid: pid, cmdline: storedCmdline, startEpoch: startEpoch, alive: true)
        switch ops.commandLine(pid) {
        case .command(let live):
            let matches = live == storedCmdline
            identityVerdict = IdentityVerdict(
                pid: pid, cmdline: storedCmdline, startEpoch: startEpoch, alive: matches)
            return matches
        case .absent:
            identityVerdict = nil
            return false
        case .unknown:
            // A stalled probe proves nothing: keep the last proven verdict for
            // this exact generation (so a slow `ps` cannot flip a live recorder
            // to Stopped and invite a second Start). With no prior proof the
            // refresh fails closed.
            if let prior = identityVerdict, prior.sameGeneration(as: generation) {
                return prior.alive
            }
            return false
        }
    }

    private struct IdentityVerdict {
        let pid: Int32
        let cmdline: String
        let startEpoch: Double
        let alive: Bool
        func sameGeneration(as other: IdentityVerdict) -> Bool {
            pid == other.pid && cmdline == other.cmdline && startEpoch == other.startEpoch
        }
    }
    private var identityVerdict: IdentityVerdict?

    private func setControlAuthority(_ authority: ControlAuthority?) {
        if controlAuthority != authority { clearTelemetry() }
        controlAuthority = authority
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
        let alive = pid.map {
            processAlive($0.pid, storedCmdline: $0.cmdline, startEpoch: $0.startEpoch)
        } ?? false
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
        let authority: ControlAuthority? = {
            guard (ownedPublication || externalPublication), let pid else { return nil }
            return ControlAuthority(
                directory: directory, pid: pid.pid, startEpoch: pid.startEpoch)
        }()
        setControlAuthority(authority)

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
                if let pid {
                    ownedRecorderGeneration = RecorderGeneration(
                        pid: pid.pid, command: pid.cmdline, startEpoch: pid.startEpoch)
                }
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
        guard let authority = controlAuthority,
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
        ownedRecorderGeneration = nil
        setControlAuthority(nil)
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
        guard let target = controlAuthority, target.directory == directory,
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
        guard let stopping = controlAuthority, stopping.directory == directory,
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
        let knownGeneration = ownedRecorderGeneration
        prestartDetail = nil
        setControlAuthority(nil)
        if handle.terminationStatus == 0 {
            state = .stopped
        } else {
            var status = directory.flatMap { readStatus(under: $0) }
            if let value = status {
                if knownGeneration == nil
                    || knownGeneration!.pid != handle.pid
                    || knownGeneration!.command.isEmpty
                    || value.pid != knownGeneration!.pid || spawnedAt == nil
                    || value.start_time < spawnedAt!.timeIntervalSince1970
                    || value.start_time != knownGeneration!.startEpoch {
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
        ownedRecorderGeneration = nil
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
              let generation = ownedRecorderGeneration,
              generation.pid == handle.pid, !generation.command.isEmpty,
              let status = readStatus(under: directory)
        else { return false }
        return status.pid == generation.pid
            && status.start_time == generation.startEpoch
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
