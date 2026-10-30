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
    var alive: Set<Int32> = []
    var commands: [Int32: String] = [:]
    var executable = true
    var nextPid: Int32 = 900
    var requests: [ProcessRequest] = []
    var processes: [FakeProcess] = []
    var exits: [ObjectIdentifier: @MainActor (ProcessHandle) -> Void] = [:]
    var scheduledIntervals: [TimeInterval] = []
    var failSpawn = false
    var readTextCount = 0
    var processAliveCount = 0
    var commandLineCount = 0
    var telemetryReadCount = 0

    func spawn(
        _ request: ProcessRequest,
        onExit: @escaping @MainActor (ProcessHandle) -> Void
    ) throws -> ProcessHandle {
        if failSpawn { throw FakeSpawnError() }
        requests.append(request)
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
    func commandLine(_ pid: Int32) -> String? {
        commandLineCount += 1
        return commands[pid]
    }
    func readData(at url: URL) -> Data? {
        if url.lastPathComponent == UiTelemetryContract.fileName { telemetryReadCount += 1 }
        return files[url.path]
    }
    func readText(at url: URL) -> String? {
        readTextCount += 1
        return files[url.path].flatMap { String(data: $0, encoding: .utf8) }
    }
    func write(_ data: Data, to url: URL) throws { files[url.path] = data }
    func delete(at url: URL) throws { files[url.path] = nil }
    func fileExists(at url: URL) -> Bool { files[url.path] != nil }
    func isExecutable(at url: URL) -> Bool { executable }
    func now() -> Date { nowValue }
    func schedule(
        every interval: TimeInterval, _ action: @escaping @MainActor () -> Void
    ) -> PollHandle {
        scheduledIntervals.append(interval)
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
        "schema": 1,
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

    model.flush()
    try require(ops.requests.last?.arguments == ["flush", "--data-dir", old.path],
                "Flush did not use the active directory")

    let owned = ops.processes[0]
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

    let pidReads = ops.readTextCount
    let livenessReads = ops.processAliveCount
    let commandReads = ops.commandLineCount
    model.refreshTelemetry()
    try require(ops.readTextCount == pidReads,
                "telemetry refresh reread the PID file instead of cached identity")
    try require(ops.processAliveCount == livenessReads && ops.commandLineCount == commandReads,
                "telemetry refresh reprobed process identity/liveness")
    try require(model.telemetryAvailable, "cached authoritative identity rejected telemetry")
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

    choice.url = next
    model.refresh()
    model.stopExternal()
    try require(ops.requests.last?.arguments == ["stop", "--data-dir", directory.path],
                "external Stop used next-session directory")
    try require(model.stopRequested, "external Stop did not guard duplicate requests")

    putPid(ops, directory: directory, pid: 77, epoch: 11)
    model.refresh()
    try require(!model.stopRequested, "same-PID new epoch inherited stopping guard")
    try require(model.state == .running(ours: false), "PID reuse changed liveness")

    model.flush()
    try require(ops.requests.last?.arguments == ["flush", "--data-dir", directory.path],
                "external Flush used next-session directory")
}

@MainActor
private func testPrestartDetailBoundaries(_ root: URL) throws {
    let directory = root.appendingPathComponent("prestart")
    let choice = DirectoryChoice(directory)
    let ops = FakeSystemOps()
    let model = makeModel(ops, suite: "onoats.harness.prestart", directory: choice)
    model.start()
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

    ops.files[active(directory, "onoats.ui.json").path] = nil
    model.refreshTelemetry()
    try require(model.state == .running(ours: false), "missing telemetry changed liveness")
    try require(!model.telemetryAvailable, "missing telemetry did not clear display state")
    try require(model.meActivity == nil && model.aggregateStt == nil,
                "neutral telemetry state was not restored")
    try require(model.categoryUnavailable, "external category did not degrade neutrally")
}

@main
struct RecorderModelHarness {
    @MainActor
    static func main() throws {
        guard CommandLine.arguments.count == 2 else {
            throw HarnessFailure(description: "usage: harness <isolated-home>")
        }
        let home = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true)
        let root = home.appendingPathComponent("data")
        try testDirectoryAuthority(root)
        try testStartPublicationAssociation(root)
        try testExternalRoutingAndPidReuse(root)
        try testPrestartDetailBoundaries(root)
        try testTelemetryNeverControlsLiveness(root)
        try testPollingDiscipline(root)
        try testModelDeallocation(root)
        for suite in [
            "onoats.harness.directory", "onoats.harness.publication-association",
            "onoats.harness.external", "onoats.harness.prestart",
            "onoats.harness.prestart-publication", "onoats.harness.telemetry",
            "onoats.harness.polling", "onoats.harness.deallocation-complete",
            "onoats.harness.deallocation-failed",
        ] { cleanupSuite(suite) }
        print("RecorderModelHarness: ok")
    }
}
