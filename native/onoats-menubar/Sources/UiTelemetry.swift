// Strict, display-only consumer for src/onoats/ui_telemetry.py.
//
// Recorder liveness never comes from this file.  Callers must first establish
// a live pid-file identity and pass that identity to `decode`; the returned
// value is only decoration for that already-live recorder.
import Foundation

enum UiTelemetryContract {
    static let schemaVersion = 2
    static let fileName = "onoats.ui.json"
    static let maxWriteHz = 4.0
    static let heartbeatSeconds = 2.0
    static let snapshotExpirySeconds: TimeInterval = 5.0
    static let audioObservationExpirySeconds: TimeInterval = 1.0
    static let audioObservationRefreshSeconds = 0.5
    static let transcriptLimit = 240
    static let detailLimit = 256
    static let maxSnapshotBytes = 65536
    static let levelMin = 0.0
    static let levelMax = 1.0
    static let levelMinimum = levelMin
    static let levelMaximum = levelMax
    static let seminarCategory = "seminars"
    static let transcriptRunIDPattern = "^[0-9a-f]{32}$"
    static let sttStates = ["failed", "recovering", "starting", "unknown", "ready"]
    static let sttPrecedence = ["failed", "recovering", "starting", "unknown", "ready"]
    static let rootRequiredFields: Set<String> = [
        "schema", "pid", "start_epoch", "transcript_run_id", "updated_at", "audio", "stt",
    ]
    static let rootOptionalFields: Set<String> = ["transcript", "locked_category"]
    static let audioFields: Set<String> = ["level", "observed_at"]
    static let sttFields: Set<String> = ["me", "them", "aggregate"]
    static let sttBranchRequiredFields: Set<String> = ["state"]
    static let sttBranchOptionalFields: Set<String> = ["detail"]
    static let transcriptRequiredFields: Set<String> = ["text"]
    static let transcriptOptionalFields: Set<String> = ["source", "speaker_label"]
}

enum UiSttState: String, Decodable, CaseIterable {
    case failed, recovering, starting, unknown, ready
}

struct UiTelemetryActivity: Equatable {
    let level: Double
    let observedAt: Date
}

struct UiTelemetrySttBranch: Equatable {
    let state: UiSttState
    let detail: String?
}

struct UiTelemetryTranscript: Equatable {
    let text: String
    let source: String?
    let speakerLabel: String?
}

func transcriptDisplayLabel(source: String?, speakerLabel: String?) -> String {
    if let speakerLabel, !speakerLabel.isEmpty { return speakerLabel }
    if source == "me" { return "Me" }
    if source == "them" { return "Them" }
    return "Transcript"
}

struct UiTelemetrySnapshot: Equatable {
    let pid: Int32
    let startEpoch: Double
    let transcriptRunID: String
    let updatedAt: Date
    /// nil means that branch has no observation or its observation is stale.
    let me: UiTelemetryActivity?
    let them: UiTelemetryActivity?
    let meStt: UiTelemetrySttBranch
    let themStt: UiTelemetrySttBranch
    let aggregateStt: UiSttState
    let transcript: UiTelemetryTranscript?
    let lockedCategory: String?
}

enum UiTelemetryDecoder {
    private struct WireAudio: Decodable {
        let level: Double
        let observed_at: Double
    }

    private struct WireSttBranch: Decodable {
        let state: String
        let detail: String?
    }

    private struct WireStt: Decodable {
        let me: WireSttBranch
        let them: WireSttBranch
        let aggregate: String
    }

    private struct WireTranscript: Decodable {
        let text: String
        let source: String?
        let speaker_label: String?
    }

    private struct WireRecord: Decodable {
        let schema: Int
        let pid: Int32
        let start_epoch: Double
        let transcript_run_id: String
        let updated_at: Double
        let audio: [String: WireAudio?]
        let stt: WireStt
        let transcript: WireTranscript?
        let locked_category: String?
    }

    private static func scalars(_ value: String) -> Int {
        value.unicodeScalars.count
    }

    private static func validTranscriptRunID(_ value: String) -> Bool {
        guard let expression = try? NSRegularExpression(
            pattern: UiTelemetryContract.transcriptRunIDPattern
        ) else { return false }
        let range = NSRange(value.startIndex..<value.endIndex, in: value)
        guard let match = expression.firstMatch(in: value, range: range) else {
            return false
        }
        return match.range == range
    }

    private static func exactKeys(
        _ object: Any?, required: Set<String>, optional: Set<String> = []
    ) -> Bool {
        guard let dictionary = object as? [String: Any] else { return false }
        let keys = Set(dictionary.keys)
        return required.isSubset(of: keys) && keys.isSubset(of: required.union(optional))
    }

