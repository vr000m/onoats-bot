// onoats-capturer — shared helpers: logging, errors, exit codes, monotonic clock.
//
// Production capturer for AUDIO_SOURCE=socket (wire contract v1,
// docs/audio-socket-contract.md). Built from the recipe proven by the
// Pre-req spikes 3 + 4 (tree retired; preserved at the `spike-archive` tag —
// dev plan docs/dev_plans/20260609-feature-milestone-b-macos-capture-menubar.md).

import Foundation

func logLine(_ s: String) {
    FileHandle.standardError.write(("onoats-capturer: " + s + "\n").data(using: .utf8)!)
}

/// Machine-parseable event line for the supervisor's stderr reader
/// (docs/audio-socket-contract.md "Capturer event lines"). Format:
/// `ONOATS-EVENT <type> k=v …` — the prefix starts the line (no `onoats-capturer:`
/// prologue), values are single tokens except a trailing `hint=` field, which
/// consumes the rest of the line. One line per event; never multi-line.
func emitEvent(_ type: String, _ fields: String = "") {
    let line = "ONOATS-EVENT " + type + (fields.isEmpty ? "" : " " + fields) + "\n"
    FileHandle.standardError.write(line.data(using: .utf8)!)
}

struct CapturerError: Error, CustomStringConvertible {
    let message: String
    init(_ message: String) { self.message = message }
    var description: String { message }
}

/// Exit codes — the supervisor treats ANY capturer exit before the recorder ends
/// as fail-loud regardless of code, but distinct codes make the WARNING/ERROR
/// line diagnosable.
enum ExitCode {
    static let ok: Int32 = 0
    static let usage: Int32 = 2
    static let micDenied: Int32 = 10
    // Genuine AudioHardwareCreateProcessTap API failure (after the ×3@500 ms
    // retry) — NEVER a TCC denial: a denied tap succeeds and delivers zeros
    // (verified 2026-06-11), so denial's only observable is the zero-run
    // WARNING. The supervisor maps this to exit_reason "system-audio-failed".
    static let systemAudioFailed: Int32 = 11
    static let socketFailed: Int32 = 12
    static let captureFailed: Int32 = 13
}

func fourCC(_ s: Int32) -> String {
    let n = UInt32(bitPattern: s)
    let chars = [
        UInt8((n >> 24) & 0xFF), UInt8((n >> 16) & 0xFF),
        UInt8((n >> 8) & 0xFF), UInt8(n & 0xFF),
    ]
    let printable = chars.allSatisfy { $0 >= 0x20 && $0 < 0x7F }
    let cc = printable ? " '" + String(bytes: chars, encoding: .ascii)! + "'" : ""
    return "\(s)\(cc)"
}

/// One capturer-wide monotonic clock. Both streams stamp captured_monotonic_ns
/// from mach host time via this single mapping (== CLOCK_UPTIME_RAW), so
/// me/them drift is measurable in one domain — never per-callback wall clocks.
enum MonotonicClock {
    private static let timebase: mach_timebase_info_data_t = {
        var tb = mach_timebase_info_data_t()
        mach_timebase_info(&tb)
        return tb
    }()

    static func nanos(fromHostTime hostTime: UInt64) -> UInt64 {
        hostTime &* UInt64(timebase.numer) / UInt64(timebase.denom)
    }

    static func nowNanos() -> UInt64 {
        nanos(fromHostTime: mach_absolute_time())
    }
}
