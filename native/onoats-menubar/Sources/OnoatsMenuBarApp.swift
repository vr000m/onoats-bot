// Onoats menu-bar launcher (Phase 5b) — LSUIElement MenuBarExtra wrapping the
// `onoats` CLI. GUI launch makes Onoats.app the TCC responsible process, which
// is the entire reason this app exists (terminal launches attribute the mic +
// system-audio grants to the terminal and cost ~2-3 s of tap-creation dropout).
import AppKit
import CoreAudio
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
        if let note = model.flushNote {
            Text("⚠ \(note)")
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

        // Flat inline pickers (no submenus — SwiftUI MenuBarExtra submenus
        // lose hover focus and vanish; the Sound-menu pattern keeps every
        // option visible in the main menu). The mic picker sets the macOS
        // DEFAULT input device (system-wide) — the only selection that
        // actually applies on the socket path, since the capturer binds the
        // system default at start. A running session keeps its device.
        Picker("Mic (me) — sets macOS default input", selection: micSelection) {
            ForEach(model.inputDevices) { device in
                Text(device.name).tag(device.id)
            }
        }
        .pickerStyle(.inline)

        Text("System (them): \(model.outputDevice)")
        if let stt = model.sttLabel {
            Text("STT: \(stt)")
        }

        Divider()

        // Writes config.toml — shared with the CLI. Applies on next Start.
        Picker("STT service", selection: sttSelection) {
            ForEach(RecorderModel.sttServices, id: \.self) { service in
                Text(service).tag(service)
            }
        }
        .pickerStyle(.inline)

        Divider()

        Text("Data dir: \(model.dataDirDisplay)")
        Button("Change data dir…") { model.chooseDataDir() }
        Button("Open config.toml…") { model.openConfig() }
        if case .running = model.state {
            Text("Config changes apply on next Start")
        }

        Divider()

        Text("CLI: \(model.cliPath)\(model.cliAvailable ? "" : "  ⚠ not found — run `make -C native install-cli`")")
            .font(.caption)
        // Routed through the model: stops a GUI-started session first so Quit
        // never orphans it as an unstoppable external session on relaunch.
        Button(quitLabel) { model.quitApp() }
    }

    private var quitLabel: String {
        if case .running(ours: true) = model.state { return "Stop & Quit Onoats" }
        return "Quit Onoats"
    }

    private var micSelection: Binding<AudioObjectID> {
        Binding(
            get: { model.defaultInputID },
            set: { id in
                if let device = model.inputDevices.first(where: { $0.id == id }) {
                    model.setMicDevice(device)
                }
            })
    }

    private var sttSelection: Binding<String> {
        Binding(
            get: { model.sttService },
            set: { model.setSTTService($0) })
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
