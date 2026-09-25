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

### Mic-stall fix (same branch)

The diagnostic reproduced the stall on 2026-09-25 21:31: `WARNING mic: bind still blocked in 'AudioDeviceStart' after 5s`, still blocked 60+ s later, with the built-in mic as default input and no Bluetooth device connected. A process sample showed the main thread parked in `AudioDeviceStart` → `HALB_IOThread::StartAndWaitForState` (waiting on coreaudiod; it had been up ~6 days). Fix in `MicCapture.swift`/`Resampler.swift`: first bind on a fresh thread with a 5 s bounded wait; background retries (10 s, max 3) while unbound; `bindLock` so a late bind discards itself instead of double-feeding; device-change listener installed before the first bind; `FrameChunker.onStale` rebinds a *bound* mic after 10 s with no real data (30 s cooldown; mic only). Pinned by `tests/test_capturer_mic_liveness.py` (source-shape tests; the stall itself cannot be triggered deterministically).

- [ ] Not verified against a real stall: the retry path is untested until it recurs. Watch `~/Library/Logs/Onoats/onoats-bot.log` for `retry N/3`, `discarded a superseded bind`, `rebound after no capture data`.
- Known limit: a rebind/retry that itself blocks in `AudioDeviceStart` can still park `rebindQueue`, making `stop()`'s `rebindQueue.sync` wait; the supervisor's SIGTERM→SIGKILL bound covers it.

