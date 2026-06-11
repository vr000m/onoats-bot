// Read + surgically write ~/.config/onoats/config.toml — the SAME file the
// Python CLI reads, so GUI settings and terminal sessions can never diverge.
//
// The writer is deliberately a single-key line editor, not a TOML serializer:
// it replaces (or inserts) exactly one `key = "value"` line and leaves every
// other line — comments included — byte-identical. Values are written
// double-quoted. Env vars still override at runtime (env > config.toml >
// default), but a GUI app sees no shell env, so for GUI starts config.toml is
// effectively authoritative.
import Foundation

enum ConfigStore {
    // NSHomeDirectory() consults the user database, NOT the $HOME env var —
    // a HOME override does not redirect this path (verified the hard way).
    static var configURL: URL {
        URL(fileURLWithPath: NSHomeDirectory() + "/.config/onoats/config.toml")
    }

    /// Value of `key` inside `[section]`, unquoted/comment-stripped, or nil.
    static func readValue(section: String, key: String) -> String? {
        guard let text = try? String(contentsOf: configURL, encoding: .utf8) else {
            return nil
        }
        var current = ""
        for rawLine in text.components(separatedBy: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("["), line.hasSuffix("]") {
                current = String(line.dropFirst().dropLast())
                continue
            }
            guard current == section, !line.hasPrefix("#") else { continue }
            guard let eq = line.firstIndex(of: "=") else { continue }
            let lhs = String(line[..<eq]).trimmingCharacters(in: .whitespaces)
            guard lhs == key else { continue }
            var value = String(line[line.index(after: eq)...])
                .trimmingCharacters(in: .whitespaces)
            if value.hasPrefix("\""), let close = value.dropFirst().firstIndex(of: "\"") {
                value = String(value[value.index(after: value.startIndex)..<close])
            } else if let hash = value.firstIndex(of: "#") {
                value = String(value[..<hash]).trimmingCharacters(in: .whitespaces)
            }
            return value.isEmpty ? nil : value
        }
        return nil
    }

    /// Replace (or insert) `key = "value"` inside `[section]`. Creates the
    /// file/section when missing. Atomic write; all other lines untouched.
    static func writeValue(section: String, key: String, value: String) throws {
        let text = (try? String(contentsOf: configURL, encoding: .utf8)) ?? ""
        var lines = text.components(separatedBy: "\n")
        let newLine = "\(key) = \"\(value)\""

        var current = ""
        var sectionHeaderIdx: Int? = nil
        var keyIdx: Int? = nil
        for (i, rawLine) in lines.enumerated() {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("["), line.hasSuffix("]") {
                current = String(line.dropFirst().dropLast())
                if current == section { sectionHeaderIdx = i }
                continue
            }
            guard current == section, !line.hasPrefix("#") else { continue }
            if let eq = line.firstIndex(of: "="),
               String(line[..<eq]).trimmingCharacters(in: .whitespaces) == key {
                keyIdx = i
                break
            }
        }

        if let i = keyIdx {
            lines[i] = newLine
        } else if let header = sectionHeaderIdx {
            lines.insert(newLine, at: header + 1)
        } else {
            if !text.isEmpty, lines.last?.isEmpty == false { lines.append("") }
            lines.append("[\(section)]")
            lines.append(newLine)
            lines.append("")
        }

        try FileManager.default.createDirectory(
            at: configURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try lines.joined(separator: "\n")
            .write(to: configURL, atomically: true, encoding: .utf8)
    }
}
