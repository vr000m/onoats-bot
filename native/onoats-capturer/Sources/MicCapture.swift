// Mic ("me") branch: direct HAL IOProc on the default input device →
// resample → 20 ms frames.
//
// Deliberately NOT AVAudioEngine: on this machine AVAudioEngine's inputNode
// reports running=true yet delivers ZERO tap callbacks from a Focusrite
// Scarlett Solo (verified with --selftest-mic: engine callbacks=0 while a raw
// HAL IOProc on the same device streams fine). PortAudio — the Milestone A
// path that works daily — also uses raw HAL. So the mic branch uses the same
// copy-only IOProc + worker-thread pattern as SystemCapture.
//
// Device-change survival (contract MUST): a listener on
// kAudioHardwarePropertyDefaultInputDevice rebinds the IOProc to the new
// default device (AirPods disconnect etc.) and keeps streaming to the same
// socket. The chunker's silence pacer covers the rebind gap so the timeline
// stays continuous.

import AVFoundation
import CoreAudio
import Foundation

func requestMicGrantBlocking() -> Bool {
    let pre = AVCaptureDevice.authorizationStatus(for: .audio)
    if pre == .authorized { return true }
    if pre == .denied || pre == .restricted { return false }
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    AVCaptureDevice.requestAccess(for: .audio) { ok in
        granted = ok
        sem.signal()
    }
    sem.wait()
    return granted
}

func defaultInputDeviceID() -> AudioObjectID {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var deviceID = AudioObjectID(kAudioObjectUnknown)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let err = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &deviceID)
    return err == noErr ? deviceID : AudioObjectID(kAudioObjectUnknown)
}

/// id + name + UID of the system default INPUT device, or nil when none.
func defaultInputDeviceIdentity() -> (id: AudioObjectID, name: String, uid: String)? {
    let deviceID = defaultInputDeviceID()
    guard deviceID != kAudioObjectUnknown else { return nil }

    func stringProp(_ selector: AudioObjectPropertySelector) -> String {
        var propAddr = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var cfStr: CFString?
        var strSize = UInt32(MemoryLayout<CFString?>.size)
        let err = withUnsafeMutablePointer(to: &cfStr) {
            AudioObjectGetPropertyData(deviceID, &propAddr, 0, nil, &strSize, $0)
        }
        guard err == noErr, let s = cfStr else { return "?" }
        return s as String
    }
    return (deviceID, stringProp(kAudioObjectPropertyName), stringProp(kAudioDevicePropertyDeviceUID))
}

/// Name + UID of the system default INPUT device — logged at mic start so a
/// wrong default (e.g. a leftover virtual loopback device) is diagnosable at
/// a glance.
func defaultInputDeviceDescription() -> String {
    guard let dev = defaultInputDeviceIdentity() else { return "<no default input device>" }
    return "\"\(dev.name)\" (uid=\(dev.uid), id=\(dev.id))"
}

/// Status-file field shape for the device event: `<name> (uid=<uid>)` —
/// the flat string the supervisor stores verbatim in `mic_device`
/// (status schema v2; release-plan Phase 5).
func defaultInputDeviceFieldDescription() -> String {
    guard let dev = defaultInputDeviceIdentity() else { return "<no default input device>" }
    return "\(dev.name) (uid=\(dev.uid))"
}

/// Times the steps of `MicCapture.bind()` so a HAL stall is attributable.
///
/// `bind()` can block for 40+ s inside a CoreAudio call (observed 2026-09-25:
/// default input = built-in mic, no `capturing from` line until the default
/// input changed), and the capturer's log carries no timestamps, so the stall
/// was visible only as a long run of `pacing silence`. This logs (a) any step
/// slower than `slowStepSec` when it finishes, and (b) a WARNING naming the
/// step still in flight once `stallWarnSec` passes, which is the only signal
/// when a call never returns. Diagnostic only: it never changes bind behaviour.
private final class BindWatch {
    static let slowStepSec = 1.0
    static let stallWarnSec = 5.0

    private let lock = NSLock()
    private let begin = MonotonicClock.nowNanos()
    private var stepStart = MonotonicClock.nowNanos()
    private var current = "start"
    private var finished = false

