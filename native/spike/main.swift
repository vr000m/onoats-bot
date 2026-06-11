// onoats Phase-4 Pre-req spike — THROWAWAY verification code, not production.
//
// One signed binary, several modes, exercising the two unverified Apple-platform
// premises the dev plan gates Phase 4 on:
//
//   Spike 3 (TCC persistence): `tcc` mode requests the mic grant (AVFoundation)
//     and triggers the system-audio grant (by creating + destroying a global
//     Core Audio process tap), then prints a PASS/FAIL for each. Fast, so the
//     rebuild-+-resign ×3 persistence loop is quick.
//
//   Spike 4 (Core Audio tap recipe): `tap` proves a global process tap + private
//     aggregate device yields a real system-output stream from other apps without
//     muting them, then tears everything down. `concurrent` runs the tap alongside
//     mic capture to prove they coexist. `list-aggregates` enumerates leftover
//     private aggregates (residue check for start/kill/start ×3).
//
// Build with `make build`; this is plain swiftc, not SwiftPM. Expect to iterate on
// the Core Audio API surface — it is macOS 14.4+ only and lightly documented.
//
// References: Apple `AudioHardwareCreateProcessTap` / `CATapDescription`
// (CoreAudio/AudioHardwareTapping.h, macOS 14.4+); the AudioCap sample's
// `stereoGlobalTapButExcludeProcesses: []` global-tap pattern; Apple TN2206
// (codesign designated-requirement stability).

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

// MARK: - small helpers

func log(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

// GUI launches (`open Onoats.app`) are detached — stderr/stdout go nowhere. Append
// every result line to a fixed file so a GUI-rooted run (the only faithful TCC test
// for the menu-bar topology) is observable: `cat` the path below. Lives under the
// user's own ~/Library/Logs (not world-writable /tmp, where a fixed name is a
// symlink-planting target on a multi-user box).
let RESULT_FILE = NSHomeDirectory() + "/Library/Logs/Onoats/spike-result.txt"

func result(_ s: String) {
    let stamp = ISO8601DateFormatter().string(from: Date())
    let line = "[\(stamp)] pid=\(getpid()) \(s)\n"
    log(s)
    try? FileManager.default.createDirectory(
        atPath: (RESULT_FILE as NSString).deletingLastPathComponent,
        withIntermediateDirectories: true)
    if let fh = FileHandle(forWritingAtPath: RESULT_FILE) {
        fh.seekToEndOfFile()
        fh.write(line.data(using: .utf8)!)
        fh.closeFile()
    } else {
        try? line.data(using: .utf8)!.write(to: URL(fileURLWithPath: RESULT_FILE))
    }
}

func fourCC(_ s: OSStatus) -> String {
    // Many CoreAudio OSStatus values are packed 4-char codes; show both forms.
    let n = UInt32(bitPattern: s)
    let chars = [
        UInt8((n >> 24) & 0xFF), UInt8((n >> 16) & 0xFF),
        UInt8((n >> 8) & 0xFF), UInt8(n & 0xFF),
    ]
    let printable = chars.allSatisfy { $0 >= 0x20 && $0 < 0x7F }
    let cc = printable ? "'" + String(bytes: chars, encoding: .ascii)! + "'" : ""
    return "\(s) \(cc)"
}

@discardableResult
func ck(_ status: OSStatus, _ label: String) -> Bool {
    if status != noErr {
        log("  ✗ \(label): OSStatus \(fourCC(status))")
        return false
    }
    return true
}

let AGG_UID_PREFIX = "onoats-spike-agg-"
// Residue scan matches ANY onoats aggregate — the spike's own
// ("onoats-spike-agg-") AND the production capturer's ("onoats-capturer-agg-",
// see onoats-capturer/Sources/SystemCapture.swift) — so `list-aggregates`
// works as the leak check for both binaries.
let RESIDUE_UID_PREFIX = "onoats-"

// MARK: - microphone grant (TCC: NSMicrophoneUsageDescription)

func requestMicGrant() -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    let status = AVCaptureDevice.authorizationStatus(for: .audio)
    log("  mic authorization (pre): \(status.rawValue) " +
        "(0=notDetermined 1=restricted 2=denied 3=authorized)")
    AVCaptureDevice.requestAccess(for: .audio) { ok in
        granted = ok
        sem.signal()
    }
    sem.wait()
    log("  mic grant: \(granted ? "GRANTED" : "DENIED")")
    return granted
}

