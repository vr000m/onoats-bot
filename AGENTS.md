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
`tests/test_socket_supervisor.py`):

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

## Supervisor ↔ capturer lifecycle (`cli._run_socket_supervisor`)

- The **supervisor owns the capturer process.** It mints a private `0700` socket
  dir + a fresh generation **nonce**, exports `ONOATS_MIC_SOCKET` /
  `ONOATS_SYSTEM_SOCKET` / `ONOATS_CAPTURER_NONCE`, spawns `ONOATS_CAPTURER_BIN`
  (paths + nonce via **both** argv and env), waits (bounded) for both sockets,
  then runs the recorder.
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
- **[Documentation] won't-fix**: README cross-platform matrix still lists only
  BlackHole for system loopback — deliberately deferred to Phase 6 (BlackHole
  demotion); the footnote covering the socket path keeps the matrix accurate
  until then. (2026-06-10)
- **[Architecture] won't-fix**: `native/spike/Info.plist` shares the production
  `CFBundleIdentifier` — intentional: Spike 3's entire purpose was validating
  TCC persistence on the PRODUCTION designated requirement (bundle id + cert),
  so a distinct spike identity would invalidate the spike evidence. The spike
  tree is slated for deletion after Phase 5b/6. (2026-06-10)
- **[Logic] analysis-error**: `FrameChunker.append` back-extrapolating from the
  total `pending.count` "over-counts leftover samples" — the math is exact while
  capture is contiguous (leftovers are contiguous with the next buffer); only a
  frame straddling a capture gap inherits a bounded <20 ms skew, governed by the
  existing `lastEmittedEndNs` clamp. Comment added at the site. (2026-06-10)
