# Task: Stoppable orphaned recorder sessions (`onoats stop` + menu-bar rewiring)

**Status**: Not Started
**Component**: recorder, macos
**Assigned to**: Varun Singh
**Priority**: High
**Branch**: bug/stoppable-orphan-session
**Created**: 2026-06-19
**Completed**: (fill when done)

## Objective

Make any identity-verified live recorder session stoppable from the menu bar — including a GUI-started session orphaned by an app crash — by adding an identity-checked `onoats stop` CLI subcommand and routing the menu-bar Stop button through it, instead of hard-disabling Stop whenever the app lacks an in-memory `Process` handle.

## Context

**The incident (2026-06-19).** The Onoats menu-bar app crashed. The supervisor it had spawned (`onoats bot`, pid 70364) survived and reparented to launchd (PPID 1), still recording. On relaunch the app could only **Flush**, not **Stop**, leaving an unkillable-from-GUI session. Separately, the session's system-audio tap was delivering all-zero samples because Screen & System Audio Recording permission was denied — surfaced only as a passive 30 s watchdog warning.

**Root cause of the unstoppable state.** Ownership ("did I start this?") is tracked *solely* by an in-memory `Process` handle (`RecorderModel.swift:76`). A crash destroys that handle. On relaunch, `refresh()` sees a live, identity-valid supervisor it has no handle for and classifies it `.running(ours: false)` (`RecorderModel.swift:254`); the Stop button is then hard-disabled via `.disabled(!ours)` (`OnoatsMenuBarApp.swift:71-72`). Flush still works because it shells out to the Python CLI, which performs its own identity-checked signalling (`resolve_flush_target` → marker + `ps` fingerprint) rather than relying on the handle.

**Why the original design disabled Stop.** The comment at `RecorderModel.stop()` (`RecorderModel.swift:294-296`) states the deliberate rationale: "the safe identity-checked signalling lives in the Python CLI, not here." Stop was disabled for external sessions because no CLI seam existed to do it safely — `onoats flush` (SIGUSR1) was the only signal subcommand. This plan closes that gap: add the missing safe seam (`onoats stop`, SIGTERM) and let the GUI delegate to it exactly as `flush()` already does. The `quitApp()` comment (`RecorderModel.swift:304-313`) already documents the orphan hazard for graceful Quit; a *crash* bypasses `quitApp()` entirely, which is the unhandled path this plan addresses.

## Requirements

- `onoats stop` MUST reuse `resolve_flush_target` verbatim for identity verification before signalling — no weaker or duplicated check. PID-recycling defense matters *more* for SIGTERM than SIGUSR1 because SIGTERM kills by default; signalling a recycled foreign pid would terminate an unrelated process.
- `onoats stop` MUST send `SIGTERM` (graceful shutdown path, `runtime.py:1144`), giving the same drain semantics as the GUI's existing owned `p.terminate()`. (Assumption: SwiftUI `Process.terminate()` maps to SIGTERM — Foundation-documented behaviour, not repo-verifiable; named here rather than left implicit.)
- `onoats stop` MUST handle the identity-check→signal TOCTOU race exactly as `_cmd_flush` does (catch `ProcessLookupError`, treat as stale, unlink only when `stale=True`).
- The CLI command returns success on **signal delivery**, NOT on confirmed exit (parity with flush, which does not wait). The GUI MUST NOT interpret exit-0 as "stopped"; the stopped transition is driven by `refresh()` observing the supervisor **no longer alive** (`processAlive` → false; see correction below), NOT by exit code.
- The menu-bar Stop button MUST become enabled for verified `.running(ours: false)` sessions and route through `onoats stop` (mirroring `flush()`), while owned `.running(ours: true)` sessions keep the existing in-handle `p.terminate()` path.
- The external-stop GUI transition MUST NOT reuse the `.stopping` enum state, whose only exit is `handleExit` — which never fires for a handle-less external session. (See Architecture Decisions / Issue risk.)
- The double-stop guard MUST be a flag set **synchronously** in the button action (before the subprocess spawn) and gate `.disabled(stopRequested)` directly — NOT via the next 1 s poll tick. A redundant SIGTERM to a draining supervisor is harmless/idempotent (`runtime.py` ignores the second signal); the guard exists to prevent spawning a duplicate `onoats stop` subprocess, not for signal safety.
- Swift/Python pid-file parity (`test_native_contract_parity.py`) MUST remain green; no change to pid-file format.
- Final-flush-on-shutdown correctness MUST hold for the external-stop path (it shares the SIGTERM → `shutdown_event` path). NOTE: this is **inferred** from the shared path, not directly proven by `test_shutdown_drain.py` — that test asserts an EndFrame is *queued* before the terminal flush, not that it drains content. The content-bearing final flush was live-verified on 2026-06-10 (memory `shutdown-drain-final-segment-edge`); a content-bearing assertion SHOULD be added (see Testing Notes).
- No regression to `onoats flush`, `quitApp()`, or the zero-run watchdog.

## Review Focus

