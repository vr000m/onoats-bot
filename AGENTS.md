# AGENTS.md — onoats maintainer & agent guide

Conventions an agent (or human) needs before changing onoats. Scoped to the
non-obvious parts; the code and `docs/` cover the rest.

## Tooling

- Package manager is **`uv`**. Install: `uv sync`. Run: `uv run <cmd>`.
- Tests: `uv run pytest`. Lint/format before every push: `uv run ruff format`
  **and** `uv run ruff check` (a PostToolUse hook formats on edit, but verify).
- Prefer the **pipecat-context-hub MCP** tools for Pipecat framework questions
  over reading `.venv` source.
- **Dev-plan review markers are CI-gated.** `python3 scripts/check_review_markers.py`
  (a CI step, stdlib + git only) fails if a reviewed plan's contract section —
  everything above its `<!-- reviewed: YYYY-MM-DD @ <sha1> -->` marker — was edited
  without refreshing the hash. The marker hashes only the above-marker bytes
  (same convention as the skein `/review-plan` + `/conduct` tooling), so editing
  the `## Progress` / `## Findings` workspace below it is free. Enable the same
  check locally with `git config core.hooksPath .githooks`. To clear a failure:
  re-run `/review-plan`, or recompute the hash for a purely administrative
  above-marker edit (`head -n <marker_line-1> plan.md | git hash-object --stdin`).
- **The landing site lives in `site/`** and shares no runtime code with the
  package. Use Node 24, npm 11.19 and `just`; `just deps` installs the locked
  Wrangler graph in a deny-by-default environment, and `just deploy` loads
  Cloudflare credentials only after installation. The prompt text in
  `site/public/prompts/*.txt` is canonical. Two scripts own the site contracts
  and their module docstrings hold the rule values; do not restate them here.
  `build_prompts.py` owns the generated prompts page and the untrusted-data
  fences. `check_site.py` owns shared chrome and `aria-current`, copied assets,
  versioned image names and references, the social card named by `og:image`,
  `public/.assetsignore` and the `_headers` rules (security headers and CSP,
  cache scope). Each check covers the whole set (every page, tag, link, image
  and rule), not "some match exists". `just check` and CI run both. Files in
  `site/public/img/` are immutable and therefore versioned and quantized with
  `pngquant`; regenerate the social PNG with
  `swift scripts/render_social_card.swift` from `site/` (needs `pngquant`).
  `site/` is excluded from the sdist, and its tests skip when it is absent.

## Audio capture: PortAudio vs socket (`AUDIO_SOURCE`)

`onoats bot` has two capture backends, selected by `AUDIO_SOURCE` (env /
`config.toml [audio].source`), branched in exactly one place each:

- **`portaudio`** (default) — today's `LocalAudioTransport` path, unchanged.
- **`socket`** — reads framed PCM16 from two per-branch unix sockets via
  `onoats.transports.socket_audio.UnixSocketAudioTransport`.

Load-bearing invariants — do not break these without updating the tests that
pin them (`tests/test_dual_socket_source.py`, `tests/test_socket_audio_transport.py`,
`tests/test_socket_supervisor.py`, `tests/test_status_file.py`,
`tests/test_native_contract_parity.py` — the last pins Swift literals to their
Python contract constants):

- **Never-mix.** One socket → one branch → one STT session → one `SourceTagger`.
  Nothing fans a socket to both branches. `_build_socket_transports` refuses to
  start if the two socket paths resolve (`Path.expanduser().resolve()`) to the
  same file.
- **No PortAudio on the socket path.** `AUDIO_SOURCE=socket` must neither import
  nor invoke the PyAudio device-enumeration path (`select_dual_input_devices`).
  `import onoats.dual` must succeed with **no native binary present** (CI is
  native-free).
- **EOF / read-idle is fatal, no self-reconnect.** The transport surfaces a
  **fatal `ErrorFrame`** (`push_error(..., fatal=True)`, which goes *upstream* —
  that's what cancels the pipeline) on EOF, a framing error, a read-idle timeout,
  a BLOCK consumer-stall, or a staging-pump failure. The supervisor owns restart;
  the transport never reconnects itself.
- **Bounded staging + downstream gate.** The reader stages into a buffer bounded by
  **both** a frame count (`max_buffered_frames`) and total bytes
  (`max_buffered_bytes`); the drop policy (default `drop-oldest`, with a WARNING)
  keeps memory capped under a faster-than-consumer writer. Because pipecat's
  `_audio_in_queue` is unbounded, the pump also **gates on the base queue's depth**
  (`_await_downstream_room`, high-water = `max_buffered_frames`) so a *stalled
  consumer* can't grow it without limit — the frame cap bounds both queues (~2×
  worst case). Don't reintroduce an unconditional `push_audio_frame` drain in the
  pump; that's the bug Codex caught (re-verified via the integrated reader+pump
  regression tests in `tests/test_socket_audio_transport.py`).

Release metadata (canonical BSD-2-Clause LICENSE body, pyproject SPDX
`license` field, `hatchling>=1.27` pin for PEP 639) is pinned by
`tests/test_release_meta.py` — don't weaken those without touching the
licensing decision in the release plan. The version lives on **three
surfaces** that must agree — `pyproject.toml`, the `uv.lock` onoats entry
(regenerate via `uv lock`), and the menu-bar Info.plist
`CFBundleShortVersionString` — pinned by the same test file and gated at
tag time by `scripts/release_check.sh vX.Y.Z <commit>` (run before pushing
any release tag). That pre-tag gate also runs the macOS-only capturer heartbeat
suite, including real STOP/CONT against the production `FrameChunker`; it fails
on non-macOS rather than silently skipping the native contract.

Releasing to PyPI (v1.1.0+): pushing a `v*` tag triggers
`.github/workflows/release.yml` — full suite + two guards (tag must equal
the pyproject version; wheel metadata must carry no direct-URL deps), then
publish via PyPI **trusted publishing** (OIDC, no stored token) gated behind
the GitHub `pypi` environment. One-time prerequisites: a PyPI trusted
publisher (project `onoats`, repo `vr000m/onoats-bot`, workflow
`release.yml`, environment `pypi`) and the GitHub `pypi` environment itself.
PyPI rejects direct-URL `Requires-Dist` entries — dependencies must come
from PyPI (this is why `pipecat-local-stt-server` is a `>=0.5.0,<0.6`
registry dep, not a git pin).

## Supervisor ↔ capturer lifecycle (`cli._run_socket_supervisor`)

- The **supervisor owns the capturer process.** It mints a private `0700` socket
  dir + a fresh generation **nonce**, exports `ONOATS_MIC_SOCKET` /
  `ONOATS_SYSTEM_SOCKET` / `ONOATS_CAPTURER_NONCE`, spawns `ONOATS_CAPTURER_BIN`
  (paths + nonce via **both** argv and env), waits (bounded) for both sockets,
  then runs the recorder. The capturer's tap preflight (release-plan Phase 7)
  makes the TCC-prompting tap call **before** binding sockets, announced by
  `ONOATS-EVENT waiting-for-permission`; if the base socket wait expires with
  that event seen, the supervisor extends the wait once (+120 s) and surfaces
  the pending prompt in the status file.