    init() {
        DispatchQueue.global().asyncAfter(deadline: .now() + Self.stallWarnSec) { [self] in
            lock.lock()
            defer { lock.unlock() }
            if !finished {
                logLine(
                    "WARNING mic: bind still blocked in '\(current)' after "
                        + "\(Int(Self.stallWarnSec))s")
            }
        }
    }

    /// Close the previous step (logging it if slow) and open `name`.
    func step(_ name: String) {
        lock.lock()
        defer { lock.unlock() }
        closeStep()
        current = name
    }

    /// Idempotent; returns total seconds spent in bind().
    @discardableResult
    func finish() -> Double {
        lock.lock()
        defer { lock.unlock() }
        if !finished {
            closeStep()
            finished = true
        }
        return Self.seconds(since: begin)
    }

    private func closeStep() {
        let sec = Self.seconds(since: stepStart)
        if sec >= Self.slowStepSec {
            logLine("mic: bind step '\(current)' took \(String(format: "%.1f", sec))s")
        }
        stepStart = MonotonicClock.nowNanos()
    }

    private static func seconds(since start: UInt64) -> Double {
        Double(MonotonicClock.nowNanos() - start) / 1e9
    }
}

/// Cross-thread result of the first bind attempt (see MicCapture.start()).
private final class InitialBindOutcome {
    private let lock = NSLock()
    private var finishedError: Error?
    private var timedOut = false

    var error: Error? {
        lock.lock()
        defer { lock.unlock() }
        return finishedError
    }

    func markTimedOut() {
        lock.lock()
        timedOut = true
        lock.unlock()
    }

    /// Records the result. A failure that lands after start() already gave up
    /// waiting can no longer fail the capturer, so it is logged instead.
    func finish(error: Error?) {
        lock.lock()
        finishedError = error
        let late = timedOut
        lock.unlock()
        if late, let error {
            logLine("mic: first bind failed after start() stopped waiting (\(error))")
        }
    }
}

final class MicCapture {
    private var deviceID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private let chunker: FrameChunker
    private let rebindQueue = DispatchQueue(label: "onoats.mic.rebind")
    private var running = false
    private var listenerInstalled = false
    private var retryTimer: DispatchSourceTimer?

    // bind() can now run on several threads at once (the initial attempt, a
    // device-change rebind, stall retries), so committing/clearing the bound
    // IOProc goes through bindLock, and a bind that finishes after another one
    // already committed (or after stop()) discards itself instead of leaving a
    // second IOProc feeding duplicate chunks.
    private let bindLock = NSLock()
    private var bindClosed = false

    /// How long start() waits for the first bind before carrying on without it.
    /// AudioDeviceStart has been seen blocked >60 s in coreaudiod's IO-thread
    /// start (2026-09-25); start() must not wedge the whole capturer on it.
    private static let startTimeoutSec = 5.0
    private static let stallRetryIntervalSec = 10.0
    private static let maxStallRetries = 3

    // IOProc → worker handoff (same realtime constraint as SystemCapture:
    // the IO callback only memcpys; the worker resamples/chunks).
    //
    // Each chunk carries the format + resampler of the device GENERATION it was
    // captured under (both captured by that generation's IOProc closure). A
    // rebind swaps devices on rebindQueue while the worker may still be
    // draining old-device chunks — decoding those with the new device's
    // format/converter would corrupt them, so the worker must never read a
    // shared "current format"; it uses what travelled with the chunk.
    private let lock = NSCondition()
    private var queue:
        [(
            bytes: Data, frames: AVAudioFrameCount, endNs: UInt64,
            format: AVAudioFormat, resampler: Resampler16k
        )] = []
    private var workerClosed = false
    private var worker: Thread?
    private var droppedChunks: UInt64 = 0
    private let maxQueuedChunks = 128

    private lazy var deviceListener: AudioObjectPropertyListenerBlock = {
        [weak self] _, _ in
        guard let self else { return }
        self.rebindQueue.async { self.rebind(reason: "default input device changed") }
    }

