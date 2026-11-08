import Darwin
import Foundation

/// Process-level harness for the production FrameChunker pacer. The parent
/// test sends SIGSTOP/SIGCONT; an over-threshold wake must reach this callback
/// without any Core Audio callback or audio sample participating.
@main
struct FrameChunkerSuspensionHarness {
    static func main() {
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
}