- **Signal-safety parity:** confirm `_cmd_stop` cannot signal an unverified/recycled pid — same guarantees as `_cmd_flush`. The only intended divergence is the signal number (SIGTERM vs SIGUSR1).
- **GUI state-machine soundness:** the external-stop path must converge to `.stopped` via polling and never wedge in a `.stopping`-like state with no clearing event. Trace every `refresh()` branch for a handle-less session through the drain window. NOTE the actual convergence driver is `processAlive` (`kill(0)` + `ps` fingerprint) returning false — i.e. process death — NOT pid-file removal per se (see Integration Seams correction). Verify there is no window where `processAlive` returns false while the drain is still in progress (which would flip the UI to `.stopped` prematurely). This holds because the recorder runs in the *same process* as the supervisor/pid-owner, which exits only after the full teardown `finally` block. Confirm it holds for **both** teardown branches: recorder-first SIGTERM drain, and capturer-first (`cli.py:519-522`, ErrorFrame → `_RECORDER_DRAIN_GRACE_SEC`=30 s force-cancel) — both keep the supervisor process alive until after drain, so `kill(0)` gates identically.
- **Enablement scope (DECIDED 2026-06-19):** Stop is enabled for *all* identity-verified live sessions, for parity with `flush` (which already reaches external sessions). The "crash-orphans only" alternative was rejected to avoid a controlling-tty heuristic the Swift side would have to compute and keep correct.
- **TOCTOU window:** residual race between `resolve_flush_target` returning and `os.kill(SIGTERM)` firing — confirm it is bounded and identical to flush's accepted residual risk.
- **Drain duration UX:** SIGTERM drain is not instant; verify the button cannot be spammed and the UI shows progress without faking a terminal state.

## Implementation Checklist

### Phase 1: `onoats stop` CLI subcommand (Python)

**Impl files:** `src/onoats/cli.py`
**Test files:** `tests/test_cli.py`
**Test command:** `uv run pytest tests/test_cli.py tests/test_shutdown_drain.py -v`
**Validation cmd:** `uv run onoats stop --help`

- Add `_cmd_stop(rest)` as a near-clone of `_cmd_flush` (`cli.py:1117-1166`): same `--data-dir` arg, same `resolve_flush_target` call, same stale-unlink + `ProcessLookupError` handling — the **only** change is `os.kill(pid, signal.SIGTERM)` instead of `SIGUSR1`, and the user-facing strings ("stop"/"SIGTERM"/"graceful shutdown").
- Register `"stop": _cmd_stop` in the dispatch dict (anchor to the `"flush": _cmd_flush` entry, not an absolute line — `_HANDLERS` spans `cli.py:1300-1308`) and add `sub.add_parser("stop", help="Signal the running recorder to stop gracefully (drain + final flush).")` next to the `sub.add_parser("flush", ...)` line (`cli.py:1320-1326` block). Anchor inserts to these symbols; absolute line numbers drift as P1's own edits land.
- Prefer the near-clone over refactoring the already-shipped `_cmd_flush` in this PR. If drift between flush and stop is a concern, pin it with a parity test (Phase 1 tests) rather than extracting a shared helper that both stable+new paths depend on.
- Add `"stop"` to the parametrized subcommand tuple in `test_top_level_help_no_command` (`tests/test_cli.py:30`) — the existing assertion lists `flush` but not `stop`, and will keep passing while silently not guarding the new subcommand.
- Ensure `onoats stop --help` resolves without booting any service — the no-boot guarantee comes from `_cmd_stop`'s own local `argparse` + lazy import (mirroring `_cmd_flush` at `cli.py:1124-1130`), not from the top-level dispatch.

### Phase 2: Menu-bar Stop rewiring (Swift — manual smoke, not /conduct-driven)

**Impl files:** `native/onoats-menubar/Sources/RecorderModel.swift, native/onoats-menubar/Sources/OnoatsMenuBarApp.swift`

- In `RecorderModel`, add a `stopExternal()` path that spawns `onoats stop` exactly as `flush()` does (`RecorderModel.swift:319-341`): subprocess, surface non-zero exit in a note (reuse the `flushNote` pattern), do NOT block on exit.
- Route the Stop button on the **`ours` value the `.running(ours:)` enum already carries** (single source — derived once in `refresh()` at `RecorderModel.swift:246` vs `:254`), NOT a fresh `proc != nil` read in the button action: `ours` → existing `p.terminate()`; `!ours` (verified external) → `stopExternal()`. Re-deriving `proc` independently in the action can disagree with the enum across a poll tick. Keep the in-handle path untouched for owned sessions.
- Drive the external-stop transition through `refresh()` polling only. Introduce a lightweight, cosmetic `stopRequested` flag that is **set synchronously in the button action** (before the subprocess spawn) and is the direct argument to `.disabled(stopRequested)` — do not wait for the next poll tick. Clear it **level-triggered inside `refresh()`**, placed **after** the `if let p = proc { … return }` early-return block (`RecorderModel.swift:243-252`) — i.e. at the `if alive`/`proc == nil` site (`:253`), NOT above it: `if !alive { stopRequested = false }`. Invariant: `stopRequested` is set only by `stopExternal()` and cleared only by `refresh()` observing `!alive`; no other writer. Do **not** set the `.stopping` enum case for external stops — it is cleared only by `handleExit`, which never fires without a `Process` handle.
- Enable the button for verified external sessions: change `.disabled(!ours)` to enable Stop for all verified sessions (enablement decision) and relabel `"Stop (external session)"` → `"Stop"`.
- Both the button-enable change and the `stopExternal()` handler MUST land in the **same commit** — enabling `.disabled` without the handler makes the button clickable with no action.
- Confirm `quitApp()` and `flush()` are unchanged in behaviour.
- **Smoke precondition (install step, not just merge):** `stopExternal()` execs the *installed* `onoats` resolved by `cliPath` (`RecorderModel.swift:321`), NOT the repo source — so an explicit install/refresh of the console script must happen between P1 and P2. Verify with the **bare installed binary** (`onoats stop --help`), NOT `uv run onoats stop --help` (which exercises repo source and can pass while the installed binary is stale). An older installed binary fails as a cosmetic "invalid choice" note, easily mis-diagnosed as a P2 bug.
- Manual smoke (no Swift unit harness — native Swift is not /conduct-runnable): (a) start via menu bar, kill the GUI to orphan the supervisor, relaunch, click Stop → supervisor drains and exits, menu returns to Stopped; (b) start, normal Stop still works; (c) double-click Stop → no duplicate `onoats stop` subprocess, no crash; (d) Flush still works mid-session; (e) during drain the menu shows "Stopping…" and does NOT flip to Stopped until the supervisor exits.

