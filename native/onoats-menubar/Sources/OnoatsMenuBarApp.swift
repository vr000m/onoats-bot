// Onoats menu-bar launcher (Phase 5b) — LSUIElement MenuBarExtra wrapping the
// `onoats` CLI. GUI launch makes Onoats.app the TCC responsible process, which
// is the entire reason this app exists (terminal launches attribute the mic +
// system-audio grants to the terminal and cost ~2-3 s of tap-creation dropout).
import AppKit
import SwiftUI

@main
struct OnoatsMenuBarApp: App {
    @StateObject private var model = RecorderModel()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model)
        } label: {
            Image(systemName: menuSymbol)
        }
    }

    private var menuSymbol: String {
        switch model.state {
        case .running: return "waveform.circle.fill"
        case .starting, .stopping: return "ellipsis.circle"
        case .failed: return "exclamationmark.triangle.fill"
        case .stopped: return "waveform.circle"
        }
    }
}

struct MenuContent: View {
    @ObservedObject var model: RecorderModel

    var body: some View {
        Text(statusLine)
        if case .failed(let reason, let detail) = model.state {
            Text("Last session failed: \(reason)")
            if let detail, !detail.isEmpty {
                Text(detail).font(.caption)
            }
        }
        if model.schemaDrift {
            Text("⚠ status file schema drift — update onoats / this app")
        }

        Divider()

        // The capturer binds the system DEFAULT devices; surfacing them here is
        // the guard against silent wrong-device capture (Findings, 2026-06-10).
        Text("Mic (me): \(model.micDevice)")
        Text("System (them): \(model.outputDevice)")
        if let stt = model.sttLabel {
            Text("STT: \(stt)")
        }

        Divider()

        switch model.state {
        case .running(let ours):
            Button(ours ? "Stop" : "Stop (external session)") { model.stop() }
                .disabled(!ours)
            Button("Flush") { model.flush() }
        case .starting, .stopping:
            Button("Start") {}.disabled(true)
        case .stopped, .failed:
            Button("Start") { model.start() }
                .disabled(!model.cliAvailable)
        }

        Divider()

        Menu("Settings") {
            // Writes config.toml — shared with the CLI. Applies on next Start.
            Menu("STT service: \(model.sttService)") {
                ForEach(RecorderModel.sttServices, id: \.self) { service in
                    Button(service == model.sttService ? "✓ \(service)" : service) {
                        model.setSTTService(service)
                    }
                }
            }
            Text("Data dir: \(model.dataDirDisplay)")
            Button("Change data dir…") { model.chooseDataDir() }
            Divider()
            Button("Open config.toml…") { model.openConfig() }
            if case .running = model.state {
                Text("Changes apply on next Start")
            }
        }

        Divider()

        Text("CLI: \(model.cliPath)\(model.cliAvailable ? "" : "  ⚠ not found — run `make -C native install-cli`")")
            .font(.caption)
        Button("Quit Onoats") { NSApp.terminate(nil) }
    }

    private var statusLine: String {
        switch model.state {
        case .stopped:
            return "Onoats: stopped"
        case .starting:
            return "Onoats: starting…"
        case .stopping:
            return "Onoats: stopping (draining)…"
        case .failed:
            return "Onoats: stopped"
        case .running(let ours):
            var line = "Onoats: recording"
            if let since = model.startTime {
                line += " since \(since.formatted(date: .omitted, time: .shortened))"
            }
            if let source = model.audioSource {
                line += " (\(source))"
            }
            if !ours { line += " — started outside the menu bar" }
            return line
        }
    }
}
