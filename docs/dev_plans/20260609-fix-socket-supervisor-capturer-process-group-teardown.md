# Task: Socket Supervisor Hardening — Capturer Process-Group Teardown

**Status**: Complete — implemented directly (not via /conduct) on `feat/socket-audio-transport-milestone-a`. All 3 phases shipped; full suite green (159 passed, +1 new I3 test); `ruff` clean. The new regression test was verified to FAIL against the pre-fix single-PID teardown.
**Component**: cli (socket supervisor)
**Assigned to**: vr000m
**Priority**: High (no-ship — blocks shipping socket-supervisor mode)
**Branch**: `feat/socket-audio-transport-milestone-a`
**Created**: 2026-06-09

> **Provenance.** Third finding from the Codex adversarial review of the
> `feat/socket-audio-transport-milestone-a` branch (17 files, +5811/-42).
> Verified against `src/onoats/cli.py` before planning. Sibling plans:
> `docs/dev_plans/20260609-fix-socket-supervisor-signal-env-hardening.md` (Complete),
> `docs/dev_plans/20260607-feature-menubar-coreaudio-socket-transport.md`.

---

## Objective

Close the [high] no-ship defect the adversarial review surfaced in the socket supervisor
teardown (`src/onoats/cli.py`):

**Capturer teardown kills only the parent process, not the isolated process group.**
The capturer is spawned with `start_new_session=True` (`cli.py:362`), making it a
session/process-group leader. `_stop_capturer` (`cli.py:550–574`) then calls
`capturer_proc.terminate()` / `.kill()` on **only that one PID**. If the native capturer is a
wrapper script or spawns a CoreAudio/helper subprocess, that descendant lives in the same isolated
group but is never signalled — it can survive recorder shutdown while still holding the audio
device / capture resources. The supervisor returns success and `shutil.rmtree(sock_dir)`
(`cli.py:388–394`) runs while capture continues outside its control (orphaned capture path,
possible device lock).

## Invariant (state, then prove)

- **I3 (process-group teardown):** After the supervisor stops the capturer, *no process in the
  capturer's process group remains alive* — not just the direct child. Because the capturer is a
  group leader (`start_new_session=True`), its PGID equals its PID, so signalling the group via
  `os.killpg(pgid, …)` reaches every descendant.
  **Evidence:** a regression test where a fake capturer spawns a long-lived child, run the
  supervisor to clean shutdown, then assert the child PID is gone (`os.kill(pid, 0)` raises
  `ProcessLookupError`).

---

## Phase 1 — Group-aware teardown in `_stop_capturer`

**Files:** `src/onoats/cli.py`

**Change:** Replace single-PID `terminate()`/`kill()` with process-group signalling:
- Resolve `pgid = os.getpgid(capturer_proc.pid)` (guarded).
- SIGTERM the group: `os.killpg(pgid, signal.SIGTERM)`; bounded wait
  (`_CAPTURER_TERM_GRACE_SEC`, cli.py:171); on timeout `os.killpg(pgid, signal.SIGKILL)`.
- Always `await capturer_proc.wait()` afterwards to reap the direct child.
- **Idempotency / fallback:** wrap each step so `ProcessLookupError` (group/process already gone)
  returns cleanly. If `os.getpgid`/`os.killpg` is unavailable or raises (platform without process
  groups), fall back to the existing per-PID `terminate()`/`kill()` path so non-POSIX behaviour is
  unchanged. `import os` and `import signal` are already at module top — no new imports.
- Targeting is safe: the capturer is in its **own** new session/group, so `killpg` never touches
  the `onoats` foreground process group.

**Done when:** `_stop_capturer` is group-aware + idempotent, `ruff format` + `ruff check` clean,
existing `tests/test_socket_supervisor.py` still green.

---

## Phase 2 — Regression test for orphaned children (I3)

**Files:** `tests/test_socket_supervisor.py`

**Change:** (reuse the `fake_capturer` fixture + `.control` sidecar mechanism, ~120–260)
- Add a `spawn_child` control flag to `_FAKE_CAPTURER_SRC`: when set, the fake capturer spawns a
  long-lived child (e.g. `sleep`) and records the child PID where the test can read it (write to a
  file inside `sock_dir`, or stdout the test captures).
- New test: run the supervisor to completion (recorder-finishes-first / clean shutdown path), then
  assert the recorded child PID is gone — `os.kill(child_pid, 0)` raises `ProcessLookupError`
  (poll briefly to avoid races).

**Done when:** new test passes, full `tests/test_socket_supervisor.py` green, `ruff` clean. The
test must fail if the Phase-1 change is reverted (proves it guards I3).

---

## Phase 3 — Docs

**Files:** `docs/audio-socket-contract.md`

State (supervisor-lifecycle section, ~116–132) that the supervisor tears down the **entire
capturer process group** on shutdown, so no helper/child subprocess can outlive the session.

**Done when:** contract doc reflects the group-teardown guarantee.

---

## Non-goals

- No change to spawn-time isolation (`start_new_session=True`) or the env allowlist — both shipped.
- No change to the capturer exit-code / fail-loud contract.
