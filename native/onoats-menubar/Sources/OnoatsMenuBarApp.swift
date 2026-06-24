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
        // A live capture warning (all-zero input — likely a denied grant or a
        // muted mic) changes the icon so the anomaly is visible without
        // opening the menu. Warning, not failure: the session keeps running.
        case .running:
            // An external stop in flight reuses the draining glyph so the icon
            // tracks the menu's "stopping…" affordance.
            if model.stopRequested { return "ellipsis.circle" }
            return model.warning == nil
                ? "waveform.circle.fill" : "waveform.badge.exclamationmark"
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
        // Live capture warning (schema-v2 `warning`): the branch-specific hint
        // from the capturer's all-zero-input detector. Cleared automatically
        // when real audio re-arms the detector. Native menu items render one
        // line and never wrap, and the full hint is ~200 chars — rendered as
        // ONE item it stretches the whole menu to its width (observed live,
        // 2026-06-11). Split on the hint's em-dash clause breaks into stacked
        // caption lines instead; the unsplit text stays in `onoats status`
        // and the log.
        if let warning = model.warning {
            let lines = warning.components(separatedBy: " — ")
            ForEach(Array(lines.enumerated()), id: \.offset) { i, line in
                Text(i == 0 ? "⚠ \(line)" : "   \(line)").font(.caption)
            }
        }
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
            // Stop is enabled for ALL identity-verified live sessions (parity
            // with Flush, which already reaches external sessions). Route on the
            // `ours` value the enum already carries — owned sessions keep the
            // in-handle `p.terminate()`; verified external/orphaned sessions go
            // through `onoats stop` (the CLI's identity-checked SIGTERM). The
            // cosmetic `stopRequested` flag (set synchronously inside
            // `stopExternal`) disables the button for the external drain window.
            Button("Stop") {
                if ours { model.stop() } else { model.stopExternal() }
            }
            .disabled(model.stopRequested)
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
            // External stop in flight: show "stopping…" (not "Stopped") for the
            // whole drain window — `refresh()` flips to `.stopped` only once the
            // supervisor actually exits, so this never fakes a terminal state.
            if model.stopRequested {
                return "Onoats: stopping (draining)…"
            }
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
