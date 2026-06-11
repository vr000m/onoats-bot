// Recorder state for the menu bar: poll the Phase-5a status file (read-only
// consumer, schema-guarded) + the pid-file liveness backstop, and own the
// Start/Stop/Flush process control.
//
// Lifecycle invariant (dev plan Phase 5b): Stop signals the `onoats bot`
// SUPERVISOR process (SIGTERM → graceful drain, runtime.py installs the
// handler), NEVER the capturer — the supervisor owns the capturer lifecycle;
// killing the capturer directly would surface as a fatal capturer-crash.
//
// Liveness mirrors status.resolve_liveness(): the pid file is authoritative
// for the live/stopped VERDICT, the status file supplies the DETAIL (source,
// STT label, start time, why a start failed). A stale status file must never
// render a dead recorder as running.
import AppKit
import CoreAudio
import Foundation
import SwiftUI

/// Mirror of `src/onoats/status.py` `StatusRecord` (STATUS_SCHEMA_VERSION=1).
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
}

enum RecorderState: Equatable {
    case stopped
    case starting        // we spawned `onoats bot`; recorder pid not yet alive
    case running(ours: Bool)
    case stopping        // SIGTERM sent; draining
    case failed(reason: String, detail: String?)
}

@MainActor
final class RecorderModel: ObservableObject {
    static let statusSchemaVersion = 1

    @Published var state: RecorderState = .stopped
    @Published var micDevice = "—"
    @Published var outputDevice = "—"
    @Published var inputDevices: [AudioInputDevice] = []
    @Published var defaultInputID = AudioObjectID(kAudioObjectUnknown)
    @Published var sttLabel: String?
    @Published var audioSource: String?
    @Published var startTime: Date?
    @Published var schemaDrift = false
    /// Configured STT service from config.toml (next-start value, distinct
    /// from `sttLabel`, which is what the *running* session reports).
    @Published var sttService = "whisper"
    @Published var dataDirDisplay = ""

    /// Valid `[stt].service` values — mirror of runtime.py
    /// `VALID_STT_SERVICES` (parity-checked by
    /// tests/test_native_contract_parity.py).
    static let sttServices = ["whisper", "websocket", "deepgram"]

    private var proc: Process?
    private var userRequestedStop = false
    private var timer: Timer?

    // ------------------------------------------------------------------ paths

    /// CLI shim installed by `make install-cli` (uv tool install --editable).
    /// Override: `defaults write net.varunsingh.onoats cliPath /abs/path`.
    var cliPath: String {
        if let override = UserDefaults.standard.string(forKey: "cliPath"), !override.isEmpty {
            return (override as NSString).expandingTildeInPath
        }
        return NSHomeDirectory() + "/.local/bin/onoats"
    }

    var cliAvailable: Bool { FileManager.default.isExecutableFile(atPath: cliPath) }

    var capturerPath: String {
        Bundle.main.bundlePath + "/Contents/MacOS/onoats-capturer"
    }

    /// Mirrors the Python resolution (store.py onoats_data_dir) as seen from
    /// a LaunchServices app. Python's full chain is ONOATS_DATA_DIR env >
    /// legacy env var > XDG_DATA_HOME/onoats > ~/.local/share/onoats; a GUI
    /// app inherits no shell env, so the three env steps are intentionally
    /// unreachable here and only config.toml `[storage].data_dir` >
    /// `~/.local/share/onoats` remain. The XDG-default literal is
    /// parity-checked against store.py by tests/test_native_contract_parity.py.
    static func resolveDataDir() -> URL {
        if let value = ConfigStore.readValue(section: "storage", key: "data_dir") {
            return URL(fileURLWithPath: (value as NSString).expandingTildeInPath)
        }
        return URL(fileURLWithPath: NSHomeDirectory() + "/.local/share/onoats")
    }

    var dataDir: URL { Self.resolveDataDir() }

    private var logURL: URL {
        URL(fileURLWithPath: NSHomeDirectory() + "/Library/Logs/Onoats/onoats-bot.log")
    }

    // ------------------------------------------------------------------ init

