import AVFoundation
import Darwin
import Foundation

/// Process-level harness for the production FrameChunker pacer. The parent
/// test sends SIGSTOP/SIGCONT; an over-threshold wake must reach this callback
/// without any Core Audio callback or audio sample participating.
@main
struct FrameChunkerSuspensionHarness {
    static func main() {
        if CommandLine.arguments.dropFirst().first == "--append-after-stop" {
            verifyAppendAfterStop()
            return
        }
        let chunker = FrameChunker(
            label: "system",
            zeroHint: "unused by suspension harness",
            onHeartbeatStall: { gapNs in
                FileHandle.standardError.write(
                    "heartbeat gap \(gapNs / 1_000_000)ms\n".data(using: .utf8)!)
                Darwin._exit(ExitCode.captureHeartbeatStalled)
            },
            emit: { _, _ in })
        chunker.activate()
        print("READY")
        fflush(stdout)
        while true { Thread.sleep(forTimeInterval: 1.0) }
    }

    private static func verifyAppendAfterStop() {
        var emitted = 0
        let chunker = FrameChunker(
            label: "system",
            zeroHint: "unused by stopped append harness",
            emit: { _, _ in emitted += 1 })
        guard let format = AVAudioFormat(
            commonFormat: .pcmFormatInt16,
            sampleRate: OUT_SAMPLE_RATE,
            channels: 1,
            interleaved: false),
            let buffer = AVAudioPCMBuffer(
                pcmFormat: format, frameCapacity: AVAudioFrameCount(SAMPLES_PER_FRAME))
        else {
            FileHandle.standardError.write(Data("buffer allocation failed\n".utf8))
            Darwin._exit(2)
        }
        buffer.frameLength = AVAudioFrameCount(SAMPLES_PER_FRAME)
        buffer.int16ChannelData?[0].initialize(repeating: 1, count: SAMPLES_PER_FRAME)
        chunker.stop()
        chunker.append(buffer, endNs: MonotonicClock.nowNanos())
        guard emitted == 0 else {
            FileHandle.standardError.write(
                Data("stopped chunker emitted \(emitted) frame(s)\n".utf8))
            Darwin._exit(3)
        }
        print("APPEND_AFTER_STOP_OK")
    }
}
