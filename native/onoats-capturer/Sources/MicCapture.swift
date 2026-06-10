// Mic ("me") branch: AVAudioEngine input tap → resample → 20 ms frames.
//
// Device-change survival (contract MUST): AVAudioEngine stops itself when the
// default input device changes (AirPods disconnect etc.) and posts
// AVAudioEngineConfigurationChange. We rebuild the tap + converter against the
// new input format and restart — never exit on a recoverable change. If the
// restart fails (e.g. no input device at all for a moment), retry on a timer
// AND on the next configuration-change notification.

import AVFoundation
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

final class MicCapture {
    private let engine = AVAudioEngine()
    private var resampler: Resampler16k?
    private let chunker: FrameChunker
    private let restartQueue = DispatchQueue(label: "onoats.mic.restart")
    private var running = false
    private var observer: NSObjectProtocol?
    private var retryTimer: DispatchSourceTimer?

    init(emit: @escaping (Data, UInt64) -> Void) {
        chunker = FrameChunker(label: "mic", emit: emit)
    }

    func start() throws {
        observer = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil
        ) { [weak self] _ in
            guard let self else { return }
            self.restartQueue.async { self.restart(reason: "configuration change") }
        }
        try attachAndStart()
        chunker.activate()
        running = true
    }

    func stop() {
        restartQueue.sync {
            running = false
            retryTimer?.cancel()
            retryTimer = nil
        }
        chunker.stop()
        if let observer { NotificationCenter.default.removeObserver(observer) }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }

    private func attachAndStart() throws {
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw CapturerError("mic input has no usable format (no input device?)")
        }
        let resampler = try Resampler16k(inputFormat: format)
        self.resampler = resampler
        // ~20 ms at the device rate keeps emission cadence close to the wire's.
        let bufferSize = AVAudioFrameCount(max(256, Int(format.sampleRate / 50)))
        input.installTap(onBus: 0, bufferSize: bufferSize, format: format) {
            [weak self] buffer, when in
            guard let self else { return }
            // hostTime stamps the START of the buffer; the chunker wants its end.
            let startNs =
                when.isHostTimeValid
                ? MonotonicClock.nanos(fromHostTime: when.hostTime) : MonotonicClock.nowNanos()
            let durNs = UInt64(Double(buffer.frameLength) / format.sampleRate * 1e9)
            guard let out = self.resampler?.convert(buffer) else { return }
            self.chunker.append(out, endNs: startNs + durNs)
        }
        try engine.start()
        logLine("mic: capturing at \(Int(format.sampleRate)) Hz / \(format.channelCount) ch")
    }

    /// Runs on restartQueue only.
    private func restart(reason: String) {
        guard running else { return }
        retryTimer?.cancel()
        retryTimer = nil
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        do {
            try attachAndStart()
            logLine("mic: restarted after \(reason)")
        } catch {
            logLine("mic: restart after \(reason) failed (\(error)); retrying in 2s")
            let timer = DispatchSource.makeTimerSource(queue: restartQueue)
            timer.schedule(deadline: .now() + 2)
            timer.setEventHandler { [weak self] in self?.restart(reason: "retry") }
            timer.resume()
            retryTimer = timer
        }
    }
}
