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
any release tag).

Releasing to PyPI (v1.1.0+): pushing a `v*` tag triggers
`.github/workflows/release.yml` — full suite + two guards (tag must equal
the pyproject version; wheel metadata must carry no direct-URL deps), then
publish via PyPI **trusted publishing** (OIDC, no stored token) gated behind
the GitHub `pypi` environment. One-time prerequisites: a PyPI trusted
publisher (project `onoats`, repo `vr000m/onoats-bot`, workflow
`release.yml`, environment `pypi`) and the GitHub `pypi` environment itself.
PyPI rejects direct-URL `Requires-Dist` entries — dependencies must come
from PyPI (this is why `pipecat-local-stt-server` is a `>=0.3.3,<0.4`
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
- **Nonce gating end-to-end:** supervisor mints → `cfg.capturer_nonce` →
  transport `expected_nonce`. A capturer presenting a missing/stale nonce on the
  supervisor's paths is rejected at handshake. Ungated (None) when socket mode is
  driven manually without the supervisor.
- **Fail-loud is the contract.** Every failure path (missing/unspawnable capturer,
  sockets that never appear, capturer death mid-session, STT preflight) yields a
  non-zero exit + a WARNING/ERROR log, and a partial session still rotates into
  `pending/` — never a hang.
- **Capturer-exit-before-recorder is always fail-loud, even on `rc=0`** — the
  recording is truncated regardless of exit code. Default-input-device changes
  (e.g. AirPods removal) are the **capturer's** job to absorb by re-binding and
  continuing to stream; see `docs/audio-socket-contract.md`.
- Shared recorder arg handling lives in `dual._apply_recorder_args` — both
  `dual.main` and the supervisor route through it, so interactive/category
  handling can't drift.

## Identity-checked signalling (`onoats flush` / `onoats stop`)

Both CLI signal subcommands share **one** identity gate — `resolve_flush_target`
(`_vendor/pid.py`) — and differ **only** in the signal sent:

- `onoats flush` → **SIGUSR1** (continuation flush: rotate buffer, keep recording).
- `onoats stop` → **SIGTERM** (graceful shutdown: drain + final flush, then exit;
  same trigger as a single Ctrl-C and the menu bar's owned `Process.terminate()`).

Load-bearing invariants (pinned by `tests/test_cli.py`):

- **Never signal an unverified or recycled pid.** The resolver validates the
  marker, requires a cmdline fingerprint, probes liveness (`kill(0)`), and
  compares the live `ps` cmdline against the stored one. Only a fully-verified
  pid is signalled; unlink **only** when `stale=True`, and even then
  **compare-and-unlink** (`_compare_and_unlink_stale_pid` → `_remove_pid_file`
  ownership check) — never blindly, or a fresh recorder that won the lock and wrote
  its pid in the resolve→cleanup window would be deleted; treat `ProcessLookupError`
  at signal time (TOCTOU) as stale. This matters **more** for `stop` than `flush`
  — SIGTERM kills by default, so a blind signal to a recycled foreign pid would
  terminate an unrelated process. A differential test asserts `stop` sends
  SIGTERM-not-SIGUSR1 and `flush` sends SIGUSR1-not-SIGTERM, so a copy-paste
  signal swap fails.
- **`stop` is a near-clone of `flush`, not a refactor.** Drift is pinned by tests
  rather than a shared helper, keeping the shipped `flush` path untouched.
- Both return on **signal delivery**, not confirmed exit — a consumer must derive
  "stopped" from the process actually exiting, never from the CLI exit code.
- **`onoats stop --help` resolves without booting a service** (local argparse +
  lazy resolver import), like `flush`.

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
- **Pid removal is ownership-checked and fails closed.**
  `_remove_pid_file(pid_path, owner_pid=…)` unlinks **only** when the file still
  records exactly that pid. If it reads back as `None` (unreadable/foreign/already
  gone) it is left in place — it must never be assumed to be our own benign
  mid-write. Because `stop` returns on signal delivery (not exit), a
  `stop`-then-immediate-`bot` could otherwise let a draining recorder delete a
  NEWER recorder's pid file. Recorder teardown passes `owner_pid=os.getpid()`; the
  GUI's menu gating (Start only in `.stopped`) already prevents this from the app,
  so the guard protects the CLI/scripted path. A leftover invalid pid file is
  self-healing: `status` reports no valid recorder and the next start atomically
  replaces it.

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
