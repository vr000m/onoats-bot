# Changelog

All notable changes to onoats are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

All listed versions — including the reconstructed pre-0.9 era — are licensed
under BSD-2-Clause (see [LICENSE](LICENSE)).

Versions before 0.9.0 were never published or tagged; they are reconstructed
retrospectively from the merged-PR history (nothing was ever distributed, so
no backdated tags exist). PR numbers `#1`–`#7` refer to this repository;
older history predates the extraction and is cited by merge-commit SHA.
Annotated tags exist from `v0.9.0` forward.

## [Unreleased]

### Fixed

- A normal stop (`onoats stop`, Ctrl+C) in socket mode no longer exits rc=1 with
  a `capturer exited mid-session (rc=0)` ERROR. The recorder's drain closes the
  capture sockets, the capturer exits 0 on "peer closed" before the drain ends,
  and the supervisor read that as the capturer dying. A capturer `rc=0` exit
  after a stop signal is now a clean stop; a non-zero exit, or any exit before a
  signal, is still fail-loud. The session was always rotated into `pending/`;
  only the exit code and log were wrong.

### Changed

- `native/Makefile`: `install-cli` (and so `install`, `setup`, `setup-cli`) takes
  an `EXTRAS` variable, default `macos`. `uv tool install` replaces the tool
  env's extras, so a plain `make install` used to uninstall the `diarization`
  extra; use `make -C native install EXTRAS=macos,diarization` to keep it.

## [1.5.0] - 2026-10-28

### Changed

- Bumped `pipecat-local-stt-server` from `>=0.3.3,<0.4` to `>=0.5.0,<0.6`.
  The wire protocol (`PROTOCOL_VERSION == "0.1"`), `stt_server.client` and
  `stt_server.protocol` are unchanged, so the recorder's client code is too.
  0.4.0 adds a sibling `<socket>.lock` (single-server flock, left on disk) next
  to each STT socket; a second `serve` on a live socket now exits with
  `EADDRINUSE` instead of replacing the running server.
