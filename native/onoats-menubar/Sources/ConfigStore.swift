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

struct ConfigWriteError: Error, CustomStringConvertible {
    let description: String
}

enum ConfigStore {
    // NSHomeDirectory() consults the user database, NOT the $HOME env var —
    // a HOME override does not redirect this path (verified the hard way).
    static var configURL: URL {
        URL(fileURLWithPath: NSHomeDirectory() + "/.config/onoats/config.toml")
    }

    // TOML basic-string escaping, restricted to the subset this editor speaks
    // (see writeValue): backslash and double-quote are the two characters
    // legal in macOS paths that would otherwise break out of the quoted
    // value and corrupt config.toml for the Python CLI (tomllib raises on
    // the next `onoats` command — ANY command — until the file is hand-fixed).
    private static func escape(_ s: String) -> String {
        s.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
    }

    /// Inverse of `escape`: a backslash takes the next character literally
    /// (single scan — sequential string replaces would mis-handle `\\"`).
    private static func unescape(_ s: String) -> String {
        var out = ""
        var iter = s.makeIterator()
        while let c = iter.next() {
            if c == "\\", let next = iter.next() {
                out.append(next)
            } else {
                out.append(c)
            }
        }
        return out
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
                    .trimmingCharacters(in: .whitespaces)  // tolerate hand-edited [ stt ]
                continue
            }
            guard current == section, !line.hasPrefix("#") else { continue }
            guard let eq = line.firstIndex(of: "=") else { continue }
            let lhs = String(line[..<eq]).trimmingCharacters(in: .whitespaces)
            guard lhs == key else { continue }
            var value = String(line[line.index(after: eq)...])
                .trimmingCharacters(in: .whitespaces)
            if value.hasPrefix("\"") {
                // Scan to the closing UNESCAPED quote (a `\"` written by
                // escape() must not terminate the string), then unescape.
                var body = ""
                var escaped = false
                var closed = false
                for c in value.dropFirst() {
                    if escaped {
                        body.append("\\")
                        body.append(c)
                        escaped = false
                    } else if c == "\\" {
                        escaped = true
                    } else if c == "\"" {
                        closed = true
                        break
                    } else {
                        body.append(c)
                    }
                }
                value = closed ? unescape(body) : ""
            } else if let hash = value.firstIndex(of: "#") {
                value = String(value[..<hash]).trimmingCharacters(in: .whitespaces)
            }
            return value.isEmpty ? nil : value
        }
        return nil
    }

    /// Replace (or insert) `key = "value"` inside `[section]`. Creates the
    /// file/section when missing. Atomic write; all other lines untouched.
    ///
    /// TOML subset contract (deliberately thin — this is a line editor, not a
    /// serializer): plain `[section]` headers, single-line `key = "basic
    /// string"` values with `\\`/`\"` escapes, `#` comments. NO dotted keys,
    /// arrays, multi-line strings, or literal ('…') strings — the Python side
    /// reads with full tomllib, so anything this writer emits must stay
    /// inside the subset both agree on (tests/test_native_contract_parity.py
    /// round-trips a sample through tomllib).
    static func writeValue(section: String, key: String, value: String) throws {
        // A control character (esp. newline) would let the value break out of
        // its line and inject arbitrary TOML keys — refuse, never sanitize.
        guard value.rangeOfCharacter(from: .newlines) == nil,
            value.rangeOfCharacter(from: .controlCharacters) == nil
        else {
            throw ConfigWriteError(
                description: "config value for \(section).\(key) contains a control character")
        }
        let text = (try? String(contentsOf: configURL, encoding: .utf8)) ?? ""
        var lines = text.components(separatedBy: "\n")
        let newLine = "\(key) = \"\(escape(value))\""

        var current = ""
        var sectionHeaderIdx: Int? = nil
        var keyIdx: Int? = nil
        for (i, rawLine) in lines.enumerated() {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("["), line.hasSuffix("]") {
                current = String(line.dropFirst().dropLast())
                    .trimmingCharacters(in: .whitespaces)  // tolerate hand-edited [ stt ]
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