// MARK: - global process tap (TCC: system-audio capture)

/// Create a global stereo process tap with an EMPTY exclusion list — i.e. tap all
/// processes = system output. `.unmuted` so other apps keep playing through it.
/// Returns the tap AudioObjectID and its UUID string (for the aggregate's taplist).
func createGlobalTap(mute: CATapMuteBehavior = .unmuted) -> (AudioObjectID, String)? {
    let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
    desc.name = "onoats-spike-tap"
    desc.isPrivate = true
    desc.muteBehavior = mute
    log("  tap muteBehavior set to .\(mute) (raw=\(mute.rawValue))")
    var tapID = AudioObjectID(kAudioObjectUnknown)
    let err = AudioHardwareCreateProcessTap(desc, &tapID)
    guard err == noErr, tapID != kAudioObjectUnknown else {
        log("  ✗ AudioHardwareCreateProcessTap: \(fourCC(err)) (this is the call " +
            "that triggers the system-audio TCC prompt on first use)")
        return nil
    }
    log("  ✓ created global process tap id=\(tapID) uuid=\(desc.uuid.uuidString)")
    return (tapID, desc.uuid.uuidString)
}

func destroyTap(_ tapID: AudioObjectID) {
    ck(AudioHardwareDestroyProcessTap(tapID), "AudioHardwareDestroyProcessTap")
}

/// Read the tap's stream format so the IOProc knows the sample layout.
func tapFormat(_ tapID: AudioObjectID) -> AudioStreamBasicDescription? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyFormat,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    let err = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &asbd)
    guard err == noErr else {
        log("  ✗ get kAudioTapPropertyFormat: \(fourCC(err))")
        return nil
    }
    return asbd
}

// MARK: - private aggregate device wrapping the tap

func createAggregate(tapUUID: String) -> (AudioObjectID, String)? {
    let uid = "\(AGG_UID_PREFIX)\(getpid())"
    let desc: [String: Any] = [
        kAudioAggregateDeviceNameKey: "onoats-spike-agg",
        kAudioAggregateDeviceUIDKey: uid,
        kAudioAggregateDeviceIsPrivateKey: true,
        kAudioAggregateDeviceIsStackedKey: false,
        kAudioAggregateDeviceTapAutoStartKey: true,
        kAudioAggregateDeviceTapListKey: [
            [
                kAudioSubTapUIDKey: tapUUID,
                kAudioSubTapDriftCompensationKey: true,
            ]
        ],
    ]
    var aggID = AudioObjectID(kAudioObjectUnknown)
    let err = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &aggID)
    guard err == noErr, aggID != kAudioObjectUnknown else {
        log("  ✗ AudioHardwareCreateAggregateDevice: \(fourCC(err))")
        return nil
    }
    log("  ✓ created private aggregate id=\(aggID) uid=\(uid)")
    return (aggID, uid)
}

func destroyAggregate(_ aggID: AudioObjectID) {
    ck(AudioHardwareDestroyAggregateDevice(aggID), "AudioHardwareDestroyAggregateDevice")
}

// MARK: - residue check

func aggregateUID(_ devID: AudioObjectID) -> String? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyDeviceUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var cfStr: CFString? = nil
    let err = withUnsafeMutablePointer(to: &cfStr) {
        AudioObjectGetPropertyData(devID, &addr, 0, nil, &size, $0)
    }
    guard err == noErr, let s = cfStr else { return nil }
    return s as String
}

