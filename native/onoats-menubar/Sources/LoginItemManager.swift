// Syncs Onoats.app's login-item registration to `[app].launch_at_login` in
// config.toml (dev plan Phase 5) — checked once at every launch, via the
// existing ConfigStore line-editor (no new parsing infrastructure).
//
// `config.toml` is deliberately the ONLY control surface (no menu toggle,
// per explicit user decision — consistent with config.toml already being
// "one source of truth" for GUI-managed settings). Absent is a strict
// no-op, distinct from explicit `false`: it never silently opts a user in,
// and it never overrides a login item registered out-of-band (e.g. via
// System Settings ▸ Login Items directly).
import Foundation
import ServiceManagement

enum LoginItemManager {
    /// Reads `[app].launch_at_login` and registers/unregisters
    /// `SMAppService.mainApp` to match, only when the current `.status`
    /// disagrees with what the config wants — never calls `.register()` on
    /// an already-`.enabled` service or `.unregister()` on one that isn't.
    /// Returns a menu-bar hint string for anything worth surfacing (a
    /// registration failure, `.requiresApproval`, or an unrecognized
    /// value), or nil when there's nothing to show.
    static func sync() -> String? {
        let raw: String
        switch ConfigStore.readValuePresence(section: "app", key: "launch_at_login") {
        case .absent:
            return nil  // absent: no action, ever — see file header
        case .malformed:
            // Round-10 finding: a present-but-empty or unterminated-quote
            // value (e.g. `launch_at_login = ""` or a dangling `"`) used to
            // read back as plain `nil` via `readValue`, identical to the
            // key being absent — so it silently no-opped instead of
            // surfacing the same warning an unrecognized value gets below.
            return
                "launch_at_login: empty or malformed value in config.toml (expected true/false) — ignored"
        case .value(let v):
            raw = v
        }
        // Only bare-spelled TOML booleans are meaningful (ConfigStore's
        // quote-stripping means `"true"` reads back identically to `true` —
        // both accepted). Anything else (e.g. Python-style "True") is
        // treated as absent, but logged as a hint so a typo doesn't
        // silently no-op forever.
        let wantsEnabled: Bool
        switch raw {
        case "true": wantsEnabled = true
        case "false": wantsEnabled = false
        default:
            return
                "launch_at_login: unrecognized value \"\(raw)\" in config.toml (expected true/false) — ignored"
        }

        let service = SMAppService.mainApp
        do {
            // `.requiresApproval` means a registration EXISTS but the user
            // has not approved it in System Settings yet. It counts as
            // registered for both branches:
            //   * enabling: re-`register()`ing it changes nothing (the
            //     pending item is already there) — skip the XPC round trip;
            //   * disabling: NOT unregistering it would leave a pending item
            //     that a later user approval silently turns on, re-enabling
            //     exactly what `launch_at_login = false` asked us to disable.
            let isRegistered =
                service.status == .enabled || service.status == .requiresApproval
            if wantsEnabled {
                if !isRegistered {
                    try service.register()
                }
            } else if isRegistered {
                try service.unregister()
            }
        } catch {
            return
                "launch_at_login: failed to \(wantsEnabled ? "register" : "unregister") — \(error.localizedDescription)"
        }

        // A fresh .register() can land in .requiresApproval (System
        // Settings hasn't approved it yet) rather than throwing — surface
        // it the same "show state, let the user notice" way as every other
        // menu hint, instead of silently leaving the app not actually
        // launching at login.
        if wantsEnabled, service.status == .requiresApproval {
            return "launch_at_login: approve Onoats in System Settings ▸ Login Items"
        }
        return nil
    }
}
