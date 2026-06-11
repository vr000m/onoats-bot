// System-output ("them") branch: Core Audio global process tap → private
// aggregate device → IOProc → resample → 20 ms frames.
//
// Recipe proven by Pre-req spike 4 (native/spike/main.swift, Findings
// 2026-06-09): global tap with EMPTY exclusion list (= tap all = system
// output), .unmuted so other apps stay audible, isPrivate, wrapped in a
// private aggregate (auto-reclaimed by macOS on process death — no SIGKILL
// residue). Signed builds create the tap in ~200 ms; unsigned blocks ~4 s in
// TCC verification per call — never ship/benchmark unsigned.
//
// REALTIME CONSTRAINT (learned building this): the IOProc runs on Core Audio's
// realtime IO thread. Doing AVAudioConverter work / allocations / contended
// locks in it overloads the deadline and Core Audio silently STOPS calling the
// IOProc after a handful of cycles (observed: 5 callbacks then silence). So
// the IOProc only memcpys the buffer + timestamp into a bounded queue; a
// dedicated worker thread does the wrap → resample → chunk → emit.
//
// Lifecycle invariant (every exit path): AudioDeviceStop → destroy IOProc →
// destroy aggregate → destroy tap, in that order. The tap is created ONCE per
// session and the IOProc started immediately (touching the tap subsystem
// glitches audio briefly, so no needless tap ops mid-session).
//
// Device-change note: the tap is process-scoped (all processes), not bound to
// an output device, so a default-OUTPUT-device change keeps flowing through
// the same tap; the default-INPUT-device contract requirement is the mic
// branch's job (MicCapture).

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

let AGGREGATE_UID_PREFIX = "onoats-capturer-agg-"

final class SystemCapture {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?
    private var resampler: Resampler16k?
    private var tapFormat: AVAudioFormat?
    private let chunker: FrameChunker

    // IOProc → worker handoff. Bounded; drop-oldest under a stalled worker
    // (the recorder-side backpressure policy is the real one — this only caps
    // capturer memory). ~128 × ~10ms chunks ≈ 1.3 s.
    private let lock = NSCondition()
    private var queue: [(bytes: Data, frames: AVAudioFrameCount, endNs: UInt64)] = []
    private var workerClosed = false
    private var worker: Thread?
    private var droppedChunks: UInt64 = 0
    private let maxQueuedChunks = 128

    init(emit: @escaping (Data, UInt64) -> Void) {
        chunker = FrameChunker(
            label: "system",
            zeroHint: "if you expect system audio, check System Settings ▸ "
                + "Privacy & Security ▸ Screen & System Audio Recording — a "
                + "DENIED grant still delivers callbacks, but all-zero",
            emit: emit)
    }

    func start() throws {
        do {
            try startInner()
        } catch {
            stop()  // never leave a partially-built tap/aggregate behind
            throw error
        }
    }

