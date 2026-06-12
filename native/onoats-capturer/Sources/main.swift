// onoats-capturer — production macOS capturer for AUDIO_SOURCE=socket.
//
// Wire contract: docs/audio-socket-contract.md (v1). Launch contract: spawned
// by the onoats supervisor with --mic-socket/--system-socket/--nonce (also in
// env as ONOATS_MIC_SOCKET / ONOATS_SYSTEM_SOCKET / ONOATS_CAPTURER_NONCE).
//
// Startup sequence (the order is load-bearing; reordered by release-plan
// Phase 7 — tap preflight BEFORE the sockets):
//   1. mic TCC grant (fail loud before any socket exists)
//   2. system tap preflight: emit `ONOATS-EVENT waiting-for-permission`, then
//      start the FULL system capture chain (tap → aggregate → IOProc). The
//      tap creation is the system-audio TCC-prompting call, and a pending
//      prompt BLOCKS it — doing this before any socket exists means the
//      supervisor (which extends its socket wait on the event) absorbs the
//      block, instead of the recorder's 10 s read-idle watchdog killing the
//      session while the user reads the prompt. The full chain (not just the
//      tap) starts here so the tap-created→IOProc-started window stays
//      ~200 ms (that window is the audible output dropout — SystemCapture
//      header); frames emitted before the system writer attaches are
//      pre-session audio and are dropped by LateBoundWriter by design.
//   3. create BOTH listening sockets (what _wait_for_sockets watches for)
//   4. accept BOTH connections within one deadline (startup barrier)
//   5. write each handshake AS ITS CONNECTION ARRIVES (echoing the nonce) —
//      the recorder's handshake read is bounded tighter than the barrier
//   6. attach the system writer + start the mic capture: mic → mic socket,
//      system tap → system socket — exactly one source per socket, never
//      mixed (keystone). The mic engine still starts strictly AFTER the tap
//      (Milestone B: tap creation while the engine runs was flaky), which the
//      preflight makes structural.
//
// Error ordering after the reorder (restated for the supervisor's mapping):
// rc=10 (mic denied) and rc=11 (tap API failure, AFTER the ×3@500 ms retry)
// now both fire BEFORE any socket exists; rc=12 (socket/barrier failure) can
// only fire after a healthy tap. rc=11 is NEVER produced by a TCC denial —
// a denied tap succeeds and delivers zeros (verified 2026-06-11); denial's
// sole observable is the zero-run WARNING.
//
// Any terminal event — SIGTERM/SIGINT, either socket closing, a write error,
// a capture failure — tears down BOTH branches and the full Core Audio chain
// (AudioDeviceStop → IOProc → aggregate → tap) before exit.

import AVFoundation
import Foundation

// MARK: - teardown coordinator (single exit path)

// Scope note: Teardown is a process-lifetime singleton registered by the
// module-level startup code below, not a reusable abstraction. The selftest
// modes (--selftest-tap / --selftest-concurrent) intentionally do NOT register
// with it — they are short-lived debug harnesses that rely on OS cleanup
// (process exit destroys taps/aggregates), so a selftest crash skipping this
// chain is by design.
final class Teardown {
    static let shared = Teardown()
    private let lock = NSLock()
    private var triggered = false

    // All resources are private and registered through the methods below, so
    // partial/bypassed registration is structurally impossible at call sites.
    private var mic: MicCapture?
    private var system: SystemCapture?
    private var writers: [FrameWriter] = []
    private var connectionFds: [Int32] = []
    private var listenFds: [Int32] = []
    private var socketPaths: [String] = []

    /// First caller wins; runs the full teardown and exits the process.
    /// Subsequent callers (e.g. the second writer noticing its peer vanished
    /// while the first is tearing down) return and let the winner finish.
    func trigger(rc: Int32, reason: String) {
        lock.lock()
        if triggered {
            lock.unlock()
            return
        }
        triggered = true
        lock.unlock()

        logLine("shutting down (\(reason))")
        // Stop producers first so nothing enqueues mid-teardown, then drop the
        // Core Audio chain (the lifecycle invariant lives in SystemCapture.stop),
        // then the sockets.
        mic?.stop()
        system?.stop()
        for writer in writers { writer.shutdown() }
        for fd in connectionFds { close(fd) }
        for fd in listenFds { close(fd) }
        for path in socketPaths { unlink(path) }
        exit(rc)
    }

