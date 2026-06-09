# AGENTS.md — onoats maintainer & agent guide

Conventions an agent (or human) needs before changing onoats. Scoped to the
non-obvious parts; the code and `docs/` cover the rest.

## Tooling

- Package manager is **`uv`**. Install: `uv sync`. Run: `uv run <cmd>`.
- Tests: `uv run pytest`. Lint/format before every push: `uv run ruff format`
  **and** `uv run ruff check` (a PostToolUse hook formats on edit, but verify).
- Prefer the **pipecat-context-hub MCP** tools for Pipecat framework questions
  over reading `.venv` source.

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
