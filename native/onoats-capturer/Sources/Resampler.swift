// Per-stream resample (device rate → 16 kHz PCM16 mono LE) + 20 ms chunking.
//
// Each stream owns its converter (AVAudioConverter is stateful across calls,
// which is what makes streaming rate conversion correct) and its chunker.

import AVFoundation
import Foundation

let OUT_SAMPLE_RATE = 16000.0
let SAMPLES_PER_FRAME = 320  // 20 ms @ 16 kHz
let BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2  // 640 — wire-contract reference
let NS_PER_SAMPLE: UInt64 = 62_500  // 1e9 / 16000

let outputFormat = AVAudioFormat(
    commonFormat: .pcmFormatInt16, sampleRate: OUT_SAMPLE_RATE, channels: 1, interleaved: true)!

final class Resampler16k {
    private let converter: AVAudioConverter
    private let inputRate: Double

    init(inputFormat: AVAudioFormat) throws {
        guard let c = AVAudioConverter(from: inputFormat, to: outputFormat) else {
            throw CapturerError(
                "AVAudioConverter init failed for input format "
                    + "\(inputFormat.sampleRate) Hz / \(inputFormat.channelCount) ch")
        }
        converter = c
        inputRate = inputFormat.sampleRate
    }

    /// Convert one capture buffer; returns nil on a conversion error (logged).
    /// The converter buffers fractional samples internally between calls.
    func convert(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        let capacity =
            AVAudioFrameCount(Double(buffer.frameLength) * OUT_SAMPLE_RATE / inputRate) + 64
        guard let out = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            return nil
        }
        var fed = false
        var convError: NSError?
        let status = converter.convert(to: out, error: &convError) { _, inputStatus in
            if fed {
                inputStatus.pointee = .noDataNow
                return nil
            }
            fed = true
            inputStatus.pointee = .haveData
            return buffer
        }
        if status == .error {
            logLine("resample error: \(convError?.localizedDescription ?? "unknown")")
            return nil
        }
        return out
    }
}

/// Accumulates 16 kHz mono Int16 samples and emits exact 320-sample (640-byte)
/// frames. Timestamps: the LAST sample appended in a call corresponds to
/// `endNs` (end of the capture buffer on the capturer-wide monotonic clock);
/// each emitted frame is stamped with its FIRST sample's time, extrapolated
/// back at the output rate.
///
/// Silence pacing: a Core Audio process tap delivers IO callbacks ONLY while
/// some tapped process is actually rendering audio (observed empirically) —
/// and the mic branch can gap during a device change. Without filler, a quiet
/// system starves the branch and trips the recorder's 10 s read-idle watchdog,
/// and the me/them timeline loses continuity. Each chunker therefore runs a
/// 20 ms pacer thread that emits silence frames whenever no real capture data
/// has arrived for `silenceAfterNs`, continuing the timestamp timeline.
final class FrameChunker {
    private let lock = NSLock()
    private var pending: [Int16] = []
    private let emit: (Data, UInt64) -> Void
    private let label: String

    private var lastRealDataWallNs: UInt64 = 0
    private var lastEmittedEndNs: UInt64 = 0  // timeline cursor (ns, end of last frame)
    private var pacer: Thread?
    private var stopped = false
    private var silenceFramesTotal: UInt64 = 0

    /// Fill after 100 ms without real data (~5 missed tap callbacks).
    private let silenceAfterNs: UInt64 = 100_000_000
    private static let silentFrame = Data(count: BYTES_PER_FRAME)

    init(label: String, emit: @escaping (Data, UInt64) -> Void) {
        self.label = label
        self.emit = emit
    }

    /// Start the timeline + pacer. Call once, when the capture starts.
    func activate() {
        lock.lock()
        let now = MonotonicClock.nowNanos()
        lastRealDataWallNs = now
        lastEmittedEndNs = now
        lock.unlock()
        let t = Thread { [weak self] in self?.runPacer() }
        t.name = "pacer-\(label)"
        pacer = t
        t.start()
    }

    func stop() {
        lock.lock()
        stopped = true
        lock.unlock()
    }

    func append(_ buffer: AVAudioPCMBuffer, endNs: UInt64) {
        guard let data = buffer.int16ChannelData else { return }
        let n = Int(buffer.frameLength)
        if n == 0 { return }
        lock.lock()
        defer { lock.unlock() }
        lastRealDataWallNs = MonotonicClock.nowNanos()
        pending.append(contentsOf: UnsafeBufferPointer(start: data[0], count: n))
        while pending.count >= SAMPLES_PER_FRAME {
            let samplesFromFrameStartToStreamEnd = UInt64(pending.count)
            let backNs = samplesFromFrameStartToStreamEnd * NS_PER_SAMPLE
            // Clamp into the already-emitted timeline: after a silence-filled
            // gap the first real frame's capture time can land slightly before
            // the filler cursor; captured_monotonic_ns must never regress.
            let ts = max(endNs > backNs ? endNs - backNs : 0, lastEmittedEndNs)
            let frame = pending[0..<SAMPLES_PER_FRAME].withUnsafeBufferPointer {
                Data(buffer: $0)  // Int16 native LE on all Apple targets
            }
            pending.removeFirst(SAMPLES_PER_FRAME)
            lastEmittedEndNs = ts + UInt64(SAMPLES_PER_FRAME) * NS_PER_SAMPLE
            emit(frame, ts)
        }
    }

    private func runPacer() {
        while true {
            Thread.sleep(forTimeInterval: 0.02)
            lock.lock()
            if stopped {
                lock.unlock()
                return
            }
            let now = MonotonicClock.nowNanos()
            if now > lastRealDataWallNs + silenceAfterNs {
                // Emit silence up to (now - silenceAfterNs): trail the live
                // edge so resumed real data doesn't collide with filler.
                let target = now - silenceAfterNs
                // If we are far behind (the process was suspended/throttled),
                // jump the cursor instead of slewing for seconds — a visible
                // timeline gap beats a stream that lags real time forever.
                let frameNs = UInt64(SAMPLES_PER_FRAME) * NS_PER_SAMPLE
                if target > lastEmittedEndNs + 2_000_000_000 {
                    logLine(
                        "WARNING \(label): pacer \( (target - lastEmittedEndNs) / 1_000_000 )ms "
                            + "behind; jumping timeline cursor forward")
                    lastEmittedEndNs = target - frameNs
                }
                var emitted = 0
                while lastEmittedEndNs + frameNs <= target {
                    let ts = lastEmittedEndNs
                    lastEmittedEndNs = ts + frameNs
                    silenceFramesTotal += 1
                    emitted += 1
                    emit(Self.silentFrame, ts)
                }
                if emitted > 0 && (silenceFramesTotal <= 1 || silenceFramesTotal % 500 == 0) {
                    logLine(
                        "\(label): pacing silence (no capture data; "
                            + "\(silenceFramesTotal) filler frames so far)")
                }
            }
            lock.unlock()
        }
    }
}
