import Foundation

private struct HarnessFailure: Error, CustomStringConvertible {
    let description: String
}

private func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() { throw HarnessFailure(description: message) }
}

private func fixtureURL(_ root: URL, _ name: String) -> URL {
    root.appendingPathComponent(name)
}

private func mixedScalarString(count: Int) -> String {
    // Six Unicode scalars: combining mark, astral characters, and a ZWJ sequence.
    let motif = Array("e\u{301}🙂👩\u{200D}💻".unicodeScalars)
    var scalars = String.UnicodeScalarView()
    for index in 0..<count { scalars.append(motif[index % motif.count]) }
    return String(scalars)
}

private func baseSnapshot(
    transcript: String? = nil, detail: String? = nil,
    speakerLabel: String? = nil, lockedCategory: String? = nil
) -> [String: Any] {
    let detailValue: Any = detail.map { $0 as Any } ?? NSNull()
    let meStt: [String: Any] = ["state": "ready", "detail": detailValue]
    let themStt: [String: Any] = ["state": "ready", "detail": NSNull()]
    var root: [String: Any] = [
        "schema": 1,
        "pid": 4242,
        "start_epoch": 1_699_999_900.0,
        "updated_at": 1_700_000_000.0,
        "audio": ["me": NSNull(), "them": NSNull()],
        "stt": [
            "me": meStt,
            "them": themStt,
            "aggregate": "ready",
        ],
    ]
    if let transcript {
        var preview: [String: Any] = ["text": transcript, "source": "them"]
        if let speakerLabel { preview["speaker_label"] = speakerLabel }
        root["transcript"] = preview
    }
    if let lockedCategory { root["locked_category"] = lockedCategory }
    return root
}

private func decode(_ snapshot: [String: Any]) throws -> UiTelemetrySnapshot? {
    let data = try JSONSerialization.data(withJSONObject: snapshot, options: [.sortedKeys])
    return UiTelemetryDecoder.decode(
        data,
        expectedPid: 4242,
        expectedStartEpoch: 1_699_999_900.0,
        now: Date(timeIntervalSince1970: 1_700_000_000.0)
    )
}

private func runDecoderCases(_ root: URL) throws {
    let data = try Data(contentsOf: fixtureURL(root, "decoder_cases.json"))
    guard let cases = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
        throw HarnessFailure(description: "decoder_cases.json is not an array")
    }
    for item in cases {
        guard let name = item["name"] as? String,
              let expected = item["accepted"] as? Bool,
              let ownerAlive = item["owner_alive"] as? Bool,
              let ownerPid = item["owner_pid"] as? NSNumber,
              let ownerEpoch = item["owner_start_epoch"] as? NSNumber,
              let now = item["now"] as? NSNumber,
              let snapshot = item["snapshot"] as? [String: Any]
        else { throw HarnessFailure(description: "invalid decoder case metadata") }

        let snapshotData = try JSONSerialization.data(
            withJSONObject: snapshot, options: [.sortedKeys])
        // Liveness is the caller's gate. A dead owner must not even offer the
        // display-only snapshot to the decoder.
        let decoded = ownerAlive
            ? UiTelemetryDecoder.decode(
                snapshotData,
                expectedPid: ownerPid.int32Value,
                expectedStartEpoch: ownerEpoch.doubleValue,
                now: Date(timeIntervalSince1970: now.doubleValue))
            : nil
        try require((decoded != nil) == expected, "\(name): acceptance mismatch")

        if name == "stale-me-only", let decoded {
            try require(decoded.me == nil, "stale Me observation must become No signal")
            try require(decoded.them?.level == 0.75, "fresh Them observation was dropped")
            try require(decoded.aggregateStt == .ready, "branch expiry changed STT state")
        }
        if name == "level-boundaries", let decoded {
            try require(decoded.me?.level == 0.0, "negative zero was not canonicalised")
            try require(decoded.them?.level == 1.0, "inclusive upper bound rejected")
        }
    }
}