- **Startup signals are owned before spawn.** Until the recorder installs its
  normal drain handlers, SIGINT/SIGTERM cancel startup through the supervisor's
  authoritative `finally`. Cancellation during spawn first retains the child
  handle, so the capturer group is still reaped. A signal that lands while that
  `finally` is already tearing down is deferred
  (`CancellationLedger.run_to_completion` in `onoats._closing`): every teardown
  step still runs — including when the signal lands in the same event-loop turn
  a step finishes, whose result is kept — and a start failure, non-zero rc, or
  surviving capturer group is never masked as a clean stop. A step's OWN
  exception or self-cancellation is handled by one owner (`cli._run_teardown_step`,
  whose docstring holds the step × outcome contract table): an exception is
  logged with redacted text, a self-cancellation is logged as `cancelled`, and
  neither skips a later step; within that `finally` the socket-dir removal and
  env restore always run, a failed capturer stop counts as a surviving group,
  and a failed stderr-reader retirement is logged only. One path never reaches
  that `finally`: if a startup cancellation finds the recorder still running
  past its drain grace, `_run_recorder_with_capturer` stamps
  `recorder-drain-timeout`, stops the capturer group, then hard-exits (rc=1).
  The process environment dies with it, and the leftover socket dir is a
  private `0700` temp dir whose sockets are nonce-gated to that dead
  generation (every Start mints a new dir and nonce), so it is harmless. The app waits for owned child
  exit on Quit; its bounded timeout keeps the app alive with an error rather
  than killing the supervisor before capturer cleanup.
- **Nonce gating end-to-end:** supervisor mints → `cfg.capturer_nonce` →
  transport `expected_nonce`. A capturer presenting a missing/stale nonce on the
  supervisor's paths is rejected at handshake. Ungated (None) when socket mode is
  driven manually without the supervisor.
- **Fail-loud is the contract.** Every failure path (missing/unspawnable capturer,
  sockets that never appear, capturer death mid-session, STT preflight) yields a
  non-zero exit + a WARNING/ERROR log, and a partial session still rotates into
  `pending/` — never a hang.
- **A native heartbeat stall invalidates the whole generation.** The system
  branch's 20 ms pacer samples `mach_absolute_time`; a wake gap strictly greater
  than 2 s stops that pacer and admits `capture-heartbeat-stalled` (rc 14).
  Audio content, absent tap callbacks, exactly 2 s, and host sleep do not decide
  this. The first terminal event wins: graceful SIGINT/SIGTERM admitted first
  stays rc 0; the heartbeat fault admitted first arms an independent 2 s hard
  exit before best-effort ordered Core Audio cleanup. Terminal admission
  quiesces both branch chunkers and disables both writer sockets before either
  branch enters blocking Core Audio cleanup. This clears queued frames and
  prevents successful sends after `shutdown(2)` takes effect. Bytes a writer
  handed to the kernel before that point may still arrive.
  Never rebuild the tap or
  reconnect sockets in-process; only a later supervisor-owned Start creates a
  fresh generation. For an app-owned session, `RecorderModel` waits 0.5 s after
  supervisor rc 1, re-reads the exact failed generation's status, and starts a
  fresh generation only for `capture-heartbeat-stalled`. One restart is allowed
  per failure cluster; the exact replacement must remain authoritative for 60 s
  to rearm it. Missing or replaced PID publication cancels that stability
  window and requires a fresh full 60 s; a live external successor cancels the
  app replacement. A manual Start resets the cluster. Stop, Cancel Recovery,
  Quit, stale timers, missing replacement prerequisites, generic failures, and
  external sessions never auto-restart. If recorder EOF wins the process-exit
  race, the supervisor waits within the native hard-exit bound for a mapped
  capturer rc, preserving `capture-heartbeat-stalled` instead of flattening it
  to `fatal_error_frame`.
  Direct CLI sessions remain stopped. Pinned by
  `native/test_capturer_watchdog.sh`, `native/test_recorder_model.sh`,
  `tests/test_capturer_mic_liveness.py`, and contract-parity tests.
- **Capturer-exit-before-recorder is always fail-loud, even on `rc=0`** — the
  recording is truncated regardless of exit code. The one exemption is a stop
  the recorder itself asked for: once `runtime.graceful_shutdown_requested()`
  (set by the first SIGINT/SIGTERM), an `rc=0` capturer exit is the sockets
  closing during the drain, not a crash, and the supervisor waits for the recorder
  and returns its rc (bounded by the drain grace; an overrun force-cancels,
  stamps `recorder-drain-timeout`, rc=1). A non-zero exit, or any exit before a
  signal, stays fail-loud (`tests/test_socket_supervisor.py`, `…graceful_stop…`).
  Only signals set the flag today; any NEW trigger of `shutdown_event` must also
  call `runtime._mark_graceful_shutdown_requested()`, or a clean stop would read
  as a crash again (loud, not silent). The supervisor resets the flag when a
  session begins. Default-input-device changes (e.g. AirPods removal) are the **capturer's** job
  to absorb by re-binding and continuing to stream; see
  `docs/audio-socket-contract.md`.
- **Failure stamps are floor-guarded.** `status.stamp_supervisor_failure` takes
  `not_before`, and the supervisor passes `stamp_floor`, minted before this Start's
  first status write (so the prompt-pending waiting record still counts). A record
  older than the floor belongs to a previous generation and is never rewritten;
  `onoats status` must not show this Start's failure on an older session's record.
  The recorder's `session_floor` (device apply) is later and deliberately
  separate. Pinned by `tests/test_socket_supervisor.py`
  (`capturer_death_before_write_running`) and `tests/test_status_file.py`.
- `onoats flush` and `onoats stop` share `cli._send_generation_bound_command`, so
  argument checks, generation verification and stale-pid cleanup cannot drift.
- Shared recorder arg handling lives in `dual._apply_recorder_args` — both
  `dual.main` and the supervisor route through it, so interactive/category
  handling can't drift.

## Recorder-owned control (`onoats flush` / `onoats stop`)

Normal CLI control uses `.active/onoats-control.sock`; it never sends a PID
signal. Every bounded v1 JSON request carries the complete expected
PID/start-epoch generation, and the server requires same-UID Darwin
`LOCAL_PEERCRED`, Linux `SO_PEERCRED`, or another proved kernel credential.
Windows AF_UNIX exposes no peer credential through Python, and `chmod` does not
establish a restrictive Windows DACL, so Windows control requests fail closed.
Direct SIGUSR1/SIGTERM remain
compatibility inputs for terminals and service managers. See
`docs/control-socket-contract.md`.

Load-bearing invariants (pinned by `tests/test_cli.py`):

- **One lifecycle owner.** `RecorderLifecycleController` is the sole session
  state machine for both recorder entrypoints. Control requests, SIGUSR1,
  SIGINT/SIGTERM, and pipeline-finally paths are adapters into its one async
  lock; they do not set the shutdown event or rotate buffers independently. A
  Flush admitted first completes its continuation rotation before Stop; once
  Stop or terminal rotation is admitted — or a termination signal has latched
  or set `shutdown_event`/`force_exit_event` before its Stop reached the lock —
  every later Flush is rejected (admission table in the controller docstring).
  The second termination signal retains the force-exit escape without reopening
  lifecycle admission.
- **Never target an unverified, replaced, or recycled generation.** The CLI
  verifies one complete PID record, then sends that record's PID/start epoch to
  the socket. A missing, legacy, replaced endpoint or dropped acknowledgement
  fails closed. Stale cleanup compares PID, cmdline, and start epoch, so a
  same-PID successor survives.
- **A captured UI action is generation-bound.** The menu bar passes the pid and
  start epoch it observed as `--expected-pid` / `--expected-start-epoch`; both
  commands reject the action if the pid file now names another
  generation. Direct CLI calls without those optional arguments still target
  the currently verified recorder in the selected data directory.
- Both return on **command acceptance**, not confirmed exit — a consumer must derive
  "stopped" from the process actually exiting, never from the CLI exit code.
- **Force exit bounds admitted work.** Ordinary client cancellation cannot revoke
  a Flush after lifecycle admission, but a second SIGINT/SIGTERM cancels its
  stuck continuation or terminal rotation and bounds accepted-handler/server
  settlement before exact endpoint cleanup. Deadline expiry never joins a
  cancellation-resistant task again; direct recorders and the socket supervisor
  enforce a final hard process boundary. Internal callback exceptions retain
  server-side diagnostics while clients receive only a generic error.
- **`onoats stop --help` resolves without booting a service** (local argparse +
  lazy resolver import), like `flush`.