/// Enumerate live process taps via kAudioHardwarePropertyTapList. Unlike private
/// aggregate devices (auto-reclaimed on process death), process taps appear to
/// SURVIVE a SIGKILL — so a force-killed capturer leaks its tap, and enough leaks
/// make AudioHardwareCreateProcessTap return noErr with kAudioObjectUnknown.
func tapList() -> [AudioObjectID] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyTapList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard ck(AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size),
        "get tap-list size") else { return [] }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    if count == 0 { return [] }
    var taps = [AudioObjectID](repeating: 0, count: count)
    guard ck(AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &taps),
        "get tap list") else { return [] }
    return taps
}

func listTaps() {
    let taps = tapList()
    log("  process taps present: \(taps.count) \(taps)")
    print(taps.isEmpty ? "TAPS: none" : "TAPS: \(taps.count) leaked \(taps)")
}

/// Destroy ALL live process taps. On this machine nothing but our spike creates
/// taps, so a blanket sweep is the cleanup. (Phase 4 will instead sweep only taps
/// matching our name on capturer startup, and rely on SIGTERM-graceful teardown.)
func cleanTaps() {
    let taps = tapList()
    var destroyed = 0
    for t in taps {
        if AudioHardwareDestroyProcessTap(t) == noErr { destroyed += 1 }
    }
    log("  destroyed \(destroyed)/\(taps.count) process taps")
    print("CLEANED \(destroyed)/\(taps.count) taps")
}

func listAggregates() {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard ck(AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size),
        "get device-list size") else { return }
    let count = Int(size) / MemoryLayout<AudioObjectID>.size
    var devices = [AudioObjectID](repeating: 0, count: count)
    guard ck(AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &devices),
        "get device list") else { return }
    var found = 0
    for dev in devices {
        if let uid = aggregateUID(dev), uid.hasPrefix(RESIDUE_UID_PREFIX) {
            log("  RESIDUE: leftover aggregate id=\(dev) uid=\(uid)")
            found += 1
        }
    }
    if found == 0 {
        log("  clean: no \(RESIDUE_UID_PREFIX)* aggregate devices present")
    }
    print(found == 0 ? "RESIDUE: none" : "RESIDUE: \(found) leftover")
}

// MARK: - run the tap and measure that real audio arrives

/// Install an IOProc on the aggregate, run for `seconds`, and report frames seen
/// and peak level so we can prove a real system-output stream (not silence).
/// Returns (framesSeen, peak).
func runTapTimed(aggID: AudioObjectID, format: AudioStreamBasicDescription,
                 seconds: Double) -> (Int, Float, Date) {
    var frames = 0
    var peak: Float = 0
    let isFloat = (format.mFormatFlags & kAudioFormatFlagIsFloat) != 0
    let lock = NSLock()

    var procID: AudioDeviceIOProcID?
    let createErr = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) {
        (_, inInputData, _, _, _) in
        let abl = inInputData.pointee
        // Single-buffer fast path; tap output is interleaved float by default.
        let buffers = UnsafeBufferPointer<AudioBuffer>(
            start: withUnsafePointer(to: abl.mBuffers) { $0 },
            count: Int(abl.mNumberBuffers))
        var localPeak: Float = 0
        var localFrames = 0
        for buf in buffers {
            guard let data = buf.mData else { continue }
            let byteCount = Int(buf.mDataByteSize)
            if isFloat {
                let n = byteCount / MemoryLayout<Float>.size
                let p = data.bindMemory(to: Float.self, capacity: n)
                for i in 0..<n { localPeak = max(localPeak, abs(p[i])) }
                localFrames += n
            } else {
                let n = byteCount / MemoryLayout<Int16>.size
                let p = data.bindMemory(to: Int16.self, capacity: n)
                for i in 0..<n {
                    localPeak = max(localPeak, abs(Float(p[i]) / 32768.0))
                }
                localFrames += n
            }
        }
        lock.lock()
        frames += localFrames
        peak = max(peak, localPeak)
        lock.unlock()
    }
    guard createErr == noErr, let proc = procID else {
        log("  ✗ AudioDeviceCreateIOProcIDWithBlock: \(fourCC(createErr))")
        return (0, 0, Date())
    }
    guard ck(AudioDeviceStart(aggID, proc), "AudioDeviceStart") else {
        AudioDeviceDestroyIOProcID(aggID, proc)
        return (0, 0, Date())
    }
    let tStart = Date()  // IOProc is now draining the tap → output resumes here
    log("  ▶ capturing system output for \(seconds)s — PLAY AUDIO NOW (music, a " +
        "video, anything) so we can prove a real stream arrives…")
    Thread.sleep(forTimeInterval: seconds)
    ck(AudioDeviceStop(aggID, proc), "AudioDeviceStop")
    ck(AudioDeviceDestroyIOProcID(aggID, proc), "AudioDeviceDestroyIOProcID")
    lock.lock(); let f = frames; let pk = peak; lock.unlock()
    return (f, pk, tStart)
}