- `pipecat-local-stt-server` 0.5 moves the STT LaunchAgents from Unix sockets
  to loopback TCP (whisper 9565, kyutai 9665, parakeet 9765, nemotron 9865;
  point `ws_host`/`ws_port` at the one you run). With nothing configured,
  onoats connects to a live legacy `~/Library/Caches/pipecat-stt/stt.sock` if
  one exists (a 0.4.x agent), else to the whisper agent at `127.0.0.1:9565`;
  `STT_WS_DEFAULT_SOCKET` still overrides both. A lone `STT_WS_PORT` /
  `ws_port` (no host) is now reported as an incomplete endpoint instead of being
  silently replaced by the default. **Upgrade note:** a `[stt].ws_socket` left
  in `config.toml` outranks `ws_host`/`ws_port`, so remove it when you switch an
  agent to TCP. Loopback TCP has no peer-uid check, unlike the 0700 Unix socket;
  set `STT_WS_TOKEN` (with the server's `--auth-token-file`) if that matters.

### Fixed

- `onoats init` no longer drops a hand-set `[stt].ws_host` / `ws_port` on re-run
  (and its preflight now probes that endpoint). A lone host, a lone port or an
  out-of-range port is dropped with a note.

## [1.4.0] - 2026-10-12

### Added

- **Opt-in live speaker diarization on the "them" channel.** New
  `[stt].diarization` (env `ONOATS_DIARIZATION`, which takes precedence;
  default off) labels each `them` utterance with a speaker (`S1`, `S2`, … in
  order of first speech) and flags overlap; the labels persist in the session
  JSONL (`speakers` / `overlap`) and render as `**Them (S1):**` (or
  `Them (S1 / S2, overlapping)`). The `me` channel is never labelled. Websocket
  STT only. Needs the optional extra (`uv sync --extra diarization`, or
  `uv tool install 'onoats[macos,diarization]'`) and the model weights, which
  are never auto-downloaded: `uvx --from huggingface_hub hf download mlx-community/Nemotron-3-Diarization`
  (a `uv tool install` links only `onoats` onto PATH, so a bare `hf` may not exist).
  A startup preflight (right after the single-instance lock, before any capture
  side effect) exits non-zero with the remedy if the STT backend is not
  websocket, the extra is missing, or the weights are not cached. After startup
  it fails open: a runtime diarizer failure logs one WARNING and the session
  continues with unlabelled `them` lines, never losing a transcript.
  `onoats bot-single` ignores the flag with one WARNING.
- **`session_meta` header on every session file.** The first line now always
  carries a per-run `run_id` (opaque random token, stable across the files one
  recorder run rotates through), `category` only when `--category` locks one
  (never defaulted), and `diarization: {model, version}` when diarization is
  configured on. A consumer can treat equal `run_id`s as "same run" and a
  missing one as unknown.
- **Speaker labels are never reused within a run.** A model slot idle for more
  than 20 minutes (`SLOT_IDLE_REMINT_SEC`, 1200 s; override with
  `[stt].diarization_idle_remint_sec` or `ONOATS_DIARIZATION_IDLE_REMINT_SEC`) is given a fresh label on its next
  speech, so `S1` cannot silently move to a different person; numbers continue
  past `S8`. Errs toward splitting one person in two; a swap while both speakers
  are active is not detected. First activity and each re-mint of a model slot log
  at INFO with the slot number and label.
- **`onoats init` carries `[stt].diarization` and
  `[stt].diarization_idle_remint_sec` over** when re-run over an existing
  `config.toml`, so re-running init does not silently turn diarization off or
  drop a tuned re-mint threshold.

### Changed

- **pipecat floor raised to `>=1.3.0`.** The segment-sample stamping that
  diarization relies on needs `SegmentedSTTService` to hand segments to
  `run_stt` in VAD-stop order: inline on 1.3.0-1.8.x, through one in-order
  queue task from 1.9 (the STT keeps an ordered span queue, so both work). CI
  runs the real-pipecat stamping tests (`tests/test_websocket_stt_stamping.py`)
  and the `segmented_stt_orders_spans` preflight probe tests
  (`tests/test_diarization_timeline.py`) against the locked pipecat 1.12.0, the
  queue shape; the inline shape (1.3.0-1.8.x) is covered in CI only by that
  probe's source fixtures, not by a real pipecat run. The diarization tests
  were additionally run by hand in isolated pipecat 1.9.0, 1.11.0 and 1.12.0
  environments (not in CI). The diarization startup preflight exits non-zero
  on a pipecat that does neither; a checkout's `uv.lock` pins 1.12.0. For the
  queue shape the probe also requires one segment `put` per VAD stop and a
  single reader loop that awaits `run_stt` once per segment until the finish
  sentinel; a reader that transcribes one segment and returns, or skips or
  repeats `run_stt`, is refused. For the inline shape it likewise requires one
  unconditional `await self.process_generator(self.run_stt(...))` per stop: a
  handler that returns early, or calls `run_stt` under a branch, loop or `try`,
  is refused.
- **Diarization needs a 160-sample model row hop.** The worker's load-time
  check now also requires the model's `processor_config.hop_length` to be
  exactly the integer 160, not only `output_subsampling_factor == 1`. A model
  revision with `hop_length=320`, which passed before, now logs one WARNING and
  the session carries on with unlabelled `them` lines.
- **`scripts/release_check.sh` gained a diarization-contract check (check 5).**
  It runs `tests/test_diarization_contract.py` under
  `ONOATS_REQUIRE_DIARIZATION_CONTRACT=1` via `uv run --extra diarization`,
  so a missing or drifted `mlx-audio` fails instead of skipping. Unlike checks
  1-4 it runs against the working tree, so it fails unless `HEAD` equals the
  commit under check and the tree is clean. The weights-dependent contract
  tests still skip when the model is not in the local HF cache; pre-cache it on
  the release machine. The contract checks the model's row rate against the
  audio fed: N whole 160-sample frames plus the final flush must yield exactly N
  rows.

### Fixed

- **Final transcript lost at shutdown on pipecat 1.9+ (websocket STT).** The
  websocket STT's graceful `stop()` closed the session before pipecat's own
  `stop()` drained its queued segments, so a segment still decoding at shutdown
  never got its transcript and the shutdown drain timed out. The session now
  closes only after pipecat's drain returns (a cancelled drain still takes the
  hard-cancel path). No change on pipecat 1.3.0-1.8.x, which queue nothing.

