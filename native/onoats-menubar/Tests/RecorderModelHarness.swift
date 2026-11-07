import Foundation

private struct HarnessFailure: Error, CustomStringConvertible {
    let description: String
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw HarnessFailure(description: message) }
}

private final class FakePoll: PollHandle {
    private(set) var cancelled = false
    func cancel() { cancelled = true }
}

private final class FakeProcess: ProcessHandle {
    let pid: Int32
    var isRunning = true
    var terminationStatus: Int32 = 0
    private(set) var terminateCount = 0

    init(pid: Int32) { self.pid = pid }
    func terminate() { terminateCount += 1 }
}

private struct FakeSpawnError: Error {}

@MainActor
private final class FakeSystemOps: SystemOps {
    var nowValue = Date(timeIntervalSince1970: 1_700_000_000)
    var files: [String: Data] = [:]
    var nonRegularFiles: Set<String> = []
    var alive: Set<Int32> = []
    var commands: [Int32: String] = [:]
    var executable = true
    var nextPid: Int32 = 900
    var requests: [ProcessRequest] = []
    var processes: [FakeProcess] = []
    var exits: [ObjectIdentifier: @MainActor (ProcessHandle) -> Void] = [:]
    var scheduledIntervals: [TimeInterval] = []
    var failSpawn = false
    /// Counts production PID-record reads, which go through the bounded
    /// `readData(at:maxBytes:)` (RecorderRecordReader.pid). SystemOps has no
    /// unbounded read requirement, so the fake defines none either.
    var pidReadCount = 0
    var processAliveCount = 0
    var commandLineCount = 0
    /// PIDs whose identity probe times out (`.unknown`) instead of answering.
    var unknownCommands: Set<Int32> = []
    var telemetryReadCount = 0
    var boundedReadLimits: [Int] = []
    var opened: [URL] = []
    var deleted: [URL] = []
    var tailReadLimits: [Int] = []
    var failReservation = false
    var applicationTerminations = 0
    var pollActions: [@MainActor () -> Void] = []
    func terminateApplication() { applicationTerminations += 1 }

    func spawn(
        _ request: ProcessRequest,
        onExit: @escaping @MainActor (ProcessHandle) -> Void
    ) throws -> ProcessHandle {
        if failSpawn { throw FakeSpawnError() }
        requests.append(request)
        if let log = request.appendOutputTo, files[log.path] == nil {
            files[log.path] = Data()
        }
        let process = FakeProcess(pid: nextPid)
        nextPid += 1
        processes.append(process)
        exits[ObjectIdentifier(process)] = onExit
        return process
    }

    func finish(_ process: FakeProcess, status: Int32) {
        process.isRunning = false
        process.terminationStatus = status
        exits.removeValue(forKey: ObjectIdentifier(process))?(process)
    }

    func processIsAlive(_ pid: Int32) -> Bool {
        processAliveCount += 1
        return alive.contains(pid)
    }
    func commandLine(_ pid: Int32) -> CommandLineProbe {
        commandLineCount += 1
        if unknownCommands.contains(pid) { return .unknown }
        return commands[pid].map(CommandLineProbe.command) ?? .absent
    }
    func readData(at url: URL, maxBytes: Int) -> Data? {
        boundedReadLimits.append(maxBytes)
        if url.lastPathComponent == UiTelemetryContract.fileName { telemetryReadCount += 1 }
        if url.lastPathComponent == "onoats.pid" { pidReadCount += 1 }
        guard let data = files[url.path], data.count <= maxBytes else { return nil }
        return data
    }
    /// Test-only inspector of fake storage; not a SystemOps read and never
    /// counted as one.
    func storedText(at url: URL) -> String? {
        files[url.path].flatMap { String(data: $0, encoding: .utf8) }
    }
    func write(_ data: Data, to url: URL) throws { files[url.path] = data }
    func deleteRegularFile(at url: URL) throws {
        guard isRegularFile(at: url) else { throw FakeSpawnError() }
        deleted.append(url)
        files[url.path] = nil
    }
    func reserveFile(at url: URL) throws -> Bool {
        if failReservation { throw FakeSpawnError() }
        guard files[url.path] == nil else { return false }
        files[url.path] = Data()
        return true
    }
    func fileExists(at url: URL) -> Bool { files[url.path] != nil }
    func isRegularFile(at url: URL) -> Bool {
        files[url.path] != nil && !nonRegularFiles.contains(url.path)
    }
    func contentsOfDirectory(at url: URL) -> [URL] {
        files.keys.compactMap { path in
            let candidate = URL(fileURLWithPath: path)
            return candidate.deletingLastPathComponent().path == url.path ? candidate : nil
        }
    }
    func readTail(at url: URL, maxBytes: Int) -> String? {
        tailReadLimits.append(maxBytes)
        guard let data = files[url.path] else { return nil }
        return String(decoding: data.suffix(maxBytes), as: UTF8.self)
    }
    func open(_ url: URL) { opened.append(url) }
    func isExecutable(at url: URL) -> Bool { executable }
    func now() -> Date { nowValue }
    func schedule(
        every interval: TimeInterval, _ action: @escaping @MainActor () -> Void
    ) -> PollHandle {
        scheduledIntervals.append(interval)
        pollActions.append(action)
        return FakePoll()
    }

    func put(_ text: String, _ path: URL) { files[path.path] = Data(text.utf8) }
    func putJSON(_ object: [String: Any], _ path: URL) throws {
        files[path.path] = try JSONSerialization.data(withJSONObject: object)
    }
}

private func active(_ directory: URL, _ name: String) -> URL {
    directory.appendingPathComponent(".active/\(name)")
}

@MainActor
private func putPid(
    _ ops: FakeSystemOps, directory: URL, pid: Int32, epoch: Double,
    command: String = "onoats bot"
) {
    ops.put("\(pid)\nonoats-bot\n\(command)\n\(epoch)\n", active(directory, "onoats.pid"))
    ops.alive.insert(pid)
    ops.commands[pid] = command
}

@MainActor
private func removePid(_ ops: FakeSystemOps, directory: URL, pid: Int32) {
    ops.files[active(directory, "onoats.pid").path] = nil
    ops.alive.remove(pid)
}

private func status(
    pid: Int32, epoch: Double, warning: String? = nil, running: Bool = true
) -> [String: Any] {
    [
        "schema": 2,
        "pid": pid,
        "start_time": epoch,
        "audio_source": "socket",
        "stt_label": running ? "websocket" : "",
        "running": running,
        "last_rotation_time": NSNull(),
        "last_error": NSNull(),
        "exit_reason": NSNull(),
        "supervisor_rc": NSNull(),
        "warning": warning ?? NSNull(),
        "mic_device": NSNull(),
        "system_device": NSNull(),
    ]
}

private func telemetry(pid: Int32, epoch: Double, now: Double) -> [String: Any] {
    [
        "schema": 2,
        "transcript_run_id": "0123456789abcdef0123456789abcdef",
        "pid": pid,
        "start_epoch": epoch,
        "updated_at": now,
        "audio": [
            "me": ["level": 0.25, "observed_at": now],
            "them": ["level": 0.75, "observed_at": now],
        ],
        "stt": [
            "me": ["state": "ready", "detail": NSNull()],
            "them": ["state": "ready", "detail": NSNull()],
            "aggregate": "ready",
        ],
        "transcript": ["text": "private preview", "source": "them"],
        "locked_category": "seminars",
    ]
}

private final class DirectoryChoice {
    var url: URL
    init(_ url: URL) { self.url = url }
}

@MainActor
private func makeModel(
    _ ops: FakeSystemOps, suite: String, directory: DirectoryChoice
) -> RecorderModel {
    let defaults = UserDefaults(suiteName: suite)!
    defaults.removePersistentDomain(forName: suite)
    defaults.set("/fake/onoats", forKey: "cliPath")
    return RecorderModel(
        systemOps: ops, userDefaults: defaults,
        resolveDataDir: { directory.url }, startPolling: false)
}

@MainActor
private func cleanupSuite(_ suite: String) {
    UserDefaults(suiteName: suite)?.removePersistentDomain(forName: suite)
}

