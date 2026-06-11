// Default-device names for the menu bar — the same devices the capturer will
// bind (it captures the SYSTEM DEFAULT input; the tap follows default output).
// Shown in the UI because the first A/B parity attempt failed from silent
// wrong-device selection (dev plan ## Findings, 2026-06-10).
import CoreAudio
import Foundation

enum AudioDevices {
    static func defaultInputName() -> String { defaultDeviceName(input: true) }
    static func defaultOutputName() -> String { defaultDeviceName(input: false) }

    private static func defaultDeviceName(input: Bool) -> String {
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
        guard err == noErr, deviceID != kAudioObjectUnknown else { return "<none>" }

        var nameAddr = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain)
        var cfStr: CFString?
        var strSize = UInt32(MemoryLayout<CFString?>.size)
        let nameErr = withUnsafeMutablePointer(to: &cfStr) {
            AudioObjectGetPropertyData(deviceID, &nameAddr, 0, nil, &strSize, $0)
        }
        guard nameErr == noErr, let s = cfStr else { return "<unknown>" }
        return s as String
    }
}