    /// Register the system capture the moment it exists — BEFORE its start()
    /// is attempted. The tap preflight (startup step 2) runs the system chain
    /// long before the writers exist, so every later failure path (socket
    /// creation, accept barrier, mic start) must already find it here and tear
    /// the Core Audio chain down.
    func registerSystem(_ system: SystemCapture) {
        lock.lock()
        self.system = system
        lock.unlock()
    }

    /// Registers the mic capture + writers and only then starts the writer
    /// threads. The ordering contract ("a writer hitting a terminal error must
    /// find a fully-populated Teardown") is self-enforcing here: a writer's
    /// onTerminal cannot fire before its start(), and start() is sequenced
    /// after every field assignment by this method's body — callers cannot get
    /// it wrong. (The system capture is registered earlier, by the tap
    /// preflight, via registerSystem.)
    func registerAndStart(mic: MicCapture, writers: [FrameWriter]) {
        lock.lock()
        self.mic = mic
        self.writers = writers
        lock.unlock()
        for writer in writers { writer.start() }
    }

    /// Register a listening socket the moment it exists (fd + its filesystem
    /// path together, so neither can be torn down without the other).
    func registerListener(fd: Int32, path: String) {
        lock.lock()
        listenFds.append(fd)
        socketPaths.append(path)
        lock.unlock()
    }

    /// Register the accepted per-branch connections (post startup barrier).
    func registerConnections(_ fds: [Int32]) {
        lock.lock()
        connectionFds = fds
        lock.unlock()
    }
}

func fail(_ rc: Int32, _ message: String) -> Never {
    logLine("ERROR: \(message)")
    Teardown.shared.trigger(rc: rc, reason: message)
    exit(rc)  // unreachable (trigger exits); satisfies Never
}

// MARK: - arguments

let cliArgs = CommandLine.arguments
func argValue(_ name: String) -> String? {
    guard let i = cliArgs.firstIndex(of: name), i + 1 < cliArgs.count else { return nil }
    return cliArgs[i + 1]
}

let environment = ProcessInfo.processInfo.environment
let micSocketPath = argValue("--mic-socket") ?? environment["ONOATS_MIC_SOCKET"]
let systemSocketPath = argValue("--system-socket") ?? environment["ONOATS_SYSTEM_SOCKET"]
let nonce = argValue("--nonce") ?? environment["ONOATS_CAPTURER_NONCE"]
let acceptTimeoutSeconds = Double(argValue("--accept-timeout-s") ?? "") ?? 30.0

// SIGPIPE must never kill the process — a peer close surfaces as EPIPE from
// write() and is handled as a normal terminal condition.
signal(SIGPIPE, SIG_IGN)

// App Nap defense (found the hard way): a windowless background process whose
// main thread idles in dispatchMain() gets napped by macOS a few seconds in —
// the WHOLE process (IOProc delivery, worker threads, even dispatch timers)
// freezes and the recorder's read-idle watchdog kills the session. A
// latency-critical activity assertion opts the process out for its lifetime.
let appNapActivity = ProcessInfo.processInfo.beginActivity(
    options: [.userInitiated, .latencyCritical],
    reason: "realtime audio capture for onoats")
_ = appNapActivity  // held until exit; never endActivity while capturing

// Maintenance subcommands (no sockets, no TCC): residue enumeration/sweep for
// residue_check.sh — see Maintenance.swift for the stdout verdict contract.
if cliArgs.count > 1 {
    switch cliArgs[1] {
    case "list-aggregates": listAggregates(); exit(0)
    case "list-taps": listTaps(); exit(0)
    case "clean-taps": cleanTaps(); exit(0)
    default: break
    }
}