    private static func validateShape(_ data: Data) -> Bool {
        guard let root = try? JSONSerialization.jsonObject(with: data),
              exactKeys(
                root,
                required: UiTelemetryContract.rootRequiredFields,
                optional: UiTelemetryContract.rootOptionalFields
              ),
              let dictionary = root as? [String: Any],
              let audio = dictionary["audio"] as? [String: Any],
              Set(audio.keys) == Set(["me", "them"]),
              let stt = dictionary["stt"],
              exactKeys(stt, required: UiTelemetryContract.sttFields),
              let sttDictionary = stt as? [String: Any],
              exactKeys(
                sttDictionary["me"],
                required: UiTelemetryContract.sttBranchRequiredFields,
                optional: UiTelemetryContract.sttBranchOptionalFields),
              exactKeys(
                sttDictionary["them"],
                required: UiTelemetryContract.sttBranchRequiredFields,
                optional: UiTelemetryContract.sttBranchOptionalFields)
        else { return false }

        for branch in ["me", "them"] {
            if audio[branch] is NSNull { continue }
            guard exactKeys(audio[branch], required: UiTelemetryContract.audioFields) else {
                return false
            }
        }
        if let transcript = dictionary["transcript"], !(transcript is NSNull),
           !exactKeys(
             transcript,
             required: UiTelemetryContract.transcriptRequiredFields,
             optional: UiTelemetryContract.transcriptOptionalFields
           ) {
            return false
        }
        return true
    }

    static func aggregate(_ me: UiSttState, _ them: UiSttState) -> UiSttState {
        for raw in UiTelemetryContract.sttPrecedence {
            guard let state = UiSttState(rawValue: raw) else { continue }
            if me == state || them == state { return state }
        }
        return .unknown
    }

    /// Decode only telemetry belonging to the already-verified live recorder.
    /// Any contract failure returns nil and therefore degrades the UI to its
    /// neutral/unavailable presentation.
    static func decode(
        _ data: Data,
        expectedPid: Int32,
        expectedStartEpoch: Double,
        now: Date = Date()
    ) -> UiTelemetrySnapshot? {
        guard validateShape(data),
              let wire = try? JSONDecoder().decode(WireRecord.self, from: data),
              wire.schema == UiTelemetryContract.schemaVersion,
              wire.pid > 0,
              wire.pid == expectedPid,
              wire.start_epoch.isFinite,
              wire.start_epoch == expectedStartEpoch,
              validTranscriptRunID(wire.transcript_run_id),
              wire.updated_at.isFinite
        else { return nil }

        let nowEpoch = now.timeIntervalSince1970
        let snapshotAge = nowEpoch - wire.updated_at
        guard snapshotAge <= UiTelemetryContract.snapshotExpirySeconds,
              wire.updated_at <= nowEpoch + UiTelemetryContract.snapshotExpirySeconds else {
            return nil
        }

        func sttBranch(_ value: WireSttBranch) -> UiTelemetrySttBranch? {
            guard let state = UiSttState(rawValue: value.state) else { return nil }
            if let detail = value.detail,
               scalars(detail) > UiTelemetryContract.detailLimit { return nil }
            return UiTelemetrySttBranch(state: state, detail: value.detail)
        }
        guard let meStt = sttBranch(wire.stt.me),
              let themStt = sttBranch(wire.stt.them),
              let publishedAggregate = UiSttState(rawValue: wire.stt.aggregate),
              publishedAggregate == aggregate(meStt.state, themStt.state)
        else { return nil }

        func activity(_ value: WireAudio?) -> UiTelemetryActivity? {
            guard let value,
                  value.level.isFinite,
                  value.level >= UiTelemetryContract.levelMin,
                  value.level <= UiTelemetryContract.levelMax,
                  value.observed_at.isFinite
            else { return nil }
            let age = nowEpoch - value.observed_at
            guard age >= 0,
                  age <= UiTelemetryContract.audioObservationExpirySeconds else {
                return nil
            }
            return UiTelemetryActivity(
                level: value.level == 0 ? 0 : value.level,
                observedAt: Date(timeIntervalSince1970: value.observed_at)
            )
        }

        // Invalid numeric observations reject the whole snapshot. Missing,
        // old, or future observations become per-branch nil. A future branch
        // timestamp can be a valid pre-rollback observation retained across a
        // heartbeat after the wall clock moved backwards; it must not hide STT,
        // category, or transcript state from the otherwise-current snapshot.
        for value in [wire.audio["me"] ?? nil, wire.audio["them"] ?? nil] {
            if let value,
               (!value.level.isFinite
                || value.level < UiTelemetryContract.levelMin
                || value.level > UiTelemetryContract.levelMax
                || !value.observed_at.isFinite) {
                return nil
            }
        }

        var transcript: UiTelemetryTranscript?
        if let value = wire.transcript {
            guard !value.text.isEmpty,
                  scalars(value.text) <= UiTelemetryContract.transcriptLimit,
                  value.source == nil || value.source == "me" || value.source == "them",
                  value.speaker_label.map({ scalars($0) <= UiTelemetryContract.detailLimit })
                    ?? true
            else { return nil }
            transcript = UiTelemetryTranscript(
                text: value.text, source: value.source, speakerLabel: value.speaker_label)
        }
        if let category = wire.locked_category,
           category.isEmpty || scalars(category) > UiTelemetryContract.detailLimit {
            return nil
        }

        return UiTelemetrySnapshot(
            pid: wire.pid,
            startEpoch: wire.start_epoch,
            transcriptRunID: wire.transcript_run_id,
            updatedAt: Date(timeIntervalSince1970: wire.updated_at),
            me: activity(wire.audio["me"] ?? nil),
            them: activity(wire.audio["them"] ?? nil),
            meStt: meStt,
            themStt: themStt,
            aggregateStt: publishedAggregate,
            transcript: transcript,
            lockedCategory: wire.locked_category
        )
    }
}
