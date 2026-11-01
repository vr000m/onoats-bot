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
            ControlPanelRoot(model: model, launch: launch)
        } label: {
            Image(nsImage: menuImage)
                .accessibilityLabel("Onoats")
        }
        .menuBarExtraStyle(.window)
    }

    private var menuImageName: String {
        switch model.state {
        // A live capture warning (all-zero input — likely a denied grant or a
        // muted mic) changes the icon so the anomaly is visible without
        // opening the menu. Warning, not failure: the session keeps running.
        case .running:
            // An external stop in flight reuses the draining glyph so the icon
            // tracks the menu's "stopping…" affordance.
            if model.stopRequested { return "OnoatsMenuBarTemplateBusy" }
            return model.warning == nil
                ? "OnoatsMenuBarTemplateRecording"
                : "OnoatsMenuBarTemplateWarning"
        case .starting, .stopping: return "OnoatsMenuBarTemplateBusy"
        case .failed: return "OnoatsMenuBarTemplateFailed"
        case .stopped: return "OnoatsMenuBarTemplateIdle"
        }
    }

    /// AppKit template rendering adapts the pre-rendered monochrome glyph to
    /// light and dark menu bars. Recorder state is carried by shape, not tint.
    private var menuImage: NSImage {
        guard let image = Bundle.main.image(forResource: menuImageName) else {
            let fallback = NSImage(
                systemSymbolName: "waveform.circle",
                accessibilityDescription: "Onoats"
            ) ?? NSImage(size: NSSize(width: 18, height: 18))
            fallback.isTemplate = true
            return fallback
        }
        image.isTemplate = true
        return image
    }
}

private enum PanelDestination {
    case recording
    case settings
}

struct ControlPanelRoot: View {
    @ObservedObject var model: RecorderModel
    @ObservedObject var launch: LaunchState
    @Environment(\.dismiss) private var dismiss
    @State private var destination: PanelDestination = .recording

    var body: some View {
        Group {
            switch destination {
            case .recording:
                RecordingPanelView(model: model, launch: launch) {
                    destination = .settings
                }
            case .settings:
                SettingsPanelView(model: model) { destination = .recording }
            }
        }
        .frame(width: 360)
        .onExitCommand { dismiss() }
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