## [1.3.0] - 2026-09-26

### Added

- **Capturer mic stalled for tens of seconds at start.** `AudioDeviceStart` on the
  built-in mic blocks when a system-audio tap already exists in the process, so the
  capturer now starts the mic before the tap.
- **Capturer mic-bind timing diagnostic.** The capturer now logs any `bind()`
  step slower than 1 s, a `WARNING mic: bind still blocked in '<step>'` after 5 s
  if a CoreAudio call has not returned, and the total bind time on the
  `mic: capturing from …` line. Diagnostic only (the fixes below change behaviour separately).
- **Menu-bar Seminar toggle.** A "Seminar (next recording)" toggle makes the
  next Start run `onoats bot --category seminars`; it resets when that session
  ends. Requires `seminars` in `config.toml` `[categories] set`.
- **STT server self-healing.** New optional `[stt].launchd_label` (env
  `STT_LAUNCHD_LABEL`) names the `launchctl` job onoats may restart when the
  STT server is unreachable. When set, both the startup preflight and a live
  session's exhausted reconnect backoff fire one
  `launchctl kickstart -k gui/$UID/<label>`, capped by a process-wide,
  label-keyed cooldown shared by the mic and system pipelines. Recovery is
  reported only after a handshake actually succeeds (never on `launchctl`'s
  exit code) and surfaces in `onoats status` / the menu bar via the `stt`,
  `stt-mic` and `stt-system` warning branches. Absent by default — today's
  behaviour is unchanged for existing installs.
- **Onoats.app launch at login.** New optional `[app].launch_at_login` (Swift
  side only; the Python config reader never reads it) registers or unregisters
  the menu-bar app with `SMAppService` at launch. Absent means *no action at
  all* — an out-of-band login-item registration is never touched; an explicit
  `false` unregisters. A malformed config value or a failed register/unregister
  call surfaces as a `⚠` login-item warning line in the menu bar (does not
  block Start/Stop).

### Changed

- `status.warning` is now written per branch (`set_warning_branch`) instead of
  whole-field, so the capturer's `mic`/`system` warnings and the new `stt*`
  warnings coexist and clear independently. On-disk format and
  `STATUS_SCHEMA_VERSION` are unchanged.
- `onoats init` no longer drops `[stt].launchd_label` or the `[app]` section
  when regenerating `config.toml` on a re-run.
- **Credential redaction (`onoats._redact`) rewritten.** The tiered scanner
  (three widening searches, each with its own accept conditions and its own
  vetoes) is replaced by a single candidate scan behind positive gates.
  Four credential leaks are closed — a veto added for one tier had been
  silently disabling a protection another tier relied on, which is why each
  previous round's patch reopened an older leak from a new angle. Also
  closed: a password containing `://` leaked its username and password
  prefix verbatim. The specification is now a generated sweep in
  `tests/test_redact.py` (every combination of scheme, username shape,
  password delimiter, host shape and trailing prose — ~2000 cases), not a
  hand-picked case list; hand-picked lists are what missed every gap.
- `safe_exc_text()` now drops a URI's query string as well as its userinfo,
  matching what the whole-URI display call sites already did. A
  `?token=...` in an operator-supplied `STT_WS_URI` was reaching the
  reconnect warning and the status-file warnings verbatim inside
  `websockets.InvalidURI`'s message. New `display_uri()` is the single
  owner of the redact-then-strip-query composition both display call sites
  had been open-coding.

### Fixed

- **Capturer mic no longer wedges when the first CoreAudio bind stalls.** The
  first `bind()` runs off the main thread with a 5 s bounded wait, so the
  capturer reaches `streaming` and handles signals even if `AudioDeviceStart`
  blocks in coreaudiod (seen 60+ s on the built-in mic). While unbound it
  retries on fresh threads (every 10 s, up to 3); a late bind that loses the race
  discards its own IOProc. A bound mic that delivers no real data for 10 s is
  rebound (mic only — the system tap legitimately goes quiet).