// MARK: - modes

func modeTCC() -> Int32 {
    log("== TCC spike (mic + system-audio grants) ==")
    // Record the PRE status: for a freshly-attributed bundle id that has never been
    // granted, this is 0 (notDetermined) and a prompt fires. A non-zero pre-status
    // on a brand-new bundle means TCC attributed the request to a DIFFERENT identity
    // (e.g. the launching terminal) — the false-pass confound to watch for.
    let preMic = AVCaptureDevice.authorizationStatus(for: .audio).rawValue
    let mic = requestMicGrant()
    log("  triggering system-audio grant via process-tap creation…")
    var sysGrant = false
    if let (tapID, _) = createGlobalTap() {
        sysGrant = true
        destroyTap(tapID)
    }
    // GUI-launched system-audio prompts can be async — hold the process so the
    // prompt can be answered before we exit.
    Thread.sleep(forTimeInterval: 3)
    result(
        "TCC mic_pre=\(preMic) mic=\(mic ? "PASS" : "FAIL") "
            + "system=\(sysGrant ? "PASS" : "FAIL")")
    return (mic && sysGrant) ? 0 : 1
}

func modeTap(seconds: Double, mute: CATapMuteBehavior) -> Int32 {
    log("== Core Audio tap recipe spike ==")
    // Measure the "output undrained" window = from tap creation (output diverted
    // into the tap) until AudioDeviceStart (IOProc begins draining). This is the
    // audible-dropout the user hears at record-start. AudioHardwareCreateAggregate
    // Device is the slow step inside it.
    let t0 = Date()
    guard let (tapID, tapUUID) = createGlobalTap(mute: mute) else { return 1 }
    defer { destroyTap(tapID); log("  ✓ tap destroyed") }
    let tTap = Date()
    guard let fmt = tapFormat(tapID) else { return 1 }
    let tFmt = Date()
    guard let (aggID, uid) = createAggregate(tapUUID: tapUUID) else { return 1 }
    let tAgg = Date()
    defer { destroyAggregate(aggID); log("  ✓ aggregate destroyed (uid=\(uid))") }
    let (frames, peak, tStart) = runTapTimed(aggID: aggID, format: fmt, seconds: seconds)
    let window = tStart.timeIntervalSince(t0) * 1000
    log("  tap format: \(fmt.mSampleRate) Hz, \(fmt.mChannelsPerFrame) ch")
    log(String(
        format: "  TIMING createTap=%.0fms format=%.0fms createAggregate=%.0fms "
            + "IOProc+start=%.0fms | UNDRAINED WINDOW=%.0fms",
        tTap.timeIntervalSince(t0) * 1000,
        tFmt.timeIntervalSince(tTap) * 1000,
        tAgg.timeIntervalSince(tFmt) * 1000,
        tStart.timeIntervalSince(tAgg) * 1000,
        window))
    let ok = frames > 0 && peak > 0.0001
    result(
        "TAP mute=\(mute) frames=\(frames) peak=\(String(format: "%.4f", peak)) "
            + "undrained_window=\(String(format: "%.0f", window))ms "
            + "\(ok ? "PASS (real stream)" : "FAIL (silence/no frames)")")
    return ok ? 0 : 1
}