@MainActor
private func testQuitOwnershipMatrix(_ root: URL) throws {
    for mode in ["stopped", "failed", "starting", "running", "stopping", "external"] {
        for withAuxiliary in [false, true] {
            let suite = "onoats.harness.quit.\(mode).\(withAuxiliary)"
            defer { cleanupSuite(suite) }
            let ops = FakeSystemOps()
            let directory = DirectoryChoice(root.appendingPathComponent(suite))
            let model = makeModel(ops, suite: suite, directory: directory)
            var owned: FakeProcess?
            if ["starting", "running", "stopping"].contains(mode) {
                model.start()
                owned = ops.processes[0]
                if mode != "starting" {
                    putPid(ops, directory: directory.url, pid: owned!.pid,
                           epoch: ops.nowValue.timeIntervalSince1970)
                    model.refresh()
                }
                if mode == "stopping" { model.stop() }
            } else if mode == "failed" {
                ops.failSpawn = true
                model.start()
                ops.failSpawn = false
            } else if mode == "external" {
                putPid(ops, directory: directory.url, pid: 777,
                       epoch: ops.nowValue.timeIntervalSince1970)
                model.refresh()
            }
            var auxiliary: FakeProcess?
            if withAuxiliary && model.activeDataDir != nil {
                let processCount = ops.processes.count
                model.flush()
                if ops.processes.count > processCount { auxiliary = ops.processes.last }
            }
            model.quitApp()
            model.quitApp() // pending Quit must be idempotent
            if let owned {
                try require(owned.terminateCount == 1, "Quit duplicated or missed owned stop: \(mode)")
                try require(ops.applicationTerminations == 0, "Quit abandoned owned process: \(mode)")
                ops.finish(owned, status: 0)
            }
            if let auxiliary {
                try require(ops.applicationTerminations == 0, "Quit abandoned auxiliary: \(mode)")
                ops.finish(auxiliary, status: 0)
            }
            try require(ops.applicationTerminations >= 1, "Quit never completed: \(mode)")
        }
    }
    let suite = "onoats.harness.quit.timeout"
    defer { cleanupSuite(suite) }
    let ops = FakeSystemOps()
    let model = makeModel(ops, suite: suite, directory: DirectoryChoice(root))
    model.start()
    model.quitApp()
    ops.nowValue = ops.nowValue.addingTimeInterval(15)
    for action in ops.pollActions { action() }
    try require(ops.applicationTerminations == 0, "timeout abandoned supervisor")
    try require(model.flushNote?.contains("Quit timed out") == true, "timeout not surfaced")
    try require(ops.processes[0].terminateCount == 1, "timeout sent unsafe extra signal")
}

@MainActor
private func testDirectoryAuthority(_ root: URL) throws {
    let old = root.appendingPathComponent("old")
    let new = root.appendingPathComponent("new")
    let choice = DirectoryChoice(old)
    let ops = FakeSystemOps()
    let model = makeModel(ops, suite: "onoats.harness.directory", directory: choice)

    model.seminarMode = true
    model.start()
    try require(ops.requests.count == 1, "Start did not spawn")
    try require(ops.requests[0].environment?[RecorderModel.dataDirEnvironment] == old.path,
                "Start did not export the pinned data directory")
    try require(ops.requests[0].arguments == ["bot", "--category", "seminars"],
                "Start category routing changed")
    try require(model.activeDataDir == old, "Start did not retain its directory")

    choice.url = new
    model.refresh()
    try require(model.activeDataDir == old, "mid-run config retargeted active reads")
    try require(model.nextDataDir == new, "next recording directory did not update")

    let owned = ops.processes[0]
    putPid(
        ops, directory: old, pid: owned.pid,
        epoch: ops.nowValue.timeIntervalSince1970)
    model.refresh()
    model.flush()
    try require(ops.requests.last?.arguments == [
        "flush", "--data-dir", old.path,
        "--expected-pid", String(owned.pid),
        "--expected-start-epoch", String(ops.nowValue.timeIntervalSince1970),
    ],
                "Flush did not use the active directory")

    model.stop()
    try require(owned.terminateCount == 1, "owned Stop did not terminate retained handle")
    try require(model.state == .stopping, "owned Stop did not enter draining state")
    putPid(
        ops, directory: old, pid: owned.pid,
        epoch: ops.nowValue.timeIntervalSince1970)
    model.refresh()
    try require(model.state == .stopping,
                "refresh resurrected Stop while the terminated handle/PID were still alive")
    try require(owned.terminateCount == 1, "refresh made Stop actionable a second time")
    ops.finish(owned, status: 0)
    try require(model.state == .stopped, "owned clean exit did not converge")
    try require(model.activeDataDir == nil, "old run retained directory after exit")
    try require(model.nextDataDir == new, "exit did not adopt next-run directory")

    try require(!model.canStart, "Start enabled while old Flush retains the sink")
    model.start()
    try require(ops.requests.count == 2,
                "next Start raced the still-running owned Flush writer")
    ops.finish(ops.processes[1], status: 0)
    model.start()
    try require(ops.requests.last?.environment?[RecorderModel.dataDirEnvironment] == new.path,
                "next Start did not adopt changed directory")
}


@MainActor
private func testStartPublicationAssociation(_ root: URL) throws {
    let directory = root.appendingPathComponent("publication-association")
    let ops = FakeSystemOps()
    let model = makeModel(
        ops, suite: "onoats.harness.publication-association",
        directory: DirectoryChoice(directory))
    let attempt = ops.nowValue.timeIntervalSince1970
    model.start()
    let supervisor = ops.processes[0]

    putPid(ops, directory: directory, pid: 701, epoch: attempt + 1)
    model.refresh()
    try require(model.state == .starting,
                "an unrelated racing PID record was claimed by the current Start")

    putPid(ops, directory: directory, pid: supervisor.pid, epoch: attempt - 1)
    try ops.putJSON(status(pid: supervisor.pid, epoch: attempt - 1),
                    active(directory, "onoats.status.json"))
    model.refresh()
    try require(model.state == .starting,
                "a same-supervisor PID publication older than the Start was claimed")

    putPid(ops, directory: directory, pid: supervisor.pid, epoch: attempt)
    try ops.putJSON(status(pid: supervisor.pid, epoch: attempt),
                    active(directory, "onoats.status.json"))
    model.refresh()
    try require(model.state == .running(ours: true),
                "a matching post-attempt recorder publication was not claimed")
}

@MainActor
private func testPollingDiscipline(_ root: URL) throws {
    let directory = root.appendingPathComponent("polling")
    let ops = FakeSystemOps()
    let defaults = UserDefaults(suiteName: "onoats.harness.polling")!
    defaults.removePersistentDomain(forName: "onoats.harness.polling")
    defaults.set("/fake/onoats", forKey: "cliPath")
    let model = RecorderModel(
        systemOps: ops, userDefaults: defaults,
        resolveDataDir: { directory }, startPolling: true)
    try require(ops.scheduledIntervals == [1.0, 0.25],
                "poll intervals must be lifecycle=1.0s and telemetry=0.25s")
    putPid(ops, directory: directory, pid: 88, epoch: 40)
    try ops.putJSON(telemetry(pid: 88, epoch: 40, now: ops.nowValue.timeIntervalSince1970),
                    active(directory, "onoats.ui.json"))

    let telemetryBeforeRefresh = ops.telemetryReadCount
    model.refresh()
    try require(ops.telemetryReadCount == telemetryBeforeRefresh,
                "lifecycle refresh read/decoded telemetry")

    let pidReads = ops.pidReadCount
    let livenessReads = ops.processAliveCount
    let commandReads = ops.commandLineCount
    model.refreshTelemetry()
    try require(pidReads > 0, "lifecycle refresh never read the PID record")
    try require(ops.pidReadCount == pidReads,
                "telemetry refresh reread the PID file instead of cached identity")
    try require(ops.processAliveCount == livenessReads && ops.commandLineCount == commandReads,
                "telemetry refresh reprobed process identity/liveness")
    try require(model.telemetryAvailable, "cached authoritative identity rejected telemetry")
}