- Redaction no longer destroys the hostname it was protecting. A ported,
  path-and-query-bearing URI (`wss://host:443/v1?redirect=user@example.com`)
  had its real host discarded and one fabricated from the query's tail; so
  did any message where a real credential was followed by an unrelated
  email address in trailing prose. Both reached `onoats status` and every
  STT log line.
- STT shutdown no longer burns the full reader-join timeout on every close.
  `_graceful_close` cleared the reader's ownership handle before sending
  `session.close`, so the reader discarded the `session.closed` ack the
  close was waiting for — several seconds of dead shutdown time per
  pipeline, doubled for the mic/system pair.
- Cancelling an STT connect between the handshake and the `session.update`
  ack no longer leaves the instance marked connected on an unconfigured
  session.

## [1.2.0] - 2026-06-28

### Changed
- Bumped `pipecat-local-stt-server` from `>=0.1.2,<0.2` to `>=0.3.3,<0.4`,
  keeping the bundled client library in lockstep with the running server
  generation. 0.3.3 adds server-side UDS trust-boundary hardening (0700
  socket-directory enforcement + peer-credential `403` for cross-uid
  connections); the wire protocol stays `"0.1"`, so the recorder's client
  code is unchanged. `websockets` now resolves to 16.0 (satisfies onoats's
  `>=13` and the server's new `>=16,<17`). **Operator note:** the 0.3.3
  server refuses to start on a legacy `0755` socket dir — re-run the
  server installer (don't `git pull` + bare `launchctl kickstart`) so it
  self-heals `0755`→`0700` and restarts in one step.

### Added
- `onoats stop` subcommand: signal the running recorder to stop gracefully
  (SIGTERM → drain + final flush, then exit). It is a behavioural twin of
  `onoats flush` and reuses the same identity gate (`resolve_flush_target`:
  marker + cmdline-fingerprint + liveness), so it only ever signals the verified
  recorder and never a recycled pid — which matters more than for flush because
  SIGTERM kills by default. Returns on signal delivery, not confirmed exit; like
  flush, `onoats stop --help` resolves without booting a service.
- Menu-bar **Stop now works for orphaned/external sessions**: a GUI-started
  recorder orphaned by an app crash (seen as `running(ours: false)` on relaunch),
  or any terminal-started session, can be stopped from the menu. Owned sessions
  keep the in-handle `Process.terminate()`; verified external sessions route
  through `onoats stop`. The menu shows "stopping (draining)…" for the whole
  drain and flips to Stopped only when the supervisor actually exits (polled),
  never faking a terminal state. (Completes the stoppable-orphan-session fix.)

### Fixed
- **Stop→immediate-start pid-file race.** `onoats stop` returns on signal
  delivery, not exit, so a new `onoats bot` launched during the old recorder's
  drain could overwrite the draining recorder's pid file — and the drainer would
  then unlink the *new* recorder's file, leaving it invisible to
  `status`/`stop`/`flush`. Guards close this: (1) an **atomic `flock`
  single-instance lock** acquired before any capture side effect — the socket
  supervisor takes an exclusive `flock(LOCK_EX|LOCK_NB)` on `.active/onoats.lock`
  *before spawning the capturer*, `run_onoats_dual` *before opening PortAudio*, and
  `run_onoats` (`bot-single` / `python -m onoats`) *before importing the native
  deps*, so of N racing starts exactly one wins and the rest raise
  `RecorderAlreadyRunningError` **before touching CoreAudio/TCC/a device**; held
  for the whole process lifetime and released by the kernel on exit (graceful or
  crash), so there is no stale lock to reclaim, and a chained `onoats stop &&
  onoats bot` cleanly refuses until the drainer's process exits; (2) the identity
  check (`resolve_flush_target`) remains as a secondary guard refusing a verified
  or indeterminate-but-live recorder (legacy/cross-version), never blocking on a
  stale/recycled/foreign pid; (3) pid-file writes are atomic (temp + `os.replace`,
  never a truncating in-place write) so a concurrent reader never sees an
  empty/partial file; (4) pid-file removal is ownership-checked and fails closed —
  a recorder unlinks only a file that still records *its own* pid, and leaves an
  unreadable/foreign record in place rather than deleting a newer recorder's
  (possibly in-progress) file; (5) `stop`/`flush` stale cleanup is
  **compare-and-unlink** (not a blind `unlink`) so a fresh recorder that won the
  lock and wrote its pid in the resolve→cleanup window is never deleted. The menu's external Stop also re-enables itself if
  the `onoats stop` subprocess fails or exits non-zero (e.g. a stale installed
  CLI), rather than wedging the only Stop control until app restart.

## [1.1.0] - 2026-06-12

First PyPI release (`pip install onoats` / `uv tool install onoats`).

### Changed
- `pipecat-local-stt-server` is now consumed from PyPI (`>=0.1.2,<0.2`)
  instead of a git-URL pin — `0.1.2` is the release of the exact commit
  previously pinned (`5062b98` == tag `v0.1.2`). This removes the
  direct-reference metadata PyPI rejects, unblocking publication. Packaging
  metadata (readme, authors, URLs, classifiers) added for the PyPI page.

### Added
- Tag-triggered PyPI publish via GitHub Actions trusted publishing
  (`.github/workflows/release.yml`): pushing a `v*` tag runs the full suite
  plus two guards (tag == pyproject version; no direct-URL deps in wheel
  metadata), then publishes via OIDC behind the `pypi` GitHub environment —
  no stored token.
- `[stt] language` in `config.toml`: the STT decode language is now a
  first-class config key (env `STT_LANGUAGE` > legacy alias `STT_WS_LANGUAGE`
  > `[stt].language` > `en`),
  shared by every launch path (CLI, menu-bar app, `onoats init`). `auto`
  means auto-detect and maps to `None` at the backend boundary. The local
  whisper/MLX branches now honour it too (they previously hardcoded `en`);
  Deepgram does not consume it. `onoats init` prompts for it in the local
  STT branch. (PR #22)

### Fixed
- Review fixes on the language key (PR #22): a whitespace-only
  `[stt].language` now falls back to `en` instead of reaching the backend as
  `language=""`; non-interactive `onoats init` re-runs carry an existing
  `language` forward instead of silently erasing it; switching the wizard to
  Deepgram preserves the key for a later switch back.

## [1.0.0] - 2026-06-12

First stable release. Closes out the 0.9.x series' 1.0.0 gates: pre-socket
tap preflight (PR #17), BlackHole prose pruning + the `setup-cli` install
path (PR #18), and the ConfigStore TOML-subset parity suite with its CRLF
fix (PR #19).

### Added
- `make -C native setup-cli` (release-plan Phase 8): one-command CLI +
  native-capture install (cert → capturer build/sign → CLI shim → `onoats
  init`) that skips the menu-bar app bundle. README now documents the three
  install paths side by side: menubar (`setup`), CLI + native capture
  (`setup-cli`, macOS 14.4+), and CLI + PortAudio (toolchain-free, see
  `docs/blackhole-fallback.md`).

### Changed
- BlackHole prose pruned to the conservative keep-list (release-plan
  Phase 8): redundant README mentions and the `pyproject.toml` `[macos]`
  comment now point at `docs/blackhole-fallback.md`; the `_LOOPBACK_HINTS`
  auto-detection, no-loopback NOTE, backend help text, and all config-wiring
  tests are unchanged.
- Pre-socket tap preflight (release-plan Phase 7, PR #17): the capturer makes
  the TCC-prompting tap call **before** binding its sockets, announced by
  `ONOATS-EVENT waiting-for-permission`. A first Start with the Screen &
  System Audio Recording dialog unanswered no longer dies at ~10 s — the
  supervisor extends its socket wait once (+120 s) and surfaces "waiting for
  the system-audio permission prompt" in the status file / menu bar.
- rc=11 `exit_reason` renamed `system-audio-denied` → `system-audio-failed`:
  a TCC denial never exits the capturer (denied taps deliver zeros and
  surface as the zero-run warning); rc=11 fires only on genuine tap API
  failure.

### Fixed
- Menu-bar settings edits no longer corrupt a `config.toml` with CRLF line
  endings (release-plan Phase 9): ConfigStore's line scan trimmed with a
  whitespace set that excludes `\r`, so CRLF section headers were never
  matched and `writeValue` appended a duplicate section that fails TOML
  parsing. Untouched lines keep their CRLF bytes verbatim; the edited line
  is written with LF. Pinned by the new TOML-subset parity suite
  (12 cases: comments, whitespace variants, absent section/key, duplicate
  keys, non-string neighbours, byte-identity, CRLF).
- Mic silence pacer now activates before the HAL bind, so a slow bind
  (pending TCC dialog, Bluetooth device activation) can no longer trip the
  recorder's 10 s read-idle and kill the session.
- Latent start-timeout stamping bug: the prestart failure stamp read the
  capturer's exit code after reaping it, so a hung-but-alive capturer was
  mislabeled `capturer-start-failed`; `capturer-start-timeout` is now
  reachable end-to-end.

## [0.9.0] - 2026-06-11

Milestone B: native macOS capture + menu-bar app (PR #5, `16da012`; docs
ride-alongs PR #6 `6aa0025`, PR #7 `8db8840`).

### Added
- Native macOS system-audio capture via a Core Audio process tap
  (`onoats-capturer` Swift binary); no loopback driver needed on macOS 14.4+.
- SwiftUI menu-bar app (`Onoats.app`): Start/Flush/Stop, inline mic picker
  (sets the macOS default input), STT service picker, data-dir chooser,
  status display.
- Per-chunk capture-generation stamping — each queued audio chunk carries its
  generation's format and resampler, preventing stale-format races on device
  switch.
- Self-signed signing identity workflow: `make -C native cert` (refuses to
  regenerate an existing cert) + `make -C native install` / `install-cli`.
- Sustained all-zero-input detector (30 s) in the capturer — surfaces a
  denied system-audio grant as a WARNING instead of silent empty recordings.
- Kill-×3 tap/aggregate residue smoke (`native/residue_check.sh`) and
  one-command wire smoke (`native/smoke_wire_check.sh`).

### Changed
- The native capturer is the default macOS capture story;
  BlackHole/PortAudio demoted to the documented fallback (macOS 13.x–14.3 /
  off-mac).
- `ConfigStore` (menu bar) writes TOML with correct basic-string escaping;
  data-dir handling made canonical and per-session.

### Fixed
- Pre-start capturer death now writes a status record — a denied grant no
  longer surfaces as "failed: graceful".
- Realtime-thread logging and state races; fail-loud flush path; IOProc
  zero-guard.

### Notes
- The runtime dependency `pipecat-local-stt-server` is intentionally
  git-pinned (`pyproject.toml`) — correct for the from-source install story;
  revisit only if PyPI publishing is ever taken up.

## [0.8.0] - 2026-06-09

Milestone A: socket audio transport + supervisor (PR #4, `1f9dfdc`).

### Added
- `UnixSocketAudioTransport` — framed-PCM16 Unix-socket audio input pipeline
  with bounded staging, downstream-queue gating, and fail-loud fatal errors.
- `AUDIO_SOURCE=portaudio|socket` backend switch, branched in exactly one
  place per layer.
- CLI supervisor: private `0700` socket dir, per-session generation nonce
  (handshake-enforced), bounded socket-appearance wait, process-group
  teardown on both crash and graceful paths.
- Audio-socket wire contract document (`docs/audio-socket-contract.md`) with
  a parity test that fails CI when doc and code constants drift.
- `AGENTS.md` contract notes; dev-plan review-marker CI gate
  (`scripts/check_review_markers.py`).

### Changed
- Capturer environment built from a deny-by-default allowlist (blocks DYLD
  injection); capturer spawned in an isolated session so terminal signals
  don't trip the fail-loud path.

### Fixed
- Transport failures are fatal upstream errors, so the recorder terminates
  instead of hanging; handshake reads bounded by the idle watchdog; tilde
  expansion on socket paths; capturer group swept on the crash path.

## [0.7.0] - 2026-06-08

Packaging and extraction era: standalone `onoats` package (untagged;
PR #1 `7f33e72`, PR #2 `645d90b`, PR #3 `8e6c165`, plus direct commits).

### Added
- `src/onoats/` installable package layout, extracted from the `bot/`
  monolith; `onoats init` CLI; CI with ruff + PortAudio build steps.
- Configurable `[storage].data_dir` (`config.toml` + `onoats init
  --data-dir`); XDG data paths.
- `STT_WS_LANGUAGE` env to control the websocket decoder language.

### Changed
- `config.toml` honoured in the recorder process for STT and device
  settings; SQLite overlay removed; transcript date grouping uses local
  date, not UTC.
- STT server renamed `onoats-stt` → `pipecat-stt` (server v0.2.0): socket
  paths and labels updated.

### Fixed
- STT RSS probe resolves `stt_server` from the config-layered env (PR #1).
- Flush (SIGUSR1) verifies live process identity before signalling — a stale
  PID file can no longer kill an unrelated process (PR #2).
- Shutdown drains the final transcript segment before flush; Ctrl+C cancel
  timeout capped (no more ~20 s hang); device provenance logged accurately
  (PR #3).

## [0.5.0] - 2026-05-21

Dual-input diarization and STT-service era (untagged; pre-extraction
history, cited by merge SHA).

### Added
- Standalone Whisper WebSocket transcription server with a Pipecat
  `STTService` wrapper and launchd auto-restart (`c4f08d5`).
- STT health preflight, status probe, and `./onoats stt` wrapper
  (`e40f2de`).
- Live passive transcript view with rotation and orphan guards (`2536687`).
- SmartTurn V3 read-only shadow observer on the `me` branch + raw PCM dump
  for offline A/B testing (`0e4ae64`).
- Parakeet ASR backend with per-server multi-ASR selection (`21ebfae`).
- Cron-driven post-processing worker decoupled from the bot harness; the
  bot rotates session files instead of processing in-flight (`4d54b88`,
  the `stt-extraction-base` tag's commit).

### Changed
- Dual-input pipeline became the default bot path — coarse Me/Them
  diarization via two PortAudio branches (`196aef2`).
- Post-processing queue rebuilt as an FSM; the bot harness no longer owns
  worker state (`4d54b88`).

### Fixed
- Continuation-flush session-file swap race; speaker-label plumbing and
  source resolution; STT reconnect backoff made exponential (0.5 → 8 s);
  long turns chunked.

## [0.3.0] - 2026-04-16

Processing-pipeline era (untagged; pre-extraction history).

### Added
- Topic discovery, collation, and management pipeline (`6124d56`).
- Multi-task LLM benchmark, LM Studio provider, per-task LLM routing, and
  manual transcript flush via Ctrl+T / `./onoats flush` (`ced152a`).
- "seminars" processing category + `--category` bot CLI flag (`81afbc0`).

### Fixed
- Timeline corruption: ownership-aware rollback + cross-midnight concat in
  segmenter/classifier/merge (`7b97d7e`).
- PID-file identity hardened; pipeline cancel interruptible on Ctrl+C
  (`ced152a`).
- `ONOATS_DATA_DIR` tilde expansion across all entry points.

## [0.1.0] - 2026-04-04

Initial recorder (untagged; root commit `d645650`, 2026-03-30).

### Added
- Pipecat-based meeting recorder: PortAudio input, silence detection,
  transcript buffering, Ctrl+C graceful shutdown with the current session
  processed before exit.
- SQLite overlay, web intelligence layer, and transcript management
  (`3d36ae3`).
- Collation service: living documents aggregated from related idea
  transcripts (`5cd908c`).
- LLM transcript cleanup + dictionary/provenance plumbing (`9d974c1`).