// Hidden debug mode: run ONLY the system tap chain for N seconds (no sockets,
// no mic) and report emitted frames — bisection harness for IOProc stalls.
if cliArgs.contains("--selftest-tap") {
    let seconds = Double(argValue("--seconds") ?? "") ?? 6.0
    var frames = 0
    var peak: Float = 0
    let selftest = SystemCapture { pcm, _ in
        frames += 1
        pcm.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
            for v in raw.bindMemory(to: Int16.self) {
                peak = max(peak, abs(Float(v)) / 32768.0)
            }
        }
    }
    do { try selftest.start() } catch { logLine("selftest: \(error)"); exit(1) }
    Thread.sleep(forTimeInterval: seconds)
    selftest.stop()
    logLine("selftest-tap: emitted \(frames) frames in \(seconds)s, peak=\(peak)")
    exit(frames > Int(seconds * 40) ? 0 : 1)  // expect ~50/s; allow slack
}

// Hidden debug mode: raw AVAudioEngine mic probe — no resampler, no chunker,
// no silence pacer. Reports the actual default input device, raw tap-callback
// count, and peak, so "engine bound to the wrong/dead device" is unambiguous.
if cliArgs.contains("--selftest-mic") {
    let seconds = Double(argValue("--seconds") ?? "") ?? 8.0
    logLine("default input device: \(defaultInputDeviceDescription())")
    guard requestMicGrantBlocking() else {
        logLine("mic permission DENIED")
        exit(1)
    }
    let engine = AVAudioEngine()
    let input = engine.inputNode
    let hwFormat = input.inputFormat(forBus: 0)
    let outFormat = input.outputFormat(forBus: 0)
    logLine(
        "inputNode hw=\(hwFormat.sampleRate) Hz/\(hwFormat.channelCount) ch  "
            + "out=\(outFormat.sampleRate) Hz/\(outFormat.channelCount) ch")
    let format = hwFormat
    var callbacks = 0
    var frames = 0
    var peak: Float = 0
    let probeLock = NSLock()
    input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
        probeLock.lock()
        callbacks += 1
        frames += Int(buffer.frameLength)
        if let ch = buffer.floatChannelData?[0] {
            for i in 0..<Int(buffer.frameLength) { peak = max(peak, abs(ch[i])) }
        }
        probeLock.unlock()
    }
    do { try engine.start() } catch {
        logLine("engine.start failed: \(error)")
        exit(1)
    }
    logLine("engine running=\(engine.isRunning) — SPEAK for \(seconds)s…")
    Thread.sleep(forTimeInterval: seconds)
    engine.stop()
    probeLock.lock()
    logLine(
        "selftest-mic (AVAudioEngine): callbacks=\(callbacks) frames=\(frames) "
            + "peak=\(String(format: "%.4f", peak))")
    probeLock.unlock()

    // Second probe: direct HAL IOProc on the default input device (the same
    // primitive PortAudio uses, and that our system branch uses).
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var deviceID = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    guard
        AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &deviceID) == noErr
    else { exit(1) }
    var halCallbacks = 0
    var halFrames = 0
    var halPeak: Float = 0
    var procID: AudioDeviceIOProcID?
    let err = AudioDeviceCreateIOProcIDWithBlock(&procID, deviceID, nil) {
        _, inInputData, _, _, _ in
        let abl = inInputData.pointee
        probeLock.lock()
        halCallbacks += 1
        let buf = abl.mBuffers
        if let data = buf.mData, buf.mDataByteSize > 0 {
            let n = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
            let p = data.bindMemory(to: Float.self, capacity: n)
            for i in 0..<n { halPeak = max(halPeak, abs(p[i])) }
            halFrames += n
        }
        probeLock.unlock()
    }
    guard err == noErr, let proc = procID else {
        logLine("HAL probe: create IOProc failed \(fourCC(err))")
        exit(1)
    }
    let startErr = AudioDeviceStart(deviceID, proc)
    logLine("HAL probe on device \(deviceID): start=\(fourCC(startErr)) — SPEAK for \(seconds)s…")
    Thread.sleep(forTimeInterval: seconds)
    AudioDeviceStop(deviceID, proc)
    AudioDeviceDestroyIOProcID(deviceID, proc)
    probeLock.lock()
    logLine(
        "selftest-mic (HAL IOProc): callbacks=\(halCallbacks) frames=\(halFrames) "
            + "peak=\(String(format: "%.4f", halPeak))")
    probeLock.unlock()
    exit((callbacks > 0 || halCallbacks > 0) ? 0 : 1)
}

