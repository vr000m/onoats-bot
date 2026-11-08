import Foundation

private func require(_ condition: @autoclosure () -> Bool, _ message: String) {
    if !condition() {
        FileHandle.standardError.write("FAIL: \(message)\n".data(using: .utf8)!)
        exit(1)
    }
}

@main
struct ProcessSuspensionWatchdogHarness {
    static func main() {
        let threshold = ProcessSuspensionWatchdog.thresholdNs
        var watchdog = ProcessSuspensionWatchdog()

        watchdog.activate(at: 1_000_000_000)
        require(watchdog.observe(wakeNs: 1_020_000_000) == nil, "20 ms wake tripped")

        watchdog.activate(at: 5_000_000_000)
        require(
            watchdog.observe(wakeNs: 5_000_000_000 + threshold) == nil,
            "exact threshold must be allowed")

        watchdog.activate(at: 10_000_000_000)
        require(
            watchdog.observe(wakeNs: 10_000_000_000 + threshold + 1) == threshold + 1,
            "over-threshold gap was not reported")
        require(
            watchdog.observe(wakeNs: 12_020_000_001) == nil,
            "ordinary wake after a trip did not re-arm")
        require(
            watchdog.observe(wakeNs: 12_020_000_001 + threshold + 1) == threshold + 1,
            "a later distinct gap was not reported")

        watchdog.activate(at: 20_000_000_000)
        require(watchdog.observe(wakeNs: 19_000_000_000) == nil, "clock regression tripped")
        require(
            watchdog.observe(wakeNs: 19_000_000_000 + threshold) == nil,
            "clock regression did not reset the baseline")

        let watchdogFirst = TerminalAdmission()
        require(watchdogFirst.admit(), "watchdog-first event was rejected")
        require(!watchdogFirst.admit(), "graceful stop replaced watchdog-first event")

        let stopFirst = TerminalAdmission()
        require(stopFirst.admit(), "stop-first event was rejected")
        require(!stopFirst.admit(), "watchdog replaced stop-first event")

        print("PASS: process suspension watchdog and terminal admission")
    }
}