@MainActor
private func testGenerationCacheRequiresPidCommandAndPositiveEpoch(_ root: URL) throws {
    let directory = root.appendingPathComponent("generation-cache")
    let ops = FakeSystemOps()
    let model = makeModel(
        ops, suite: "onoats.harness.generation-cache",
        directory: DirectoryChoice(directory))

    putPid(ops, directory: directory, pid: 88, epoch: 40, command: "onoats bot --one")
    model.refresh()
    let firstReads = ops.commandLineCount
    try require(model.state == .running(ours: false), "complete generation was rejected")

    // A live PID can be recycled to a different process inside the former
    // cache TTL. Every lifecycle refresh must revalidate the live command.
    ops.commands[88] = "unrelated successor"
    model.refresh()
    try require(ops.commandLineCount == firstReads + 1,
                "same-PID successor bypassed live command revalidation")
    try require(model.state == .stopped, "recycled PID authenticated from cache")
    ops.commands[88] = "onoats bot --one"

    putPid(ops, directory: directory, pid: 88, epoch: 40, command: "onoats bot --two")
    model.refresh()
    try require(ops.commandLineCount == firstReads + 2,
                "same-PID new-command generation reused a stale cache entry")

    putPid(ops, directory: directory, pid: 88, epoch: 41, command: "onoats bot --two")
    model.refresh()
    try require(ops.commandLineCount == firstReads + 3,
                "same-PID new-epoch generation reused a stale cache entry")

    putPid(ops, directory: directory, pid: 88, epoch: 0, command: "onoats bot --two")
    model.refresh()
    try require(model.state == .stopped, "zero-epoch generation was accepted")
    putPid(ops, directory: directory, pid: 88, epoch: 42, command: "")
    model.refresh()
    try require(model.state == .stopped, "missing-command generation was accepted")
}

@MainActor
private func testModelDeallocation(_ root: URL) throws {
    let ops = FakeSystemOps()
    weak var completed: RecorderModel?
    do {
        var model: RecorderModel? = makeModel(
            ops, suite: "onoats.harness.deallocation-complete",
            directory: DirectoryChoice(root.appendingPathComponent("deallocation-complete")))
        completed = model
        model?.start()
        ops.finish(ops.processes[0], status: 0)
        model = nil
    }
    try require(completed == nil, "completed spawn retained RecorderModel in an exit cycle")

    let failedOps = FakeSystemOps()
    failedOps.failSpawn = true
    weak var failed: RecorderModel?
    do {
        var model: RecorderModel? = makeModel(
            failedOps, suite: "onoats.harness.deallocation-failed",
            directory: DirectoryChoice(root.appendingPathComponent("deallocation-failed")))
        failed = model
        model?.start()
        model = nil
    }
    try require(failed == nil, "failed spawn retained RecorderModel")
}

