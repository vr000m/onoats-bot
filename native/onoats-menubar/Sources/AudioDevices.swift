// Device display + mic picker for the menu bar — the same devices the
// capturer will bind (it captures the SYSTEM DEFAULT input; the tap follows
// default output). Shown in the UI because the first A/B parity attempt
// failed from silent wrong-device selection (dev plan ## Findings,
// 2026-06-10).
//
// The picker deliberately sets the macOS DEFAULT INPUT DEVICE (system-wide,
// affects other apps) rather than a private per-app selection: the capturer
// has no device-selection argument — it binds whatever the system default is
// at start — so the global default is the only knob that actually applies on
// the socket path (plan "option 2"; a capturer --mic-uid is the follow-up if
// the global side effect ever bothers in practice).
import CoreAudio
import Foundation

struct AudioInputDevice: Identifiable, Equatable {
    let id: AudioObjectID
    let name: String
}

enum AudioDevices {
    static func defaultInputName() -> String {
        deviceName(defaultDeviceID(input: true))
    }

    static func defaultOutputName() -> String {
        deviceName(defaultDeviceID(input: false))
    }

    static func defaultInputID() -> AudioObjectID {
        defaultDeviceID(input: true)
    }

    /// All devices with at least one input channel, sorted by name.
    static func inputDevices() -> [AudioInputDevice] {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        let system = AudioObjectID(kAudioObjectSystemObject)
        guard AudioObjectGetPropertyDataSize(system, &addr, 0, nil, &size) == noErr,
              size > 0
        else { return [] }
        var ids = [AudioObjectID](
            repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(system, &addr, 0, nil, &size, &ids) == noErr
        else { return [] }
        return ids.filter(hasInputChannels)
            .map { AudioInputDevice(id: $0, name: deviceName($0)) }
            .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
    }

    /// Set the macOS default input device (system-wide). Returns false on error.
    @discardableResult
    static func setDefaultInput(_ id: AudioObjectID) -> Bool {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultInputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var device = id
        return AudioObjectSetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil,
            UInt32(MemoryLayout<AudioObjectID>.size), &device) == noErr
    }

    // ------------------------------------------------------------- internals

    private static func defaultDeviceID(input: Bool) -> AudioObjectID {
        var addr = AudioObjectPropertyAddress(
            mSelector: input
                ? kAudioHardwarePropertyDefaultInputDevice
                : kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var deviceID = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        let err = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &deviceID)
        return err == noErr ? deviceID : AudioObjectID(kAudioObjectUnknown)
    }

    private static func hasInputChannels(_ id: AudioObjectID) -> Bool {
        var addr = AudioObjectPropertyAddress(
            mSelector: kAudioDevicePropertyStreamConfiguration,
            mScope: kAudioDevicePropertyScopeInput,
            mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr,
              size > 0
        else { return false }
        let raw = UnsafeMutableRawPointer.allocate(
            byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw) == noErr
        else { return false }
        let abl = UnsafeMutableAudioBufferListPointer(
            raw.assumingMemoryBound(to: AudioBufferList.self))
        return abl.reduce(0) { $0 + Int($1.mNumberChannels) } > 0
    }

    private static func deviceName(_ deviceID: AudioObjectID) -> String {
        guard deviceID != kAudioObjectUnknown else { return "<none>" }
        var nameAddr = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var cfStr: CFString?
        var strSize = UInt32(MemoryLayout<CFString?>.size)
        let err = withUnsafeMutablePointer(to: &cfStr) {
            AudioObjectGetPropertyData(deviceID, &nameAddr, 0, nil, &strSize, $0)
        }
        guard err == noErr, let s = cfStr else { return "<unknown>" }
        return s as String
    }
}
