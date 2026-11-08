// Pure process-uptime heartbeat gap detector.
//
// The caller owns clock sampling and terminal action. This type deliberately
// knows nothing about audio content, Core Audio, threads, sockets, or teardown.

struct ProcessSuspensionWatchdog {
    static let thresholdNs: UInt64 = 2_000_000_000

    private var previousWakeNs: UInt64?

    mutating func activate(at nowNs: UInt64) {
        previousWakeNs = nowNs
    }

    /// Return the observed gap only when it is strictly above the threshold.
    /// Every observation becomes the next baseline. A regressed clock is
    /// therefore a harmless reset rather than an underflow or terminal event.
    mutating func observe(wakeNs: UInt64) -> UInt64? {
        guard let previous = previousWakeNs else {
            previousWakeNs = wakeNs
            return nil
        }
        previousWakeNs = wakeNs
        guard wakeNs >= previous else { return nil }
        let gap = wakeNs - previous
        return gap > Self.thresholdNs ? gap : nil
    }
}
