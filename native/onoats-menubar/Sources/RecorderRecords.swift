// Bounded schema parsing for recorder-owned status and PID records.
import Foundation

struct StatusRecord: Decodable {
    let schema: Int
    let pid: Int32
    let start_time: Double
    let audio_source: String
    let stt_label: String
    let running: Bool
    let last_rotation_time: Double?
    let last_error: String?
    let exit_reason: String?
    let supervisor_rc: Int?
    let warning: String?
    let mic_device: String?
    let system_device: String?
}

struct RecorderPidRecord {
    let pid: Int32
    let cmdline: String
    let startEpoch: Double
}

enum StatusRecordRead {
    case missingOrInvalid
    case unsupportedSchema
    case record(StatusRecord)
}

private let recorderAuthorityRecordMaxBytes = 65_536

@MainActor
enum RecorderRecordReader {
    static func status(
        under directory: URL, expectedSchema: Int, using ops: SystemOps
    ) -> StatusRecordRead {
        let url = directory.appendingPathComponent(".active/onoats.status.json")
        guard let data = ops.readData(
            at: url, maxBytes: recorderAuthorityRecordMaxBytes
        ) else { return .missingOrInvalid }
        if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let schema = object["schema"] as? Int,
           schema != expectedSchema {
            return .unsupportedSchema
        }
        guard let record = try? JSONDecoder().decode(StatusRecord.self, from: data) else {
            return .missingOrInvalid
        }
        return .record(record)
    }

    static func pid(under directory: URL, using ops: SystemOps) -> RecorderPidRecord? {
        let url = directory.appendingPathComponent(".active/onoats.pid")
        guard let data = ops.readData(
            at: url, maxBytes: recorderAuthorityRecordMaxBytes
        ),
              let text = String(data: data, encoding: .utf8) else { return nil }
        let lines = text.trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: "\n").map { $0.trimmingCharacters(in: .whitespaces) }
        guard lines.count >= 2, lines[1] == "onoats-bot", let pid = Int32(lines[0]), pid > 0 else {
            return nil
        }
        return RecorderPidRecord(
            pid: pid,
            cmdline: lines.count >= 3 ? lines[2] : "",
            startEpoch: lines.count >= 4 ? (Double(lines[3]) ?? 0) : 0)
    }
}
