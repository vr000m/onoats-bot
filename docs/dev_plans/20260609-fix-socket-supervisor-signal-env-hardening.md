# Task: Socket Supervisor Hardening — Signal Isolation + Capturer Env Allowlist

**Status**: Complete — both phases shipped on `feat/socket-audio-transport-milestone-a` (Phase 1 `551aab4`, Phase 2 `fae0c6d`); full suite green (158 passed)
**Component**: recorder, transport, cli
**Assigned to**: vr000m
**Priority**: High (blocks shipping the socket supervisor — graceful Ctrl+C currently mis-classified as failure)
**Branch**: `feat/socket-audio-transport-milestone-a`
**Created**: 2026-06-09

> **Provenance.** Two findings from the Codex adversarial review of the
> `feat/socket-audio-transport-milestone-a` branch (16 files, +5350/-42).
> Both verified against `src/onoats/cli.py` before planning. Sibling plan:
> `docs/dev_plans/20260607-feature-menubar-coreaudio-socket-transport.md`.

---

## Objective

Close the two pre-ship defects the adversarial review surfaced in the socket
supervisor (`src/onoats/cli.py`):

1. **[high] Signal isolation.** The capturer is spawned with
   `asyncio.create_subprocess_exec` (cli.py:285) with no session/process-group
   isolation. On POSIX, a terminal Ctrl+C/SIGTERM is delivered to the entire
   foreground process group, so it hits **both** `onoats` and the capturer. If
   the capturer exits first, `_run_recorder_with_capturer` (cli.py:440-478)
   unconditionally classifies "capturer exited before recorder" as a fail-loud
   event — force-cancelling the drain and returning rc=1. The result: a **normal
   graceful shutdown is mis-reported as a failure** and the partial session may
   be force-cancelled instead of cleanly drained.

2. **[medium] Env over-exposure.** `capturer_env = dict(os.environ)` (cli.py:276)
   copies the **full** recorder environment — including STT credentials such as
   `DEEPGRAM_API_KEY` / STT WS tokens — into the capturer process, which only
   needs the socket paths + nonce. A buggy/replaced/crash-reporting capturer
   could leak credentials it never needed.

## Invariants (state, then prove)

- **I1 (signal):** A user-initiated terminal SIGINT/SIGTERM during a healthy
  session must NOT be delivered to the capturer by the OS as a side effect of
  process-group membership. The capturer is stopped **explicitly** by the
  supervisor (`_stop_capturer`) after the recorder finishes — never by inherited
  terminal signals. Evidence: capturer spawned in its own session
  (`start_new_session=True`); a regression test that asserts a graceful recorder
  shutdown is not classified as capturer-death.
- **I2 (env):** The capturer environment contains ONLY an explicit allowlist —
  socket paths, nonce, and required OS/runtime vars (PATH/HOME/TMPDIR/etc.) — and
  contains NONE of the STT/application secrets present in the recorder env.
  Evidence: a test that sets a sentinel secret in `os.environ` and asserts it is
  absent from the env the capturer is launched with.

---

## Phase 1 — Signal isolation for the capturer

**Files:** `src/onoats/cli.py`, `tests/test_socket_supervisor.py`,
`docs/audio-socket-contract.md`

**Change:**
- In `_supervise_socket_session`, pass `start_new_session=True` to
  `asyncio.create_subprocess_exec` (cli.py:285) so the capturer runs in its own
  session/process group and does NOT receive terminal-delivered SIGINT/SIGTERM.
  (`start_new_session=True` is the portable `subprocess` spelling of `setsid`;
  it is the right primitive here — it covers both SIGINT from Ctrl+C and SIGTERM
  from the controlling terminal.)
- Confirm the existing explicit teardown path still works: `_stop_capturer`
  (cli.py:481) already does SIGTERM → bounded wait → SIGKILL directly to the
  capturer pid, which is unaffected by session isolation. No change needed there,
  but verify it after the spawn change.
- Re-read the capturer-death classification in `_run_recorder_with_capturer`
  (cli.py:440-478). With isolation in place, a graceful recorder shutdown means
  the **recorder** task completes first (capturer still alive) → the existing
  `recorder_task in done` branch (cli.py:414) handles it. Document why isolation
  is what makes that branch correct under Ctrl+C (today, without it, the capturer
  could win the race).

**Test (regression for I1):**
- Add a test that simulates a user terminal signal during a healthy session and
  asserts the recorder's graceful shutdown is honoured (rc=0 / clean-stop path),
  NOT mis-classified as capturer-death (rc=1, force-cancel). Reuse the existing
  `fake_capturer` / `_install_fake_recorder` harness. Prefer asserting the
  spawn was made with `start_new_session=True` (spy on `create_subprocess_exec`)
  AND a behavioural assertion that a recorder-first completion with a still-alive
  capturer yields rc=0, rather than relying on real OS signal delivery in CI.

**Docs:** Note the session-isolation guarantee in
`docs/audio-socket-contract.md` (supervisor owns capturer lifecycle; terminal
signals are not relayed to the capturer by the OS).

**Done when:** new test passes, full `tests/test_socket_supervisor.py` green,
`ruff format` + `ruff check` clean.

---

## Phase 2 — Capturer environment allowlist

**Files:** `src/onoats/cli.py`, `tests/test_socket_supervisor.py`,
`docs/audio-socket-contract.md`

**Change:**
- Replace `capturer_env = dict(os.environ)` (cli.py:276) with an explicit
  allowlist build. Include: the three socket/nonce vars (already set explicitly
  below), plus a minimal set of runtime/OS vars required for a native process to
  launch — e.g. `PATH`, `HOME`, `TMPDIR`, `LANG`, `LC_*`, `USER`, and (macOS)
  `DYLD_*`/`__CF*` only if needed. Use a named module-level constant
  (e.g. `_CAPTURER_ENV_PASSTHROUGH`) so the allowlist is auditable and testable.
- Explicitly EXCLUDE STT/application secrets (`DEEPGRAM_API_KEY`, any
  `*_API_KEY`, `*_TOKEN`, `*_SECRET`, `STT_*`). Implement as an allowlist (deny
  by default), not a denylist, so new secrets don't leak by omission.
- Keep the three socket/nonce vars set explicitly (they already are).

**Test (regression for I2):**
- Set a sentinel secret (e.g. `DEEPGRAM_API_KEY=should-not-leak`) in the env,
  run the supervisor with a spy on `create_subprocess_exec`, and assert the
  captured `env=` kwarg contains the socket paths + nonce but NOT the sentinel
  secret. Assert `PATH` IS present (so the allowlist isn't over-aggressive and
  breaking capturer launch).

**Docs:** Document the env contract in `docs/audio-socket-contract.md` — the
capturer receives only socket paths, nonce, and a fixed runtime allowlist;
secrets are never forwarded.

**Done when:** new test passes, full suite green, `ruff` clean.

---

## Non-goals

- No change to the Phase-4 capturer exit-code "clean stop" contract (still
  reserved — see the menubar/socket plan). The fail-loud-on-capturer-exit rule
  stays; this plan only ensures terminal signals don't *cause* a spurious
  capturer exit in the first place.
- No change to the transport's EOF-is-fatal rule.