- **One bounded event-loop cleanup owner.** Direct recorder entrypoints and the
  socket supervisor both delegate pending-task cancellation plus async-generator
  and default-executor finalization to `onoats._event_loop.run_bounded_event_loop`.
  Teardown is ordered stages (pending-task cancellation, then
  `shutdown_asyncgens`, then `shutdown_default_executor`, then a check that no task is left pending) and the **first failed
  stage ends it**: no later finalizer runs and no abandoned task gets another
  loop turn, then `on_cleanup_failure()` + `hard_exit(1)` (`os._exit` in both
  callers). A zero cancel timeout means no grace for pending work — a task
  cancelled under it never runs again. Each finalizer has its **own**
  `FINALIZER_TIMEOUT_SECONDS` budget, so neither cancellation grace nor a slow
  async-generator shutdown can starve the executor; a clean run is never turned
  into rc=1. Worst case to `hard_exit`: grace + 2 × finalizer budget. The
  contract table is the function docstring; `tests/test_event_loop.py` pins it.

Single-instance + pid-file ownership (`runtime.py`, pinned by
`tests/test_status_file.py` + `tests/test_socket_supervisor.py`):

- **Start is gated by an atomic `flock` single-instance lock, acquired before any
  capture side effect.** `_acquire_instance_lock` takes an exclusive
  `flock(LOCK_EX|LOCK_NB)` on `.active/onoats.lock`. It is hoisted to the EARLIEST
  point in each entrypoint — the socket supervisor takes it *before spawning the
  capturer* (`_supervise_socket_session`), `run_onoats_dual` *before opening
  PortAudio*, and `run_onoats` (`bot-single` / `python -m onoats`) *before even
  importing the native deps* — so a losing concurrent start raises
  `RecorderAlreadyRunningError` (clean rc=1 at all CLI boundaries) **before** it
  touches CoreAudio/TCC/a device, never after. Acquisition is **idempotent**
  (already-held → no-op), so the later `_write_pid_file` call (a backstop) is a
  no-op in the hoisted paths. This is the primary gate and the only *atomic* one: of N
  racing starts exactly one wins. The lock is held for the **whole process
  lifetime**; the kernel releases it on exit (graceful OR crash/SIGKILL) — there is
  no stale lock to reclaim and **no teardown release call** (releasing during
  shutdown would free the slot while the supervisor is still tearing down its
  capturer). POSIX-only (no-op on Windows; macOS product).
- **The lock descriptor and public leaf are one authority.** The lock opens with
  `O_NOFOLLOW`, must be a safe owner-held regular file, and its device/inode must
  still match `.active/onoats.lock` after `flock` and after the legacy identity
  preflight. A pathname replacement discards that descriptor and retries; a
  nested acquire fails closed if the held public identity was replaced.
- **Identity preflight is the secondary guard, and it runs INSIDE the lock.**
  `_acquire_instance_lock` calls `_refuse_if_live_recorder` (`resolve_flush_target`
  + the indeterminate-but-live refusal) immediately after taking the `flock`, so
  both guards fire at the same hoisted, before-capture point. This catches a live
  legacy/cross-version recorder that holds no `flock` (an older build) — without
  it, such a start would acquire the `flock` and spawn the capturer before
  refusing late in `_write_pid_file`. A stale/recycled/foreign pid does NOT block a
  legitimate start. The `flock` catches concurrent same-version starts the
  read-then-act identity check cannot; the identity preflight catches the legacy
  recorder the `flock` cannot. On Windows (no `flock`) the preflight is the only
  guard.
- **Pid writes are atomic.** `_write_pid_file` writes to a temp file and
  `os.replace`s it into place (same dir → atomic rename), never truncating in
  place — mirrors `onoats.status.write_status`. A concurrent reader (a draining
  recorder's owner-checked removal) sees either the complete old record or the
  complete new one, never an empty/partial file mid-write.
- **The installer's rollback record has one line-format owner.**
  `install_staged._record_line` is used by `_write_record` and by
  `_record_names_archive`, which keeps a baseline archive the record may name. A
  record that is missing is the only "not named" answer besides a regular file
  that is fully read and does not contain the archive line; unreadable, non-regular
  and oversized (larger than the read prefix) records all retain the archive.
- **Pid removal is exact-record checked and fails closed.**
  `compare_and_unlink_pid_record` removes **only** when the file still records
  the captured PID, cmdline, and start epoch. It atomically moves the public
  entry to a private same-directory quarantine and verifies the moved inode
  before removal, so a check-to-unlink successor race cannot delete the new
  record; an inode it cannot prove (mismatch or unprovable identity) is
  restored with a no-replace primitive or retained in quarantine, never
  deleted. `control_socket._quarantine_and_remove` is the twin of this policy
  (neither may import a shared helper: `pid` is vendored, `control_socket` is
  an import-free leaf); `tests/test_quarantine_contract_parity.py` drives both
  through one state space, so change them together. If it reads back as `None` (unreadable/foreign/already
  gone) it is left in place — it must never be assumed to be our own benign
  mid-write. Because `stop` returns on command acceptance (not exit), a
  `stop`-then-immediate-`bot` could otherwise let a draining recorder delete a
  NEWER recorder's pid file. Recorder teardown passes its immutable `PidRecord`; the
  GUI's menu gating (Start only in `.stopped`) already prevents this from the app,
  so the guard protects the CLI/scripted path. A leftover invalid pid file is
  self-healing: `status` reports no valid recorder and the next start atomically
  replaces it.

Running-status publication is a fail-open display boundary after the fail-loud
PID/control authority has been published. If the initial running-status write
raises `OSError` or returns false, the recorder logs one redacted warning,
creates no UI telemetry publisher for that run, and continues capture, STT,
control, transcript persistence, and cleanup with unchanged return semantics.
The owned process exit and run log are then the failure authority; do not invent
a later status record or make rich status a prerequisite for recording.
Native status and PID polling are display consumers of recorder authority, so
both use the same bounded, no-follow, owner-only regular-file reader with a
pathname/inode recheck. Never reintroduce an unbounded `Data(contentsOf:)` read
on the menu bar's main actor.

Direct-recorder shutdown has two separate bounds. `wait_or_force` propagates a
normally completed task's exception, but after a force event it cancels and
waits only for `SHUTDOWN_CANCEL_TIMEOUT_SEC`; resistant work is left to the
process boundary. `_event_loop.run_bounded_event_loop` then gives pending-task
cancellation and each loop-owned finalizer separate deadlines, so cancellation
grace cannot consume the async-generator/default-executor finalizer budgets.
Once pending-task cancellation fails (a survivor, a raising wait, or zero grace
with pending work) no finalizer runs — finalizing needs `run_until_complete`,
which would hand the abandoned task more turns — and the process hard-exits.

## Per-run configuration and native/release identity

- **One immutable recorder configuration per Start.** Both entrypoints capture
  `RecorderSessionConfig` once after the instance lock and initial config read.
  STT construction for both arms, banner/display text, telemetry, diarization
  preflight, reconnect/kickstart authority, and shutdown RSS diagnostics consume
  that same snapshot. Recorder paths must not reload configuration on demand;
  only explicitly standalone diagnostics may omit the snapshot.
- **Native caches are generation-complete.** Menu-bar liveness cache keys contain
  PID, the stored command fingerprint, and a positive start epoch. Missing or
  malformed epochs and changed commands/epochs invalidate the entry. An owned
  process exit is clean only at status `0`; a requested Stop never masks a
  non-zero exit, and status diagnostics are accepted only from the matching
  generation, spawn floor, and active data directory.
- **Native model helpers stay separated by responsibility.** `RecorderModel.swift`
  coordinates observable lifecycle/UI state; `RecorderSystemOps.swift` owns
  process, filesystem, clock, and polling effects; `RecorderRecords.swift` owns
  status/PID schema parsing. Config and audio-device parsing remain in their
  existing `ConfigStore.swift` and `AudioDevices.swift` owners.
- **One mandatory release identity.** Staged installation and signed-app smoke
  consume the same immutable `ReleaseIdentity`: canonical bundle identifier,
  designated requirement, cdhash, app-tree hash, main-executable hash, canonical
  CLI path, CLI entrypoint hash, interpreter path/hash, and an independently
  verified clean CLI git identity. Smoke verifies the static identity
  before and after recording and uses Security.framework to verify the running
  app PID's designated requirement and cdhash. That dynamic check and recorder
  discovery are bound to the launched app's kernel birth tuple (read before and
  after), so a recycled app PID can never supply accepted evidence. The smoke invalidates any prior
  receipt before launch and atomically publishes an identity-digest receipt only
  after success and cleanup; staged installation requires that exact receipt
  before mutation. A static path/signature check is not dynamic running-code
  proof.