@MainActor
private func testExternalRoutingAndPidReuse(_ root: URL) throws {
    let directory = root.appendingPathComponent("external")
    let next = root.appendingPathComponent("external-next")
    let choice = DirectoryChoice(directory)
    let ops = FakeSystemOps()
    putPid(ops, directory: directory, pid: 77, epoch: 10)
    let model = makeModel(ops, suite: "onoats.harness.external", directory: choice)
    try require(model.state == .running(ours: false), "external PID was not discovered")
    try require(model.activeDataDir == directory, "external identity directory was not pinned")
    try require(!model.canOpenLogForCurrentSurface,
                "external session exposed an app-owned Open Log action")

    choice.url = next
    model.refresh()
    // The panel still displays generation 77/10. A successor may publish its
    // pid record before the next refresh, but the captured action must remain
    // bound to the displayed generation and let the CLI fail closed.
    putPid(ops, directory: directory, pid: 78, epoch: 11)
    model.stopExternal()
    try require(ops.requests.last?.arguments == [
        "stop", "--data-dir", directory.path,
        "--expected-pid", "77", "--expected-start-epoch", "10.0",
    ],
                "external Stop used next-session directory")
    try require(model.stopRequested, "external Stop did not guard duplicate requests")
    try require(ops.requests.last?.environment?["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin",
                "external Stop helper did not receive the minimal environment")

    putPid(ops, directory: directory, pid: 77, epoch: 11)
    model.refresh()
    try require(!model.stopRequested, "same-PID new epoch inherited stopping guard")
    try require(model.state == .running(ours: false), "PID reuse changed liveness")

    model.flush()
    try require(ops.requests.last?.arguments == [
        "flush", "--data-dir", directory.path,
        "--expected-pid", "77", "--expected-start-epoch", "11.0",
    ],
                "external Flush used next-session directory")

    let planted = RecorderModel.auxiliaryEnvironment(from: [
        "HOME": "/Users/test", "TMPDIR": "/tmp/test", "LANG": "en_US.UTF-8",
        "OPENAI_API_KEY": "secret", "DYLD_INSERT_LIBRARIES": "/tmp/inject.dylib",
        "ONOATS_STT_WS_URI": "ws://user:password@example.invalid",
    ])
    try require(planted["HOME"] == "/Users/test" && planted["TMPDIR"] == "/tmp/test",
                "helper environment dropped required user context")
    try require(planted["OPENAI_API_KEY"] == nil
                    && planted["DYLD_INSERT_LIBRARIES"] == nil
                    && planted["ONOATS_STT_WS_URI"] == nil,
                "helper environment forwarded a secret or injection variable")
}

@MainActor
private func testPrestartDetailBoundaries(_ root: URL) throws {
    let directory = root.appendingPathComponent("prestart")
    let choice = DirectoryChoice(directory)
    let ops = FakeSystemOps()
    let model = makeModel(ops, suite: "onoats.harness.prestart", directory: choice)
    model.start()
    try require(model.canOpenLogForCurrentSurface,
                "app-owned starting surface did not expose its run log")
    let supervisor = ops.processes[0]

    try ops.putJSON(
        status(pid: supervisor.pid, epoch: ops.nowValue.timeIntervalSince1970,
               warning: "waiting-for-permission"),
        active(directory, "onoats.status.json"))
    model.refresh()
    try require(model.prestartDetail == "waiting-for-permission",
                "equal attempt timestamp was rejected")
    try require(model.state == .starting, "pre-PID detail established liveness")

    try ops.putJSON(
        status(pid: supervisor.pid, epoch: ops.nowValue.timeIntervalSince1970 - 0.001,
               warning: "old"),
        active(directory, "onoats.status.json"))
    model.refresh()
    try require(model.prestartDetail == nil, "older pre-PID status was accepted")
    try require(model.state == .starting, "old detail changed liveness")

    try ops.putJSON(
        status(pid: supervisor.pid + 1, epoch: ops.nowValue.timeIntervalSince1970,
               warning: "wrong pid"),
        active(directory, "onoats.status.json"))
    model.refresh()
    try require(model.prestartDetail == nil, "mismatched supervisor PID was accepted")

    try ops.putJSON(
        status(pid: supervisor.pid, epoch: ops.nowValue.timeIntervalSince1970,
               warning: "waiting-for-permission"),
        active(directory, "onoats.status.json"))
    model.refresh()
    try require(model.prestartDetail != nil, "exit transition setup lost prestart detail")
    ops.finish(supervisor, status: 1)
    try require(model.prestartDetail == nil, "supervisor exit did not clear detail")
    if case .failed = model.state {} else {
        throw HarnessFailure(description: "unexpected supervisor exit did not fail loud")
    }

    let publicationOps = FakeSystemOps()
    let publicationModel = makeModel(
        publicationOps, suite: "onoats.harness.prestart-publication", directory: choice)
    publicationModel.start()
    let publicationSupervisor = publicationOps.processes[0]
    try publicationOps.putJSON(
        status(
            pid: publicationSupervisor.pid,
            epoch: publicationOps.nowValue.timeIntervalSince1970,
            warning: "waiting-for-permission"),
        active(directory, "onoats.status.json"))
    publicationModel.refresh()
    try require(publicationModel.prestartDetail != nil,
                "publication transition setup lost prestart detail")
    putPid(
        publicationOps, directory: directory, pid: publicationSupervisor.pid,
        epoch: publicationOps.nowValue.timeIntervalSince1970)
    try publicationOps.putJSON(
        status(
            pid: publicationSupervisor.pid,
            epoch: publicationOps.nowValue.timeIntervalSince1970),
        active(directory, "onoats.status.json"))
    publicationModel.refresh()
    try require(publicationModel.state == .running(ours: true),
                "authoritative PID was ignored")
    try require(publicationModel.prestartDetail == nil,
                "PID publication did not clear prestart detail")
}

@MainActor
private func testTelemetryNeverControlsLiveness(_ root: URL) throws {
    let directory = root.appendingPathComponent("telemetry")
    let choice = DirectoryChoice(directory)
    let ops = FakeSystemOps()
    try ops.putJSON(telemetry(pid: 81, epoch: 30, now: ops.nowValue.timeIntervalSince1970),
                    active(directory, "onoats.ui.json"))
    let model = makeModel(ops, suite: "onoats.harness.telemetry", directory: choice)
    model.refreshTelemetry()
    try require(model.state == .stopped, "telemetry created recorder liveness")
    try require(!model.telemetryAvailable, "telemetry without live PID was accepted")

    putPid(ops, directory: directory, pid: 81, epoch: 30)
    model.refresh()
    model.refreshTelemetry()
    try require(model.state == .running(ours: false), "live external PID not authoritative")
    try require(model.telemetryAvailable, "same-owner fresh telemetry was rejected")
    try require(model.meActivity?.level == 0.25 && model.themActivity?.level == 0.75,
                "activity did not decode")
    try require(model.aggregateStt == .ready, "STT aggregate did not decode")
    try require(model.transcriptPreview == nil, "preview default was not off")
    try require(model.categoryForDisplay == "seminars", "external category was not decoded")
    try require(ops.boundedReadLimits.last == RecorderModel.telemetryMaxBytes,
                "telemetry polling did not use the bounded reader")

    ops.files[active(directory, "onoats.ui.json").path] = Data(
        repeating: 0x7b, count: RecorderModel.telemetryMaxBytes + 1)
    model.refreshTelemetry()
    try require(!model.telemetryAvailable, "oversized telemetry was accepted")
    try require(ops.boundedReadLimits.last == RecorderModel.telemetryMaxBytes,
                "oversized telemetry bypassed the bounded reader")

    ops.files[active(directory, "onoats.ui.json").path] = nil
    model.refreshTelemetry()
    try require(model.state == .running(ours: false), "missing telemetry changed liveness")
    try require(!model.telemetryAvailable, "missing telemetry did not clear display state")
    try require(model.meActivity == nil && model.aggregateStt == nil,
                "neutral telemetry state was not restored")
    try require(model.categoryUnavailable, "external category did not degrade neutrally")
}

@MainActor
private func testPanelTelemetryPresentationInputs(_ root: URL) throws {
    let directory = root.appendingPathComponent("panel-telemetry")
    let ops = FakeSystemOps()
    let now = ops.nowValue.timeIntervalSince1970
    putPid(ops, directory: directory, pid: 92, epoch: 50)
    try ops.putJSON(status(pid: 92, epoch: 50), active(directory, "onoats.status.json"))
    var snapshot = telemetry(pid: 92, epoch: 50, now: now)
    var audio = snapshot["audio"] as! [String: [String: Any]]
    audio["me"]?["observed_at"] =
        now - UiTelemetryContract.audioObservationExpirySeconds - 0.001
    snapshot["audio"] = audio
    snapshot["stt"] = [
        "me": ["state": "recovering", "detail": "reconnecting"],
        "them": ["state": "ready", "detail": NSNull()],
        "aggregate": "recovering",
    ]
    try ops.putJSON(snapshot, active(directory, "onoats.ui.json"))

    let model = makeModel(
        ops, suite: "onoats.harness.panel-telemetry", directory: DirectoryChoice(directory))
    model.setTranscriptPreviewEnabled(true)
    model.refreshTelemetry()
    try require(model.meActivity == nil, "stale Me observation did not become No signal")
    try require(model.themActivity?.level == 0.75, "fresh Them observation was lost")
    try require(model.aggregateStt == .recovering, "aggregate STT state was lost")
    try require(model.meStt?.detail == "reconnecting", "unhealthy branch detail was lost")
    try require(model.themStt?.state == .ready, "healthy branch detail was corrupted")
    try require(model.transcriptPreview?.source == "them", "preview source was lost")

    ops.files[active(directory, "onoats.ui.json").path] = nil
    model.refreshTelemetry()
    try require(!model.telemetryAvailable, "missing external telemetry stayed available")
    try require(model.audioSource == "socket", "status source disappeared with telemetry")
    try require(model.categoryUnavailable, "missing external category was inferred")
}

@MainActor
private func testOwnedSeminarBadgeSurvivesTelemetryLoss(_ root: URL) throws {
    let directory = root.appendingPathComponent("owned-seminar")
    let ops = FakeSystemOps()
    let model = makeModel(
        ops, suite: "onoats.harness.owned-seminar", directory: DirectoryChoice(directory))
    model.seminarMode = true
    model.start()
    let process = ops.processes[0]
    let epoch = ops.nowValue.timeIntervalSince1970
    putPid(ops, directory: directory, pid: process.pid, epoch: epoch)
    try ops.putJSON(status(pid: process.pid, epoch: epoch),
                    active(directory, "onoats.status.json"))
    model.refresh()
    model.refreshTelemetry()
    try require(model.state == .running(ours: true), "owned recorder was not claimed")
    try require(!model.telemetryAvailable, "test unexpectedly had telemetry")
    try require(model.categoryForDisplay == RecorderModel.seminarCategory,
                "owned Seminar lock depended on telemetry")
}

@MainActor
private func testMissingCliDisablesStart(_ root: URL) throws {
    let ops = FakeSystemOps()
    ops.executable = false
    let model = makeModel(
        ops, suite: "onoats.harness.missing-cli",
        directory: DirectoryChoice(root.appendingPathComponent("missing-cli")))
    try require(!model.cliAvailable, "unresolvable CLI was reported available")
    try require(!model.canStart, "Start availability ignored missing CLI")
    model.start()
    try require(ops.requests.isEmpty, "Start spawned an unresolvable CLI")
    try require(model.state == .stopped, "missing CLI changed recorder state")
}

@MainActor
private func testPerRunLogsPruningAndOwnedRouting(_ root: URL) throws {
    let directory = root.appendingPathComponent("logs-data")
    let ops = FakeSystemOps()
    let model = makeModel(
        ops, suite: "onoats.harness.logs", directory: DirectoryChoice(directory))
    let logs = root.standardizedFileURL.appendingPathComponent(
        "Library/Logs/Onoats", isDirectory: true)
    let unrelated = logs.appendingPathComponent("keep-me.txt")
    ops.put("unrelated", unrelated)
    let unsafe = logs.appendingPathComponent("onoats-bot-9999999999999.log")
    ops.put("unsafe", unsafe)
    ops.nonRegularFiles.insert(unsafe.path)
    var runLogs: [URL] = []

    for index in 0..<11 {
        ops.nowValue = Date(timeIntervalSince1970: 1_700_000_000 + Double(index))
        model.start()
        guard let log = ops.requests.last?.appendOutputTo else {
            throw HarnessFailure(description: "owned Start did not capture a run log")
        }
        runLogs.append(log)
        try require(log.lastPathComponent == "onoats-bot-\(1_700_000_000_000 + index * 1000).log",
                    "run log name is not the Start-attempt epoch milliseconds")
        let recorder = ops.processes.last!
        putPid(
            ops, directory: directory, pid: recorder.pid,
            epoch: ops.nowValue.timeIntervalSince1970)
        model.refresh()
        model.flush()
        try require(ops.requests.last?.appendOutputTo == log,
                    "owned Flush did not append to the active run log")
        let flush = ops.processes[ops.processes.count - 1]
        ops.finish(recorder, status: 0)
        ops.finish(flush, status: 0)
    }

    try require(!ops.fileExists(at: runLogs[0]), "11th run did not prune the oldest log")
    try require(runLogs.dropFirst().allSatisfy { ops.fileExists(at: $0) },
                "pruning removed one of the newest ten run logs")
    try require(ops.fileExists(at: unrelated), "pruning deleted an unrelated file")
    try require(ops.fileExists(at: unsafe), "pruning deleted a non-regular matching entry")
    try require(ops.deleted == [runLogs[0]], "pruning touched more than the oldest run log")

    // External commands are never appended to an app-owned run log.
    let externalOps = FakeSystemOps()
    putPid(externalOps, directory: directory, pid: 77, epoch: 10)
    let external = makeModel(
        externalOps, suite: "onoats.harness.logs-external",
        directory: DirectoryChoice(directory))
    external.flush()
    try require(externalOps.requests.last?.appendOutputTo == nil,
                "external Flush was written to an app-owned run log")
    external.stopExternal()
    try require(externalOps.requests.last?.appendOutputTo == nil,
                "external Stop was written to an app-owned run log")
}

@MainActor
private func testRunLogOwnerOutlivesOverlappingFlush(_ root: URL) throws {
    let directory = root.appendingPathComponent("owner-lifetime-data")
    let ops = FakeSystemOps()
    let model = makeModel(
        ops, suite: "onoats.harness.owner-lifetime",
        directory: DirectoryChoice(directory))
    model.start()
    let recorder = ops.processes[0]
    putPid(
        ops, directory: directory, pid: recorder.pid,
        epoch: ops.nowValue.timeIntervalSince1970)
    model.refresh()
    model.flush()
    let flush = ops.processes[1]
    guard let recorderSink = ops.requests[0].outputSink,
          let flushSink = ops.requests[1].outputSink else {
        throw HarnessFailure(description: "owned writer request lost its sink")
    }
    try require(recorderSink === flushSink,
                "owned recorder and Flush did not share one retained sink")

    ops.finish(recorder, status: 0)
    try require(!model.canStart, "Start enabled while old Flush retains the sink")
    model.start()
    try require(ops.requests.count == 2,
                "a new run started while the prior Flush still owned a log duplicate")
    ops.finish(flush, status: 0)
    try require(model.canStart, "Start stayed disabled after auxiliary cleanup")
    model.start()
    try require(ops.requests.count == 3,
                "run-log owner was not released after the final child exited")
}

@MainActor
private func testActiveRunLogSurvivesClockRollback(_ root: URL) throws {
    let directory = root.appendingPathComponent("clock-rollback-data")
    let ops = FakeSystemOps()
    let logs = root.standardizedFileURL.appendingPathComponent(
        "Library/Logs/Onoats", isDirectory: true)
    for index in 0..<10 {
        ops.put("future", logs.appendingPathComponent("onoats-bot-\(9_000 + index).log"))
    }
    ops.nowValue = Date(timeIntervalSince1970: 1)
    let model = makeModel(
        ops, suite: "onoats.harness.clock-rollback", directory: DirectoryChoice(directory))
    model.start()
    let active = ops.requests[0].appendOutputTo!
    try require(ops.fileExists(at: active),
                "wall-clock rollback allowed pruning to unlink the active run log")
    try require(ops.deleted.count == 1 && ops.deleted[0] != active,
                "active-log retention did not prune exactly one historical log")
}

@MainActor
private func testReservationFailureClearsPriorRunAuthority(_ root: URL) throws {
    let directory = root.appendingPathComponent("reservation-failure-data")
    let ops = FakeSystemOps()
    let model = makeModel(
        ops, suite: "onoats.harness.reservation-failure",
        directory: DirectoryChoice(directory))
    model.start()
    let oldLog = ops.requests[0].appendOutputTo!
    ops.finish(ops.processes[0], status: 0)
    ops.failReservation = true
    model.start()
    try require(model.mostRecentRunLogURL == nil,
                "failed reservation retained the prior run as current authority")
    model.openLog()
    try require(ops.opened.last == oldLog.deletingLastPathComponent(),
                "failed reservation opened the previous run instead of the log folder")
}

@MainActor
private func testNativeCommandLineProbeIsBounded(_ root: URL) throws {
    // Round 7: the `ps` identity probe runs on every lifecycle refresh on the
    // main actor; a stalled child must not freeze the menu bar. Round 8: it is
    // also killed and reported `.unknown` (never read as "dead").
    let ops = NativeSystemOps()
    if case .command(let live) = ops.commandLine(getpid()) {
        try require(!live.isEmpty, "bounded probe returned an empty command line")
    } else {
        throw HarnessFailure(
            description: "bounded probe lost the live command line of a real process")
    }
    try require(ops.commandLine(Int32.max - 1) == .absent,
                "ps proved a missing process but the probe did not say absent")
    let directory = root.appendingPathComponent("command-line-probe")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let pidFile = directory.appendingPathComponent("stalled.pid")
    let stalled = directory.appendingPathComponent("stalled-ps")
    // Ignore SIGTERM so the probe must escalate to SIGKILL.
    try Data(
        "#!/bin/sh\ntrap '' TERM\necho $$ > '\(pidFile.path)'\nwhile :; do /bin/sleep 1; done\n"
            .utf8
    ).write(to: stalled)
    try FileManager.default.setAttributes(
        [.posixPermissions: 0o700], ofItemAtPath: stalled.path)
    ops.commandLineExecutable = stalled
    ops.commandLineTimeout = 0.3
    let started = Date()
    let value = ops.commandLine(getpid())
    let elapsed = Date().timeIntervalSince(started)
    try require(value == .unknown, "a stalled probe did not report unknown")
    try require(elapsed < 2.0,
                "stalled ps probe blocked the main actor for \(elapsed)s")
    let pidText = try String(contentsOf: pidFile, encoding: .utf8)
        .trimmingCharacters(in: .whitespacesAndNewlines)
    guard let stalledPID = Int32(pidText) else {
        throw HarnessFailure(description: "stalled probe never wrote its pid")
    }
    var gone = false
    for _ in 0..<40 {
        if kill(stalledPID, 0) != 0 && errno == ESRCH { gone = true; break }
        Thread.sleep(forTimeInterval: 0.05)
    }
    try require(gone, "a timed-out ps child survived the probe")
}

private func testConfigStoreReadIsBoundedAndNeverOverwritesUnreadable(_ root: URL) throws {
    let directory = root.appendingPathComponent("config-bounded")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)

    func kind(_ result: ConfigStore.ConfigText) -> String {
        switch result {
        case .absent: return "absent"
        case .unreadable: return "unreadable"
        case .text(let text): return "text:\(text)"
        }
    }

    let missing = directory.appendingPathComponent("missing.toml")
    try require(kind(ConfigStore.readConfigText(at: missing)) == "absent", "missing file is absent")

    let regular = directory.appendingPathComponent("regular.toml")
    try "[stt]\nservice = \"whisper\"\n".write(to: regular, atomically: true, encoding: .utf8)
    try require(
        kind(ConfigStore.readConfigText(at: regular)) == "text:[stt]\nservice = \"whisper\"\n",
        "a regular file reads back verbatim")

    let oversized = directory.appendingPathComponent("oversized.toml")
    try Data(repeating: 0x61, count: ConfigStore.maxConfigBytes + 1).write(to: oversized)
    try require(kind(ConfigStore.readConfigText(at: oversized)) == "unreadable", "oversized is unreadable")

    let notUTF8 = directory.appendingPathComponent("binary.toml")
    try Data([0xff, 0xfe, 0xfd]).write(to: notUTF8)
    try require(kind(ConfigStore.readConfigText(at: notUTF8)) == "unreadable", "invalid UTF-8 is unreadable")

    // A FIFO with no writer would block String(contentsOf:) forever.
    let fifo = directory.appendingPathComponent("fifo.toml")
    try require(mkfifo(fifo.path, 0o600) == 0, "mkfifo failed")
    try require(kind(ConfigStore.readConfigText(at: fifo)) == "unreadable", "a FIFO must not hang or read")

    // The writer must refuse, never replace the whole file with one key.
    for target in [oversized, notUTF8, fifo] {
        var threw = false
        do {
            try ConfigStore.writeValue(section: "stt", key: "service", value: "mlx", at: target)
        } catch { threw = true }
        try require(threw, "writeValue must refuse an unreadable config: \(target.lastPathComponent)")
    }
    let untouched = try Data(contentsOf: notUTF8)
    try require(untouched == Data([0xff, 0xfe, 0xfd]), "the unreadable config was left untouched")

    // Absent still creates it, a readable one is edited in place.
    let created = directory.appendingPathComponent("created.toml")
    try ConfigStore.writeValue(section: "stt", key: "service", value: "mlx", at: created)
    try require(kind(ConfigStore.readConfigText(at: created)).contains("service = \"mlx\""), "created")
    try ConfigStore.writeValue(section: "stt", key: "service", value: "whisper", at: regular)
    try require(kind(ConfigStore.readConfigText(at: regular)).contains("whisper"), "edited in place")
}