    init(emit: @escaping (Data, UInt64) -> Void) {
        chunker = FrameChunker(
            label: "mic",
            zeroHint: "if you expect mic audio, check the input device is not "
                + "hardware-muted (gain at zero) and the right device is selected",
            emit: emit)
    }

    func start() throws {
        let workerThread = Thread { [weak self] in self?.runWorker() }
        workerThread.name = "mic-capture-worker"
        worker = workerThread
        workerThread.start()

        // Pacer BEFORE bind (live finding, 2026-06-11 22:26 smoke): the HAL
        // bind/start calls below can block >10 s — observed with the
        // system-audio TCC dialog still pending and a Bluetooth default input
        // (AirPods) activating — and the recorder's read-idle clock is already
        // running by the time start() is called. The system branch survived
        // that exact window only because its pacer was already emitting
        // silence; activating the mic chunker first gives this branch the
        // same protection (paced silence until the device delivers data).
        // Liveness: a bound mic input delivers callbacks continuously (zeros if
        // muted), so 10 s with no real data means the IOProc/device silently
        // stopped. Rebind — but only when a bind is already committed: the
        // never-bound case is retryStalledBind's, and running rebind() there
        // could park rebindQueue inside a stuck AudioDeviceStart.
        chunker.onStale = { [weak self] in
            guard let self else { return }
            self.bindLock.lock()
            let bound = self.ioProcID != nil
            self.bindLock.unlock()
            guard bound else { return }
            self.rebindQueue.async { self.rebind(reason: "no capture data for 10 s") }
        }
        chunker.activate()

        // The device-change listener goes in BEFORE the first bind so a default
        // input change during a stalled bind can still trigger a rebind.
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        let err = AudioObjectAddPropertyListenerBlock(
            AudioObjectID(kAudioObjectSystemObject), &addr, rebindQueue, deviceListener)
        if err == noErr {
            listenerInstalled = true
        } else {
            logLine("WARNING mic: could not install device-change listener (\(fourCC(err)))")
        }
        running = true

        // First bind on a fresh thread with a bounded wait. A bind that is still
        // blocked after startTimeoutSec (observed: AudioDeviceStart waiting on
        // coreaudiod for 60+ s on the built-in mic, healed only by an unrelated
        // device change) no longer wedges the main thread: start() returns,
        // the capturer reaches "streaming", and retries run in the background.
        let outcome = InitialBindOutcome()
        let done = DispatchSemaphore(value: 0)
        DispatchQueue.global().async { [self] in
            do {
                try bind()
                outcome.finish(error: nil)
            } catch {
                outcome.finish(error: error)
            }
            done.signal()
        }
        if done.wait(timeout: .now() + Self.startTimeoutSec) == .timedOut {
            outcome.markTimedOut()
            logLine(
                "WARNING mic: first bind has not finished after "
                    + "\(Int(Self.startTimeoutSec))s; continuing on paced silence "
                    + "and retrying in the background")
            rebindQueue.async { [self] in retryStalledBind(attempt: 1) }
        } else if let error = outcome.error {
            throw error
        }
    }

    /// Runs on rebindQueue only. While nothing is bound, start another bind on
    /// a FRESH thread (the stuck one keeps its own) every stallRetryIntervalSec,
    /// up to maxStallRetries. Whichever bind commits first wins; the others
    /// discard themselves (see bind()). A device-change rebind cancels this loop
    /// (it clears retryTimer) and takes over.
    private func retryStalledBind(attempt: Int) {
        bindLock.lock()
        let settled = ioProcID != nil || bindClosed
        bindLock.unlock()
        if settled || !running { return }
        guard attempt <= Self.maxStallRetries else {
            logLine(
                "ERROR mic: still not bound after \(Self.maxStallRetries) retries; "
                    + "the branch stays on paced silence until the default input changes")
            return
        }
        logLine("WARNING mic: not bound yet; retry \(attempt)/\(Self.maxStallRetries)")
        DispatchQueue.global().async { [weak self] in
            do { try self?.bind() } catch { logLine("mic: stall retry failed (\(error))") }
        }
        let timer = DispatchSource.makeTimerSource(queue: rebindQueue)
        timer.schedule(deadline: .now() + Self.stallRetryIntervalSec)
        timer.setEventHandler { [weak self] in self?.retryStalledBind(attempt: attempt + 1) }
        timer.resume()
        retryTimer = timer
    }

