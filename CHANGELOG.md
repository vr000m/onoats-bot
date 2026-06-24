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