@MainActor
private func testUnknownIdentityProbeKeepsTheLastProvenVerdict(_ root: URL) throws {
    let directory = root.appendingPathComponent("unknown-probe")
    let ops = FakeSystemOps()
    let model = makeModel(
        ops, suite: "onoats.harness.unknown-probe",
        directory: DirectoryChoice(directory))

    // No prior proof: an unproven identity fails closed.
    putPid(ops, directory: directory, pid: 90, epoch: 40, command: "onoats bot --one")
    ops.unknownCommands = [90]
    model.refresh()
    try require(model.state == .stopped, "unproven identity was accepted without a prior proof")

    // Proven once, then the probe stalls: the live recorder must not flip to Stopped.
    ops.unknownCommands = []
    model.refresh()
    try require(model.state == .running(ours: false), "proven generation was rejected")
    ops.unknownCommands = [90]
    model.refresh()
    try require(model.state == .running(ours: false),
                "a stalled probe read a proven live recorder as dead")

    // A different generation at the same pid does not inherit the verdict.
    putPid(ops, directory: directory, pid: 90, epoch: 41, command: "onoats bot --one")
    model.refresh()
    try require(model.state == .stopped, "a new generation inherited an old verdict")

    // Definitive absence clears the verdict even if the probe later stalls.
    ops.unknownCommands = []
    model.refresh()
    try require(model.state == .running(ours: false), "re-proven generation was rejected")
    ops.commands[90] = nil
    model.refresh()
    try require(model.state == .stopped, "an absent process stayed running")
    ops.commands[90] = "onoats bot --one"
    ops.unknownCommands = [90]
    model.refresh()
    try require(model.state == .stopped, "an absent verdict was resurrected by a stalled probe")
}

