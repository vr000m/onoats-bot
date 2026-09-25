// Onoats menu-bar launcher (Phase 5b) — LSUIElement MenuBarExtra wrapping the
// `onoats` CLI. GUI launch makes Onoats.app the TCC responsible process, which
// is the entire reason this app exists (terminal launches attribute the mic +
// system-audio grants to the terminal and cost ~2-3 s of tap-creation dropout).
import AppKit
import CoreAudio
import SwiftUI

/// App-lifecycle state that belongs to the launch itself, not to the recorder.
///
/// The login-item sync is an "every launch" checkpoint (dev plan Phase 5): it
/// reconciles `config.toml`'s `launch_at_login` against `SMAppService`'s actual
/// registration once, at startup. It has no relationship to recorder state, and
/// living on `RecorderModel` — whose job is consuming the status file and
/// backstopping pid-file liveness — was justified only by that class happening
/// to be constructed once at startup. That is a coincidence of construction
/// timing, not an ownership claim, and it put an unrelated `@Published` field
/// in front of every view that observes recorder state.
///
/// Owned by `OnoatsMenuBarApp` via `@StateObject`, so it is created exactly
/// once for the app's lifetime — the same "every launch" guarantee, from the
/// object that actually represents the launch.
@MainActor
final class LaunchState: ObservableObject {
    /// Login-item registration hint — set once at launch by
    /// `LoginItemManager.sync()`, e.g. `.requiresApproval` or a registration
    /// failure. nil once `launch_at_login` is absent, matches the registered
    /// state already, or was never configured.
    @Published var loginItemHint: String?

    init() {
        // `SMAppService`'s `.status`/`.register()`/`.unregister()` are
        // synchronous XPC round-trips to the background-task-management daemon
        // (up to 3 blocking calls) — right at login, when that daemon is
        // busiest. Run off the main actor so a slow daemon can't stall the menu
        // bar's first render; hop back only to publish the result.
        Task.detached { [weak self] in
            let hint = LoginItemManager.sync()
            guard let self else { return }
            await self.applyLoginItemHint(hint)
        }
    }

    /// Publishes the login-item sync result. A dedicated `@MainActor`-isolated
    /// method (rather than an inline `MainActor.run { self?...}` closure) so
    /// the actor hop from `Task.detached` above doesn't recapture `self` in a
    /// nested closure — that pattern is a warning today and an error under the
    /// Swift 6 language mode (`self` isn't provably `Sendable`).
    private func applyLoginItemHint(_ hint: String?) {
        loginItemHint = hint
    }
}

@main
struct OnoatsMenuBarApp: App {
    @StateObject private var model = RecorderModel()
    @StateObject private var launch = LaunchState()

    var body: some Scene {
        MenuBarExtra {
            MenuContent(model: model, launch: launch)
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

/// Break one status-hint string into the caption lines the menu renders.
///
/// The one rendering policy for every long, delimiter-joined hint this menu
/// shows, not just the schema-v2 `warning` field. It used to live inline in
/// the `warning` branch alone, so `loginItemHint` — added later, built by
/// `LoginItemManager` with the same `" — "` clause delimiter and carrying
/// unbounded text (a user-edited `config.toml` value,
/// `error.localizedDescription`) — rendered as a single unwrapped item and
/// stretched the whole menu, which is the exact problem the `warning` split
/// exists to prevent. Two producers of the same string shape must not get
/// opposite rendering because only one call site knew the rule.
///
/// `status.set_warning_branch` can merge SEVERAL branches
/// (mic/system/stt/stt-mic/stt-system) into one string, `"; "`-joined per
/// `status.format_warning_branch`, so the outer split comes first and breaks
/// per-branch entries apart; the inner `" — "` split then breaks each entry's
/// own em-dash clauses. Native menu items render one line and never wrap, and
/// a single hint can be ~200 chars (observed live, 2026-06-11).
///
/// The `"; "` literal here is under the lockstep convention documented in
/// `status.py`'s header and checked by `tests/test_status_file.py::
/// test_swift_menu_bar_splits_on_the_documented_warning_delimiter`, which
/// reads this source. The parameter is named `warning` because that test
/// matches the split structurally, against that name.
func hintCaptionLines(_ warning: String) -> [String] {
    return warning
        .components(separatedBy: "; ")
        .flatMap { $0.components(separatedBy: " — ") }
}

struct MenuContent: View {
    @ObservedObject var model: RecorderModel
    /// Launch-lifecycle state, observed separately from recorder state — the
    /// login-item hint is not a recorder field and must not arrive as one.
    @ObservedObject var launch: LaunchState

    /// One hint, rendered as stacked caption lines with a leading marker on
    /// the first. Shared by every `⚠` hint in the menu.
    @ViewBuilder
    private func hintLines(_ text: String) -> some View {
        let lines = hintCaptionLines(text)
        ForEach(Array(lines.enumerated()), id: \.offset) { i, line in
            Text(i == 0 ? "⚠ \(line)" : "   \(line)").font(.caption)
        }
    }

    var body: some View {
        Text(statusLine)
        // Live capture warning (schema-v2 `warning`): the branch-specific hint
        // from the capturer's all-zero-input detector, or an STT
        // kickstart-recovery message. Cleared automatically when the branch
        // clears. The unsplit text stays in `onoats status` and the log; see
        // `hintCaptionLines` for the splitting policy and why it is shared.
        if let warning = model.warning {
            hintLines(warning)
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
            hintLines(note)
        }
        if let hint = launch.loginItemHint {
            // `LoginItemManager` builds these with the same `" — "` clause
            // delimiter the `warning` grammar uses, and interpolates
            // unbounded text into them (a user-edited `config.toml` value,
            // `error.localizedDescription`). Same shape, same rendering.
            hintLines(hint)
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

        // Applies to the next Start only; resets when that session ends.
        // Needs "seminar" in config.toml [categories] set (else Start fails
        // with the CLI's "--category must be one of" error).
        Toggle("Seminar (next recording)", isOn: $model.seminarMode)
            .disabled({
                if case .stopped = model.state { return false }
                if case .failed = model.state { return false }
                return true
            }())

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