### Phase 3: Zero-sample / denied-permission watchdog escalation (SEPARABLE — optional)

**Impl files:** `native/onoats-menubar/Sources/RecorderModel.swift, src/onoats/cli.py`

- Escalate the existing `zero-run-warning` ONOATS-EVENT (currently passive `status.set_warning`, surfaced only in the menu) to a macOS user notification so a denied Screen & System Audio Recording grant is loud instead of looking like a silent dead session.
- Keep the watchdog threshold/behaviour (`Resampler.swift` `zeroRunWarnSamples = 480_000`); only add the notification on the menu-bar side when `warning` transitions nil→set.
- This phase is independent of Phases 1–2 and can ship separately. It addresses the *other* half of the incident (silent permission denial) but does not affect stop-ability. **Sequencing:** its impl files (`RecorderModel.swift`, `cli.py`) overlap P1/P2; sequence P3 *after* P1+P2 merge — do not run it concurrently in an isolated worktree, which would cause textual merge conflicts (no logic dependency, just file overlap).

## Technical Specifications

### Files to Modify
- `src/onoats/cli.py` — add `_cmd_stop`; register subcommand + parser. Mirrors `_cmd_flush` (`cli.py:1117-1166`) and dispatch table (`_HANDLERS` at `cli.py:1300-1308`, `add_parser` calls at `cli.py:1320-1326`).
- `native/onoats-menubar/Sources/RecorderModel.swift` — add `stopExternal()` (clone of `flush()` at `:319-341`); branch `stop()`/button action by ownership; cosmetic stop-requested flag.
- `native/onoats-menubar/Sources/OnoatsMenuBarApp.swift` — enable Stop for verified external sessions; adjust label (`:71-72`).
- (Phase 3 only) zero-run notification wiring in `RecorderModel.swift`.

### New Files to Create
- None. (New tests extend existing `tests/test_cli.py`.)

### Architecture Decisions
- **Reuse `resolve_flush_target`, do not duplicate identity logic in Swift.** The original design deliberately centralises safe signalling in Python (`RecorderModel.swift:294-296`); duplicating the marker+`ps`-fingerprint resolver in Swift would risk drift against `test_native_contract_parity.py`. Routing Stop through a CLI subcommand keeps one source of truth and matches how `flush()` already works.
- **SIGTERM, not a new signal.** SIGTERM is already the graceful-shutdown trigger (`runtime.py:1144`, same as a single Ctrl-C) and is what the GUI's owned `p.terminate()` sends (Foundation maps `Process.terminate()` → SIGTERM — documented, not repo-verifiable; named in Requirements). No new runtime handler is needed; the external-stop path inherits the existing drain + final-flush. (Drain *content* correctness is inferred from the shared path and live-verified 2026-06-10, not unit-asserted — see Testing Notes.)
- **External-stop transition via polling, not `.stopping`.** `.stopping` is cleared only by `handleExit` (`RecorderModel.swift:389-412`), which fires off the `Process.terminationHandler` — absent for a handle-less external session. (For a `proc==nil` session `refresh()` would actually *overwrite* `.stopping` on the next tick rather than honour it, so the visible failure is a flickering/incorrect state, not a literal permanent wedge — either way `.stopping` is the wrong tool.) Instead, let `refresh()`'s existing `alive && proc==nil → .running(ours:false)` / `!alive → .stopped` logic drive convergence; the UI shows a cosmetic "Stopping…" affordance in the interim.
- **What drives `.stopped` is `processAlive` going false, not pid-file removal.** `refresh()` computes `alive = processAlive(pid)` via `kill(0)` + `ps` fingerprint (`RecorderModel.swift:175-189,229`) and sets `.stopped` on `!alive` (`:258`). Process **exit** alone flips it — even if the pid file momentarily lingers. Pid-file removal is *one* trigger, not the gating one. This matters because it means the GUI does NOT depend on the supervisor's status-before-pid-removal write ordering; correctness rests on the process actually exiting after drain. Verify there is no window where `kill(0)` fails while drain is still in progress.
- **CLI returns on signal delivery, not exit.** Like flush, `onoats stop` does not wait for the supervisor to finish draining (drain + capturer-group SIGTERM→grace→SIGKILL can take seconds — empirical, not pinned by a constant). Exit-0 means "signal sent." The GUI relies on `processAlive` → false for the authoritative stopped state.
- **Enablement scope = all verified sessions (DECIDED 2026-06-19).** Stop is enabled for *any* identity-verified live session (parity with `flush`, which already reaches external/terminal sessions). The "crash-orphans only" alternative (restrict by controlling tty / PPID==1) was rejected: it adds a heuristic the Swift side must compute and keep correct, for marginal protection of a deliberately foreground terminal session the same user could Ctrl-C anyway.
- **Rejected alternative — persist GUI ownership to re-adopt the orphan.** Writing a "GUI-started" marker the relaunched app could re-claim is more state to keep consistent and still fails if the marker write races the crash. The CLI-stop route works regardless of who started the session, so it strictly dominates.

### Dependencies
- No new Python or Swift dependencies. Uses stdlib `signal`/`os.kill` (already imported in `cli.py`) and the existing subprocess-spawn pattern in `RecorderModel`.

### Integration Seams