@MainActor
private func testNativeBoundedReadStateSpace(_ root: URL) throws {
    let ops = NativeSystemOps()
    let directory = root.appendingPathComponent("bounded-read")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let regular = directory.appendingPathComponent("regular")
    try Data("abcdef".utf8).write(to: regular)
    try require(ops.readData(at: regular, maxBytes: 6) == Data("abcdef".utf8),
                "bounded reader rejected an in-limit regular file")
    try require(ops.readData(at: regular, maxBytes: 5) == nil,
                "bounded reader accepted an oversized regular file")
    try require(ops.readTail(at: regular, maxBytes: 3) == "def",
                "tail reader did not read from its validated descriptor")

    let symlink = directory.appendingPathComponent("symlink")
    try FileManager.default.createSymbolicLink(at: symlink, withDestinationURL: regular)
    try require(ops.readData(at: symlink, maxBytes: 32) == nil,
                "bounded reader followed a symbolic link")

    let fifo = directory.appendingPathComponent("fifo")
    guard Darwin.mkfifo(fifo.path, S_IRUSR | S_IWUSR) == 0 else {
        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
    }
    let started = Date()
    try require(ops.readData(at: fifo, maxBytes: 32) == nil,
                "bounded reader accepted a FIFO")
    try require(Date().timeIntervalSince(started) < 0.5,
                "bounded reader blocked while opening a FIFO")
    try require(ops.readData(at: directory, maxBytes: 32) == nil,
                "bounded reader accepted a directory")

    let writable = directory.appendingPathComponent("group-writable")
    try Data("authority".utf8).write(to: writable)
    try FileManager.default.setAttributes([.posixPermissions: 0o660], ofItemAtPath: writable.path)
    try require(ops.readData(at: writable, maxBytes: 32) == nil,
                "bounded reader accepted a group-writable authority file")
}

@MainActor
private func testStatusRecordsUseTheBoundedAuthorityReader(_ root: URL) throws {
    let directory = root.appendingPathComponent("bounded-status-record")
    let ops = FakeSystemOps()
    try ops.putJSON(status(pid: 42, epoch: 84), active(directory, "onoats.status.json"))
    ops.boundedReadLimits.removeAll()
    guard case .record = RecorderRecordReader.status(
        under: directory, expectedSchema: 2, using: ops
    ) else {
        throw HarnessFailure(description: "bounded status reader rejected a valid record")
    }
    try require(ops.boundedReadLimits.last == 65_536,
                "status polling bypassed the bounded authority reader")
}

@MainActor
private func testSchemaDriftClearsOnlyWhenEvidenceDisappears(_ root: URL) throws {
    let first = root.appendingPathComponent("schema-first")
    let second = root.appendingPathComponent("schema-second")
    let choice = DirectoryChoice(first)
    let ops = FakeSystemOps()
    try ops.putJSON(["schema": 999], active(first, "onoats.status.json"))
    let model = makeModel(ops, suite: "onoats.harness.schema-drift", directory: choice)
    try require(model.schemaDrift, "current unsupported status schema was hidden")
    ops.files[active(first, "onoats.status.json").path] = nil
    model.refresh()
    try require(!model.schemaDrift, "missing status retained stale schema drift")
    try ops.putJSON(["schema": 999], active(first, "onoats.status.json"))
    model.refresh()
    try require(model.schemaDrift, "restored unsupported schema was not surfaced")
    choice.url = second
    model.refresh()
    try require(!model.schemaDrift, "data-directory change retained prior schema drift")
}

@MainActor
private func testRunLogCollisionReservesFreshPath(_ root: URL) throws {
    let directory = root.appendingPathComponent("collision-data")
    let ops = FakeSystemOps()
    ops.nowValue = Date(timeIntervalSince1970: 1_700_000_000)
    let logs = root.standardizedFileURL.appendingPathComponent(
        "Library/Logs/Onoats", isDirectory: true)
    let stale = logs.appendingPathComponent("onoats-bot-1700000000000.log")
    let expected = logs.appendingPathComponent("onoats-bot-1700000000001.log")
    let staleContents = "stale\n\(RecorderModel.invalidCategoryErrorTag)\n"
    ops.put(staleContents, stale)
    let unrelated = logs.appendingPathComponent("onoats-bot-1700000000000.log.saved")
    ops.put("keep", unrelated)

    let model = makeModel(
        ops, suite: "onoats.harness.log-collision", directory: DirectoryChoice(directory))
    model.seminarMode = true
    model.start()
    let process = ops.processes[0]
    guard let reserved = ops.requests[0].appendOutputTo else {
        throw HarnessFailure(description: "colliding Start did not reserve a run log")
    }
    try require(reserved == expected,
                "colliding Start reserved \(reserved.path), expected \(expected.path)")
    try require(ops.storedText(at: stale) == staleContents,
                "colliding Start truncated or rewrote the stale run log")
    try require(ops.fileExists(at: unrelated),
                "collision handling touched a non-matching log-like file")

    // The stale tag belongs to the previous run and must not classify this exit.
    ops.finish(process, status: 2)
    try require(!model.seminarConfigurationFailure,
                "a stale colliding log classified the new Start failure")
    try require(ops.fileExists(at: stale), "the stale collision file was deleted")
    try require(ops.fileExists(at: unrelated), "pruning deleted a non-run-log file")
}

