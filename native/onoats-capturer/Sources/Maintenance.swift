// Maintenance subcommands: enumerate / sweep Core Audio residue after hard
// kills. Used by native/residue_check.sh (manual-smoke step 8) to assert that
// no aggregate device or process tap survives a kill -9 of the capturer.
// Ported from the retired native/spike/ kit (recipe proven in Pre-req spike 4,
// preserved at the `spike-archive` tag).
//
// Verdict lines (`RESIDUE: …` / `TAPS: …` / `CLEANED …`) go to STDOUT — they
// are the machine-greppable contract residue_check.sh asserts on; diagnostic
// detail goes through logLine (stderr) like everything else.

import AudioToolbox
import CoreAudio
import Foundation

// Match ANY onoats aggregate, not just this binary's AGGREGATE_UID_PREFIX
// ("onoats-capturer-agg-") — a broad scan keeps the residue check honest if
// another onoats-* variant ever leaks one.
private let RESIDUE_UID_PREFIX = "onoats-"

@discardableResult
private func ck(_ status: OSStatus, _ label: String) -> Bool {
    if status != noErr {
        logLine("✗ \(label): OSStatus \(fourCC(status))")
        return false
    }
    return true
}

private func aggregateUID(_ devID: AudioObjectID) -> String? {
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

/// Enumerate live process taps via kAudioHardwarePropertyTapList. Unlike
/// private aggregate devices (auto-reclaimed on process death), process taps
/// SURVIVE a SIGKILL — so a force-killed capturer leaks its tap, and enough
/// leaks make AudioHardwareCreateProcessTap return noErr + kAudioObjectUnknown.
private func tapList() -> [AudioObjectID] {
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
    logLine("process taps present: \(taps.count) \(taps)")
    print(taps.isEmpty ? "TAPS: none" : "TAPS: \(taps.count) leaked \(taps)")
}

/// Destroy ALL live process taps. Nothing but onoats creates taps on a dev
/// machine, so a blanket sweep is the cleanup; normal sessions never need it
/// (graceful teardown destroys the tap).
func cleanTaps() {
    let taps = tapList()
    var destroyed = 0
    for t in taps {
        if AudioHardwareDestroyProcessTap(t) == noErr { destroyed += 1 }
    }
    logLine("destroyed \(destroyed)/\(taps.count) process taps")
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
    if count == 0 {
        print("RESIDUE: none")
        return
    }
    var devices = [AudioObjectID](repeating: 0, count: count)
    guard ck(AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &devices),
        "get device list") else { return }
    var found = 0
    for dev in devices {
        if let uid = aggregateUID(dev), uid.hasPrefix(RESIDUE_UID_PREFIX) {
            logLine("RESIDUE: leftover aggregate id=\(dev) uid=\(uid)")
            found += 1
        }
    }
    if found == 0 {
        logLine("clean: no \(RESIDUE_UID_PREFIX)* aggregate devices present")
    }
    print(found == 0 ? "RESIDUE: none" : "RESIDUE: \(found) leftover")
}