| Seam | Writer (task) | Caller (task) | Contract |
|------|---------------|---------------|----------|
| Identity-checked stop signal | `resolve_flush_target` (`_vendor/pid.py`) | `_cmd_stop` (`cli.py`) | Resolver returns a verified pid or `stale`/refuse; caller signals ONLY a returned pid, unlinks ONLY when `stale=True`, treats `ProcessLookupError` as stale. |
| CLI stop ↔ GUI | `_cmd_stop` (`cli.py`) | `stopExternal()` (`RecorderModel.swift`) | CLI exit-0 = signal delivered, NOT stopped. GUI must derive stopped from `processAlive` → false via `refresh()`, never from exit code. |
| Drain → stopped detection | runtime SIGTERM handler (`runtime.py`/`dual.py`) | `refresh()` (`RecorderModel.swift`) | GUI keys "stopped" off `processAlive` (`kill(0)` + `ps` fingerprint) returning false, i.e. supervisor **exit** — NOT off pid-file absence. The supervisor's status-before-pid-removal write ordering (`dual.py`, writes status-stopped at ~`:570`, unlinks pid at ~`:584`) is real but is NOT what the GUI observes; the GUI never reads that ordering on the alive-path. |

### Notes on the watchdog (context for Phase 3)
- Zero-run detection: `Resampler.swift` `zeroRunWarnSamples = 480_000` (30 s @ 16 kHz), one-shot, re-arms on real audio; emits `zero-run-warning`/`zero-run-clear` ONOATS-EVENTs.
- Supervisor drain: `cli.py` `_drain_capturer_stderr` parses ONOATS-EVENT and calls `status.set_warning(...)`. A denied tap has no preflight API — zero samples are the only signal.

## Testing Notes