@MainActor
private func testNativeAppendIsAtomicAcrossSeparateWriters(_ root: URL) throws {
    let log = root.appendingPathComponent("atomic-append/onoats-bot-1.log")
    let ops = NativeSystemOps()
    guard let sink = try ops.reserveRunLogSink(at: log) else {
        throw HarnessFailure(description: "could not reserve descriptor-owned run log")
    }
    let probe = try sink.duplicateOutputHandle()!
    defer { try? probe.close() }
    try require(Darwin.fcntl(probe.fileDescriptor, F_GETFL) & O_APPEND != 0,
                "duplicated run-log writer lost O_APPEND")

    // Replace the presentation pathname after reservation. Every child and
    // the bounded tag reader must remain attached to the reserved inode.
    let reservedInode = log.appendingPathExtension("reserved-inode")
    try FileManager.default.moveItem(at: log, to: reservedInode)
    try Data("replacement-path\n".utf8).write(to: log)
    let recorderLine = "RECORDER-COMPLETE-" + String(repeating: "r", count: 4096) + "\n"
    let auxiliaryLine = "AUXILIARY-COMPLETE-" + String(repeating: "a", count: 4096) + "\n"
    let tagLine = RecorderModel.invalidCategoryErrorTag + "\n"
    let harness = URL(fileURLWithPath: CommandLine.arguments[0])
    let recorder = try ops.spawn(
        ProcessRequest(
            executable: harness,
            arguments: ["--append-writer", "0.2", recorderLine + tagLine],
            environment: nil, outputSink: sink),
        onExit: { _ in })
    let auxiliary = try ops.spawn(
        ProcessRequest(
            executable: harness,
            arguments: ["--append-writer", "0", auxiliaryLine],
            environment: nil, outputSink: sink),
        onExit: { _ in })

    let deadline = Date().addingTimeInterval(3)
    while (recorder.isRunning || auxiliary.isRunning) && Date() < deadline {
        Thread.sleep(forTimeInterval: 0.01)
    }
    try require(!recorder.isRunning && !auxiliary.isRunning,
                "concurrent append regression exceeded its bounded deadline")
    try require(recorder.terminationStatus == 0 && auxiliary.terminationStatus == 0,
                "concurrent append fixture writer failed")
    let contents = try String(contentsOf: reservedInode, encoding: .utf8)
    try require(contents.components(separatedBy: recorderLine).count == 2,
                "recorder output was overwritten or split by the auxiliary writer")
    try require(contents.components(separatedBy: auxiliaryLine).count == 2,
                "auxiliary output was overwritten or split by the recorder writer")
    try require(contents.utf8.count == recorderLine.utf8.count
                    + auxiliaryLine.utf8.count + tagLine.utf8.count,
                "concurrent writers left truncated or interleaved output")
    let replacementContents = try String(contentsOf: log, encoding: .utf8)
    try require(replacementContents == "replacement-path\n",
                "child output was retargeted through the replaced pathname")
    try require(sink.readTail(maxBytes: 65_536)?.contains(
                    RecorderModel.invalidCategoryErrorTag) == true,
                "bounded invalid-category read reopened the replaced pathname")
    sink.closeOwner()
    try require(sink.readTail(maxBytes: 65_536) == nil,
                "run-log owner descriptor remained usable after explicit release")
}

@MainActor
private func testOpenLogResolution(_ root: URL) throws {
    let directory = root.appendingPathComponent("open-log-data")
    let ops = FakeSystemOps()
    let model = makeModel(
        ops, suite: "onoats.harness.open-log", directory: DirectoryChoice(directory))
    model.start()
    let remembered = ops.requests[0].appendOutputTo!
    let folder = remembered.deletingLastPathComponent()
    ops.finish(ops.processes[0], status: 0)
    model.openLog()
    try require(ops.opened.last == remembered, "in-process Open Log lost its run path")

    ops.files[remembered.path] = nil
    let older = folder.appendingPathComponent("onoats-bot-100.log")
    ops.put("old", older)
    model.openLog()
    try require(ops.opened.last == folder,
                "missing in-memory run log scanned an older file instead of opening folder")

    let newest = folder.appendingPathComponent("onoats-bot-999.log")
    ops.put("new", newest)
    let unsafeNewest = folder.appendingPathComponent("onoats-bot-1000.log")
    ops.put("unsafe", unsafeNewest)
    ops.nonRegularFiles.insert(unsafeNewest.path)
    let relaunched = makeModel(
        ops, suite: "onoats.harness.open-log-relaunch", directory: DirectoryChoice(directory))
    relaunched.openLog()
    try require(ops.opened.last == newest, "fresh launch did not resolve newest surviving log")
    try require(ops.opened.last != unsafeNewest,
                "fresh launch opened a non-regular matching entry")
    relaunched.showLogsFolder()
    try require(ops.opened.last == folder, "Show Logs Folder did not open canonical folder")
}

@MainActor
private func invalidCategoryCase(
    _ root: URL, suite: String, exit: Int32, contents: String,
    publication: String? = nil, refreshAfterPublication: Bool = true,
    removePidBeforeExit: Bool = false
) throws -> (RecorderModel, FakeSystemOps) {
    let directory = root.appendingPathComponent(suite)
    let ops = FakeSystemOps()
    let model = makeModel(ops, suite: suite, directory: DirectoryChoice(directory))
    model.seminarMode = true
    model.start()
    let process = ops.processes[0]
    guard let log = ops.requests[0].appendOutputTo else {
        throw HarnessFailure(description: "invalid-category run has no log")
    }
    ops.put(contents, log)
    if let publication {
        let attempt = ops.nowValue.timeIntervalSince1970
        let pid = publication == "foreign" ? process.pid + 1 : process.pid
        let epoch = publication == "stale" ? attempt - 1 : attempt
        putPid(ops, directory: directory, pid: pid, epoch: epoch)
        try ops.putJSON(status(pid: pid, epoch: epoch),
                        active(directory, "onoats.status.json"))
        if refreshAfterPublication { model.refresh() }
        if removePidBeforeExit { removePid(ops, directory: directory, pid: pid) }
    }
    ops.finish(process, status: exit)
    return (model, ops)
}

@MainActor
private func testInvalidCategoryStableTagMatrix(_ root: URL) throws {
    let tag = RecorderModel.invalidCategoryErrorTag
    let (matched, matchedOps) = try invalidCategoryCase(
        root, suite: "onoats.harness.invalid-tag-match", exit: 2,
        contents: "prefix\n\(tag)\n")
    try require(matched.seminarConfigurationFailure,
                "tagged nonzero pre-publication exit was not classified")
    try require(matched.canOpenLogForCurrentSurface,
                "failed app-owned Start did not expose Open Log")
    try require((1...65_536).contains(RecorderModel.categoryErrorTailBytes),
                "log tail bound is missing or unreasonably large")
    try require(matchedOps.tailReadLimits == [RecorderModel.categoryErrorTailBytes],
                "failure did not read exactly one bounded log tail")

    let (zero, _) = try invalidCategoryCase(
        root, suite: "onoats.harness.invalid-tag-zero", exit: 0, contents: tag)
    try require(!zero.seminarConfigurationFailure, "zero exit matched invalid-category tag")

    let (published, _) = try invalidCategoryCase(
        root, suite: "onoats.harness.invalid-tag-published", exit: 2,
        contents: tag, publication: "current")
    try require(!published.seminarConfigurationFailure,
                "a run that published a recorder PID matched the pre-PID failure")

    let (publishedThenExited, _) = try invalidCategoryCase(
        root, suite: "onoats.harness.invalid-tag-publish-exit-race", exit: 2,
        contents: tag, publication: "current", refreshAfterPublication: false,
        removePidBeforeExit: true)
    try require(publishedThenExited.seminarConfigurationFailure,
                "status without a stored command-bearing generation suppressed a pre-PID failure")

    let (stalePublication, _) = try invalidCategoryCase(
        root, suite: "onoats.harness.invalid-tag-stale-publication", exit: 2,
        contents: tag, publication: "stale", refreshAfterPublication: false)
    try require(stalePublication.seminarConfigurationFailure,
                "stale publication suppressed a true pre-PID invalid-category failure")

    let (foreignPublication, _) = try invalidCategoryCase(
        root, suite: "onoats.harness.invalid-tag-foreign-publication", exit: 2,
        contents: tag, publication: "foreign", refreshAfterPublication: false)
    try require(foreignPublication.seminarConfigurationFailure,
                "foreign publication suppressed a true pre-PID invalid-category failure")

    let (missing, _) = try invalidCategoryCase(
        root, suite: "onoats.harness.invalid-tag-missing", exit: 2,
        contents: "--category must be one of: seminars")
    try require(!missing.seminarConfigurationFailure,
                "human prose was used as machine interface")

    let (outsideTail, _) = try invalidCategoryCase(
        root, suite: "onoats.harness.invalid-tag-old", exit: 2,
        contents: tag + "\n" + String(repeating: "x", count: 70_000))
    try require(!outsideTail.seminarConfigurationFailure,
                "tag outside bounded tail was accepted")
}

