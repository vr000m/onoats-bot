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

/// Name + UID of the system default INPUT device — logged at mic start so a
/// wrong default (e.g. a leftover virtual loopback device) is diagnosable at
/// a glance.
func defaultInputDeviceDescription() -> String {
    let deviceID = defaultInputDeviceID()
    guard deviceID != kAudioObjectUnknown else { return "<no default input device>" }

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
    let name = stringProp(kAudioObjectPropertyName)
    let uid = stringProp(kAudioDevicePropertyDeviceUID)
    return "\"\(name)\" (uid=\(uid), id=\(deviceID))"
}

final class MicCapture {
    private var deviceID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private let chunker: FrameChunker
    private let rebindQueue = DispatchQueue(label: "onoats.mic.rebind")
    private var running = false
    private var listenerInstalled = false
    private var retryTimer: DispatchSourceTimer?

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

        try bind()

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

        chunker.activate()
        running = true
    }

    /// Bind an IOProc to the CURRENT default input device.
    private func bind() throws {
        let device = defaultInputDeviceID()
        guard device != kAudioObjectUnknown else {
            throw CapturerError("no default input device")
        }

        // The device's input-side stream format (what the IOProc will deliver).
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
        let resampler = try Resampler16k(inputFormat: format)

        let sampleRate = format.sampleRate
        let bytesPerFrame = asbd.mBytesPerFrame
        var newProcID: AudioDeviceIOProcID?
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
        let startErr = AudioDeviceStart(device, proc)
        guard startErr == noErr else {
            AudioDeviceDestroyIOProcID(device, proc)
            throw CapturerError("mic AudioDeviceStart failed (OSStatus \(fourCC(startErr)))")
        }
        deviceID = device
        ioProcID = proc
        logLine(
            "mic: capturing from \(defaultInputDeviceDescription()) at "
                + "\(Int(format.sampleRate)) Hz / \(format.channelCount) ch")
    }

    private func unbind() {
        if deviceID != kAudioObjectUnknown, let proc = ioProcID {
            AudioDeviceStop(deviceID, proc)
            AudioDeviceDestroyIOProcID(deviceID, proc)
        }
        deviceID = AudioObjectID(kAudioObjectUnknown)
        ioProcID = nil
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
            try bind()
            logLine("mic: rebound after \(reason)")
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