func modeConcurrent(seconds: Double, mute: CATapMuteBehavior) -> Int32 {
    log("== Concurrent mic + system-audio spike ==")
    // Mic via AVAudioEngine input tap; system via Core Audio aggregate. Prove both
    // stream together with no aggregate/clock-domain conflict.
    guard requestMicGrant() else {
        log("  mic denied — cannot run concurrency spike"); return 1
    }
    let engine = AVAudioEngine()
    let input = engine.inputNode
    var micFrames = 0
    var micPeak: Float = 0
    let micLock = NSLock()
    let micFmt = input.outputFormat(forBus: 0)
    input.installTap(onBus: 0, bufferSize: 1024, format: micFmt) { buf, _ in
        let ch = buf.floatChannelData![0]
        var p: Float = 0
        for i in 0..<Int(buf.frameLength) { p = max(p, abs(ch[i])) }
        micLock.lock(); micFrames += Int(buf.frameLength); micPeak = max(micPeak, p); micLock.unlock()
    }

    guard let (tapID, tapUUID) = createGlobalTap(mute: mute) else { return 1 }
    defer { destroyTap(tapID) }
    guard let fmt = tapFormat(tapID),
          let (aggID, _) = createAggregate(tapUUID: tapUUID) else { return 1 }
    defer { destroyAggregate(aggID) }

    do {
        try engine.start()
        log("  ✓ mic engine started")
    } catch {
        log("  ✗ engine.start: \(error)"); return 1
    }
    let (sysFrames, sysPeak, _) = runTapTimed(aggID: aggID, format: fmt, seconds: seconds)
    engine.stop()
    input.removeTap(onBus: 0)
    micLock.lock(); let mf = micFrames; let mp = micPeak; micLock.unlock()
    log("")
    let ok = mf > 0 && sysFrames > 0
    result(
        "CONCURRENT mic frames=\(mf) peak=\(String(format: "%.4f", mp)) "
            + "system frames=\(sysFrames) peak=\(String(format: "%.4f", sysPeak)) "
            + "\(ok ? "PASS" : "FAIL")")
    return ok ? 0 : 1
}

// MARK: - entrypoint

let args = CommandLine.arguments
let mode = args.count > 1 ? args[1] : "tcc"
// Ignore the supervisor's --mic-socket/--system-socket/--nonce flags for the spike;
// we only care which mode to run. The supervisor-exec harness passes mode via argv[1].
let secs: Double = {
    if let i = args.firstIndex(of: "--seconds"), i + 1 < args.count {
        return Double(args[i + 1]) ?? 8
    }
    return 8
}()

// --mute <unmuted|muted|mutedWhenTapped> (default unmuted). Lets us A/B which mute
// behavior actually keeps other apps audible while the tap captures.
let mute: CATapMuteBehavior = {
    guard let i = args.firstIndex(of: "--mute"), i + 1 < args.count else { return .unmuted }
    switch args[i + 1] {
    case "muted": return .muted
    case "mutedWhenTapped": return .mutedWhenTapped
    default: return .unmuted
    }
}()

let rc: Int32
switch mode {
case "tcc": rc = modeTCC()
case "tap": rc = modeTap(seconds: secs, mute: mute)
case "concurrent": rc = modeConcurrent(seconds: secs, mute: mute)
case "list-aggregates": listAggregates(); rc = 0
case "list-taps": listTaps(); rc = 0
case "clean-taps": cleanTaps(); rc = 0
default:
    log("usage: onoats-capturer "
        + "[tcc|tap|concurrent|list-aggregates|list-taps|clean-taps] "
        + "[--seconds N] [--mute unmuted|muted|mutedWhenTapped]")
    rc = 2
}
exit(rc)