- **Smoke cleanup refreshes owned descendants.** The smoke snapshots verified
  descendants when recording starts and again immediately before cleanup, then
  unions exact process generations. The cleanup refresh runs only while the
  captured recorder generation is live (before and after the snapshot): an
  already-exited recorder — the normal post-Stop state — contributes nothing,
  and only a live recorder whose ancestry changed fails the smoke. This reaches helpers created during the
  interactive recording window without granting PGID-wide signal authority;
  every member still passes the captured kernel-birth and secondary-identity
  checks before TERM/KILL. A process found after a failed or timed-out `open`
  was not captured and is never signalled; `_quit_unproven_app` may only ask it
  to quit by exact app path (AppleScript, path as argv data), and only when
  `_recorder_idle_lease` proves no recorder is live: a shared `flock` on
  `onoats.lock` (regular leaf, no pid record) that is HELD until the app is gone,
  not probed and released, so a Start racing the quit is refused by the
  single-instance gate instead of being stopped. Any unprovable answer leaves the
  app running and reports it.
- **One installer transaction at a time.** `install_staged._installer_lock` takes
  an exclusive non-blocking `flock` on the install directory itself (no lock file,
  `O_CLOEXEC`) for the whole of `install` and `rollback`, cleanup included. Without
  it, two runs could interleave: one sees the other's committed app, reads that as
  a failed verification and swaps the old bundle back over a reported success. A
  second run fails at once with "already running". Pinned by
  `tests/test_release_meta.py`.
- **Rollback approval is one immutable read.** `approved-rollback.txt` is read
  through one bounded, no-follow, owner/mode-checked regular-file descriptor;
  the bytes, descriptor metadata, and final pathname identity must agree before
  any recorded archive or identity field is trusted.
- **Supported release runtimes are jointly gated.** Python 3.12 runs the full
  release suite and Python 3.13 runs the recorder contract lane; PyPI publication
  depends on both jobs.
- **macOS signal delivery has one accepted residual race.** Installed-smoke
  cleanup rechecks `proc_pidinfo` birth identity plus secondary evidence
  immediately before TERM/KILL, but userspace cannot make that check and signal
  delivery generation-atomic. Do not describe the check-to-signal race as
  eliminated.

## Reviewing a subprocess / process-boundary change

When a change spawns a child process (`create_subprocess_*` / `Popen` / `exec`)
or otherwise crosses an OS boundary, the general review lenses tend to stay on
the in-process logic and miss the boundary itself. The capturer supervisor's
three post-review findings (one `[high]` no-ship) all came from this blind spot —
the heavyweight gate stack passed; a single adversarial pass caught them. Run this
checklist explicitly for any new spawn:

- **Signals / session.** Does the child inherit the parent's controlling-terminal
  signals (Ctrl+C / SIGTERM via the foreground process group)? If it must not,
  spawn with `start_new_session=True`. (Without it, a graceful shutdown can be
  mis-read as the child dying — the capturer's `[high]` finding.)
- **Environment / secrets.** Never pass `dict(os.environ)` to a child. Build a
  deny-by-default allowlist (see `cli._CAPTURER_ENV_POLICY`) so STT/application
  secrets — and dylib-**injection** vars like `DYLD_INSERT_LIBRARIES` — never reach
  a child that does not need them.
- **Teardown reaches the whole group.** A session-leader child may spawn its own
  helpers; signal the **process group** (`os.killpg(os.getpgid(pid), …)`), not just
  the leader PID, on **both** the graceful and crash (leader-already-reaped) paths,
  so nothing outlives the session holding a resource (e.g. the audio device).
- **File descriptors.** Does the child inherit fds it should not (sockets, pipes,
  log handles)? Pass only what is intended.
- **Working dir + argv.** cwd is inherited; spawn via argv (no shell) so there is
  no shell-injection surface — but verify `argv[0]` resolves to a trusted path.
- **Failure is loud + bounded.** Spawn failure, child death, and a silent/hung
  child each yield a non-zero exit + a WARNING/ERROR log + a bounded wait (no
  hang) — see the fail-loud contract above.

Each item should map to a regression test that fails against the pre-fix code
(signal: spawn-kwarg + `rc`; env: a planted secret stripped from the child env;
teardown: a spawned child PID is gone after stop). The supervisor's tests in
`tests/test_socket_supervisor.py` are the worked example.

## STT self-healing (`launchctl kickstart`) invariants

`[stt].launchd_label` (absent by default) turns on self-healing: when the STT
server is unreachable, onoats asks `launchd` to start or restart the *managed*
job. A loaded job is kickstarted. An unloaded job may be bootstrapped only from
the matching `~/Library/LaunchAgents/<label>.plist`, after a bounded no-follow
read confirms a regular user-owned file, safe write permissions, and an exact
`Label`, with `RunAtLoad=true`. Bootstrap consumes a private snapshot of those validated bytes (a
`0400` file in a fresh `0700` `mkdtemp` directory, removed afterwards), never the
mutable LaunchAgents pathname. It must be a real path: `launchctl bootstrap`
passes it to launchd, which opens it in its own process, so an inherited
`/dev/fd/N` fails with rc 5 (verified live on macOS).
Successful bootstrap is the launch request; do
not immediately kickstart the already-booting job. No port inference or
arbitrary plist path is allowed. These
invariants are load-bearing — breaking any of them silently degrades the
feature rather than failing a test you'd notice:

- **The kickstart cooldown is process-wide and label-keyed**, not
  per-`WebSocketSTTService`-instance. It lives in `src/onoats/stt/launchd.py`
  (a leaf module: no `runtime`/`status`/`dual` imports) so both the startup
  preflight (`runtime._preflight_stt_ws`) and the live reconnect path
  (`websocket_stt_service._ensure_connected`) stamp the *same* registry.
  `dual.py` builds two instances (mic + system) against one physical server,
  so a per-instance cooldown cannot cap total kickstarts. `launchd.try_kickstart`
  is the single owner of check-then-stamp-then-kickstart; the stamp lands with
  no `await` between check and stamp, which is what makes "two instances
  exhausting concurrently produce one kickstart" true.
- **Two status-`warning` branch-key schemes, deliberately.** The startup
  preflight recovery uses the shared `stt` branch (`status.stt_branch(None)`) —
  one probe of one shared server, before either instance exists, so it cannot
  clobber anything. Live-session recoveries are per-instance
  (`stt-mic`/`stt-system`, `status.stt_branch("mic")`) because the two
  instances recover independently and a shared key let one's clear erase the
  other's still-unconfirmed warning. Branch keys are owned by `status.py`, not
  `launchd.py`.
- **"Recovered" means a confirmed handshake, never `launchctl`'s exit code.**
  `kickstart_stt_server` returning `True` only means launchd accepted the
  restart request. `on_recovery(<message>)` fires only after a post-kickstart
  connect actually succeeds — and the claim expires after
  `KICKSTART_CONFIRM_WINDOW_SEC` (deliberately larger than the cooldown: the
  confirming reconnect is demand-driven and can burn the full ~15.5s backoff).
- **The cooldown resets only on a confirmed `transcript.*` event**, never on a
  bare successful connect and never on a timer. Because confirmation is
  per-instance while the cooldown is shared, the reset is token-scoped: the
  stamp drops only once *no* instance sharing the label is still
  exhausted-and-unconfirmed (`launchd.REGISTRY`'s unhealthy set, keyed by the
  typed `launchd.InstanceToken`). Otherwise mic confirming
  health would re-arm system to SIGKILL the server mic is using. An instance
  registers itself unhealthy on its **first failed connect attempt**, not at
  backoff exhaustion — registering only at exhaustion left a ~15.5s hole in
  which a sibling that had just started failing was invisible to the guard.
  The **preflight** path deliberately never registers: it has no lifecycle on
  which to clear a token, and is already gated by the cooldown itself.
- **Kickstart triggers on reachability failures only** (`TimeoutError` /
  `OSError`), on both paths. A protocol/auth failure (a 401 from a server that
  is demonstrably up) means the config is wrong; SIGKILLing a healthy server
  neither fixes it nor is harmless.
- **The cooldown registry is process-scoped, not cross-process.**
  `launchd.REGISTRY` is a single in-memory `KickstartRegistry` with no
  persistence — a crash-restart loop (menu bar / socket supervisor relaunching
  the recorder) starts each fresh `onoats bot` process with an EMPTY registry,
  so the `KICKSTART_COOLDOWN_SEC` cooldown caps kickstarts only *within* one process's lifetime, not
  across the relaunches that can follow a repeatedly-crashing session. This is
  accepted, not a gap to silently work around: if cross-process capping is
  ever needed, persist the stamp next to the status file in `data_dir` — don't
  add a second, undocumented scope to the in-memory registry.

Regression tests: `tests/test_stt_launchd.py`, `tests/test_runtime_preflight.py`,
`tests/test_websocket_stt_reconnect.py`.

## Them-channel diarization invariants

`[stt].diarization` / `ONOATS_DIARIZATION` (default **off**) labels `them`
utterances `S1..` live. Pipeline: `DiarizationFeeder` immediately before
`system_stt` -> `WebSocketSTTService` (stamps VAD spans) -> `SourceTagger` ->
`DiarizationLabeler`, system arm only. Load-bearing invariants:

- **Off means absent.** Flag off: `mlx_audio` is never imported (it is in
  `_FORBIDDEN` in `tests/test_onoats_imports.py`; the processors import it only
  on the worker thread via `load_mlx_diarizer`), no feeder/labeler is wired, no
  attribute reaches a frame, and utterance JSONL/render are unchanged (the
  always-written `session_meta` header then carries `run_id` only, no
  `diarization` key).
- **The worker thread owns the model and the coverage writes.** One daemon
  thread (`diar-worker`; bounded join in the feeder's `cleanup()`, never on
  the frame path) does the load, `set_streaming_config("low")`, every
  `feed()`, the final flush, and is the **sole coverage writer** to
  `DiarizationTimeline` (`record`, `record_gap`, `set_slot_count`,
  `mark_flushed`); the loop only reads. `mark_degraded` is the one idempotent,
  thread-safe flag either side may set (worker failures; the feeder's start
  failure / bounded join in `cleanup()`; the labeler's stall guard and
  fail-open). Don't add a loop-side coverage write or a cross-thread model call.
- **Fail open: never lose a transcript.** A runtime diarizer failure
  (loader/`feed()` raising, consecutive over-budget windows, a hung `feed()`)
  degrades with exactly one WARNING and never a fatal `ErrorFrame`; `them`
  transcription keeps flowing unlabelled. The labeler parks stamped
  `TranscriptionFrame`s in an ordered pending deque (released head-first by its
  own task, never blocking `process_frame`) for at most `HOLD_SEC` each, with a
  stall guard that degrades after consecutive no-progress windows; a frame stays in the held state
  until its own push returns, and fail-open / `cleanup()` release every held
  frame FIFO via `_push_labelled` (labelled if not degraded, else
  unlabelled). `EndFrame` flush-waits (bounded), `CancelFrame` releases
  immediately. Accepted gaps are in the Review Checklist below.
- **`label_for` policy** (`diarization_timeline.py`, pinned by
  `tests/test_diarization_timeline.py`): unlabelled below
  `MIN_COVERAGE_FRACTION` recorded coverage (GAP/evicted rows don't count) or
  if any covered row has activity in an unmappable (beyond-`num_speakers`) slot; 2+ slots active for
  `>= OVERLAP_MIN_FRACTION` => overlap (top two by score); otherwise the
  top-scored slot, and **`DOMINANT_MARGIN` over the runner-up is required
  whenever the top slot is not the sole `OVERLAP_MIN_FRACTION` candidate**
  (not merely when there is no candidate). A top slot that is the sole
  candidate is labelled with no margin check.
- **The `me` branch is structurally untouched.** Nothing is inserted in
  `mic_arm`; `stamp_segment_samples` is only set on the system STT. Pinned by
  the me/them x flag x source matrix in `tests/test_dual_socket_source.py`.
- **One flag, two consumers.** `dual` derives a single `diarization_on` bool
  and passes it to both `stamp_segment_samples` (STT) and `diarization` (the
  pipeline builder), so stamping and the labeler can't disagree. The STT's
  `_span_queue` is an ordered queue with one entry per VAD-stop, appended
  **before** delegating VAD-stop to `super()` and popped at the head of
  `run_stt` (pipecat 1.3.0-1.8.x run `run_stt` inline; 1.9+ queue segments and
  ONE task awaits `run_stt` for each in order - either way segments reach
  `run_stt` in VAD-stop order; `runtime._preflight_diarization` refuses any
  pipecat for which `diarization_timeline.segmented_stt_orders_spans` cannot
  see one of those two shapes - for the queue shape that means one plain
  `asyncio.Queue()` built in `__init__`, one handler task created by `start`,
  that handler as the queue's only reader, one unconditional segment `put`
  per VAD stop (only the `not self.is_usable` drop, which the STT mirrors,
  may exit early; any other `put` is the `None` sentinel), and the reader one
  sentinel-ended loop awaiting its single `run_stt` once per segment); the
  feeder and the STT
  each count samples with `SampleCounter`, and the feeder's `start_sample` is
  the worker's position authority. Pinned by
  `tests/test_websocket_stt_stamping.py` (span queue) and
  `tests/test_diarization_timeline.py` (the `segmented_stt_orders_spans`
  probe and its fail-closed cases).
- **Shutdown closes the websocket only after the parent drain.**
  `WebSocketSTTService.stop()` awaits `super().stop()` first (on pipecat 1.9+
  that drains still-queued segments through `run_stt` over the still-open
  socket) and closes the session in a `finally`; a cancelled drain takes the
  hard-close path and re-raises. `cancel()` hard-closes first. Reordering the
  close ahead of the parent stop silently drops final transcripts on 1.9+.
  Pinned by `tests/test_websocket_stt_reconnect.py`
  (`test_stop_drains_the_parent_before_closing_the_websocket`,
  `test_stop_still_closes_gracefully_when_the_parent_stop_raises`,
  `test_stop_hard_closes_and_propagates_when_the_parent_drain_is_cancelled`).
- **Labels are never reused within a run.** `DiarizationTimeline` keeps, per
  model slot, a list of generations `(first active sample, number)` from one
  monotonic counter (`record`, worker thread; `label_for` only reads). A slot
  idle for more than `SLOT_IDLE_REMINT_SEC` (1200 s; override with
  `[stt].diarization_idle_remint_sec` or `ONOATS_DIARIZATION_IDLE_REMINT_SEC`)
  gets a new number on its next active frame, and a window's label uses the generation in force at the
  slot's last active frame in it, so numbers pass `S8` and the grammar is
  `^S[0-9]+$`. This errs toward splitting a speaker and cannot catch a swap while
  both speakers are active. Pinned by `tests/test_diarization_timeline.py`.
- **`session_meta` is line 1 of every file, `category` only on a lock.**
  `TranscriptBuffer` writes `{"type":"session_meta","run_id", "category"?,
  "diarization"?}` through `categories.session_meta_line`; never default
  `category` (the consumer reads any non-blank value as a hard classification
  lock). `run_id` is minted once per buffer (so stable across continuation
  files), `diarization` is `diarization_model.provenance()` and is configuration
  provenance only (a runtime degrade leaves it stale by design). Never put a
  speaker list in it. Pinned by `tests/test_categories.py`.
- **Persistence is canonical; display is render-time.** `speakers`/`overlap`
  are stored on the utterance (omitted when empty); the `Them (S1)` string is
  composed at render/live-terminal/UI-adapter time by
  `display_labels.compose_speaker_label` and never by `TranscriptBuffer` or
  persisted (`tests/test_speaker_labels.py`). Websocket STT publishes neutral
  connection events; runtime/dual adapters translate them to UI telemetry.
- **Preflight is hoisted, import-free and non-downloading.**
  `runtime._preflight_diarization` runs right after `_acquire_instance_lock` in
  `onoats bot` (supervisor and `run_onoats_dual`), only when the flag is on:
  `find_spec` for the extra plus a local HF-cache probe
  (`diarization_model.weights_cached`); failure is a fatal
  `DiarizationPreflightError` (rc=1) before any capture side effect. Only the
  extra/weights probes are memoized process-wide (`_diarization_preflight_done`,
  success only); the `stt_service == "websocket"` check reads `cfg` and runs on
  every call. `bot-single` has no `them` arm, so it ignores the flag with one
  WARNING rather than failing.

Regression tests: `tests/test_diarization_timeline.py`,
`tests/test_diarization_feeder.py`, `tests/test_diarization_labeler.py`,
`tests/test_diarization_model.py`, `tests/test_diarization_contract.py`
(local-only; hard-fails under `ONOATS_REQUIRE_DIARIZATION_CONTRACT=1`),
`tests/test_diarization_integration.py` (local-only, `-m diarization_integration`;
real model + real pipeline on a WAV named by `ONOATS_DIARIZATION_FIXTURE`, skips
when unset),
`tests/test_stt_ws_integration.py` (local-only, `-m stt_ws_integration`; real
`WebSocketSTTService` + Silero VAD against a live STT server on a WAV named by
`ONOATS_STT_FIXTURE`, skips when unset or the handshake fails),
`tests/test_websocket_stt_stamping.py`, `tests/test_speaker_labels.py`,
`tests/test_runtime_preflight.py`, `tests/test_dual_socket_source.py`.

## Leaf modules

A recurring pattern in `src/onoats/`: two higher-level modules that must
neither import each other (`runtime.py`, the startup/preflight path, and
`stt/websocket_stt_service.py`, the live-session reconnect path) both need
the same small piece of logic. Rather than one importing it from the other
(inverting the dependency direction) or duplicating it, the logic moves into
a small **leaf module** — one with no imports from `onoats` itself (stdlib,
and third-party only) — that both sides import from as a shared meeting
point with no cycle. Check a leaf module's own docstring before assuming
its scope; this list is a pointer, not the contract itself.

- `src/onoats/stt/launchd.py` — `kickstart_stt_server()` (the
  `launchctl kickstart` shellout) and the shared, process-wide,
  label-keyed kickstart cooldown/health registry.
- `src/onoats/_redact.py` — `safe_exc_text()`/`redact_uri()`, stripping
  `user:pass@` userinfo out of a third-party exception's `str()` (or a raw
  connect URI) before it reaches a log line or user-visible error message,
  plus `strip_query()` and `display_uri()` — the latter is the **single
  owner** of the redact-then-strip-query composition every whole-URI
  display site needs (`runtime._display_target`,
  `websocket_stt_service._endpoint_label`); do not open-code the two steps
  again, and note `safe_exc_text` composes the query strip itself.
  **Read its docstring before touching it**: the scanner has been rewritten
  or restructured in most review-gauntlet rounds to date, every tiered
  version leaked, and the generated
  sweep in `tests/test_redact.py` — not any hand-picked case list — is its
  specification. Its failure modes come in two directions: a credential
  *leak*, and a destructive *over-redaction* that removes the credential
  correctly but fabricates a hostname out of text later in the string. The
  sweep pins both.
- `src/onoats/stt/diarization_model.py` — the diarization model's repo id,
  weight-file tuple and non-downloading HF-cache probes (`weights_cached()`
  for `runtime`'s startup preflight, `cached_snapshot_dir()` for the
  feeder's loader), so both check the same files; `huggingface_hub` is
  imported lazily inside the functions. Also `one_row_per_frame()`, the one
  frame-rate predicate (`output_subsampling_factor` exactly `int` 1 AND
  `processor_config.hop_length` exactly `int` 160 = `ROW_HOP_SAMPLES`, pinned
  equal to the timeline's `FRAME_SAMPLES`) shared by the worker's load-time
  guard and the release-time contract test.
- `src/onoats/stt/diarization_timeline.py` — the them-channel diarization
  correlation clock (`SampleCounter`), the thread-safe coverage ring
  (`DiarizationTimeline`; the worker is its sole coverage writer) and the
  label policy (`label_for`), shared by the feeder/worker and the STT
  service without either importing the other.
- `src/onoats/_closing.py` — bounded, best-effort teardown of a
  `TranscriptionClient` (timeout + deadline cap + cancellation safety), so
  a hung server's closing handshake can never block a caller forever, plus
  `CancellationLedger` — the one implementation of the "attempt every step,
  remember a cancellation, re-raise it once" invariant, composed by
  `close_quietly`, by `_graceful_close` (which cannot use
  `close_quietly` for its whole sequence because it must join the reader
  task *between* the two closers), and by the socket supervisor's
  authoritative teardown `finally` through `run_to_completion` (a shielded
  step runner that returns each step's outcome; only a cancellation of the
  step itself propagates, never a caller cancellation that lands in the same
  turn the step finishes). Boundedness and "never raises" belong to
  `close_quietly` and the client closers only: the ledger adds no deadline
  (a step given to `attempt`/`run_to_completion` must be bounded by its caller)
  and `run_to_completion` propagates the step's own exception. Do not
  hand-roll another shield-and-remember loop.
- `src/onoats/_event_loop.py` — the one bounded loop teardown for direct recorder
  and supervisor process boundaries. Pending-task cancellation uses the caller's
  grace; each loop finalizer uses its own positive deadline so zero remains a
  valid immediate-cancel configuration. The first failed stage ends teardown with
  no further loop turns (see the recorder-control bullet above).
- `src/onoats/ui_telemetry.py` — the stdlib-only, display-only live snapshot
  contract and its in-memory state/publisher. Telemetry is fail-open: producer,
  persistence, and cleanup failures may degrade the menu-bar panel but must not
  affect capture, STT, transcript persistence, or shutdown. Exactly one bounded
  publisher task coalesces immutable snapshots into one dedicated daemon I/O
  worker for a run; callbacks update memory only. Filesystem writes and cleanup
  never execute on the recorder event loop, and shutdown never joins a stalled
  I/O thread. The one-slot queue prevents an update backlog. Recorder entrypoints
  inject the neutral `_atomic_fs` no-replace operations; this leaf must not import
  any `onoats` module or grow a second syscall implementation.
  Audio levels must be finite and inside `[0.0, 1.0]`; invalid values are rejected
  without advancing the branch observation time, never clamped.
- `src/onoats_contracts/` — a separate, stdlib-only top-level package (shipped
  in the wheel next to `onoats`) holding schema constants shared across process
  boundaries. Today it is `ui_telemetry.py`: schema version, snapshot/audio
  expiry, level bounds, transcript run-id pattern. `onoats.ui_telemetry` imports
  only it (keeping that leaf free of `onoats` imports), and
  `native/installed_recording_smoke.py` loads the same file by checkout path.
  It must never import `onoats` or any third-party module; Swift mirrors are
  pinned by `tests/test_native_contract_parity.py`.

## Wire-format contract

`docs/audio-socket-contract.md` is the versioned (`v1`) capturer↔recorder
contract and the source of truth for framing/handshake/constants. **Extending the
wire format** means bumping `WIRE_VERSION` in `socket_audio.py` **and** the doc
together — `tests/test_audio_socket_contract_parity.py` fails CI if the doc's
constants table and the module drift.

## Review Checklist

Dismissed review findings (`won't-fix` / `analysis-error`) that future reviews
should NOT re-flag go here, one per line:

`- **[Category] disposition**: description (YYYY-MM-DD)`

- **[Architecture] won't-fix**: `dual._apply_recorder_args` (and `_parse_args`)
  keep their leading underscore despite being imported cross-module by `cli.py` —
  this matches the established in-repo convention for shared-but-internal helpers
  (`_parse_args`, `_build_socket_transports`); they are intentionally cross-module,
  not a public API. (2026-06-09)
- **[Architecture] won't-fix**: `max_buffered_bytes` is clamped to `max(1, …)`
  only inside `UnixSocketAudioInputTransport`, not at the `UnixSocketAudioTransport`
  facade — the inner clamp is the single point of use and mirrors how
  `max_buffered_frames` is handled (`Queue(maxsize=max(1, …))`); forwarding the
  raw value through the facade is intentional. (2026-06-09)
- **[Logic] analysis-error**: `nonce[:8]` in the handshake log assumes a `str` —
  unreachable: `parse_handshake` validates `nonce` is `str | None` and the log
  guards on truthiness, so a non-string nonce can never reach the slice. (2026-06-09)
- **[Architecture] won't-fix**: `native/spike/Info.plist` shares the production
  `CFBundleIdentifier` — intentional: Spike 3's entire purpose was validating
  TCC persistence on the PRODUCTION designated requirement (bundle id + cert),
  so a distinct spike identity would invalidate the spike evidence. The spike
  tree was deleted in Phase 6 of the 0.9→1.0 plan (preserved at the
  `spike-archive` tag). (2026-06-10; resolved 2026-06-11)
- **[Performance] won't-fix (scoped)**: the capturer IOProcs perform one bounded
  heap copy (`Data(bytes:count:)`, ~10 ms chunk) and take an `NSCondition` lock
  per callback (`MicCapture.enqueueChunk` / `SystemCapture.enqueueChunk`). A
  textbook RT path would use a pre-allocated lock-free ring buffer; we keep the
  copy+lock because (a) the worker holds the lock only for a queue pop —
  microsecond contention window, (b) the pattern is hardware-verified across
  the full Phase 4/5b smoke incl. hours-long real-call sessions with zero HAL
  starvation, and (c) a ring-buffer rewrite would invalidate that evidence and
  force a re-smoke for a latent, never-observed risk. What we DID fix
  (2026-06-10): the drop-path `logLine` that ran on the realtime thread —
  drops are now counted under the lock and reported from the worker thread.
  Revisit only if a real session ever logs HAL silence/dropouts. (2026-06-10)
- **[Logic] analysis-error**: `FrameChunker.append` back-extrapolating from the
  total `pending.count` "over-counts leftover samples" — the math is exact while
  capture is contiguous (leftovers are contiguous with the next buffer); only a
  frame straddling a capture gap inherits a bounded <20 ms skew, governed by the
  existing `lastEmittedEndNs` clamp. Comment added at the site. (2026-06-10)
- **[Architecture] won't-fix**: `onoats bot --source` sets `AUDIO_SOURCE` in
  `os.environ` rather than threading a parameter — deliberate: the env var is
  the pre-existing public contract (config.toml/env already select the source),
  the flag is a convenience alias onto that contract, and downstream re-parses
  argv independently (documented at the site). Threading a parameter would
  create a second, competing resolution path. (2026-06-11)
- **[Security] won't-fix**: `make_cert.sh` passes the p12 transport password on
  `security import -P` argv — `security import` has no file/stdin password
  option. Residual is a one-shot random secret guarding a file that lives
  seconds inside a 0700 tmpdir; documented at the site. (2026-06-11)
- **[Architecture] won't-fix**: `LateBoundWriter` stays defined at file scope in
  `main.swift` rather than moving to `Support.swift` — it is private plumbing of
  main.swift's startup reorder (preflight-before-sockets), used nowhere else,
  and the Swift sources just passed the full Phase 7 live smokes; relocating
  code in a file with no Swift test runner would invalidate that evidence for
  zero behavioural gain. (2026-06-11)
- **[Architecture] analysis-error**: `permission_event` "is not documented in
  the data-flow comment near `device_state`" — it is: the comment block at the
  declaration site in `_supervise_socket_session` (directly under the
  `device_state` comment) documents who sets it and who reads it. (2026-06-11)
- **[Architecture] won't-fix**: the `auto`→`None` STT-language mapping lives in
  `runtime._resolve_stt_language`, not in `OnoatsConfig.stt_language` —
  deliberate: the property stays a plain `str` for parity with `stt_model`,
  its docstring states the runtime does the mapping, and `_create_stt_service`
  is the single consumer. Moving it would change the property's type contract
  (`str | None`) for no caller. (2026-06-12)
- **[Architecture] won't-fix**: `[stt].language` has no Swift menu-bar picker
  or parity-test entry — intentional: the key is CLI/file-managed (like
  `ws_socket`); the menu bar exposes config.toml via its open-config dropdown,
  which is the supported edit path, and `ConfigStore` round-trips arbitrary
  keys without code changes. A GUI picker would be a separate feature.
  (2026-06-12)
- **[Logic] won't-fix**: a continuation flush (SIGUSR1 / Ctrl+T) does not drain
  `DiarizationLabeler`'s held frames (<= `HOLD_SEC`), so a parked `them` line can
  land in the next session file. In-flight STT transcripts already cross a
  continuation flush the same way (decode latency exceeds the hold), each entry
  keeps its own `frame.timestamp`, and the plan accepts cross-arm arrival-order
  skew; coupling `_rotate_flush` to the labeler is not worth the plumbing.
  (2026-10-08)
- **[Testing] won't-fix**: `tests/test_diarization_contract.py` skips its
  weights-dependent checks when the model is not in the HF cache, even under
  `ONOATS_REQUIRE_DIARIZATION_CONTRACT=1`, so `release_check.sh` can report ok
  without them. Deliberate (owner decision): the cache is never a hard-fail and
  nothing in the test downloads; cache the weights on the release machine
  (`hf download mlx-community/Nemotron-3-Diarization`). (2026-10-08)
- **[Logic] won't-fix**: `DiarizationLabeler._fail_open` re-pushing the
  in-flight frame after a raising `push_frame` could in theory duplicate a
  transcript. pipecat's `push_frame` only enqueues into the next processor's
  queue (`__internal_push_frame` -> `self._next.queue_frame`), so downstream side
  effects cannot raise back into the pusher; plan Req 4 deliberately prefers
  at-least-once over losing a transcript. (2026-10-08)
- **[Architecture] won't-fix**: module-level memos
  `runtime._diarization_preflight_done` and `config._warned_diarization_values`
  are deliberate process-wide dedupe (socket mode preflights twice: supervisor,
  then `run_onoats_dual`; one warning per bad value across several
  `OnoatsConfig` instances). Tests reset them explicitly. (2026-10-08)
- **[Logic] won't-fix**: `DiarizationLabeler` assumes pipecat's `push_frame`
  neither suspends nor raises (it only enqueues into the next processor's
  queue). Reports that a transcript is lost when a push is blocked or raising
  while a CancelFrame/EndFrame, a pass-through drain or the already-covered
  fast path is in progress (e.g. the CancelFrame-cancelled process task
  dropping the frame it is pushing, or a terminal frame skipped because
  `_drain_all` raised) only reproduce with a test-only blocking/raising push;
  hardening them would restructure the held-frame bookkeeping for a path the
  framework does not take. Complements the `_fail_open` re-push entry above.
  (2026-10-08)
- **[Logic] won't-fix**: `segmented_stt_orders_spans()` inspects the stop handler
  and the queue methods but not `SegmentedSTTService.process_frame`, so a future
  pipecat that gates the stop-handler dispatch (e.g. on `_user_speaking`) would
  pass the probe and strand a span. Speculative: 1.3.0 and 1.12.0 dispatch
  unconditionally, and the probe fails closed on the shapes it can see.
  (2026-10-11)
- **[Architecture] won't-fix**: the `pipecat-ai>=1.3.0` floor applies to every
  user, although only diarization needs it. Deliberate and stated in the
  CHANGELOG; the preflight alone cannot protect a user who never enables the
  flag but runs a pipecat that predates the stamping hooks. (2026-10-11)
- **[Security] won't-fix**: `jsonl.read_session_file` does not enforce the
  `^S[0-9]+$` grammar on `speakers`, so a hand-edited session file could inject
  markdown into a rendered label. onoats reads only files it wrote itself into
  its 0700 data dir; an attacker who can edit them can already edit the
  transcript text. (2026-10-11)
- **[Logic] won't-fix**: `DiarizationLabeler`'s stall guard reads "coverage did
  not advance" as "the diarizer is hung", but coverage also stalls when no audio
  reaches the feeder (capture hiccup, device re-bind). More than ~3 s of silence
  while two stamped frames are held degrades labels for the rest of the session
  although the diarizer is healthy. Accepted: the socket capturer's silence
  pacer keeps audio flowing, so it is unlikely, and fixing it means a design
  change to the fail-open contract (only count a stall when the feeder has audio
  queued or its counter has advanced). Transcription is never lost, only
  unlabelled. (2026-10-11)
- **[Logic] won't-fix**: stopping `onoats bot` while the diarizer model is still
  loading leaves the daemon worker thread behind after the feeder's bounded join
  (5 s). Tried to reproduce on 2026-10-11: five start-then-stop cycles with the
  stop sent 0.1-0.6 s after the pipeline was ready (inside the load window), all
  exit 0 in 3-4 s with no degraded WARNING, no traceback and no macOS crash
  report. Not reproducible, so left as is. (2026-10-11)
- **[Security] won't-fix**: the menu-bar app forwarding its full environment (incl. `PYTHONPATH`/`DYLD_*`) into the recorder (`RecorderModel.swift`) predates this branch, the recorder legitimately needs the user's environment (STT credentials), and env vars are trusted input in this threat model. (2026-11-06)
- **[Logic] won't-fix**: "loop finalizers are skipped after a surviving task" (round 5 accepted this and added finalizer-after-survivor; round 6 reversed it). The separate-budget rule keeps cancellation grace from consuming the finalizer budget when pending-task cancellation *succeeds*. Running a finalizer needs `run_until_complete`, which gives every abandoned task more turns (round 6 measured a zero-grace handler running and delaying `hard_exit` by 2 s), and the process `os._exit`s anyway. `_event_loop` ends teardown at the first failed stage; `tests/test_event_loop.py` pins it. (2026-11-06)
- **[Architecture] won't-fix**: the installed smoke's `ACTIVE_DIRNAME`/`STATUS_FILENAME`/`PID_FILENAME`/`PID_MARKER` stay local copies pinned by `tests/test_recording_integration.py`, not moved into `onoats_contracts`. That would not give one owner: the vendored, self-contained `onoats._vendor.pid` must keep its own `PID_FILENAME`/`PID_MARKER`, `onoats.status` owns `STATUS_FILENAME`, and Swift holds a parity-tested copy. An earlier round asked for the opposite. (2026-11-06)
- **[Architecture] won't-fix**: the panel's transcript-preview base label stays the fixed `Me`/`Them` table (`dual._publish_transcript`, `live_terminal._display_label`, `UiTelemetry.swift`) and does not merge `[speakers]` labels. `OnoatsConfig.speaker_labels()` is documented RENDER-ONLY (consumed by the transcript converter); every live surface — the terminal renderer and the panel's parity-pinned `Me`/`Them` meter rows — uses the canonical names, so the preview matches its row. Threading `[speakers]` into telemetry would add a field to `RecorderSessionConfig` and change a live-surface contract for a rendering preference. (2026-11-06)
- **[Architecture] won't-fix**: `SEMINAR_CATEGORY` stays in `onoats.ui_telemetry`, not `categories.py`. It is the Python parity anchor for the Swift `seminarCategory` literal (`tests/test_native_contract_parity.py` pins them equal; no production code in `src/` reads it), and the literal also appears in the `--category` argv and the telemetry `locked_category` field. `categories.py` knows only its default category (`uncategorized`) and deliberately no specific one: the CLI validates membership against the user's `[categories]` set, which is the only authority on whether `seminars` is configured. Moving the constant would not change any runtime behaviour. (2026-11-06)
- **[Architecture] won't-fix**: `ui_telemetry._cleanup_owned_snapshot` is not folded into `tests/test_quarantine_contract_parity.py` with the pid/control twins. It shares the one syscall seam (`_atomic_fs.rename_noreplace`, injected) but its policy differs on purpose: ownership is the snapshot's JSON `pid`/`start_epoch` re-read from the quarantined descriptor (not a pinned inode record), it is fail-open display cleanup (no no-replace primitive → leave the file), and an unlink failure restores the snapshot rather than retaining it. Forcing the twins' state space onto it would change a fail-open boundary late in the branch. (2026-11-06)
- **[Architecture] won't-fix**: the control-server bind + PID + running-status publication is not extracted into one helper shared by `__main__.py` and `dual.py`. Both sequences are pinned in order by `tests/test_status_file.py::test_recorder_entrypoints_publish_and_teardown_control_generation_in_order` and `__main__.py` points at dual.py's ordering contract; extracting late in the branch would invalidate that evidence. (2026-11-06)
- **[Logic] won't-fix**: a force exit can leave an admitted continuation rotation
  still running (`recorder_lifecycle._cancel_with_deadline` cancels it and waits only
  a bounded time). A coroutine may suppress cancellation, and joining it again would
  make the bounded shutdown unbounded; the contract is that the direct recorder and
  the socket supervisor then cross the final hard process boundary (`os._exit`), which
  ends the task. A rotation that outlives the cancel deadline is never an in-process
  state to repair. (2026-11-07)
- **[Logic] won't-fix**: `release_identity.run_bounded` does not kill a helper that
  detached (`setsid`, double fork) and outlived a leader that exited normally. The
  group kill runs only while the leader is unreaped, because a reaped group id can
  be recycled and signalling it would hit an unrelated process. The children it
  spawns (`codesign`, `ps`, `hdiutil`-style tools) do not detach; a leaked helper is
  the smaller failure than a signal to a recycled group. Documented in the
  function's docstring. (2026-11-07)
- **[Performance] won't-fix**: `just deps` reinstalls `site/node_modules` from the lockfile on every `preview`, `dry-run` and `deploy`. The clean `npm ci` is deliberate: `deploy` runs Wrangler from `node_modules` with Cloudflare credentials, so it must run exactly the locked graph, and a stamp that skips the install would trust whatever is already on disk. The cost is a few seconds per run. (2026-11-08)
- **[Testing] won't-fix**: `swift scripts/render_social_card.swift --check` byte-compares system-font AppKit output after `pngquant`, so it depends on the macOS and `pngquant` versions and does not run in CI. It is a maintainer command for regenerating the card; CI's `check_site.py` gates what can be checked portably (the card named by `og:image` exists, is 1200x630 and is the file the renderer writes). (2026-11-08)
- **[Security] won't-fix**: the example prompts' `--- BEGIN/END UNTRUSTED ... ---` fences are fixed lines, so transcript text that reproduces one can close a fence early. A static published template cannot carry a per-call random fence; `/prompts/` already says the fences are fixed and tells readers to use a random fence for each call in their own pipeline. (2026-11-08)