### Test Approach
- [ ] Unit: `_cmd_stop` sends SIGTERM to a verified pid; mock `resolve_flush_target`.
- [ ] Unit (differential, defends "only the signal differs"): assert `_cmd_stop` sends SIGTERM and **NOT** SIGUSR1, AND `_cmd_flush` sends SIGUSR1 and **NOT** SIGTERM — parametrize so a copy-paste/shared-helper signal swap fails. (Mirror `test_flush_sends_sigusr1`, `test_cli.py:335`.)
- [ ] Unit: `_cmd_stop` refuses to signal on identity mismatch / missing fingerprint / no pid file; unlinks only when `stale=True`. Mirror **every** `test_flush_*` branch test (`test_cli.py:358-462` — no-pid-file, stale-dead-pid, foreign-marker, legacy-no-fingerprint, ps-probe-fails, recycled-identity-mismatch) 1:1 as `test_stop_*`; "behavioural twin" is only earned by branch parity, not asserted by one collapsed bullet.
- [ ] Unit: `_cmd_stop` handles `ProcessLookupError` (TOCTOU) as stale, returns non-zero, unlinks.
- [ ] Integration (highest-value, given SIGTERM's lethality): `stop` analogue of `test_flush_refuses_recycled_pid_identity_mismatch` (`test_cli.py:427`) — spawn a real foreign process on a recycled pid, run `onoats stop`, assert the foreign process is **still alive** (`proc.poll() is None`). Proves no SIGTERM reached an unrelated live pid.
- [ ] Unit: `stop` is in the dispatch table, in the `test_top_level_help_no_command` subcommand tuple (`test_cli.py:30`), and `onoats stop --help` resolves without booting a service.
- [ ] New regression: assert the graceful-shutdown teardown writes **status-stopped before unlinking the pid file** (the contract the GUI's predecessor reasoning relied on). NOTE: `test_status_file.py:241-245` already asserts this ordering *statically* (source-text `index()` check) — the new test MUST be **runtime/behavioural** (observe the actual write order during a real teardown), not a duplicate index check. Neither `test_shutdown_drain.py` (drives `stop_pipeline_for_shutdown` with a `FakeTask`, never raising SIGTERM nor exercising `_remove_pid_file`) nor the static check covers the runtime order. Use the real-teardown harness in `test_socket_supervisor.py`.
- [ ] New regression (or documented waiver): a **content-bearing** final-flush assertion on the SIGTERM path. `test_shutdown_drain.py` asserts an EndFrame is *queued*, not that it drains content; if not adding the assertion, record the memory `shutdown-drain-final-segment-edge` (live-verified 2026-06-10) as the confirming source in Findings.
- [ ] Regression: `test_native_contract_parity.py` still green (pid-file format unchanged).
- [ ] Manual (Swift): orphan-then-stop, normal-stop, double-stop, flush-still-works (see Phase 2).

### Edge Cases Tested
- [ ] PID recycled to a foreign process between sessions → `resolve_flush_target` mismatch → no SIGTERM sent → foreign process survives (integration test above).
- [ ] Supervisor exits naturally during drain window → second Stop / refresh sees `processAlive` false → `.stopped`, no error.
- [ ] `ps` probe transiently fails → refuse to signal, do NOT unlink (no orphaning).
- [ ] External session stop drains slowly → UI shows "Stopping…", converges to `.stopped` when the supervisor exits (`processAlive` false), never wedges nor flips early.
- [ ] The Python half of "no early flip" — the supervisor process stays alive until drain+final-flush completes — is observable WITHOUT Swift via `test_socket_supervisor.py`'s teardown-timing harnesses (`_poll_pid_gone`, group-reap tests); assert pid liveness spans the whole drain rather than leaving the entire invariant to manual smoke.

## Acceptance Criteria

- `onoats stop` gracefully stops a verified session (owned or orphaned) and is a behavioural twin of `onoats flush` except for the signal.
- A GUI-started session orphaned by an app crash is stoppable from the menu bar after relaunch.
- Stop never signals an unverified or recycled pid (same guarantee as flush).
- Owned-session Stop, Flush, and Quit behaviours are unchanged.
- External-stop never wedges the GUI state machine; it converges to `.stopped` via polling on `processAlive` → false, and shows "Stopping…" (not Stopped) for the whole drain window.
- Content-bearing final flush on the SIGTERM path is either unit-asserted OR the waiver is recorded in `## Findings` with the `shutdown-drain-final-segment-edge` memory reference (no silent ship with neither).
- `uv run pytest` green (new + existing, incl. drain and parity tests); `ruff format` and `ruff check` clean.
- Manual smoke matrix in Phase 2 passes.
- Docs updated (README/AGENTS/CHANGELOG: new `onoats stop` subcommand).

<!-- reviewed: 2026-06-19 @ f64a8df6909545f5f24a1f087b5a3bf3ef0b273e -->

## Progress

- [x] Phase 1: `onoats stop` CLI subcommand (Python)
- [x] Phase 2 (code): Menu-bar Stop rewiring (Swift) — impl complete + compiles clean (`make build/Onoats`)
- [x] Phase 2 (manual smoke): orphan-then-stop **headline case verified live** on the user's machine (2026-06-22) — see Findings. Remaining matrix items (owned Stop, double-click guard, Flush mid-session, CLI-failure re-enable) not yet exercised.
- [ ] Phase 3: Zero-sample watchdog escalation (separable)

## Findings

### Codex re-review round 5 (2026-06-22) — lock acquired too late (before-capture hoist)

A fifth pass acknowledged the round-4 atomic lock closed the pid-overwrite race
but returned NO-SHIP: the lock was acquired **too late** to be the advertised
single-instance *start* gate.

- **[high] Socket starts could spawn a second capturer before losing the lock**
  (`cli.py`). The lock lived inside `_write_pid_file`, but socket mode reaches that
  only *after* `_supervise_socket_session` has already spawned the native capturer
  (CoreAudio process tap, TCC prompt, device acquisition) and waited for its
  sockets. So two concurrent socket starts could both spawn capturers and touch
  hardware; the loser failed only *after* those side effects (duplicate permission
  prompts, device contention, transient double capture). Fixed by **hoisting
  acquisition before any capture side effect**:
  - `_acquire_instance_lock` is now called in `_supervise_socket_session` *before*
    `create_subprocess_exec` spawns the capturer, and at the top of
    `run_onoats_dual` *before* PortAudio device open. Acquisition is **idempotent**
    (already-held → no-op), so the nested acquires (supervisor → recorder →
    `_write_pid_file` backstop, all same `data_dir`) never release-then-reacquire.
  - **Removed the explicit teardown release** (`_finalize_shutdown_status`,
    `__main__`) added in round 4. The lock is now held for the whole process
    lifetime and freed by the kernel on exit. Releasing during shutdown was itself
    a latent bug: it would free the slot while the socket supervisor is still
    tearing down its capturer, letting a chained start spawn a second capturer into
    a not-yet-released device. An autouse fixture (`tests/conftest.py`) resets the
    process-global lock between tests (pytest shares one process).
  - **Regression** (`test_socket_supervisor.py`): with the lock held, the
    supervisor refuses rc=1 and `create_subprocess_exec` is **never called** — the
    capturer is not spawned. Full suite: 292 passed; ruff clean; marker green.

### Codex re-review round 4 (2026-06-22) — atomic single-instance acquisition

A fourth Codex adversarial pass returned NO-SHIP with one [high]: the pid guard
was still **check-then-replace**, so two concurrent starts could both launch.

- **[high] Concurrent starts could both pass the guard and run** (`runtime.py`).
  `_write_pid_file` resolved the existing pid file (best-effort identity check),
  then later published via `os.replace` — with no atomic step tying the liveness
  check to ownership of the instance slot. Two `onoats bot` starts racing with no
  valid pid file present both pass `resolve_flush_target`, both proceed, and the
  later `os.replace` wins; the loser keeps running but is unrepresented by the pid
  file (invisible to status/stop/flush, double capture). A pre-existing TOCTOU in
  the single-instance model (not introduced by this branch), but the branch's
  single-instance invariant claims to prevent exactly this. Fixed with the
  durable lock the plan had deferred:
  - **`flock` single-instance lock.** `_acquire_instance_lock` takes an exclusive
    `flock(LOCK_EX|LOCK_NB)` on `.active/onoats.lock` **before** publishing the pid
    file; exactly one of N racing starts wins, the rest get
    `RecorderAlreadyRunningError`. Held for the process lifetime via a
    module-global fd; the kernel releases it on exit (graceful OR crash/SIGKILL),
    so there is **no stale lock to reclaim** (the advantage over an `O_EXCL`
    lockfile). Released explicitly on teardown (`dual._finalize_shutdown_status`,
    `__main__`) for prompt handoff. No-op on Windows (POSIX-only; macOS product).
  - The resolver-identity check stays as the **secondary** guard (catches a
    legacy/cross-version recorder that predates the lock); the `flock` is the
    primary atomic gate. This also enforces the plan's "no immediate stop→start"
    rule — a chained start refuses until the drainer's process exits.
  - **Regressions** (`test_status_file.py`): a held flock makes a second
    `_write_pid_file` refuse and publish no pid file; the lock is exclusive while
    held and frees on release. Full suite: 291 passed; ruff clean; marker green.

### Codex re-review round 3 (2026-06-22) — pid-file race residual

A post-smoke Codex adversarial review returned NO-SHIP with one [high]: the
round-1/2 ownership-checked removal still had a deletion window.

- **[high] Owner-checked pid cleanup could still delete a newer recorder's pid
  file** (`runtime.py`). Two compounding gaps: (a) `_write_pid_file` wrote the pid
  file **in place** with `write_text`, which truncates before writing — a reader
  during that window sees an empty/partial file; (b) `_remove_pid_file(owner_pid=…)`
  treated a `None` read (unparseable/empty) as "fall through to `unlink()`". So a
  draining recorder reading a newer recorder's mid-write file as `None` would
  delete it — re-opening the exact orphan-the-new-recorder failure the round-1 fix
  targeted. Verified from the code (in-place writer + `current is None` unlink); the
  round-1/2 tests only covered a *fully written* newer pid file. Fixed:
  - **Atomic write.** `_write_pid_file` now writes a temp file + `os.replace`
    (same-dir atomic rename, `fsync` first), mirroring `onoats.status.write_status`
    — no truncation window; a reader sees the complete old or complete new record.
  - **Fail-closed removal.** `_remove_pid_file(owner_pid=…)` unlinks **only** when
    the file still records exactly `owner_pid`; a `None`/foreign read is left in
    place (logged), never deleted. A leftover invalid file is self-healing
    (`status` → no valid recorder; next start atomically replaces it).
  - **Regressions** (`test_status_file.py`): `_remove_pid_file` fail-closed on
    empty/garbage/foreign-marker content (×3, parametrized) → file survives; and
    `_write_pid_file` publishes via `os.replace` with no `*.tmp` residue. Full
    suite: 289 passed; `ruff format`/`check` clean; static source-order check green.

### Codex re-review round 2 (2026-06-20) — degraded-path fixes

The re-run Codex adversarial review (after the round-1 fixes) returned a fresh
NO-SHIP with two degraded-path findings; both fixed.

- **[high] Start could overwrite a live recorder when identity probing is
  indeterminate** (`runtime.py`). The round-1 start guard only refused on a
  *verified* live recorder; if the existing recorder was live but the `ps` probe
  failed (`resolve_flush_target` → `pid=None, stale=False`) it fell through to
  warn-and-overwrite — the same indeterminate state `flush`/`stop` refuse to act
  on. Fixed: `_write_pid_file` now also raises `RecorderAlreadyRunningError` when
  a marker-valid pid file names a **live** process (`_pid_alive`, fail-safe) that
  the resolver could neither verify nor declare stale (ps-probe-fail / legacy
  fingerprint-less). Only `stale=True` (dead or recycled-to-foreign) is
  overwritten. Regression tests (`test_status_file.py`):
  `…_refuses_live_recorder_with_indeterminate_probe` (kill(0) ok + `_live_ps_cmdline`
  → None) and `…_refuses_live_legacy_fingerprintless_pid`.

- **[medium] Failed external stop wedged the GUI in "stopping"** (`RecorderModel.swift`).
  `stopExternal()` cleared `stopRequested` only on a spawn *throw*, not on a
  non-zero CLI *exit* — so a stale installed CLI lacking `stop` (argparse rc 2),
  or an identity refusal while the process stays alive, left the sole Stop button
  disabled until app restart. Fixed: the `terminationHandler` now clears
  `stopRequested` on any non-zero exit (SIGTERM not delivered → re-enable); a
  delivered SIGTERM (rc 0) still leaves it set, cleared by `refresh()` on `!alive`.
  - *Honest gap:* Codex asked for a menu-model regression with a fake non-zero
    `stop`. There is **no Swift unit-test harness** in this repo (no
    `Package.swift`/XCTest; the menu app is built with a bare `swiftc` line) — the
    project's standing decision is "native Swift = manual smoke only." So this is
    covered by **manual smoke**, not an automated test: point `cliPath` at a CLI
    without `stop` (or an `onoats` stub returning rc≠0) on a live external session,
    click Stop, confirm the button re-enables and the menu leaves "stopping…".
    Code compiles (`make build/Onoats`).

### Phase 2 + pid-race hardening (2026-06-19) — Codex adversarial-review follow-up

A Codex adversarial review of the Phase-1 branch returned **NO-SHIP** with two
[high] findings; both are now addressed (the user authorised pulling Phase 2 and
the deferred pid-race fix forward in response).

- **[high] Orphaned GUI sessions still unstoppable from the menu bar → Phase 2
  implemented.** `RecorderModel.stopExternal()` clones `flush()` to exec
  `onoats stop` for handle-less sessions; the Stop button now routes on the
  `ours` value the `.running(ours:)` enum carries (owned → `p.terminate()`,
  verified external → `stopExternal()`), is enabled for all verified sessions,
  and relabelled `"Stop"`. Convergence to `.stopped` is driven by `refresh()`
  polling `processAlive` → false; a cosmetic `stopRequested` flag (set
  synchronously in `stopExternal` before the spawn, cleared only in `refresh()`
  on `!alive`) gates `.disabled(...)` and drives the "stopping (draining)…"
  affordance — `.stopping` enum is NOT used (no `handleExit` without a handle).
  - *Deviation from the plan's strict invariant, named:* `stopExternal()` also
    clears `stopRequested` in its **spawn-failure catch** (the subprocess never
    launched, so nothing is in flight) — otherwise a missing/non-executable CLI
    would wedge the button disabled forever (the supervisor stays alive, so
    `refresh()` never sees `!alive`). This is the only writer besides `refresh()`,
    guarded to the error path.
  - **Verified:** `make build/Onoats` compiles clean (full module, real
    `swiftc`). **NOT verified by me:** the manual orphan-then-stop smoke matrix
    (Phase 2 (a)–(e)) — requires running/crashing/relaunching the signed app with
    real TCC + audio on the user's machine. Smoke precondition still applies:
    install/refresh the console script so the *installed* `onoats` (resolved by
    `cliPath`) has the `stop` subcommand — verify with the bare installed binary
    `onoats stop --help`, not `uv run`.

- **[high] `onoats stop && onoats bot` could delete the new recorder's pid file
  → fixed in Python.** Two guards in `runtime.py`: (1) `_write_pid_file` refuses
  to start over an identity-verified live recorder (`RecorderAlreadyRunningError`,
  reusing `resolve_flush_target` — a stale/recycled/foreign pid never blocks a
  legitimate start), mapped to a clean non-zero exit at all three CLI boundaries
  (`cli.py`, `dual.py`, `__main__.py`); (2) `_remove_pid_file(pid_path, owner_pid=…)`
  unlinks only when the file still records our pid, so a draining recorder never
  deletes a pid file a newer recorder has overwritten. Recorder call sites
  (`dual._finalize_shutdown_status`, `__main__`) pass `owner_pid=os.getpid()`.
  Regression tests in `test_status_file.py` (refuse-live, overwrite-stale-dead,
  ownership-skip, back-compat-unconditional). **Superseded by round 4** — an
  actual `flock` single-instance lock landed (see round-4 finding); the
  resolver-identity guard is now the *secondary* (legacy/cross-version) check
  behind the atomic lock, not the only guard.

- **Verification:** full `uv run pytest` suite **283 passed**; `ruff format` +
  `ruff check` clean; `make build/Onoats` compiles. Codex re-review not re-run.

### Phase 1 (2026-06-19) — `onoats stop` CLI subcommand shipped

- **Implementation.** `_cmd_stop` (`src/onoats/cli.py`) is a near-clone of
  `_cmd_flush`: identical `--data-dir` arg, identical `resolve_flush_target`
  call, identical stale-unlink + `ProcessLookupError` handling. The *only*
  behavioural divergence is `os.kill(pid, signal.SIGTERM)` (vs SIGUSR1) plus
  user-facing strings. The resolver is reused verbatim — no weakened or
  duplicated identity check. Registered in `_HANDLERS` (anchored to the `flush`
  entry) and via `sub.add_parser("stop", …)`; `"stop"` added to the
  `test_top_level_help_no_command` subcommand tuple.

- **Tests (all green, `tests/test_cli.py`).** Every `test_flush_*` branch mirrored
  1:1 as `test_stop_*` (no-pid-file, stale-dead-pid, foreign-marker,
  legacy-no-fingerprint, ps-probe-fails, recycled-identity-mismatch). The
  recycled-pid case spawns a real `sleep` on the recycled pid and asserts it is
  **still alive** after `onoats stop` (proves no SIGTERM reached an unrelated
  live pid). A parametrized differential test asserts `stop` sends
  SIGTERM-and-NOT-SIGUSR1 and `flush` sends SIGUSR1-and-NOT-SIGTERM, so a
  copy-paste signal swap fails. Plus dispatch-table + `--help`-without-boot.

- **Runtime write-order regression** (`tests/test_socket_supervisor.py`:
  `test_shutdown_tail_writes_status_stopped_before_pid_unlink`, parametrized over
  graceful + fatal-ErrorFrame branches). The shutdown tail that was inline in
  `dual._run_shutdown` (a nested closure, unreachable without booting the STT/VAD
  stack) was extracted to a module-level helper `dual._finalize_shutdown_status`
  — a test-induced refactor done precisely so the **actual ordering logic** is
  runtime-reachable. The test seeds a real pid file + running status, spies on
  `dual._remove_pid_file`, drives the real helper, and asserts the on-disk status
  already reads `running=False` at the instant the pid file is unlinked. Because
  it now exercises dual.py's real call order (not a hand-sequenced copy), a
  reorder of the status-stopped / pid-removal pair fails here at runtime.
  - The **static** source-text index check (`test_status_file.py:243-245`) is now
    redundant with this runtime test but kept (cheap, and still guards the
    start-half `pid-write < status-running` order). The literals it greps
    (`_write_status_stopped(`, `_remove_pid_file(pid_path)`,
    `_write_pid_file(data_dir)`, `_write_status_running(`) all survive the
    refactor in the correct source order, so it stays green.
  - *Note:* the supervisor lifecycle tests in this file substitute a *fake*
    `run_onoats_dual`, so they do not themselves exercise the recorder's
    pid/status writes — hence this dedicated helper-level runtime test.

- **Content-bearing final flush on SIGTERM — WAIVER (per acceptance criteria).**
  Not adding a new content-bearing unit assertion in Phase 1: the SIGTERM path
  shares the existing `shutdown_event` → terminal-flush drain, and the
  content-bearing final flush was **live-verified on 2026-06-10** (memory
  `shutdown-drain-final-segment-edge`, recorded EDGE CLOSED after a 5b menu-bar
  smoke). `test_shutdown_drain.py` asserts an EndFrame is *queued* before the
  terminal flush; the content-drain itself needs a hardware/pipeline run, which
  Phase 1 (CLI-only) does not boot. This waiver is the plan-sanctioned
  alternative to a silent skip.

- **Verification.** `uv run pytest` full suite: 278 passed. Target files
  (`test_cli.py test_shutdown_drain.py test_socket_supervisor.py
  test_native_contract_parity.py`): 112 passed. `uv run onoats stop --help`
  resolves without booting a service. `ruff format` + `ruff check` clean.

- **Remaining:** Phase 3 is separable/optional and sequenced after P2.

### On-device smoke — headline orphan-then-stop verified (2026-06-22)

The core bug (a crash-orphaned GUI session is unstoppable from the menu) is
**fixed and verified live** on the signed app with real TCC + audio.

- **Smoke-procedure gotcha — Force Quit reaps the bundle capturer; it does NOT
  produce an orphan.** First attempt force-quit `Onoats.app`; the capturer
  (`Contents/MacOS/onoats-capturer`, a binary *inside* the bundle) was SIGKILL'd
  (`rc=-9`) while the external supervisor (`~/.local/bin/onoats bot`) survived and
  fail-loud-exited — so no orphan remained to Stop. Inferred mechanism:
  LaunchServices reaps bundle-resident executables on app termination; the
  out-of-bundle CLI supervisor is spared. A *genuine* GUI crash (segfault of only
  the GUI process) leaves both supervisor and capturer alive. **To reproduce the
  orphan, kill only the GUI process** (`kill -9 $(pgrep -f "MacOS/Onoats$")`),
  never Force Quit.
- **Headline case PASS.** With the surgical kill, `onoats status` showed
  `RUNNING` with a live capturer (orphan survived). Relaunch rendered
  `running(ours:false)` with **Stop enabled** ("started outside the menu bar").
  Clicking Stop drove `stopExternal()` → `onoats stop` → identity-checked
  **SIGTERM** → graceful drain. Live log confirmed the runtime invariant: STT
  graceful close → **flush/rotate → pid-file removed → `Shutdown: complete`** (in
  order), then `recorder exited; stopping capturer` — recorder-first, capturer
  second, no `rc=-9`/`capturer-crash`/fail-loud. Final status: `not running` with
  no exit-reason/last-error/supervisor-rc fields (clean graceful stop).
- **Not yet exercised (lower-risk matrix tail):** owned Start→Stop (unchanged
  `p.terminate()` path, relabel only), double-click `stopRequested` guard, Flush
  mid-session, and the CLI-failure re-enable (`cliPath`→non-zero exec).

## Issues & Solutions

### Issue (anticipated): `.stopping` state has no clearing event for external sessions
- **Problem**: `.stopping` is exited only by `handleExit`, fired from `Process.terminationHandler`, which does not exist for a handle-less external session. Reusing `.stopping` for external stop yields incorrect/flickering state (for `proc==nil`, `refresh()` overwrites `.stopping` rather than honouring it).
- **Solution**: Drive external-stop convergence through `refresh()` polling on `processAlive` → false (process exit; not pid-file removal specifically); use a level-triggered cosmetic `stopRequested` flag (set synchronously in the button action, cleared in `refresh()` on `!alive`) instead of the enum state.
- **Files affected**: `native/onoats-menubar/Sources/RecorderModel.swift`

### Recommended orphan-recovery flows (UX decision, 2026-06-19 design discussion)

Captured from a post-Phase-1 design discussion so Phase 2 (and any docs) pick a
sound flow rather than re-deriving it. Verified against current code
(`RecorderModel.refresh()`, `OnoatsMenuBarApp` button gating, `_write_pid_file`,
`_remove_pid_file`, `_rotate_flush`).

- **Recommended flow — flush to checkpoint, keep recording, stop at the end.**
  `onoats flush` (SIGUSR1) is a *continuation* flush (`_flush_continuation` →
  `_rotate_flush(reason, continue_session=True)`, `dual.py:424-431`): it rotates
  the current `.active/` buffer into `pending/` **and opens a fresh `.active/`
  session in the same process** — capture never stops. So the orphan-recovery UX
  is: Flush now (salvage everything so far as a clean segment, recording
  continues), then Stop at the end of the call (graceful SIGTERM → final rotate →
  exit). Nothing is lost; the live call is uninterrupted.
  - *Ownership does NOT transfer on flush.* The orphan stays the orphan
    (`.running(ours:false)`); flush opens a new **file**, not a new **process** or
    a GUI-owned session. The only way to a GUI-owned session is stop → (wait for
    exit) → start.

### Issue (anticipated): external stop→start must not be chained immediately

- **Problem**: `onoats stop` returns on **signal delivery, not exit**; the
  graceful drain (STT drain → terminal flush → rotate → `_remove_pid_file`) takes
  seconds. A `start` issued during that window makes the new recorder's
  `_write_pid_file` **overwrite** the orphan's still-live pid (warn-and-overwrite,
  `runtime.py:1029-1050`); the draining orphan then calls `_remove_pid_file`,
  which **unlinks unconditionally** (`runtime.py:1057-1065`) — deleting the *new*
  recorder's pid file. Net result: the new session runs with no pid file (invisible
  to `onoats status`/`stop`/`flush` — a fresh uncontrollable orphan), plus two
  processes capture the same mic/system audio concurrently during the overlap.