    private func startInner() throws {
        // 1. Global process tap. This call is also where a revoked/denied
        //    system-audio TCC grant surfaces — fail loud with a pointer to the
        //    Settings pane, never hang or stream silence knowingly.
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        desc.name = "onoats-capturer-tap"
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        // AudioHardwareCreateProcessTap is intermittently flaky (observed: an
        // INSTANT return of noErr + kAudioObjectUnknown, where the legit path
        // takes ~200 ms signed). Retry briefly before declaring it a denial.
        var newTapID = AudioObjectID(kAudioObjectUnknown)
        var tapErr: OSStatus = noErr
        for attempt in 1...3 {
            tapErr = AudioHardwareCreateProcessTap(desc, &newTapID)
            if tapErr == noErr && newTapID != kAudioObjectUnknown { break }
            logLine(
                "system: AudioHardwareCreateProcessTap attempt \(attempt) failed "
                    + "(OSStatus \(fourCC(tapErr)), tapID=\(newTapID))")
            newTapID = AudioObjectID(kAudioObjectUnknown)
            if attempt < 3 { Thread.sleep(forTimeInterval: 0.5) }
        }
        guard tapErr == noErr, newTapID != kAudioObjectUnknown else {
            throw CapturerError(
                "AudioHardwareCreateProcessTap failed after 3 attempts (OSStatus "
                    + "\(fourCC(tapErr))) — system-audio capture unavailable. Check System "
                    + "Settings ▸ Privacy & Security ▸ Screen & System Audio Recording.")
        }
        tapID = newTapID

        // 2. Tap stream format (typically 48 kHz / 2 ch float interleaved).
        var formatAddr = AudioObjectPropertyAddress(
            mSelector: kAudioTapPropertyFormat,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        let fmtErr = AudioObjectGetPropertyData(tapID, &formatAddr, 0, nil, &size, &asbd)
        guard fmtErr == noErr else {
            throw CapturerError("get kAudioTapPropertyFormat failed (OSStatus \(fourCC(fmtErr)))")
        }
        guard let format = AVAudioFormat(streamDescription: &asbd) else {
            throw CapturerError("tap format not representable as AVAudioFormat")
        }
        tapFormat = format
        resampler = try Resampler16k(inputFormat: format)

        // 3. Private aggregate device wrapping the tap. Uniquely-named UID so
        //    a leaked object would be identifiable (none observed in spike 4).
        let aggregateUID = "\(AGGREGATE_UID_PREFIX)\(getpid())"
        let aggregateDesc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "onoats-capturer-agg",
            kAudioAggregateDeviceUIDKey: aggregateUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapUIDKey: desc.uuid.uuidString,
                    kAudioSubTapDriftCompensationKey: true,
                ]
            ],
        ]
        var newAggregateID = AudioObjectID(kAudioObjectUnknown)
        let aggErr = AudioHardwareCreateAggregateDevice(
            aggregateDesc as CFDictionary, &newAggregateID)
        guard aggErr == noErr, newAggregateID != kAudioObjectUnknown else {
            throw CapturerError(
                "AudioHardwareCreateAggregateDevice failed (OSStatus \(fourCC(aggErr)))")
        }
        aggregateID = newAggregateID

        // 4. Worker thread (wrap → resample → chunk off the realtime thread).
        let workerThread = Thread { [weak self] in self?.runWorker() }
        workerThread.name = "system-capture-worker"
        worker = workerThread
        workerThread.start()

        // 5. Copy-only IOProc draining the tap. Started immediately — the
        //    window from tap creation to AudioDeviceStart is the audible
        //    output dropout (~200 ms signed).
        let sampleRate = format.sampleRate
        var newProcID: AudioDeviceIOProcID?
        let procErr = AudioDeviceCreateIOProcIDWithBlock(&newProcID, aggregateID, nil) {
            [weak self] _, inInputData, inInputTime, _, _ in
            guard let self else { return }
            let abl = inInputData.pointee
            guard abl.mNumberBuffers >= 1 else { return }
            // Interleaved tap stream = single buffer; copy it verbatim.
            let buf = abl.mBuffers
            guard let src = buf.mData, buf.mDataByteSize > 0, asbd.mBytesPerFrame > 0
            else { return }
            let bytes = Data(bytes: src, count: Int(buf.mDataByteSize))
            let frames = AVAudioFrameCount(
                Int(buf.mDataByteSize) / Int(asbd.mBytesPerFrame))
            let ts = inInputTime.pointee
            let startNs =
                ts.mFlags.contains(.hostTimeValid)
                ? MonotonicClock.nanos(fromHostTime: ts.mHostTime) : MonotonicClock.nowNanos()
            let endNs = startNs + UInt64(Double(frames) / sampleRate * 1e9)
            self.enqueueChunk(bytes: bytes, frames: frames, endNs: endNs)
        }
        guard procErr == noErr, let proc = newProcID else {
            throw CapturerError(
                "AudioDeviceCreateIOProcIDWithBlock failed (OSStatus \(fourCC(procErr)))")
        }
        ioProcID = proc

        let startErr = AudioDeviceStart(aggregateID, proc)
        guard startErr == noErr else {
            throw CapturerError("AudioDeviceStart failed (OSStatus \(fourCC(startErr)))")
        }
        chunker.activate()
        logLine(
            "system: capturing via process tap at \(Int(format.sampleRate)) Hz / "
                + "\(format.channelCount) ch (aggregate uid=\(aggregateUID))")
    }

    private func enqueueChunk(bytes: Data, frames: AVAudioFrameCount, endNs: UInt64) {
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
        queue.append((bytes, frames, endNs))
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
                    "WARNING system: capture queue full; dropping oldest chunks "
                        + "(total dropped \(dropped))")
            }

            guard let format = tapFormat, let resampler else { continue }
            guard item.frames > 0,
                let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: item.frames)
            else { continue }
            buffer.frameLength = item.frames
            item.bytes.withUnsafeBytes { (raw: UnsafeRawBufferPointer) in
                let dst = buffer.audioBufferList.pointee.mBuffers
                if let dstData = dst.mData, let src = raw.baseAddress {
                    memcpy(dstData, src, min(Int(dst.mDataByteSize), raw.count))
                }
            }
            guard let out = resampler.convert(buffer) else { continue }
            chunker.append(out, endNs: item.endNs)
        }
    }

    /// Full teardown in the contract-mandated order; idempotent and safe on a
    /// partially-constructed state.
    func stop() {
        chunker.stop()
        if aggregateID != kAudioObjectUnknown, let proc = ioProcID {
            AudioDeviceStop(aggregateID, proc)
            AudioDeviceDestroyIOProcID(aggregateID, proc)
        }
        ioProcID = nil
        if aggregateID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = AudioObjectID(kAudioObjectUnknown)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = AudioObjectID(kAudioObjectUnknown)
        }
        lock.lock()
        workerClosed = true
        queue.removeAll()
        lock.signal()
        lock.unlock()
    }
}