@MainActor
private func testStartAuxiliaryStateSpace(_ root: URL) throws {
    for count in 0...2 {
        for exitStatus: Int32 in [0, 1] {
            let ops = FakeSystemOps()
            let model = makeModel(ops, suite: "onoats.harness.start-auxiliary",
                                  directory: DirectoryChoice(root))
            try require(model.canStart, "idle model disabled Start")
            model.start()
            let recorder = ops.processes[0]
            putPid(
                ops, directory: root, pid: recorder.pid,
                epoch: ops.nowValue.timeIntervalSince1970)
            model.refresh()
            for _ in 0..<count { model.flush() }
            try require(!model.canStart, "live recorder enabled Start")
            ops.finish(recorder, status: exitStatus)
            try require(model.canStart == (count == 0), "exit ignored auxiliary ownership")
            for index in 0..<count {
                ops.finish(ops.processes[index + 1], status: 0)
                try require(model.canStart == (index == count - 1),
                            "Start availability disagrees with remaining auxiliary count")
            }
            model.start()
            try require(ops.requests.count == count + 2, "enabled Start did not spawn")
        }
    }
    cleanupSuite("onoats.harness.start-auxiliary")
}

@MainActor
private func testExitDiagnosticIdentityStateSpace(_ root: URL) throws {
    for published in [false, true] {
      for matchingPid in [false, true] {
        for offset in [-1.0, 0.0, 1.0] {
            let ops = FakeSystemOps()
            let model = makeModel(ops, suite: "onoats.harness.exit-identity",
                                  directory: DirectoryChoice(root))
            model.start()
            let handle = ops.processes[0]
            let epoch = ops.nowValue.timeIntervalSince1970
            if published {
                putPid(ops, directory: root, pid: handle.pid, epoch: epoch)
                model.refresh()
            }
            var record = status(pid: matchingPid ? handle.pid : handle.pid + 1,
                                epoch: epoch + offset)
            record["exit_reason"] = "owned-diagnostic"
            record["last_error"] = "owned-detail"
            try ops.putJSON(record, active(root, "onoats.status.json"))
            ops.finish(handle, status: 1)
            // Exit diagnostics require the complete stored generation.  A
            // status record alone cannot substitute for the PID-file command
            // fingerprint captured while the owned recorder was alive.
            let accepted = published && matchingPid && offset == 0
            try require(model.state == .failed(
                reason: accepted ? "owned-diagnostic" : "exit code 1",
                detail: accepted ? "owned-detail" : nil),
                "exit diagnostic identity matrix failed")
        }
      }
    }
    cleanupSuite("onoats.harness.exit-identity")
}

@MainActor
private func testPruningQuarantineStateSpace(_ root: URL) throws {
    for replacement in ["unchanged", "regular", "symlink", "missing", "occupied", "recreated"] {
        let ops = NativeSystemOps()
        let directory = root.appendingPathComponent("prune-" + replacement)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let path = directory.appendingPathComponent("log")
        let original = directory.appendingPathComponent("original")
        let target = directory.appendingPathComponent("target")
        try Data("old".utf8).write(to: path)
        try Data("target".utf8).write(to: target)
        ops.beforeLogQuarantine = { url in
            guard replacement != "unchanged" && replacement != "recreated" else { return }
            try! FileManager.default.moveItem(at: url, to: original)
            if replacement == "regular" || replacement == "occupied" {
                try! Data("replacement".utf8).write(to: url)
            } else if replacement == "symlink" {
                try! FileManager.default.createSymbolicLink(at: url, withDestinationURL: target)
            }
        }
        if replacement == "occupied" || replacement == "recreated" {
            ops.afterLogQuarantine = { url in
                try! Data("arrival".utf8).write(to: url)
            }
        }
        let deleted: Bool
        do {
            try ops.deleteRegularFile(at: path)
            deleted = true
        } catch { deleted = false }
        try require(deleted == (replacement == "unchanged" || replacement == "recreated"),
                    "pruning deletion disagreed with descriptor identity")
        if replacement == "regular" || replacement == "occupied" || replacement == "recreated" {
            let contents = try String(contentsOf: path, encoding: .utf8)
            try require(contents == (replacement == "regular" ? "replacement" : "arrival"),
                        "pruning deleted replacement")
        }
        if replacement == "occupied" {
            let candidates = try FileManager.default.contentsOfDirectory(at: directory,
                includingPropertiesForKeys: nil).filter { $0.lastPathComponent.hasPrefix(".onoats-prune-") }
            try require(candidates.count == 1, "raced candidate was lost instead of retained")
            let preserved = try String(contentsOf: candidates[0].appendingPathComponent("candidate"), encoding: .utf8)
            try require(preserved == "replacement", "quarantined replacement was destroyed")
        }
        let targetContents = try String(contentsOf: target, encoding: .utf8)
        try require(targetContents == "target",
                    "pruning touched symlink target")
    }
}

@main
struct RecorderModelHarness {
    @MainActor
    static func main() throws {
        if CommandLine.arguments.count == 4,
           CommandLine.arguments[1] == "--append-writer",
           let delay = TimeInterval(CommandLine.arguments[2]) {
            Thread.sleep(forTimeInterval: delay)
            FileHandle.standardOutput.write(Data(CommandLine.arguments[3].utf8))
            return
        }
        guard CommandLine.arguments.count == 2 else {
            throw HarnessFailure(description: "usage: harness <isolated-home>")
        }
        let home = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        let root = home.appendingPathComponent("data")
        try testStartAuxiliaryStateSpace(root)
        try testExitDiagnosticIdentityStateSpace(root)
        try testPruningQuarantineStateSpace(root)
        try testQuitOwnershipMatrix(root)
        try testDirectoryAuthority(root)
        try testStartPublicationAssociation(root)
        try testExternalRoutingAndPidReuse(root)
        try testPrestartDetailBoundaries(root)
        try testTelemetryNeverControlsLiveness(root)
        try testPanelTelemetryPresentationInputs(root)
        try testOwnedSeminarBadgeSurvivesTelemetryLoss(root)
        try testMissingCliDisablesStart(root)
        try testPerRunLogsPruningAndOwnedRouting(home)
        try testRunLogOwnerOutlivesOverlappingFlush(home)
        try testActiveRunLogSurvivesClockRollback(home)
        try testReservationFailureClearsPriorRunAuthority(home)
        try testNativeBoundedReadStateSpace(home)
        try testNativeCommandLineProbeIsBounded(home)
        try testStatusRecordsUseTheBoundedAuthorityReader(home)
        try testSchemaDriftClearsOnlyWhenEvidenceDisappears(home)
        try testNativeAppendIsAtomicAcrossSeparateWriters(home)
        try testOpenLogResolution(home)
        try testInvalidCategoryStableTagMatrix(root)
        try testRunLogCollisionReservesFreshPath(home)
        try testPollingDiscipline(root)
        try testGenerationCacheRequiresPidCommandAndPositiveEpoch(root)
        try testUnknownIdentityProbeKeepsTheLastProvenVerdict(root)
        try testConfigStoreReadIsBoundedAndNeverOverwritesUnreadable(root)
        try testModelDeallocation(root)
        for suite in [
            "onoats.harness.directory", "onoats.harness.publication-association",
            "onoats.harness.external", "onoats.harness.prestart",
            "onoats.harness.prestart-publication", "onoats.harness.telemetry",
            "onoats.harness.polling", "onoats.harness.deallocation-complete",
            "onoats.harness.generation-cache",
            "onoats.harness.deallocation-failed",
            "onoats.harness.panel-telemetry", "onoats.harness.owned-seminar",
            "onoats.harness.missing-cli", "onoats.harness.logs",
            "onoats.harness.owner-lifetime",
            "onoats.harness.clock-rollback", "onoats.harness.reservation-failure",
            "onoats.harness.schema-drift",
            "onoats.harness.logs-external", "onoats.harness.open-log",
            "onoats.harness.log-collision", "onoats.harness.open-log-relaunch",
            "onoats.harness.invalid-tag-match",
            "onoats.harness.invalid-tag-zero", "onoats.harness.invalid-tag-published",
            "onoats.harness.invalid-tag-publish-exit-race",
            "onoats.harness.invalid-tag-stale-publication",
            "onoats.harness.invalid-tag-foreign-publication",
            "onoats.harness.invalid-tag-missing", "onoats.harness.invalid-tag-old",
        ] { cleanupSuite(suite) }
        print("RecorderModelHarness: ok")
    }
}