// Hidden debug mode: system tap + mic engine together, no sockets.
if cliArgs.contains("--selftest-concurrent") {
    let seconds = Double(argValue("--seconds") ?? "") ?? 6.0
    var sysFrames = 0
    var micFrames = 0
    let sys = SystemCapture { _, _ in sysFrames += 1 }
    let mic = MicCapture { _, _ in micFrames += 1 }
    do {
        try sys.start()
        try mic.start()
    } catch {
        logLine("selftest: \(error)")
        exit(1)
    }
    Thread.sleep(forTimeInterval: seconds)
    mic.stop()
    sys.stop()
    logLine("selftest-concurrent: system=\(sysFrames) mic=\(micFrames) frames in \(seconds)s")
    exit(sysFrames > Int(seconds * 40) ? 0 : 1)
}

guard let micSocketPath, let systemSocketPath else {
    logLine(
        "usage: onoats-capturer --mic-socket <path> --system-socket <path> "
            + "[--nonce <hex>] [--accept-timeout-s N]")
    exit(ExitCode.usage)
}

// MARK: - 1. mic grant (system-audio TCC surfaces at tap creation, step 2)

if !requestMicGrantBlocking() {
    logLine(
        "ERROR: microphone permission denied — the 'me' branch cannot capture. "
            + "Grant it in System Settings ▸ Privacy & Security ▸ Microphone.")
    exit(ExitCode.micDenied)
}

// MARK: - 2. system tap preflight (TCC-prompting call BEFORE any socket exists)

/// Late-binding handoff from the already-running system capture to its
/// FrameWriter. The tap preflight starts the system chain before the sockets
/// (and therefore the writers) exist; frames emitted before attach() are
/// pre-session audio and are dropped here by design — the session still
/// starts at the accept barrier, exactly as before the Phase 7 reorder.
final class LateBoundWriter {
    private let lock = NSLock()
    private var writer: FrameWriter?
    func attach(_ w: FrameWriter) {
        lock.lock()
        writer = w
        lock.unlock()
    }
    func enqueue(pcm: Data, capturedMonotonicNs ns: UInt64) {
        lock.lock()
        let w = writer
        lock.unlock()
        w?.enqueue(pcm: pcm, capturedMonotonicNs: ns)
    }
}

// Emitted UNCONDITIONALLY before the tap call: there is no TCC preflight API,
// so the capturer cannot know whether the next call will block on a prompt.
// The supervisor extends its socket wait only if the base wait actually
// expires, so the granted/fast path costs nothing.
emitEvent(
    "waiting-for-permission",
    "branch=system hint=creating the system-audio tap — a pending Screen & "
        + "System Audio Recording prompt blocks here until answered")

let systemWriterBox = LateBoundWriter()
let systemCapture = SystemCapture { pcm, ns in
    systemWriterBox.enqueue(pcm: pcm, capturedMonotonicNs: ns)
}
// Register BEFORE start(): every failure path from here on (tap failure,
// socket creation, accept barrier, mic start) must find the system chain in
// Teardown so the tap/aggregate are destroyed on the way out.
Teardown.shared.registerSystem(systemCapture)
do {
    try systemCapture.start()
} catch {
    fail(ExitCode.systemAudioFailed, "system capture: \(error)")
}

// MARK: - 3. listening sockets (the supervisor waits on these)

