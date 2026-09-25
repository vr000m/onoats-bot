# Task: Menu-bar "Seminar" toggle

**Status**: Implemented, pending manual build verification
**Component**: recorder, macos
**Assigned to**: Claude
**Priority**: Low
**Branch**: feature/menubar-seminars-toggle
**Created**: 2026-09-25
**Review Gates**: none

## Objective

Let the user mark the next recording as a seminars from the menu bar. Other categories are classified downstream, so only `seminars` needs a manual override.

## Requirements

- A "Seminar (next recording)" toggle in the menu; on → Start runs `onoats bot --category seminars` (the existing `--category` flag; no Python change).
- In-memory and per-session, never written to `config.toml` and not persisted: it resets when the session it applied to ends, so a forgotten toggle cannot mislabel later recordings.
- Disabled while a session is starting, running or stopping (it only affects the next Start).
- `seminars` must be in `[categories] set`; otherwise `validate_category` rejects it and Start fails with the CLI's error. Not pre-checked in Swift because `ConfigStore` reads scalars only, not TOML arrays.

## Non-Goals

- A general category picker; auto-detecting seminars; validating the category set in Swift.

## Files to Modify

- `native/onoats-menubar/Sources/RecorderModel.swift` — `seminarMode`, `seminarCategory`, argv in `start()`, reset in `handleExit`
- `native/onoats-menubar/Sources/OnoatsMenuBarApp.swift` — the toggle
- `tests/test_native_contract_parity.py` — Swift literal/argv vs the real validator
- `README.md`, `CHANGELOG.md`

## Testing Notes

- [x] `swiftc -typecheck` clean; parity test added and passing
- [ ] Manual: `make -C native install`, add `seminars` to `[categories] set`, toggle on, Start, confirm the queue file's `session_meta` line says `seminars`; confirm the toggle is off after Stop; confirm Start fails visibly when `seminars` is not in the set

## Bundled diagnostic

This branch also carries the capturer mic-bind timing diagnostic (`native/onoats-capturer/Sources/MicCapture.swift`, `BindWatch`), so the seminar toggle and the startup-stall investigation can be exercised in one installed app. A 2026-09-25 session with the built-in mic as default input logged only `pacing silence` for 40+ s before `mic: capturing from …`, meaning `bind()` was blocked in a CoreAudio call. The diagnostic logs any step over 1 s, warns if `bind()` is still blocked after 5 s, and appends the total bind time to the `capturing from` line. Log-only; behaviour is unchanged. Next stall: read `~/Library/Logs/Onoats/onoats-bot.log` for `bind step` / `bind still blocked`.