- **Why the menu is safe today**: the GUI cannot trigger this — `Start` is
  rendered only in `.stopped`/`.failed` (`OnoatsMenuBarApp.swift:69-78`), and
  `refresh()` keeps an alive handle-less orphan in `.running(ours:false)` until it
  observes `processAlive` → false (`RecorderModel.swift:243-258`). So `Start` does
  not exist during the drain; the state machine gates it behind real process exit.
  The "no early flip" invariant (recorder shares the pid-owner's process, exits
  only after teardown) is what keeps `kill(0)` true through the whole drain.
- **RESOLVED in round 4** — the `flock`-style hard single-instance lock landed.
  `_write_pid_file` now acquires an exclusive `flock` (held until process exit)
  before publishing the pid file, so the racy CLI sequence (`onoats stop &&
  onoats bot`) is now **safe**: the chained start cleanly refuses with
  `RecorderAlreadyRunningError` until the draining recorder's process exits and the
  kernel frees the lock (rather than clobbering the pid file). The GUI gating
  (`Start` only in `.stopped`/`.failed`) remains as defence in depth, but is no
  longer the *only* thing preventing the race. A future "Restart" button could now
  retry-with-backoff against the lock instead of needing a hard wait.
- **Files affected**: `src/onoats/runtime.py` (`_acquire_instance_lock` /
  `_release_instance_lock` / `_write_pid_file`), `src/onoats/dual.py` +
  `src/onoats/__main__.py` (release on teardown).

## Final Results

[Fill when complete]