private func runRawRejections(_ root: URL) throws {
    for name in ["malformed.json", "nonfinite_nan.json", "nonfinite_infinity.json"] {
        let data = try Data(contentsOf: fixtureURL(root, name))
        let decoded = UiTelemetryDecoder.decode(
            data,
            expectedPid: 4242,
            expectedStartEpoch: 1_699_999_900.0,
            now: Date(timeIntervalSince1970: 1_700_000_000.0))
        try require(decoded == nil, "\(name): raw invalid JSON was accepted")
    }
    for name in ["nonfinite_nan.json", "nonfinite_infinity.json"] {
        let raw = try String(contentsOf: fixtureURL(root, name), encoding: .utf8)
        try require(!raw.contains("\"now\""), "\(name): test clock leaked into payload")
        let token = name.contains("nan") ? "NaN" : "Infinity"
        let repaired = raw.replacingOccurrences(of: token, with: "0.5")
        let decoded = UiTelemetryDecoder.decode(
            Data(repaired.utf8), expectedPid: 4242,
            expectedStartEpoch: 1_699_999_900.0,
            now: Date(timeIntervalSince1970: 1_700_000_000.0))
        try require(decoded != nil, "\(name): fixture has an invalidity beyond \(token)")
    }
}

private func runStringBoundaries(_ root: URL) throws {
    let data = try Data(contentsOf: fixtureURL(root, "string_boundaries.json"))
    guard let cases = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
        throw HarnessFailure(description: "string_boundaries.json is not an array")
    }
    for item in cases {
        guard let field = item["field"] as? String,
              let scalarCount = item["scalar_count"] as? Int,
              let expected = item["accepted"] as? Bool
        else { throw HarnessFailure(description: "invalid string boundary metadata") }
        let value = mixedScalarString(count: scalarCount)
        try require(value.unicodeScalars.count == scalarCount, "bad Unicode fixture")
        let snapshot: [String: Any]
        switch field {
        case "transcript": snapshot = baseSnapshot(transcript: value)
        case "detail": snapshot = baseSnapshot(detail: value)
        case "speaker_label":
            snapshot = baseSnapshot(transcript: "preview", speakerLabel: value)
        case "locked_category": snapshot = baseSnapshot(lockedCategory: value)
        default: throw HarnessFailure(description: "unknown string field \(field)")
        }
        let accepted = try decode(snapshot) != nil
        try require(accepted == expected,
                    "\(field)-\(scalarCount): scalar boundary mismatch")
    }
}

private func runLevelVerdicts(_ root: URL) throws {
    let data = try Data(contentsOf: fixtureURL(root, "level_verdicts.json"))
    guard let cases = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
        throw HarnessFailure(description: "level_verdicts.json is not an array")
    }
    for item in cases {
        guard let input = item["input"] as? String,
              let expected = item["accepted"] as? Bool,
              let value = Double(input)
        else { throw HarnessFailure(description: "invalid level verdict metadata") }
        let accepted = value.isFinite
            && value >= UiTelemetryContract.levelMinimum
            && value <= UiTelemetryContract.levelMaximum
        try require(accepted == expected, "\(input): producer verdict mismatch")
        if accepted && value == 0 {
            let published = (item["published"] as? NSNumber)?.doubleValue
            try require(published == 0.0, "\(input): zero must publish canonically")
        }
    }
}

private func runAllAggregatePairs() throws {
    let states = UiSttState.allCases
    let rank = Dictionary(
        uniqueKeysWithValues: UiTelemetryContract.sttPrecedence.enumerated().map {
            (UiSttState(rawValue: $0.element)!, $0.offset)
        })
    for me in states {
        for them in states {
            let expected = rank[me]! <= rank[them]! ? me : them
            try require(UiTelemetryDecoder.aggregate(me, them) == expected,
                        "aggregate mismatch for \(me)/\(them)")
        }
    }
}

private func runTranscriptDisplayLabels() throws {
    let cases: [(String?, String?, String)] = [
        ("me", nil, "Me"),
        ("them", nil, "Them"),
        (nil, nil, "Transcript"),
        ("other", nil, "Transcript"),
        ("me", "Me", "Me"),
        ("them", "Them", "Them"),
        ("them", "Them (S2)", "Them (S2)"),
        (nil, "Them (S9)", "Them (S9)"),
    ]
    for (source, speakerLabel, expected) in cases {
        try require(
            transcriptDisplayLabel(source: source, speakerLabel: speakerLabel) == expected,
            "display label mismatch for source=\(source ?? "nil") label=\(speakerLabel ?? "nil")")
    }
}

@main
struct UiTelemetryDecoderHarness {
    static func main() throws {
        guard CommandLine.arguments.count == 2 else {
            throw HarnessFailure(description: "usage: harness <fixture-dir>")
        }
        let root = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        try runDecoderCases(root)
        try runRawRejections(root)
        try runStringBoundaries(root)
        try runLevelVerdicts(root)
        try runAllAggregatePairs()
        try runTranscriptDisplayLabels()
        print("UiTelemetryDecoderHarness: ok")
    }
}