    /// Bind an IOProc to the CURRENT default input device.
    @discardableResult
    private func bind() throws -> Bool {
        let watch = BindWatch()
        defer { watch.finish() }
        watch.step("default input lookup")
        let device = defaultInputDeviceID()
        guard device != kAudioObjectUnknown else {
            throw CapturerError("no default input device")
        }

        // The device's input-side stream format (what the IOProc will deliver).
        watch.step("stream format")
        var fmtAddr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamFormat,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain)
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        let fmtErr = AudioObjectGetPropertyData(device, &fmtAddr, 0, nil, &size, &asbd)
        guard fmtErr == noErr else {
            throw CapturerError("get input stream format failed (OSStatus \(fourCC(fmtErr)))")
        }
        guard let format = AVAudioFormat(streamDescription: &asbd) else {
            throw CapturerError("input format not representable as AVAudioFormat")
        }
        // One resampler per bind generation, owned by this generation's IOProc
        // closure and travelling with each chunk it enqueues — never stored on
        // the instance, so a rebind cannot retroactively change how chunks
        // already in the queue are decoded.
        watch.step("resampler init")
        let resampler = try Resampler16k(inputFormat: format)

        let sampleRate = format.sampleRate
        let bytesPerFrame = asbd.mBytesPerFrame
        var newProcID: AudioDeviceIOProcID?
        watch.step("create IOProc")
        let procErr = AudioDeviceCreateIOProcIDWithBlock(&newProcID, device, nil) {
            [weak self] _, inInputData, inInputTime, _, _ in
            guard let self else { return }
            let abl = inInputData.pointee
            guard abl.mNumberBuffers >= 1 else { return }
            let buf = abl.mBuffers
            guard let src = buf.mData, buf.mDataByteSize > 0, bytesPerFrame > 0 else { return }
            let bytes = Data(bytes: src, count: Int(buf.mDataByteSize))
            let frames = AVAudioFrameCount(Int(buf.mDataByteSize) / Int(bytesPerFrame))
            let ts = inInputTime.pointee
            let startNs =
                ts.mFlags.contains(.hostTimeValid)
                ? MonotonicClock.nanos(fromHostTime: ts.mHostTime) : MonotonicClock.nowNanos()
            let endNs = startNs + UInt64(Double(frames) / sampleRate * 1e9)
            self.enqueueChunk(
                bytes: bytes, frames: frames, endNs: endNs,
                format: format, resampler: resampler)
        }
        guard procErr == noErr, let proc = newProcID else {
            throw CapturerError(
                "mic AudioDeviceCreateIOProcIDWithBlock failed (OSStatus \(fourCC(procErr)))")
        }
        watch.step("AudioDeviceStart")
        let startErr = AudioDeviceStart(device, proc)
        guard startErr == noErr else {
            AudioDeviceDestroyIOProcID(device, proc)
            throw CapturerError("mic AudioDeviceStart failed (OSStatus \(fourCC(startErr)))")
        }
        // Commit only if nothing else has bound (or stop() ran) meanwhile and the
        // default input is still the device we bound; otherwise this is a late
        // straggler and must tear its own IOProc down.
        let currentDefault = defaultInputDeviceID()
        bindLock.lock()
        let stale = bindClosed || ioProcID != nil || currentDefault != device
        if !stale {
            deviceID = device
            ioProcID = proc
        }
        bindLock.unlock()
        if stale {
            AudioDeviceStop(device, proc)
            AudioDeviceDestroyIOProcID(device, proc)
            logLine("mic: discarded a superseded bind after \(String(format: "%.1f", watch.finish()))s")
            return false
        }
        let bindSec = watch.finish()
        logLine(
            "mic: capturing from \(defaultInputDeviceDescription()) at "
                + "\(Int(format.sampleRate)) Hz / \(format.channelCount) ch "
                + "(bind \(String(format: "%.2f", bindSec))s)")
        // Re-emitted on every successful bind (initial + rebind) so a mid-session
        // default-input change updates the status file's mic_device.
        emitEvent("device", "branch=mic hint=\(defaultInputDeviceFieldDescription())")
        return true
    }

    private func unbind() {
        bindLock.lock()
        let device = deviceID
        let proc = ioProcID
        deviceID = AudioObjectID(kAudioObjectUnknown)
        ioProcID = nil
        bindLock.unlock()
        if device != kAudioObjectUnknown, let proc {
            AudioDeviceStop(device, proc)
            AudioDeviceDestroyIOProcID(device, proc)
        }
    }

    /// Runs on rebindQueue only. MUST NOT exit on a recoverable device change —
    /// on failure (e.g. no input device for a moment) retry in 2 s; the silence
    /// pacer keeps the branch alive meanwhile.
    private func rebind(reason: String) {
        guard running else { return }
        retryTimer?.cancel()
        retryTimer = nil
        unbind()
        do {
            if try bind() { logLine("mic: rebound after \(reason)") }
        } catch {
            logLine("mic: rebind after \(reason) failed (\(error)); retrying in 2s")
            let timer = DispatchSource.makeTimerSource(queue: rebindQueue)
            timer.schedule(deadline: .now() + 2)
            timer.setEventHandler { [weak self] in self?.rebind(reason: "retry") }
            timer.resume()
            retryTimer = timer
        }
    }

    private func enqueueChunk(
        bytes: Data, frames: AVAudioFrameCount, endNs: UInt64,
        format: AVAudioFormat, resampler: Resampler16k
    ) {
        lock.lock()
        defer { lock.unlock() }
        if workerClosed { return }
        if queue.count >= maxQueuedChunks {
            // Count only — NO logging here: this runs on the realtime thread,
            // and string formatting + log IO is exactly the deadline overrun
            // the header warns about. The worker thread reports drops.
            queue.removeFirst()
            droppedChunks += 1
        }
        queue.append((bytes, frames, endNs, format, resampler))
        lock.signal()
    }

    private func runWorker() {
        var loggedDropped: UInt64 = 0
        while true {
            lock.lock()
            while queue.isEmpty && !workerClosed { lock.wait() }
            if workerClosed {
                lock.unlock()
                return
            }
            let item = queue.removeFirst()
            let dropped = droppedChunks
            lock.unlock()

            if dropped > loggedDropped, loggedDropped == 0 || dropped - loggedDropped >= 100 {
                loggedDropped = dropped
                logLine(
                    "WARNING mic: capture queue full; dropping oldest chunks "
                        + "(total dropped \(dropped))")
            }

            guard item.frames > 0,
                let buffer = AVAudioPCMBuffer(pcmFormat: item.format, frameCapacity: item.frames)
            else { continue }
            buffer.frameLength = item.frames
            item.bytes.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
                let dst = buffer.audioBufferList.pointee.mBuffers
                if let dstData = dst.mData, let src = raw.baseAddress {
                    memcpy(dstData, src, min(Int(dst.mDataByteSize), raw.count))
                }
            }
            guard let out = item.resampler.convert(buffer) else { continue }
            chunker.append(out, endNs: item.endNs)
        }
    }

    func stop() {
        bindLock.lock()
        bindClosed = true
        bindLock.unlock()
        rebindQueue.sync {
            running = false
            retryTimer?.cancel()
            retryTimer = nil
        }
        if listenerInstalled {
            var addr = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyDefaultInputDevice,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            AudioObjectRemovePropertyListenerBlock(
                AudioObjectID(kAudioObjectSystemObject), &addr, rebindQueue, deviceListener)
            listenerInstalled = false
        }
        chunker.stop()
        // Serialize the final unbind with any in-flight rebind() — both touch
        // deviceID/ioProcID, and an unbind racing a rebind on another thread
        // could double-destroy the IOProcID or act on a stale device.
        rebindQueue.sync { unbind() }
        lock.lock()
        workerClosed = true
        queue.removeAll()
        lock.signal()
        lock.unlock()
    }
}