    init() {
        refresh()
        // .common so the poll keeps firing during menu tracking.
        let t = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    // ------------------------------------------------------------------ reads

    private func readStatus() -> StatusRecord? {
        let path = dataDir.appendingPathComponent(".active/onoats.status.json")
        guard let data = try? Data(contentsOf: path) else { return nil }
        // Schema guard first (the whole point of the schema int): a drifted
        // file must surface as drift, never be silently mis-rendered.
        if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let schema = obj["schema"] as? Int, schema != Self.statusSchemaVersion {
            schemaDrift = true
            return nil
        }
        schemaDrift = false
        return try? JSONDecoder().decode(StatusRecord.self, from: data)
    }

    /// Pid-file read, mirroring `_vendor/pid.py`: line 1 pid, line 2 must be
    /// the "onoats-bot" identity marker, else the file is ignored.
    private func readPid() -> Int32? {
        let path = dataDir.appendingPathComponent(".active/onoats.pid")
        guard let text = try? String(contentsOf: path, encoding: .utf8) else { return nil }
        let lines = text.trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
        guard lines.count >= 2, lines[1] == "onoats-bot", let pid = Int32(lines[0])
        else { return nil }
        return pid
    }

    private func processAlive(_ pid: Int32) -> Bool {
        kill(pid, 0) == 0 || errno == EPERM
    }

    // ---------------------------------------------------------------- refresh

    func refresh() {
        micDevice = AudioDevices.defaultInputName()
        outputDevice = AudioDevices.defaultOutputName()
        inputDevices = AudioDevices.inputDevices()
        defaultInputID = AudioDevices.defaultInputID()
        sttService = ConfigStore.readValue(section: "stt", key: "service") ?? "whisper"
        dataDirDisplay = dataDir.path.replacingOccurrences(
            of: NSHomeDirectory(), with: "~")

        let status = readStatus()
        let pid = readPid()
        let alive = pid.map(processAlive) ?? false

        if let s = status {
            sttLabel = s.stt_label.isEmpty ? nil : s.stt_label
            audioSource = s.audio_source.isEmpty ? nil : s.audio_source
            startTime = alive ? Date(timeIntervalSince1970: s.start_time) : nil
        } else {
            sttLabel = nil
            audioSource = nil
            startTime = nil
        }

        if let p = proc, p.isRunning {
            if case .stopping = state { return }  // SIGTERM sent; draining
            state = alive ? .running(ours: true) : .starting
        } else if alive {
            state = .running(ours: false)
        } else if case .failed = state {
            // Sticky until the next Start so the user actually sees it.
        } else {
            state = .stopped
        }
    }

    // ---------------------------------------------------------------- actions

    func start() {
        guard proc == nil || proc?.isRunning == false else { return }
        userRequestedStop = false

        let p = Process()
        p.executableURL = URL(fileURLWithPath: cliPath)
        p.arguments = ["bot"]
        var env = ProcessInfo.processInfo.environment
        env["AUDIO_SOURCE"] = "socket"
        env["ONOATS_CAPTURER_BIN"] = capturerPath
        p.environment = env

        if let log = openLog() {
            p.standardOutput = log
            p.standardError = log
        }
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in self?.handleExit(proc) }
        }
        do {
            try p.run()
            proc = p
            state = .starting
        } catch {
            state = .failed(reason: "spawn-failed", detail: error.localizedDescription)
        }
    }

    /// SIGTERM the supervisor we spawned (graceful drain). External sessions
    /// (started from a terminal) are displayed but not signalled — the safe
    /// identity-checked signalling lives in the Python CLI, not here.
    func stop() {
        guard let p = proc, p.isRunning else { return }
        userRequestedStop = true
        state = .stopping
        p.terminate()  // SIGTERM → runtime's graceful-shutdown handler
    }

    func flush() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cliPath)
        p.arguments = ["flush"]
        if let log = openLog() {
            p.standardOutput = log
            p.standardError = log
        }
        try? p.run()
    }

    // --------------------------------------------------------------- settings
    // All settings write config.toml — the same file the CLI reads — so GUI
    // and terminal sessions share one source of truth. A change while a
    // session is recording applies on the NEXT start (config loads at start).

    func setSTTService(_ service: String) {
        try? ConfigStore.writeValue(section: "stt", key: "service", value: service)
        refresh()
    }

    func chooseDataDir() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.directoryURL = dataDir
        panel.message = "Choose the onoats data directory (config.toml [storage].data_dir)"
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try ConfigStore.writeValue(
                section: "storage", key: "data_dir", value: url.path)
        } catch {
            // Rejected write (control character in the path) — say so rather
            // than silently keeping the old dir.
            let alert = NSAlert()
            alert.messageText = "Could not save data directory"
            alert.informativeText = "\(error)"
            alert.runModal()
        }
        refresh()
    }

    func openConfig() {
        NSWorkspace.shared.open(ConfigStore.configURL)
    }

    /// Sets the macOS DEFAULT input device (system-wide) — the only knob that
    /// actually applies on the socket path, since the capturer binds the
    /// system default at start. A running session keeps the device it bound;
    /// the change takes effect on the next Start.
    func setMicDevice(_ device: AudioInputDevice) {
        AudioDevices.setDefaultInput(device.id)
        refresh()
    }

    private func handleExit(_ p: Process) {
        proc = nil
        if userRequestedStop || p.terminationStatus == 0 {
            state = .stopped
        } else {
            // Fail-loud surface: the supervisor stamps exit_reason/last_error
            // into the status file on the way down — show *why*, not just rc.
            let status = readStatus()
            let reason = status?.exit_reason ?? "exit code \(p.terminationStatus)"
            state = .failed(reason: reason, detail: status?.last_error)
        }
        userRequestedStop = false
    }

    private func openLog() -> FileHandle? {
        let fm = FileManager.default
        try? fm.createDirectory(
            at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !fm.fileExists(atPath: logURL.path) {
            fm.createFile(atPath: logURL.path, contents: nil)
        }
        guard let handle = try? FileHandle(forWritingTo: logURL) else { return nil }
        handle.seekToEndOfFile()
        return handle
    }
}
