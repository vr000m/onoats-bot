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
    static let heartbeatStallExitReason = "capture-heartbeat-stalled"
    static let heartbeatRecoveryClassificationDelay: TimeInterval = 0.5
    static let heartbeatRecoveryStabilityWindow: TimeInterval = 60

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
    /// Why the last config.toml edit from the panel was refused, or nil. The
    /// writer refuses an unreadable config rather than overwrite it, so the
    /// panel must say so instead of silently snapping the picker back.
    @Published private(set) var configWriteFailure: String?
    @Published var stopRequested = false
    @Published private(set) var prestartDetail: String?
    @Published private(set) var seminarConfigurationFailure = false
    @Published private(set) var lastFailureWasAppOwned = false
    @Published private(set) var mostRecentRunLogURL: URL?
    @Published private var mostRecentRunLogSink: RunLogSink?
    private var runLogFinishing = false
    private var runLogReservationAttempted = false
    @Published private(set) var mostRecentRunStartedAt: Date?
    @Published private(set) var automaticRecoveryActive = false

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

    private struct HeartbeatRecoveryRequest {
        let failedGeneration: RecorderGeneration
        let directory: URL
        let seminarMode: Bool
        let token: UInt64
        let failureReason: String
        let failureDetail: String?
    }
    private var heartbeatRecoveryRequest: HeartbeatRecoveryRequest?
    private var heartbeatRecoveryClassification: PollHandle?
    private var heartbeatRecoveryStability: PollHandle?
    private var heartbeatRecoveryGeneration: RecorderGeneration?
    private var heartbeatRecoveryReady = false
    private var heartbeatRecoveryToken: UInt64 = 0
    private var heartbeatRecoveryStabilityToken: UInt64 = 0
    private var heartbeatRecoveryBudgetConsumed = false

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
        heartbeatRecoveryClassification?.cancel()
        heartbeatRecoveryStability?.cancel()
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
                if let generation = ownedRecorderGeneration {
                    confirmAutomaticRecovery(generation)
                }
            } else {
                suspendAutomaticRecoveryStability()
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

        if automaticRecoveryActive {
            // Classification and old-log retirement intentionally run with no
            // retained child. Keep the admitted recovery visible until its
            // token either starts a replacement or settles as a failure.
            state = .starting
            return
        }

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

    private var canSpawnRecorder: Bool {
        proc == nil && mostRecentRunLogSink == nil && cliAvailable
    }
    var canStart: Bool { canSpawnRecorder && !automaticRecoveryActive }

    func start() {
        guard canStart else { return }
        invalidateHeartbeatRecovery(resetBudget: true)
        beginStart(directory: dataDirResolver(), seminar: seminarMode, automatic: false)
    }

    private func beginStart(directory pinned: URL, seminar: Bool, automatic: Bool) {
        guard canSpawnRecorder else { return }
        userRequestedStop = false
        if !automatic { flushNote = nil }
        prestartDetail = nil
        seminarConfigurationFailure = false
        lastFailureWasAppOwned = false
        ownedRecorderPidWasPublished = false
        ownedRecorderGeneration = nil
        setControlAuthority(nil)
        seminarMode = seminar
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
            if automatic {
                settleAutomaticRecoveryFailure(
                    reason: "log-reservation-failed", detail: error.localizedDescription,
                    note: "Automatic capture recovery could not create its run log")
            } else {
                state = .failed(
                    reason: "log-reservation-failed", detail: error.localizedDescription)
            }
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
        if seminar { p.arguments? += ["--category", Self.seminarCategory] }
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
            let reason = automatic ? "automatic-restart-spawn-failed" : "spawn-failed"
            if automatic {
                settleAutomaticRecoveryFailure(
                    reason: reason, detail: error.localizedDescription,
                    note: "Automatic capture recovery failed to start")
            } else {
                state = .failed(reason: reason, detail: error.localizedDescription)
            }
            pruneRunLogs()
        }
    }

    func stop() {
        if let handle = proc {
            // Latch intent while this retained generation still belongs to us.
            // The exit callback may already be queued after isRunning flipped.
            userRequestedStop = true
            if automaticRecoveryActive, !handle.isRunning {
                cancelAutomaticRecovery()
                return
            }
            state = .stopping
            if handle.isRunning { handle.terminate() }
            return
        }
        if automaticRecoveryActive {
            cancelAutomaticRecovery()
        }
    }

    func cancelAutomaticRecovery() {
        guard automaticRecoveryActive else { return }
        let request = heartbeatRecoveryRequest
        heartbeatRecoveryToken &+= 1
        heartbeatRecoveryClassification?.cancel()
        heartbeatRecoveryClassification = nil
        heartbeatRecoveryStability?.cancel()
        heartbeatRecoveryStability = nil
        heartbeatRecoveryStabilityToken &+= 1
        heartbeatRecoveryGeneration = nil
        heartbeatRecoveryRequest = nil
        automaticRecoveryActive = false
        flushNote = "Automatic capture recovery cancelled"
        if let handle = proc {
            userRequestedStop = true
            if handle.isRunning {
                state = .stopping
                handle.terminate()
            } else {
                state = .failed(
                    reason: request?.failureReason ?? Self.heartbeatStallExitReason,
                    detail: request?.failureDetail)
            }
        } else {
            state = .failed(
                reason: request?.failureReason ?? Self.heartbeatStallExitReason,
                detail: request?.failureDetail)
        }
    }

    func quitApp() {
        guard quitRequestedAt == nil else { return }
        quitRequestedAt = ops.now()
        if automaticRecoveryActive, proc?.isRunning != true {
            cancelAutomaticRecovery()
        } else if let handle = proc, handle.isRunning, !userRequestedStop {
            stop()
        }
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
        let attempt = spawnedAt
        let stoppedByUser = userRequestedStop
        prestartDetail = nil
        setControlAuthority(nil)
        var acceptedStatus: StatusRecord?
        if handle.terminationStatus == 0 {
            state = .stopped
        } else {
            if let directory, let knownGeneration, let attempt,
               knownGeneration.pid == handle.pid {
                acceptedStatus = matchingExitStatus(
                    under: directory, generation: knownGeneration, notBefore: attempt)
            }
            let pidWasPublished = ownedRecorderPidWasPublished
                || currentAttemptPublicationEvidence(handle: handle, directory: directory)
            state = .failed(
                reason: acceptedStatus?.exit_reason ?? "exit code \(handle.terminationStatus)",
                detail: acceptedStatus?.last_error)
            lastFailureWasAppOwned = true
            seminarConfigurationFailure = !pidWasPublished
                && runLogTailContainsInvalidCategoryTag()
        }
        let recoveryRequest: HeartbeatRecoveryRequest? = {
            guard handle.terminationStatus != 0, !stoppedByUser, quitRequestedAt == nil,
                  !heartbeatRecoveryBudgetConsumed, let directory, let knownGeneration,
                  let attempt, knownGeneration.pid == handle.pid,
                  !knownGeneration.command.isEmpty,
                  knownGeneration.startEpoch >= attempt.timeIntervalSince1970,
                  acceptedStatus?.exit_reason == Self.heartbeatStallExitReason
                    || acceptedStatus?.exit_reason == "fatal_error_frame"
            else { return nil }
            heartbeatRecoveryToken &+= 1
            return HeartbeatRecoveryRequest(
                failedGeneration: knownGeneration,
                directory: directory,
                seminarMode: seminarMode,
                token: heartbeatRecoveryToken,
                failureReason: acceptedStatus?.exit_reason
                    ?? "exit code \(handle.terminationStatus)",
                failureDetail: acceptedStatus?.last_error)
        }()
        userRequestedStop = false
        ownedRecorderGeneration = nil
        proc = nil
        runLogFinishing = true
        releaseRunLogSinkIfIdle()
        activeDataDir = nil
        activeDataDirDisplay = nil
        spawnedAt = nil
        if recoveryRequest == nil {
            seminarMode = false
            invalidateHeartbeatRecovery(resetBudget: false)
        }
        refreshDirectoryDisplays(configured: dataDirResolver())
        if let recoveryRequest {
            scheduleHeartbeatRecoveryClassification(recoveryRequest)
        }
    }

    private func matchingExitStatus(
        under directory: URL, generation: RecorderGeneration, notBefore attempt: Date? = nil
    ) -> StatusRecord? {
        guard !generation.command.isEmpty, let status = readStatus(under: directory),
              status.pid == generation.pid,
              status.start_time == generation.startEpoch,
              attempt.map({ status.start_time >= $0.timeIntervalSince1970 }) ?? true
        else { return nil }
        return status
    }

    private func scheduleHeartbeatRecoveryClassification(
        _ request: HeartbeatRecoveryRequest
    ) {
        heartbeatRecoveryRequest = request
        heartbeatRecoveryReady = false
        automaticRecoveryActive = true
        state = .starting
        flushNote = "Capture stalled; restarting automatically…"
        heartbeatRecoveryClassification?.cancel()
        heartbeatRecoveryClassification = ops.schedule(
            after: Self.heartbeatRecoveryClassificationDelay
        ) { [weak self] in
            guard let self, self.automaticRecoveryActive,
                  self.heartbeatRecoveryRequest?.token == request.token
            else { return }
            self.heartbeatRecoveryClassification = nil
            let status = self.matchingExitStatus(
                under: request.directory, generation: request.failedGeneration)
            guard status?.exit_reason == Self.heartbeatStallExitReason else {
                self.automaticRecoveryActive = false
                self.heartbeatRecoveryRequest = nil
                self.heartbeatRecoveryReady = false
                self.seminarMode = false
                self.flushNote = nil
                self.state = .failed(
                    reason: status?.exit_reason ?? request.failureReason,
                    detail: status?.last_error ?? request.failureDetail)
                return
            }
            self.heartbeatRecoveryBudgetConsumed = true
            self.heartbeatRecoveryReady = true
            self.startClassifiedHeartbeatRecoveryIfReady()
        }
    }

    private func startClassifiedHeartbeatRecoveryIfReady() {
        guard automaticRecoveryActive, heartbeatRecoveryReady,
              let request = heartbeatRecoveryRequest else { return }
        if hasLiveRecorderPublication(in: request.directory) {
            settleAutomaticRecoveryFailure(
                reason: "automatic-restart-superseded", detail: nil,
                note: "Automatic capture recovery skipped: another recorder is already running")
            activeDataDir = request.directory
            refreshDirectoryDisplays(configured: dataDirResolver())
            refresh()
            return
        }
        guard proc == nil, mostRecentRunLogSink == nil else { return }
        guard cliAvailable else {
            settleAutomaticRecoveryFailure(
                reason: "automatic-restart-cli-unavailable", detail: nil,
                note: "Automatic capture recovery failed: the onoats CLI is unavailable")
            return
        }
        heartbeatRecoveryReady = false
        beginStart(
            directory: request.directory, seminar: request.seminarMode, automatic: true)
    }

    private func confirmAutomaticRecovery(_ generation: RecorderGeneration) {
        guard automaticRecoveryActive, heartbeatRecoveryBudgetConsumed,
              heartbeatRecoveryGeneration == nil else { return }
        heartbeatRecoveryGeneration = generation
        flushNote = "Capture recovered automatically in a new recording"
        let token = heartbeatRecoveryRequest?.token ?? heartbeatRecoveryToken
        heartbeatRecoveryStability?.cancel()
        heartbeatRecoveryStabilityToken &+= 1
        let stabilityToken = heartbeatRecoveryStabilityToken
        heartbeatRecoveryStability = ops.schedule(
            after: Self.heartbeatRecoveryStabilityWindow
        ) { [weak self] in
            guard let self, self.automaticRecoveryActive,
                  self.heartbeatRecoveryRequest?.token == token,
                  self.heartbeatRecoveryStabilityToken == stabilityToken,
                  self.heartbeatRecoveryGeneration == generation,
                  self.ownedRecorderGeneration == generation,
                  self.proc?.isRunning == true
            else { return }
            guard self.hasExactPublication(generation) else {
                self.suspendAutomaticRecoveryStability()
                return
            }
            self.heartbeatRecoveryStability = nil
            self.heartbeatRecoveryRequest = nil
            self.heartbeatRecoveryGeneration = nil
            self.heartbeatRecoveryBudgetConsumed = false
            self.automaticRecoveryActive = false
        }
    }

    private func suspendAutomaticRecoveryStability() {
        guard automaticRecoveryActive, heartbeatRecoveryGeneration != nil else { return }
        heartbeatRecoveryStability?.cancel()
        heartbeatRecoveryStability = nil
        heartbeatRecoveryStabilityToken &+= 1
        heartbeatRecoveryGeneration = nil
        flushNote = "Capture recovery is waiting for recorder authority"
    }

    private func hasExactPublication(_ generation: RecorderGeneration) -> Bool {
        guard let directory = activeDataDir, let handle = proc, handle.isRunning,
              handle.pid == generation.pid, let pid = readPid(under: directory),
              pid.pid == generation.pid, pid.cmdline == generation.command,
              pid.startEpoch == generation.startEpoch
        else { return false }
        return processAlive(pid.pid, storedCmdline: pid.cmdline, startEpoch: pid.startEpoch)
    }

    private func hasLiveRecorderPublication(in directory: URL) -> Bool {
        guard let pid = readPid(under: directory) else { return false }
        return processAlive(pid.pid, storedCmdline: pid.cmdline, startEpoch: pid.startEpoch)
    }

    private func settleAutomaticRecoveryFailure(
        reason: String, detail: String?, note: String
    ) {
        heartbeatRecoveryToken &+= 1
        heartbeatRecoveryClassification?.cancel()
        heartbeatRecoveryClassification = nil
        heartbeatRecoveryStability?.cancel()
        heartbeatRecoveryStability = nil
        heartbeatRecoveryStabilityToken &+= 1
        heartbeatRecoveryRequest = nil
        heartbeatRecoveryGeneration = nil
        heartbeatRecoveryReady = false
        automaticRecoveryActive = false
        flushNote = note
        state = .failed(reason: reason, detail: detail)
    }

    private func invalidateHeartbeatRecovery(resetBudget: Bool) {
        heartbeatRecoveryToken &+= 1
        heartbeatRecoveryClassification?.cancel()
        heartbeatRecoveryClassification = nil
        heartbeatRecoveryStability?.cancel()
        heartbeatRecoveryStability = nil
        heartbeatRecoveryStabilityToken &+= 1
        heartbeatRecoveryRequest = nil
        heartbeatRecoveryGeneration = nil
        heartbeatRecoveryReady = false
        automaticRecoveryActive = false
        if resetBudget { heartbeatRecoveryBudgetConsumed = false }
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
        startClassifiedHeartbeatRecoveryIfReady()
    }

    func setSTTService(_ service: String) {
        do {
            try ConfigStore.writeValue(section: "stt", key: "service", value: service)
            configWriteFailure = nil
        } catch {
            configWriteFailure = "Could not save the speech-to-text service: \(error)"
        }
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