let micListenFd: Int32
let systemListenFd: Int32
do {
    micListenFd = try makeListeningSocket(path: micSocketPath)
    Teardown.shared.registerListener(fd: micListenFd, path: micSocketPath)
    systemListenFd = try makeListeningSocket(path: systemSocketPath)
    Teardown.shared.registerListener(fd: systemListenFd, path: systemSocketPath)
} catch {
    fail(ExitCode.socketFailed, "creating sockets: \(error)")
}

// MARK: - 4+5. startup barrier: accept both, handshake each as it arrives

let micFd: Int32
let systemFd: Int32
do {
    // Handshake per-accept (not after the barrier): the recorder's handshake
    // read is bounded at 10 s while this barrier allows 30 s — a staggered
    // second connection must not time out the first branch's read. Streaming
    // still starts only after BOTH branches are connected (acceptBoth returns).
    let conns = try acceptBoth(
        listenFds: [("mic", micListenFd), ("system", systemListenFd)],
        deadlineSeconds: acceptTimeoutSeconds,
        onAccept: { fd in try writeHandshake(fd: fd, nonce: nonce) })
    micFd = conns[0]
    systemFd = conns[1]
    Teardown.shared.registerConnections(conns)
    logLine("both branches connected; handshakes written (v\(WIRE_VERSION))")
} catch {
    fail(ExitCode.socketFailed, "startup barrier: \(error)")
}

// MARK: - 6. writers + mic → exactly one source per socket (keystone: never mix)

let micWriter = FrameWriter(label: "mic", fd: micFd) { rc, reason in
    Teardown.shared.trigger(rc: rc, reason: reason)
}
let systemWriter = FrameWriter(label: "system", fd: systemFd) { rc, reason in
    Teardown.shared.trigger(rc: rc, reason: reason)
}
let micCapture = MicCapture { pcm, ns in
    micWriter.enqueue(pcm: pcm, capturedMonotonicNs: ns)
}
// Register the mic capture + writers with Teardown, then start the writer
// threads — a writer hitting a terminal error must find a fully-populated
// Teardown, never a half-registered one (stop() on a never-started capture is
// a safe no-op; the system capture was registered by the preflight above).
// registerAndStart makes that ordering structural rather than comment-enforced.
Teardown.shared.registerAndStart(mic: micCapture, writers: [micWriter, systemWriter])

// Attach the system writer only after its thread is started: system frames
// begin streaming here — the same point they did pre-reorder.
systemWriterBox.attach(systemWriter)

// Mic engine strictly AFTER the tap (started in the preflight) — the
// spike-proven order. Creating the tap while an AVAudioEngine is already
// running was intermittently flaky (AudioHardwareCreateProcessTap returning
// noErr + kAudioObjectUnknown); the preflight makes the order structural.
do {
    try micCapture.start()
} catch {
    fail(ExitCode.captureFailed, "mic capture: \(error)")
}

// MARK: - signals → graceful teardown

// The supervisor stops the capturer with SIGTERM (→ bounded wait → SIGKILL)
// after the recorder finishes. Default disposition must be ignored so the
// dispatch source gets to run the teardown chain.
signal(SIGTERM, SIG_IGN)
signal(SIGINT, SIG_IGN)
let signalQueue = DispatchQueue(label: "onoats.signals")
let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: signalQueue)
sigtermSource.setEventHandler { Teardown.shared.trigger(rc: ExitCode.ok, reason: "SIGTERM") }
sigtermSource.resume()
let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: signalQueue)
sigintSource.setEventHandler { Teardown.shared.trigger(rc: ExitCode.ok, reason: "SIGINT") }
sigintSource.resume()

logLine("streaming (mic → \(micSocketPath), system → \(systemSocketPath))")

// Park the main thread in a sleep loop, exactly like the spike that streamed
// indefinitely. Both dispatchMain() and CFRunLoopRun() were observed to let
// the process be coalesced/napped a few seconds in (every thread — IOProc
// delivery, workers, even dispatch timers — stops until an external kernel
// event arrives). A periodically-waking main thread keeps the process out of
// that idle classification. Signal sources run on their own queue above.
while true { Thread.sleep(forTimeInterval: 1.0) }
