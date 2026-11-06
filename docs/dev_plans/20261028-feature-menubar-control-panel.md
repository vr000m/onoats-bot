# Task: Onoats Menu-Bar Control Panel and Brand Mark

**Status**: In Progress
**Component**: recorder, stt, macos, packaging
**Assigned to**: Codex
**Priority**: High
**Branch**: feature/menubar-control-panel
**Created**: 2026-10-28
**Review Gates**: full

## Objective

Replace Onoats.app's flat menu with a compact window-style recording panel centred on Start, Stop, and the optional Seminar lock. Add truthful live status, reorganise infrequent controls under an in-panel settings view, and establish a reusable Onoats brand mark plus a monochrome macOS menu-bar icon without changing recorder safety contracts.

## Context

The current SwiftUI `MenuBarExtra` uses the default menu presentation. It places lifecycle controls, Seminar, device selection, STT selection, storage paths, diagnostics, and Quit in one flat list. The product owner confirmed the primary menu-bar workflow is narrower: open the menu-bar item, optionally select Seminar, then Start or Stop. Phase 1 validates the brand direction; The Post-Gauntlet Release Steps validate the final control hierarchy in the signed native smoke.

The recorder already exposes authoritative process liveness, warnings, start time, device names, and a running STT label. It does not expose per-branch activity, structured STT health, or a safe last-transcript preview. Those values must come from the runtime that owns the audio and STT connections; Swift must not infer them from an endpoint probe or stale session file.

This repository contains no website source or existing logo assets. This plan creates canonical brand assets and integrates them into Onoats.app. Applying the brand to onoats.app itself remains a separate repository task once that checkout is identified.

Explore facts in this plan are point-in-time contract facts. A later correction to a verified path, pattern, or dependency invalidates the review marker and requires `/review-plan` again.

## Requirements

- Use `.menuBarExtraStyle(.window)` and a compact fixed-width panel.
- Make Start or Stop the dominant control. Preserve its current lifecycle routing:
  - app-owned Stop terminates the retained supervisor `Process` gracefully;
  - external-session Stop invokes identity-checked `onoats stop`;
  - Flush invokes identity-checked `onoats flush`.
- Show the Seminar control only before Start. Preserve its next-session-only, in-memory, reset-on-exit semantics. During an app-owned locked session, keep that selected lock as the badge authority even if telemetry disappears. During an external session, show the badge only from fresh same-owner telemetry; otherwise show `Category unavailable` rather than assuming Seminar.
- Keep warnings, failure detail, schema drift, external-session state, stopping/draining state, and login-item hints visible and accessible.
- Keep STT operational state visible on the main panel. Put the STT service picker and advanced endpoint access inside the settings view.
- Move microphone selection, data-directory selection, config access, CLI diagnostics, and Quit into an in-panel settings view reached by a gear button.
- Preserve `config.toml` as the shared source of truth for recorder settings. UI-only presentation preferences may use `UserDefaults`.
- Do not add a launch-at-login toggle. `config.toml` remains its only control surface.
- Show Me/Them activity only when both the runtime snapshot and that branch's last audio observation are fresh. Render a branch as a visible greyed `No signal` row after one second without a new observation; never convert missing or stale data into a healthy signal.
- Scope new activity telemetry to the CoreAudio/socket path used by Onoats.app. Preserve PortAudio and `bot-single` as unchanged CLI compatibility paths; an externally started unsupported session shows activity unavailable.
- Present one combined STT row for the menu-bar app's two required branches: `failed` if either fails, `recovering` if either recovers, `starting` if either starts, `unknown` if either branch's backend publishes no state (`whisper`, `deepgram`), and `ready` only when both websocket branches confirm ready. Show branch detail only when the aggregate is not ready. Swift recomputes this aggregate and rejects a snapshot whose published aggregate disagrees.
- Make the last-transcript preview opt-in and off by default. Bound its length and keep the full transcript only in the existing session JSONL.
- Keep the lifecycle status schema at v2. Live display telemetry uses its own versioned, ephemeral contract.
- Telemetry failures must fail open: they may degrade the panel, but must never stop capture, STT, transcript persistence, or shutdown.
- Preserve never-mix, no-PortAudio-on-socket, bounded transport, status/PID ordering, graceful drain, and Swift/Python parity invariants.
- Create two related but distinct visual assets:
  - a full Onoats brand mark/wordmark for the app header and future website use;
  - a monochrome template icon that remains legible at macOS menu-bar sizes.
- Keep canonical vector brand sources in the repository. Commit required native exports and copy them explicitly into the signed app bundle.
- Snapshot the configured data directory synchronously at Start and pin it until that recorder exits. Export it to the supervisor as `ONOATS_DATA_DIR`, use it for status/PID/telemetry reads and exit handling, and pass it explicitly to external Stop and Flush. A changed configured directory applies on the next Start.
- When the configured directory changes mid-session, show both `Recording to: <active>` and `Next recording: <configured>` in the panel until convergence.
- Expose `Open Log` on warning/failure surfaces of app-owned sessions and failed app-owned starts only, and expose both `Open Log` and `Show Logs Folder` under Settings → Diagnostics. The canonical log is the app-captured stdout/stderr file of the most recent app-owned run: one `onoats-bot-<spawnEpoch>.log` per run in the existing Logs folder, newest 10 kept. `<spawnEpoch>` is the Swift app's Start-attempt time in integer epoch milliseconds, taken just before the spawn; it is unrelated to the Python-minted recorder run epoch. `Open Log` opens the in-memory path of the most recent app-owned run of this app process; if that file is gone it reveals the Logs folder. Only when the app holds no in-memory path (fresh launch) does it first scan for the newest `onoats-bot-<spawnEpoch>.log`, still falling back to the folder when none exists. The app-owned Flush helper's output appends to that run's file; app-owned Stop terminates the retained supervisor process and has no helper output stream. External Stop and Flush output is not written to a run log. An external session's warning says `Log not captured (started outside the menu bar)` and offers no `Open Log`. Fall back to the folder when the log file does not exist; do not embed a live log tail.
- Give the panel a compact text identity that renders `Oats - Organized Transcript System` without depending on transparent padding inside the header bitmap. Keep the canonical mark available for app/icon reuse, but use native typography and a small mark in the panel header. Present Seminar as a labelled next-recording option with a native switch, explanatory secondary text, and a grouped control surface rather than a bare checkbox row.
- A successful Start is not sufficient recording evidence. Add a deterministic full recorder-path integration test that feeds known speech PCM into the mic socket and requires a durable `source=me` utterance. Add a signed installed-app smoke that exercises the real app-owned capturer/TCC path, requires a fresh non-zero Me observation, then requires a new durable Me utterance after Stop. The smoke must fail when only process/socket readiness is observed.
- Bind installed-smoke evidence to recorder-attested run identity, not filenames or directory novelty. Publish `TranscriptBuffer.run_id` as required schema-v2 UI telemetry, accept it only with the matching recorder PID/start epoch, and require every proof JSONL file's first-line `session_meta.run_id` to match. A pre-launch corpus scan is defence in depth only; moving, renaming, copying, or hard-linking historical JSONL files must never create evidence.
- Treat installed-smoke teardown as captured-identity cleanup. Once the smoke captures an exact recorder identity and descendant-group membership, it must boundedly clean those identities even if the app exits first. App ancestry gates discovery; it does not invalidate already captured ownership. Every TERM/KILL rechecks PID, command, PGID, captured member identity, and the macOS kernel process-birth tuple (`pbi_start_tvsec`, `pbi_start_tvusec`) from `proc_pidinfo(PROC_PIDTBSDINFO)`; second-resolution `ps lstart` is never signal authority.
- Validate installed-smoke telemetry as adversarial input. PID and run epoch must match; level and timestamps must be finite; level must be within `[0.0, 1.0]`; and snapshot/observation ages must be non-negative and within their freshness windows. `NaN`, `Infinity`, and future timestamps must never satisfy microphone proof.
- Do not change the website in this plan because its source is not present in this checkout.

## Review Focus

- Verify that UI changes preserve identity-checked Stop/Flush, authoritative PID liveness, and draining-state behaviour.
- Verify telemetry ownership, freshness, atomic replacement, bounded update rate, owner-checked cleanup, and fail-open error handling.
- Verify audio-level instrumentation cannot mix Me and Them or bypass transport backpressure.
- Verify STT colours never claim endpoint health from configuration, process existence, or an independent Swift probe.
- Verify transcript-preview privacy, truncation, file permissions, and stale-session rejection.
- Verify native resources remain inside the explicitly signed bundle and do not alter its bundle identifier or designated-requirement strategy.
- Verify every cross-language literal or field has parity coverage and that the lifecycle status schema remains unchanged.
- Verify the native manual smoke matrix covers keyboard access, VoiceOver labels, panel dismissal, and all existing actions.
- Verify the menu-bar scope does not add telemetry requirements to PortAudio or `bot-single`.
- Verify log actions open only the canonical log file/folder and never render log content inside the popover.
- Verify per-branch observation ages cannot be refreshed by a snapshot heartbeat and that aggregate STT drift is rejected.
- Verify active-directory pinning governs process launch, reads, Flush, external Stop, and exit handling.
- Verify the pre-PID permission-wait warning decorates only the matching app-owned starting attempt and never establishes recorder liveness.
- Verify staged smoke and installation exercise the same verified app hash and CLI git revision, with explicit clean-install and rollback-baseline behaviour.
- Verify telemetry cleanup is bounded, regular-file-only, descriptor-read, and compare-and-unlink safe against replacement races.
- Verify run-log output keeps descriptor/inode authority from exclusive reservation through every app-owned child writer; no child reopens the pathname.
- Verify installer archive checks and extraction consume one immutable descriptor-backed snapshot, normalise or reject unsafe copied metadata, and never leave a second `.app` after cleanup failure.
- Verify the panel renders the `Oats - Organized Transcript System` identity, an elegant next-recording Seminar switch, and truthful Silent/Signal/No signal labels.
- Verify recording acceptance reaches durable Me transcription through both a deterministic socket fixture and the signed installed-app microphone path.
- Verify installed-smoke cleanup still reaches captured recorder and capturer identities when the app disappears first, and that every signal requires an immediate kernel birth-tuple plus secondary-identity recheck. The residual kernel race between that recheck and signal delivery is accepted and must not be described as eliminated.
- Verify transcript acceptance is keyed by a new `session_meta.run_id`, so historical JSONL renames, startup recovery, and continuation-file movement cannot manufacture durable Me evidence.
- Verify installed-smoke telemetry rejects non-finite numbers, out-of-range levels, negative ages, and future timestamps before evaluating the Me floor.
- Verify telemetry-attested transcript run identity flows from `TranscriptBuffer` through schema-v2 Python/Swift parity and matches JSONL line 1; directory novelty remains non-authoritative.
- Verify every cleanup signal rechecks the kernel process-birth tuple and that PID/command/PGID/second-level start collisions cannot pass.
- Verify every external Stop/Flush request carries the complete PID/start-epoch generation, the server requires same-UID Darwin peer credentials, and no normal CLI path falls back to PID signalling.
- Verify both recorder entrypoints bind the control server before identity publication and close its owned endpoint before exact-record PID teardown; simultaneous commands follow the documented lock-order state machine.
- Verify staged-installer operator values never enter Make expansion, legacy Make-variable overrides fail before Python or mutation, and Swift decodes with the parity-checked run-ID pattern.

## Implementation Checklist

### Phase 1: Brand Direction and Canonical Assets (manual design gate; not `/conduct`-driven)

**Goal:** Approve one recognisable Onoats mark before native integration. The full brand and the menu-bar glyph must share a visual idea without forcing a detailed colour logo into an 18-point monochrome slot.

- Produce two or three compact directions based on an oat-grain/speech-waveform motif.
- Review each direction at website/header scale and at 16, 18, and 22 point monochrome menu-bar scale.
- Obtain user approval for one direction before creating production exports.
- Create the new `assets/brand` and `native/onoats-menubar/Resources` directories explicitly.
- Add canonical SVG mark and wordmark sources plus documented colour, spacing, and monochrome rules.
- Export the app-header image, one monochrome template menu icon per recorder state (idle, recording, warning, busy, failed) at 1x/2x, and the `.icns` app icon.
- Record export sizes and source-to-export mapping so later website work can reuse the same source.
- Before any Phase 2 code change, record the rollback baseline under `## Findings` below the marker: `git rev-parse HEAD` (with an empty `git status --porcelain`) as the baseline CLI SHA, the current branch name or detached state, the exact `install-cli` command and `EXTRAS`, `command -v onoats`, the installed app's designated requirement, and `make -C native cdhash` of the installed `Onoats.app` (or `none` when no app is installed). The Post-Gauntlet rollback preflight compares against exactly these values. A missing baseline field makes this release a clean-install case with no rollback claim; never reconstruct a missing value after implementation begins.

### Phase 2: Ephemeral UI Telemetry Contract and Runtime Producer

**Impl files:** `src/onoats/ui_telemetry.py, src/onoats/transports/socket_audio.py, src/onoats/dual.py, src/onoats/processors/transcript_buffer.py, src/onoats/runtime.py, src/onoats/stt/websocket_stt_service.py, src/onoats/__main__.py`
**Test files:** `tests/test_ui_telemetry.py, tests/test_socket_audio_transport.py, tests/test_dual_socket_source.py, tests/test_websocket_stt_reconnect.py, tests/test_speaker_labels.py, tests/test_status_file.py, tests/test_socket_supervisor.py, tests/test_stt_config_wiring.py, tests/test_runtime_preflight.py`
**Test command:** `uv run pytest tests/test_ui_telemetry.py tests/test_socket_audio_transport.py tests/test_dual_socket_source.py tests/test_websocket_stt_reconnect.py tests/test_speaker_labels.py tests/test_status_file.py tests/test_socket_supervisor.py tests/test_stt_config_wiring.py tests/test_runtime_preflight.py -q`
**Goal:** Runtime telemetry is display-only, branch-safe, stale-rejectable, bounded, and incapable of affecting recorder success or transcript durability.

- Add a separate versioned `.active/onoats.ui.json` snapshot. Phase 9 bumps this ephemeral contract from schema 1 to schema 2 for recorder-attested transcript identity. Do not add fields to `StatusRecord` or bump `STATUS_SCHEMA_VERSION`.
- Define one exact record shape shared by Python tests and Swift decoding: schema, recorder PID/start epoch, required schema-v2 `transcript_run_id`, update time, optional Me/Them levels with per-branch observation timestamps, per-branch plus aggregate STT state/detail, optional bounded transcript preview/source/speaker label, and locked category.
- Write snapshots by same-directory temporary file plus `os.replace`, with owner-only permissions. Because the file is ephemeral, do not `fsync` every meter update.
- Define Python-owned constants (the Swift mirror and telemetry parity tests land together; the stable category-error tag lands in Phase 3 and its Swift pin lands with the matcher in Phase 6): schema `2` after Phase 9, file name `onoats.ui.json`, mode `0o600`, maximum write rate `4 Hz`, unchanged-state heartbeat `2 s`, snapshot expiry `5 s`, per-branch audio-observation expiry `1 s`, per-branch observation refresh `500 ms` while frames continue, the Seminar category literal `seminars`, transcript limit `240` Unicode code points, detail limit `256` Unicode code points, level range `[0.0, 1.0]`, and transcript-run grammar `^[0-9a-f]{32}$`. Every telemetry timestamp (snapshot time, per-branch observation times, recorder `start_epoch`) is wall-clock epoch seconds from `time.time()`, matching `status.py`; no monotonic clock crosses the file boundary.
- Let audio/STT/transcript callbacks update memory only. Coalesce changed snapshots and heartbeats through one bounded background task; no callback performs filesystem I/O or awaits persistence.
- Suppress redundant intermediate snapshots, but write an unchanged heartbeat every 2 seconds so healthy telemetry never expires. A heartbeat updates only snapshot time; it never advances either branch's audio-observation timestamp. An advancing branch observation timestamp counts as a changed snapshot: a branch with continuing frames publishes an observation-time refresh at least every 500 ms, inside the 4 Hz cap.
- Reject non-finite or out-of-range level values at the publisher boundary: drop that observation and keep the last good value. A rejected observation advances neither the level nor that branch's observation time, so a branch emitting only rejected values for more than 1 s renders `No signal`. There is no clamping. Boundary verdicts: `-0.0` accepted and published as `0.0`, `0.0` and `1.0` accepted, `1.0000001`, `inf`, and `nan` rejected. The snapshot writer uses `json.dumps(..., allow_nan=False)`, so a non-finite value can never reach the file. JSON cannot encode `inf` or `nan`, so the verdict table in `tests/fixtures/ui_telemetry/` stores level inputs as strings (`"-0.0"`, `"inf"`, `"nan"`); the Python tests and the Swift harness both read it and parse each with their own float parser.
- Construct telemetry only for `audio_source == "socket"`. Inject source-labelled level observers into the two existing socket transports through one named hook, `on_level: Callable[[float], None] | None = None` (keyword-only), added to the `UnixSocketAudioTransport` facade and its inner input transport. `_build_socket_transports(cfg, *, on_mic_level=None, on_system_level=None)` hands each branch its own callable, which closes over that branch's source. Keep the transport generic and preserve one socket to one branch. PortAudio and `bot-single` remain unchanged. Compute each frame's peak from the validated PCM16 bytes with `array('h')` max-abs divided by 32768 (`audioop` is removed in Python 3.13 and the project allows `>=3.12`; no new dependency). Wrap the observer so an exception is caught, logged once and rate-limited, and never raised into the reader.
- Publish transcript preview data from final `TranscriptionFrame` handling without changing JSONL entries or render-time speaker composition.
- Track mic and system STT state independently. Aggregate by fixed precedence `failed > recovering > starting > unknown > ready`; `ready` requires both branches ready. Use `unknown` for backends without a trustworthy confirmation hook. Treat branch states as the aggregate's source of truth. Per-state definitions live once in the UI Telemetry Contract. Runtime sets `starting` at construction for websocket instances only; a non-websocket instance is set to `unknown` at construction and never changes, so `starting` cannot outrank it forever. `WebSocketSTTService` gains keyword-only `on_ready()`, `on_recovering()`, and `on_failed(detail)` callbacks. They enter through new keyword-only `on_ready`, `on_recovering`, and `on_failed` parameters on `_create_stt_service` (beside `data_dir`, `branch_instance`, and `stamp_segment_samples`); the internally built `on_recovery` is not a factory parameter and is untouched. `on_recovering` also fires when `_read_events`' teardown detects a lost established connection (not on a deliberate `stop()`/`cancel()` close), because that path only clears `_connected` and relies on a demand-driven reconnect. Reconnect semantics do not change.
- Sanitize STT detail through existing URI/exception redaction before it reaches telemetry. Redaction happens in the caller (`runtime.py` or the STT service); `ui_telemetry.py` receives already-redacted text and only caps its length.
- Mint one run epoch (`time.time()`, via one `runtime.py` helper) before PID/status publication. `_write_pid_file` takes a required caller-minted `start_epoch` instead of reading the clock itself (no optional default, so no second clock read survives), `_write_status_running` forwards it as `start_time` (`status.write_running` already accepts it, so `status.py` needs no change), and both return success so `dual.py` can gate telemetry. `_write_pid_file` has three call sites: `dual.py` (two sites) and `run_onoats` (`bot-single`) in `src/onoats/__main__.py`; each mints and passes its epoch explicitly, and `bot-single` still builds no telemetry. Build the in-memory telemetry state holder before transports and STT services so their callbacks have a target, but enable file writes only after PID then running-status publication succeeds.
- Create the one publisher task in `dual.py` right after `_write_status_running` succeeds. In `_run_shutdown`, cancel and join it (bounded) before calling the module-level sync helper `_finalize_shutdown_status`. Give that helper an optional keyword-only `telemetry_cleanup` callable it runs between `_write_status_stopped` and `_remove_pid_file`; the default `None` keeps today's behaviour. Never let the cancel/join delay `force_exit_event` or `wait_or_force`.
- Make producer and cleanup errors warning-only. Cleanup may unlink only a snapshot that still belongs to the same PID/start epoch.
- Rate-limit repeated write/cleanup warnings, log one recovery after writes resume, and test both with a fake clock.
- Invalidate or remove live telemetry after the recorder writes its stopped status; a leftover file remains harmless because consumers reject stale ownership/freshness.
- Keep `ui_telemetry.py` a leaf module: stdlib only, no `onoats` imports. `socket_audio.py`, `TranscriptBuffer`, and `WebSocketSTTService` receive plain callables, never the module.
- Add pinning tests: a raising level observer does not disturb the socket reader or staging; `audio_source == "portaudio"` builds no publisher and `run_onoats` (`bot-single`) never constructs one; no telemetry file exists before `_write_status_running` returns and a raising `write_running` leaves none; PID line 4, status `start_time`, and the telemetry epoch are identical; `run_onoats` passes its own minted epoch to `_write_pid_file`; `_finalize_shutdown_status` runs `telemetry_cleanup` after the stopped status and before pid removal and is unchanged when it is `None` (extend the direct-call ordering test in `tests/test_socket_supervisor.py`); `_create_stt_service` forwards `on_ready`/`on_recovering`/`on_failed` (`tests/test_stt_config_wiring.py`); a lost established connection fires `on_recovering` once; a non-websocket instance is `unknown` from construction.

### Phase 3: Accepted Review Remediation for Completed Runtime Work

**Impl files:** `src/onoats/__main__.py, src/onoats/categories.py, src/onoats/dual.py`
**Test files:** `tests/test_categories.py, tests/test_cli.py, tests/test_socket_supervisor.py`
**Test command:** `uv run pytest tests/test_categories.py tests/test_cli.py tests/test_socket_supervisor.py -q`
**Validation cmd:** `uv run ruff check src/onoats/__main__.py src/onoats/categories.py src/onoats/dual.py tests/test_categories.py tests/test_cli.py tests/test_socket_supervisor.py`
**Goal:** Accepted review findings that cross the completed Phase 2 boundary land in a new committed phase; Phase 2's commit and evidence remain immutable.

- Define one stable machine tag, `ONOATS-ERROR invalid-category`, beside `InvalidCategoryError`. On an invalid category, the CLI writes the exact tag and the existing human-readable explanation to stderr before returning non-zero. The tag is the machine interface; prose remains display-only.
- Pin the tag at its Python producer and error type. Phase 6 adds the Swift matcher and cross-language parity assertion in the same commit as the matcher.
- Add an async shutdown regression whose telemetry publisher resists cancellation or exceeds the normal join bound. Assert shutdown remains bounded, a set `force_exit_event` keeps the zero-timeout path responsive, and stopped-status → telemetry cleanup → pid removal ordering remains intact.
- If the regression fails, repair only the bounded publisher-stop seam. Telemetry remains display-only and cannot delay durable shutdown.

### Phase 4: Swift Telemetry Consumer

**Impl files:** `native/onoats-menubar/Sources/UiTelemetry.swift, native/onoats-menubar/Sources/RecorderModel.swift`
**Test files:** `native/onoats-menubar/Tests/UiTelemetryDecoderHarness.swift, native/onoats-menubar/Tests/RecorderModelHarness.swift, native/test_ui_telemetry.sh, native/test_recorder_model.sh, tests/fixtures/ui_telemetry/, tests/test_native_contract_parity.py`
**Test command:** `native/test_ui_telemetry.sh && native/test_recorder_model.sh && uv run pytest tests/test_native_contract_parity.py -q`
**Validation cmd:** `make -C native build`

**Goal:** The menu app accepts only fresh telemetry for the same live recorder and degrades to neutral UI without changing recorder liveness.

- Add a Swift mirror of the UI telemetry schema without modifying the existing `StatusRecord` mirror.
- Resolve and snapshot the configured directory synchronously before an app-owned Start. Export it as `ONOATS_DATA_DIR`, retain it until that PID/start epoch exits, read status/PID/telemetry and exit status only from it, and pass `--data-dir <active>` to external Stop and Flush. For an externally discovered session, pin the directory that supplied the verified identity.
- Retain the changed configured directory as `nextDataDir` and publish active/next display values when they differ.
- Accept telemetry only when schema, PID, start epoch, required schema-v2 transcript run ID, snapshot freshness, numeric ranges, and optional-string bounds pass validation. Recompute aggregate STT state from the two branches and reject any mismatch with the published aggregate. Judge per-branch observation age (1 s) per branch after acceptance, so one stale branch renders `No signal` without dropping the other branch or STT state.
- Keep PID identity/liveness authoritative. Telemetry must never create `.running`, `.stopped`, or `.failed` state.
- Clear activity, STT health, telemetry-derived category, and preview when telemetry is missing, stale, malformed, or owned by another run. Preserve the app-owned in-memory Seminar lock as its badge authority; for an external session without trustworthy category telemetry, show `Category unavailable`.
- While the retained app-owned supervisor is alive but no recorder PID exists, accept `write_prestart_waiting` detail only when its status PID matches the retained `ProcessHandle` pid at runtime and its start time is no older than that Start attempt. It may decorate `.starting` but must never create `.running` or override PID liveness; clear it on supervisor exit or authoritative recorder publication. Do not assume a generated shim preserves pid: a forking `cliPath` wrapper fails the runtime match and gets no decoration, by design.
- Poll live telemetry at 4 Hz and animate between samples; keep status/PID polling at its existing cadence.
- Persist only the transcript-preview visibility preference in `UserDefaults`, defaulting to off.
- Add source-reading parity tests for the telemetry schema, allowed STT states, aggregate precedence, bounds, freshness constants, and file name. Pin the Seminar literal `seminars` across Swift `seminarCategory`, the `--category` argv, and the telemetry `category` field. Pin the `ONOATS_DATA_DIR` environment name and the `--data-dir` flag literals the Swift model uses against `_vendor/store.py` and the `onoats stop`/`onoats flush` argparse definitions. Add a source-reading assertion that the new Swift files contain no `URLSession` or `NWConnection` use, which enforces the no-Swift-network-probe rule. The Swift stable category-error tag pin lands in Phase 6 with its Swift constant.
- Create `native/onoats-menubar/Tests/` (it does not exist today) and add a small native decoder harness with shared JSON fixtures. Every fixture carries a `now` field (or offsets relative to it) that both the Python tests and the harness inject, so freshness cases cannot rot. Execute valid, malformed, stale, foreign-owner, boundary, non-finite, aggregate-mismatch, and non-ASCII 239/240/241-scalar cases instead of relying only on source-reading tests. Non-finite snapshots are raw text carrying `NaN`/`Infinity` tokens (not valid JSON), which the strict decoder must reject; boundary level inputs are the string-encoded verdict table. A 256/257-scalar STT detail case pins the detail cap; Swift performs no redaction.
- Introduce a `ProcessHandle` protocol for pid, liveness, termination, exit status, and exit callbacks. `SystemOps` owns process spawn, `kill`, file read/write/delete, and clock operations; its spawn method returns `ProcessHandle`. Inject `SystemOps` through `RecorderModel.init` with a production default, retain protocol handles instead of concrete `Process` values, and route the existing owned Start/Stop/Flush, `kill()`, `FileManager`, and `Timer` call sites through it. Add a native `RecorderModel` harness over that seam; `native/test_recorder_model.sh` compiles `Sources/*.swift` without the `@main` file `OnoatsMenuBarApp.swift`, moving any symbol the model needs out of that file. Exercise directory changes, PID reuse, old-run exit, next-run adoption, owned termination, external `onoats stop --data-dir`, `onoats flush --data-dir`, pre-PID permission detail with PID mismatch, older start time, equality boundary, supervisor exit, authoritative recorder publication, and drain-state convergence. Each pre-PID case asserts that detail clearing never changes recorder liveness.
- Compile with `make -C native build/Onoats` after each model change.

### Phase 5: Native Resource Pipeline and Stateful Menu Icon

**Impl files:** `native/Makefile, native/onoats-menubar/Info.plist, native/onoats-menubar/Sources/OnoatsMenuBarApp.swift, native/onoats-menubar/Resources/`
**Test files:** `tests/test_release_meta.py, tests/test_native_contract_parity.py`
**Test command:** `uv run pytest tests/test_release_meta.py tests/test_native_contract_parity.py -q`
**Validation cmd:** `make -C native sign`

**Goal:** The brand assets are copied and signed predictably, while the menu icon remains a valid macOS template image with distinct recorder states.

- Recreate the staging `Onoats.app` bundle before assembly so stale resources cannot survive an earlier build.
- Extend `native/Makefile` to create `Contents/Resources` and copy each named resource explicitly before signing.
- Verify the staged Resources directory against an explicit allowlist and fail on missing or unexpected entries. Exercise the failure branch once in this phase: plant an extra file, remove an expected one, confirm the verifier fails each time, and record the result.
- Add `CFBundleIconFile` without changing `CFBundleIdentifier`, executable names, deployment floor, or TCC usage strings.
- Load each state's menu glyph as a template image. Template rendering discards colour, so express idle, recording, warning, busy (starting/stopping), and failed as separate pre-rendered monochrome variants selected by state, legible in light and dark menu bars. Do not rely on runtime colour overlays.
- Use the full brand mark only in the panel header and app icon, never as a detailed colour menu-bar image.
- Add release-metadata/source-reading assertions for resource names, bundle-copy steps, template handling, and Info.plist icon metadata.
- Verify the assembled bundle contains every resource before codesigning and that `codesign --verify --deep --strict` succeeds after signing. This Phase 5 signing is a throwaway verification sign: the Post-Gauntlet Release Steps re-assemble and re-sign from post-gauntlet bytes, and only that bundle is smoked or installed.
- If an installed app exists, validate its signature and designated requirement before treating it as a comparison or rollback baseline. A missing or invalid baseline does not block a clean install after staged verification and must never be used for rollback.
- Add staged-install targets to `native/Makefile` here, so the review gauntlet covers them, and assert their semantics in `tests/test_release_meta.py`. `app-tree-hash` is the canonical identity producer and covers bundle paths, entry types, permission modes, file bytes, and symlink targets. `install-staged` has no `sign` or `build` prerequisite: it copies the staged app once into a private same-filesystem snapshot, verifies that snapshot against the pre-smoke identity plus the resource/signature checks, and never reads the mutable staged path again. It archives an explicitly approved installed baseline with `ditto -c -k --keepParent` to a uniquely allocated `~/Library/Application Support/Onoats/app-backups/Onoats-<timestamp>-<suffix>.zip`, extracts and verifies it privately, then atomically exchanges the verified install snapshot with the installed app. The rollback record is published only after a successful exchange. `rollback` accepts only that recorded archive, verifies a private extraction including its canonical tree identity, and atomically exchanges it with the installed app. Validation or exchange failure leaves the prior app installed. `install` and `setup` stay unchanged.

### Phase 6: Window-Style Recording Panel and In-Panel Settings

**Impl files:** `native/onoats-menubar/Sources/OnoatsMenuBarApp.swift, native/onoats-menubar/Sources/RecordingPanelView.swift, native/onoats-menubar/Sources/SettingsPanelView.swift, native/onoats-menubar/Sources/RecorderModel.swift, native/onoats-menubar/Sources/ConfigStore.swift`
**Test files:** `native/onoats-menubar/Tests/RecorderModelHarness.swift, native/test_recorder_model.sh, tests/test_native_contract_parity.py, tests/test_release_meta.py`
**Test command:** `native/test_recorder_model.sh && uv run pytest tests/test_native_contract_parity.py tests/test_release_meta.py -q`
**Validation cmd:** `make -C native build`

**Goal:** Opening the menu-bar item makes Start, Stop, and Seminar immediately obvious while preserving every existing action and warning surface.

- Apply `.menuBarExtraStyle(.window)` and replace the flat `MenuContent` with a fixed-width main panel plus an in-panel settings destination.
- Main stopped/failed state: brand header, concise readiness/failure state, warning banners, Seminar toggle, and a full-width Start button.
- Main running state: elapsed time, read-only Seminar badge from the app-owned lock or fresh same-owner external telemetry, Me/Them CoreAudio activity rows, one aggregated STT state with unhealthy branch detail, optional one-line transcript preview, full-width Stop, and secondary Flush. Show `Category unavailable` for an external session whose category cannot be established.
- If active and configured data directories differ, show `Recording to: <active>` and `Next recording: <configured>` until the active recorder exits.
- Keep Start disabled when the CLI is unavailable. Keep Stop disabled only for the existing in-flight external-stop guard.
- Preserve the distinction between app-owned and external sessions, including the “started outside the menu bar” disclosure. Unsupported external capture paths show activity unavailable without gaining new telemetry requirements.
- Keep pre-PID permission-wait detail visible in the matching app-owned starting state without treating it as recorder liveness.
- Route external Stop and Flush through the pinned active directory with explicit `--data-dir`; never let a next-session directory setting retarget the active recorder.
- Settings: microphone picker, system-output display, STT service picker, Open `config.toml`, data-directory picker, transcript-preview toggle, and Quit/Stop & Quit.
- Settings → Diagnostics: Open Log, Show Logs Folder, CLI path/status, active data directory, and next-recording directory. `Open Log` follows the single rule in Requirements: the in-memory path of this process's most recent app-owned run (labelled with its start time); the folder when that file is gone; and, only after an app relaunch with no in-memory path, the newest surviving `onoats-bot-<spawnEpoch>.log`, else the folder.
- Warning and failure surfaces of app-owned sessions and failed app-owned starts include an `Open Log` action. An external session's warning says `Log not captured (started outside the menu bar)` and offers no `Open Log`. Do not tail or render log contents inside the popover.
- Change `RecorderModel` to write one `onoats-bot-<spawnEpoch>.log` per app-owned run, replacing the single appended `onoats-bot.log`. Keep the newest 10 and route app-owned Flush helper output into the active run's file. App-owned Stop terminates the retained supervisor process and therefore has no helper output to route. External Stop and Flush output is not written to a run log; their failures surface through the existing warning and failure surfaces. Cover per-run file naming, newest-10 pruning (the 11th run removes only the oldest log and nothing else in the folder), and the Open Log resolution rule in the Phase 4 model harness.
- When an app-owned Start fails with the CLI's stable `ONOATS-ERROR invalid-category` tag, the failure surface says "Seminar needs 'seminars' in config.toml [categories]" and offers `Open Config` next to `Open Log`. The CLI stays the only authority on whether the category is configured; Swift adds no TOML parser. Swift detects only the stable tag through a bounded read of the tail of the run log it just wrote, only on a non-zero exit when no recorder PID was ever published. Human-facing error prose is never a machine interface. Extend `native/test_recorder_model.sh` (`RecorderModelHarness.swift`) with a log-tail fixture: a match only on a non-zero exit with no recorder PID ever published, and no match on a zero exit, a published PID, a missing tag, prose without the tag, or a tag outside the bounded tail. Add the Swift tag constant here and, in the same commit, pin it to the Python constant in `tests/test_native_contract_parity.py`.
- Render a stale Me/Them branch as a visible greyed `No signal` row so panel height stays stable.
- The main panel shows the existing Python-produced, redacted running `sttLabel`. While stopped, settings show only the configured service; advanced endpoint editing and resolution remain config/runtime work.
- Treat advanced STT endpoint editing as config-file work in this release; do not add a partial editor that can leave `ws_socket`, `ws_uri`, `ws_host`, and `ws_port` in conflict.
- Keep “changes apply on next Start” next to controls that write recorder configuration.
- Exercise a missing or unresolvable CLI through the `SystemOps` harness and the stopped-state smoke. Start stays disabled, while Settings → Diagnostics explains the missing CLI.
- Implement the macOS 14.4 interaction fallbacks in this phase: Escape dismissal (`.onExitCommand` or a key monitor), deterministic Tab/Shift-Tab traversal (explicit `.focused` ordering when needed), Space/Return activation (`.keyboardShortcut(.defaultAction)`), and per-state template images in the `MenuBarExtra` label. Conduct verifies the compile/source contracts; the Post-Gauntlet Manual Native Smoke Matrix is the authority for actual panel, keyboard, VoiceOver, and visual behaviour. Record that smoke result under `## Findings` below the marker. If a behaviour is unsupported even with the listed fallback, re-open the plan through `/review-plan` before narrowing the Accessibility smoke wording or the Acceptance Criteria; do not edit contract text silently.
- Give every icon-only control an accessibility label and tooltip. Define a deterministic focus order and keyboard activation for Start/Stop, Seminar, settings/back, and Flush.
- Render long warnings in a bounded, wrapping region without changing the delimiter grammar pinned by Python tests.

### Phase 7: Documentation, Historic Decision Corrections, and Pre-Gauntlet Verification

**Impl files:** `README.md, native/README.md, AGENTS.md, docs/dev_plans/20260609-feature-milestone-b-macos-capture-menubar.md, docs/dev_plans/20260925-feature-menubar-seminar-toggle.md`
**Test files:** `tests/test_native_contract_parity.py, tests/test_status_file.py, tests/test_release_meta.py`
**Test command:** `uv run pytest tests/test_native_contract_parity.py tests/test_status_file.py tests/test_release_meta.py -q`
**Validation cmd:** `python3 scripts/check_review_markers.py && make -C native build`
**Goal:** Documentation describes the final control hierarchy and telemetry contract without rewriting historical implementation logs or weakening reviewed-plan markers.

- Update README/native documentation for the window panel, common Start/Stop/Seminar flow, settings/diagnostics location, telemetry meanings, privacy default, active/next directory disclosure, log actions, and logo asset ownership.
- Grep `docs/dev_plans/` for menu-bar, Seminar, status, telemetry, and branding references.
- Append concise dated corrections to historic plans whose recorded UI decisions are materially superseded, never beside the superseded decision above it. The 20260609 plan keeps its marker below `## Final Results` and above its existing `## Findings`: append under that `## Findings`, below the marker. The 20260925 plan has no review marker and no `## Findings` section: append a new dated `## Findings` section at its end. Neither edit changes a marker hash, and `scripts/check_review_markers.py` must still pass.
- Register `ui_telemetry.py` in AGENTS.md's Leaf modules list and note the telemetry invariants (display-only, fail-open, one publisher task, levels rejected not clamped).
- Run `uv run ruff format` and `uv run ruff check` before the full suite.
- Run `uv run pytest` in full.
- Run `python3 scripts/check_review_markers.py`.
- Build the unsigned compile-smoke binaries with `make -C native build`.
- Before any commit, inspect only explicitly staged paths for secrets, credentials, private transcripts, PII, and unintended binary metadata.
- This phase ends before any signing. The auto-chained gauntlet found successive contract-level defects; Phases 8–11 own their reviewed redesigns and installed-branch feedback. The Post-Gauntlet Release Steps start only after Phase 11 and a fresh gauntlet converge, so no accepted fix can land after the final bytes have been signed, smoked, or installed (the Phase 5 sign is a throwaway verification sign and does not count).
- Operator workflow: Phase 1 is the completed human design gate, and Phases 2–10 remain completed evidence. `/conduct --resume` executes the newly reviewed Phase 11 only. Conduct owns its implementation and automated verification. The gauntlet fires after Phase 11, now the last parsed phase. Signing, interaction/accessibility smoke, exact-byte installation, and rollback validation remain exclusively in the unnumbered Post-Gauntlet Release Steps.

### Phase 8: Contract Redesign, Panel Refinement, and Recording Proof

**Impl files:** `src/onoats/ui_telemetry.py, src/onoats/stt/websocket_stt_service.py, native/install_staged.py, native/Makefile, native/onoats-menubar/Sources/RecorderModel.swift, native/onoats-menubar/Sources/RecordingPanelView.swift, native/README.md`
**Test files:** `tests/test_ui_telemetry.py, tests/test_websocket_stt_reconnect.py, tests/test_release_meta.py, tests/test_native_contract_parity.py, tests/test_recording_integration.py, native/onoats-menubar/Tests/RecorderModelHarness.swift, native/test_recorder_model.sh, native/installed_recording_smoke.py`
**Test command:** `uv run pytest tests/test_ui_telemetry.py tests/test_websocket_stt_reconnect.py tests/test_release_meta.py tests/test_native_contract_parity.py tests/test_recording_integration.py -q && native/test_recorder_model.sh`
**Validation cmd:** `uv run ruff format && uv run ruff check && uv run pytest && python3 scripts/check_review_markers.py && make -C native build`
**Goal:** Filesystem authority stays descriptor-bound and failure-atomic, the panel communicates Oats clearly, and recording proof reaches durable microphone transcription rather than stopping at process readiness.

- Replace both telemetry cleanup entry points with one descriptor-safe primitive; the async publisher cleanup and the shutdown callback delegate to it rather than implementing path reads independently. The primitive performs one bounded `O_NONBLOCK | O_NOFOLLOW` descriptor read, requires a regular file, parses ownership within the telemetry byte cap, compares the opened `(st_dev, st_ino)` to `lstat` immediately before unlink, and leaves any replacement untouched. Cancellation and cleanup remain bounded on FIFOs, devices, symlinks, oversized files, malformed data, and replacement races.
- Route websocket decode-timeout/reset transitions through the existing STT state callback owner. A timed-out decode must publish `recovering` before reconnect work and may return to `ready` only after that branch's next confirmed handshake.
- Redesign run-log creation around a retained `RunLogSink`/equivalent descriptor authority. Exclusive reservation returns the open append descriptor; `ProcessRequest` carries a descriptor-backed sink instead of a URL; and the app-owned recorder plus owned Flush helpers receive duplicated `O_APPEND` descriptors for the same inode. App-owned Stop remains retained-supervisor termination and creates no Stop child; the owner descriptor stays open through graceful recorder exit. Bounded invalid-category tag detection reads through the retained sink before release, never through the replaceable pathname. The owner closes only after all duplicated writers exit. Preserve newest-10 pruning and Open Log's documented path behavior as presentation-only operations.
- Make install publication commit-aware. The commit boundary includes atomic publication, verification of the installed identity, and rollback-record publication when an approved rollback baseline exists. Before that boundary, every failure is fatal and restores the prior installed app. After it, old-tree and temporary cleanup failures are warnings and cannot report the transaction as uncommitted. The exchange candidate itself has no `.app` suffix. Do not assume suffix alone controls discovery: query LaunchServices registration before and after injected cleanup failures and require that only the canonical installed bundle path is registered for `net.varunsingh.onoats`.
- Consume rollback archives through one immutable descriptor-backed snapshot: open no-follow, require a regular file, copy from that descriptor into a private file, verify that snapshot, and extract only that snapshot. Never verify a pathname and later reopen it for extraction.
- Define the copied-metadata policy explicitly. Reject symlinks and non-regular resource entries; include executable modes in tree identity; strip or reject ACL entries that grant write access beyond the owner; and verify the installed tree against the same normalised metadata policy. Preserve code-signing metadata required by `codesign` and fail closed on unsupported entry types.
- Make resource allowlisting type-aware: expected directories must be directories and every expected resource must be a regular, non-symlink file. Missing, extra, or wrong-type entries fail before signing or installation.
- Correct the documented native commands so each command is valid from its stated working directory. Document every rollback-capable install flag and preflight input; the clean-install command remains distinct and cannot imply rollback approval.
- Replace the padded bitmap header presentation with a compact native header containing `Oats` and `Organized Transcript System`, exposed to accessibility as `Oats - Organized Transcript System`. Retain a small canonical mark without allowing transparent image bounds to create empty panel space.
- Present Seminar in a rounded grouped row with a leading semantic icon, title, `Applies to the next recording` secondary text, and a trailing native switch. Keep its existing next-session-only behavior, focus order, keyboard activation, and accessibility hint.
- Make activity wording truthful: a fresh positive level is `Signal`, a fresh zero level is `Silent`, and an absent/stale observation is `No signal` or `Unavailable` under the existing freshness rules.
- Add `tests/test_recording_integration.py`. Its deterministic path runs the real supervisor, socket transports, dual pipeline, SourceTagger, and TranscriptBuffer against a fixture-fed fake capturer and deterministic STT test double; it requires a newly persisted `source=me` utterance and fails when the mic branch supplies only silence or process/socket readiness.
- Add an opt-in live variant using the existing `ONOATS_STT_FIXTURE` and real websocket STT endpoint. It feeds the same 16 kHz mono PCM speech fixture through the full socket/supervisor recording path and asserts a durable Me utterance without committing audio or transcript text.
- Add `native/installed_recording_smoke.py` for the signed app gate with explicit `--app`, `--data-dir`, and per-stage timeout inputs. It snapshots session files; resolves the one running signed app PID for the expected bundle path and identity; walks the recorder PID's process ancestry to prove descent from that app; waits boundedly for matching status and fresh telemetry; requires at least one Me level above a conservative non-zero floor while the operator speaks; waits boundedly for graceful Stop; then requires a new JSONL utterance whose canonical source is `me`. Each wait reports a stage-specific timeout without transcript text. The smoke fails on readiness-only, stale, external-session, system-only, wrong-ancestor, or multiple-app evidence. Timeout regressions cover every wait.
- Record the installed feedback and the terminal-attributed mic-permission diagnostic below the marker. The terminal denial is not treated as evidence about Onoats.app's grant; only the signed installed-app smoke is authoritative for app-owned TCC capture.
- After focused tests pass, run the full suite, Ruff format/check, review-marker validation, and native build. Inspect explicitly staged files for secrets, PII, private transcripts, audio, credentials, and unintended binary metadata before committing. Phase 8 completes at that implementation-and-verification commit. Its post-phase gauntlet may add a newly reviewed remediation phase; Phases 9 and 10 are the current results of those handbacks.

### Phase 9: Run-Bound Recording Proof and Captured-Identity Teardown

**Impl files:** `src/onoats/ui_telemetry.py, src/onoats/dual.py, native/onoats-menubar/Sources/UiTelemetry.swift, native/installed_recording_smoke.py`
**Test files:** `tests/test_ui_telemetry.py, tests/test_dual_socket_source.py, tests/test_native_contract_parity.py, tests/test_recording_integration.py, native/onoats-menubar/Tests/UiTelemetryDecoderHarness.swift, native/test_ui_telemetry.sh`
**Test command:** `uv run pytest tests/test_ui_telemetry.py tests/test_dual_socket_source.py tests/test_native_contract_parity.py tests/test_recording_integration.py -q && native/test_ui_telemetry.sh`
**Validation cmd:** `uv run ruff format && uv run ruff check && uv run pytest && python3 scripts/check_review_markers.py && make -C native build`
**Goal:** The installed smoke accepts only evidence produced by its recorder run and cleans already-captured identities only after the strongest available immediate kernel birth-tuple and secondary-identity recheck, while explicitly retaining the residual non-atomic delivery limitation.

- Bump only the ephemeral UI telemetry contract to schema 2. Wire `TranscriptBuffer.run_id` through the socket recorder into a required `transcript_run_id` field matching `^[0-9a-f]{32}$`; update the Swift decoder and Python/Swift parity fixtures together. Lifecycle status remains schema 2 and unchanged. `start_epoch` continues to mean recorder runtime ownership, while `process_birth` means the kernel `(tvsec, tvusec)` generation used for OS signalling; never conflate them.
- Make recorder telemetry the transcript-run attestation. After exact recorder PID/start-epoch ownership and telemetry freshness are established, extract its `transcript_run_id`; accept durable evidence only from JSONL whose first line is `session_meta` with that exact ID. A pre-launch scan of canonical headers rejects unsafe/unclassifiable existing candidates and records old IDs as defence in depth, but absence from that scan cannot establish current ownership. Continuation files may share the attested ID; renamed, recovered, copied, truncated, hard-linked, or newly linked files carrying an old, missing, or different ID are never evidence.
- Require the durable proof reader to stream JSONL with a 1 MiB per-line cap, keep transcript text out of exceptions and diagnostics, and accept only a non-blank canonical `type=utterance`, `source=me` entry from the attested run. Test the exact cap, cap plus one byte, and an unterminated oversized line; plant secret transcript text and prove it never appears in errors or logs.
- Split cleanup into two authorities. Discovery of a recorder or descendant group still requires live app-owned ancestry. Immediately after that proof—and before telemetry, transcript, exit, or stop waits—capture app, recorder, group leader, and member identities using PID, command, PGID, and `proc_pidinfo(PROC_PIDTBSDINFO)` process birth. Cleanup of an already captured identity continues when the app has vanished, but every TERM/KILL first re-reads and matches the kernel birth tuple plus secondary command/PGID evidence. App termination remains conditional on its captured identity still matching. Cleanup attempts recorder, groups, and app independently, preserves the primary smoke error, aggregates cleanup errors, and keeps every wait bounded.
- Harden the Me telemetry predicate before its floor comparison. Require finite `level`, `observed_at`, and `updated_at`; require `0.0 <= level <= 1.0`; require `0 <= now - updated_at <= 5` and `0 <= now - observed_at <= 1`; and retain exact recorder PID/start-epoch ownership. Validate `--me-floor` as finite and inside `[0.0, 1.0]` before launch.
- Add whole-space regressions for app live/gone/recycled crossed with recorder live/gone/recycled and captured group live/reaped/recycled. Include a PID/command/PGID/second-level-start collision whose microsecond kernel birth differs. Mutation checks must fail if the app-gone early return is restored, `ps lstart` regains authority, or any signal loses its generation recheck.
- Add transcript regressions for an attested run, continuation files, an old run renamed from `.active` to `pending`, copied and hard-linked old files, missing/malformed/non-first `session_meta`, telemetry/JSONL ID mismatch, multiple directory-new IDs, and Me/Them/blank utterance variants. Mutation checks must fail if acceptance falls back to novelty, path, inode, mtime, or size alone.
- Add telemetry regressions for `-0.0`, `0.0`, `1.0`, finite negative values, `1.0000001`, `NaN`, positive/negative `Infinity`, stale/fresh ages, future snapshot time, future observation time, ownership mismatch, and invalid floor arguments. Structural validation accepts finite levels only in `[0, 1]`; proof still requires `level > me_floor`. Invalid `--me-floor` values (`NaN`, infinities, negative, or greater than one) fail before app launch, discovery, or settings mutation; a mutation moving validation after launch must fail.
- If implementation or review exposes any manual-smoke defect outside this phase's reviewed files or contract, stop Conduct, append a new final remediation phase, refresh review, run its focused and full validation, and restart the gauntlet. Do not patch it opportunistically in the release steps.
- Run the focused test, then the full validation command. Inspect explicitly staged files for secrets, PII, private transcripts, audio, credentials, and unintended binary metadata before committing. Conduct then advances to Phase 10; signing and installation remain blocked until the post-Phase-10 gauntlet converges.

### Phase 10: Recorder-Owned Control Channel and Boundary Hardening

**Impl files:** `src/onoats/control_socket.py, src/onoats/cli.py, src/onoats/_vendor/pid.py, src/onoats/runtime.py, src/onoats/dual.py, src/onoats/__main__.py, native/onoats-menubar/Sources/RecorderModel.swift, native/onoats-menubar/Sources/UiTelemetry.swift, native/Makefile, native/install_staged.py`
**Test files:** `tests/test_control_socket.py, tests/test_control_socket_contract_parity.py, tests/test_cli.py, tests/test_status_file.py, tests/test_dual_socket_source.py, tests/test_socket_supervisor.py, tests/test_native_contract_parity.py, tests/test_release_meta.py, native/onoats-menubar/Tests/UiTelemetryDecoderHarness.swift, native/test_ui_telemetry.sh`
**Test command:** `uv run pytest tests/test_control_socket.py tests/test_control_socket_contract_parity.py tests/test_cli.py tests/test_status_file.py tests/test_dual_socket_source.py tests/test_socket_supervisor.py tests/test_native_contract_parity.py tests/test_release_meta.py -q && native/test_ui_telemetry.sh`
**Validation cmd:** `uv run ruff format && uv run ruff check && uv run pytest && python3 scripts/check_review_markers.py && make -C native build`
**Goal:** Stop and Flush bind command acceptance to the recorder generation rather than a reusable PID, stale cleanup cannot delete a successor record, and installer/telemetry boundaries consume one authoritative representation without evaluating operator input.

- Add a stdlib-only recorder-owned Unix control server at `<data_dir>/.active/onoats-control.sock`, parameterised by the exact `PidRecord` plus async `flush` and `stop` callbacks. In both dual/supervisor and `bot-single` entrypoints, acquire the instance lock, mint the epoch, construct the record and callbacks, bind the control server, then atomically publish the PID record and running status. If either publication fails, close the server and compare-and-unlink its socket before returning failure. On shutdown, stop accepting new connections, settle already accepted commands under the command lock, close the server, compare-and-unlink the owned socket, finalise status, then exact-record-remove the PID file. The `.active` directory and socket are owner-only; descriptor/inode compare-and-unlink prevents an old generation from removing a successor socket.
- Define a bounded newline-delimited JSON v1 protocol in `docs/control-socket-contract.md`. Every request contains a command (`flush` or `stop`) and the complete expected generation (`pid` plus `start_epoch`); absent, partial, or mismatched identity is always rejected. The server also rejects malformed/oversized/multi-message input, unsupported versions/commands, and unauthorised peers. On macOS it reads `LOCAL_PEERCRED` with `getsockopt(SOL_LOCAL, ...)`, validates the returned `xucred` layout/version, and requires `cr_uid == os.geteuid()`; unsupported or malformed peer credentials fail closed. Owner-only directory/socket modes remain defence in depth.
- Serialize accepted commands through one recorder-loop command lock. A Flush that wins the lock completes the existing continuation rotation before acknowledgement. A Stop that wins marks graceful shutdown and sets the shutdown event before acknowledgement; duplicate Stop is idempotently acknowledged. After Stop is accepted, any later Flush is rejected as stopping. This defines the result of simultaneous Stop/Flush by lock acquisition order and keeps acknowledgement bound to the action it reports.
- Route `onoats flush` and `onoats stop` through the control socket with the fully verified PID-file generation. There is no PID-signal fallback for a missing, legacy, or invalid socket: fail closed with an actionable incompatibility error. Keep SIGUSR1/SIGTERM handlers for direct terminal and service-manager compatibility, but do not use `os.kill(pid, signal)` in the normal CLI control path. An already-retained native `Process` handle remains valid authority for stopping an app-owned child; external native Stop/Flush use the CLI control channel and pinned data directory.
- Make every stale PID-file removal compare the complete captured `PidRecord`, including `pid`, `cmdline`, and `start_epoch`, immediately before unlink. A same-PID successor or any unreadable/foreign/replaced record is preserved. Centralise this exact-record compare-and-unlink primitive so runtime teardown and CLI stale cleanup cannot drift.
- Remove operator-controlled Make expansion from staged installer authority. `install_staged.py` reads dedicated environment-only inputs (`ONOATS_INSTALL_APP`, `ONOATS_INSTALL_DIR`, rollback archive/identity inputs) directly and owns defaults and derived app names. Fixed Make recipes must not expand, export, or transform operator-provided path/identity values. Document the environment interface and reject legacy Make-variable overrides where accepting them would require evaluation.
- Make Swift telemetry validation compile and use the same shared `transcriptRunIDPattern` constant that parity tests inspect; remove duplicated run-ID grammar.
- Add whole-space and mutation regressions for stale/successor socket paths, reconnecting to a successor with an old expected generation, missing/legacy servers, absent/partial identity, peer-credential absence/malformed version/wrong UID, oversized and malformed requests, dropped acknowledgements, simultaneous Stop/Flush in both acquisition orders, duplicate Stop, Flush-after-Stop, cleanup replacement races, and restoration of a PID-signal fallback. Use action spies plus a handler-swap mutation to prove Flush rotates only and Stop requests graceful shutdown only. Prove same-PID/new-epoch PID records survive stale cleanup.
- Add a control-contract parity test modelled on `tests/test_audio_socket_contract_parity.py`, pinning protocol version, request and response fields, byte/message limits, peer policy, command state machine, acknowledgement points, and every documented rejection rule.
- Add hostile installer-input regressions containing Make function syntax, command substitution syntax, whitespace, and path metacharacters. Prove no marker command executes and the exact environment bytes reach Python. Invoke every legacy Make-variable override and require failure before Python starts or installation state changes. Add Swift parity mutation coverage proving the checked pattern is the pattern used for decode validation.
- Update `AGENTS.md`, `README.md`, `native/README.md`, and the materially superseded 20260619 stop/orphan decision record with the control-channel, exact-record-cleanup, and environment-only installer contracts. Grep sibling plans and add dated corrections only where an earlier decision is now materially stale.
- Run the focused test, then the full validation command. Inspect explicitly staged files for secrets, PII, private transcripts, audio, credentials, and unintended binary metadata before committing. Conduct then restarts the full gauntlet from the Phase 10 boundary; signing and installation remain blocked until that gauntlet converges.

### Phase 11: Unified Recorder Lifecycle Authority and Release Identity Closure

**Impl files:** `src/onoats/recorder_lifecycle.py, src/onoats/control_socket.py, src/onoats/runtime.py, src/onoats/dual.py, src/onoats/__main__.py, src/onoats/cli.py, src/onoats/ui_telemetry.py, native/onoats-menubar/Sources/RecorderModel.swift, native/onoats-menubar/Sources/UiTelemetry.swift, native/install_staged.py, native/installed_recording_smoke.py, .github/workflows/ci.yml`
**Test files:** `tests/test_recorder_lifecycle.py, tests/test_control_socket.py, tests/test_control_socket_contract_parity.py, tests/test_status_file.py, tests/test_socket_supervisor.py, tests/test_stt_config_wiring.py, tests/test_runtime_preflight.py, tests/test_ui_telemetry.py, tests/test_native_contract_parity.py, tests/test_release_meta.py, tests/test_recording_integration.py, native/onoats-menubar/Tests/RecorderModelHarness.swift, native/onoats-menubar/Tests/UiTelemetryDecoderHarness.swift, native/test_recorder_model.sh, native/test_ui_telemetry.sh`
**Test command:** `uv run pytest tests/test_recorder_lifecycle.py tests/test_control_socket.py tests/test_control_socket_contract_parity.py tests/test_status_file.py tests/test_socket_supervisor.py tests/test_stt_config_wiring.py tests/test_runtime_preflight.py tests/test_ui_telemetry.py tests/test_native_contract_parity.py tests/test_release_meta.py tests/test_recording_integration.py -q && native/test_recorder_model.sh && native/test_ui_telemetry.sh`
**Validation cmd:** `uv run ruff format && uv run ruff check && uv run pytest && python3 scripts/check_review_markers.py && make -C native build`
**Goal:** One recorder-owned lifecycle authority orders every Flush, Stop, signal, terminal rotation, and status transition; one immutable session snapshot reaches every STT consumer; native and installed-smoke identity checks bind UI and release evidence to the exact recorder and bundle generation.

- Add a stdlib-only `RecorderLifecycleController` as the sole session state machine for both recorder entrypoints. The authoritative topology is `signal/control intent -> RecorderLifecycleController -> continuation or terminal rotation -> shutdown event/status finalisation`; `ControlServer`, signal handlers, and pipeline-finally blocks are adapters, never state owners. The controller owns one fair async lock, the `running -> stopping -> stopped` transition, graceful-shutdown marker/event, continuation rotation, and terminal rotation. A Flush that acquires the controller first completes before Stop; Stop or terminal rotation that acquires first rejects every later Flush. The second termination signal retains the existing force-exit escape without reopening admission.
- Define the startup-signal handoff explicitly. The supervisor owns SIGINT/SIGTERM from process entry through recorder-controller readiness. The recorder publishes readiness only after the controller and its signal adapters are installed; the supervisor then removes its handlers in the same loop turn. Before that point, startup cancellation owns teardown; afterward, every first signal enters the controller. Tests pause immediately before and after readiness and prove there is neither a handler gap nor two active first-signal owners.
- Keep `ControlServer` limited to authentication, generation validation, framing, acknowledgement, and request admission. Its callback awaits the controller action it acknowledges. Add an end-to-end two-entrypoint matrix that pauses at request admission, controller-lock acquisition, rotation, signal scheduling, acknowledgement, accepted-handler cancellation, and client disconnect for Flush-first, Stop-first, SIGTERM-first, terminal-error-first, duplicate Stop, force-exit, and disconnected-client cases. Mutations that restore a second lock/event owner or raw signal-path rotation must fail.
- Treat running-status publication as best-effort display state after PID/control authority exists. An `OSError` or false return logs exactly one redacted warning, disables status and telemetry publication for that run, and preserves capture, STT, transcript persistence, control, cleanup, and recorder return code. A status-absent run reports failure through the owned process exit and run log; it does not promise a later status record. PID/control publication remains fail-loud. Both entrypoints share one helper and a status-present/status-absent x success/error matrix that asserts warning count, redaction, telemetry suppression, transcript durability, and unchanged return semantics.
- Make startup cancellation bounded at the synchronous supervisor boundary without relying on event-loop or default-executor shutdown. The normal path keeps complete task, async-generator, and executor drain. If the recorder still suppresses cancellation after `_RECORDER_DRAIN_GRACE_SEC`, stamp `recorder-drain-timeout`, run bounded capturer-group TERM/KILL/reap attempts, then invoke one injected supervisor-only hard-exit primitive (`os._exit(1)` in production) so blocked executor threads cannot extend process lifetime. The acceptance deadline is drain grace + capturer TERM grace + capturer KILL/reap grace + 1 second scheduling tolerance. Tests replace the hard-exit primitive, use a cancellation-suppressing recorder and blocked executor, assert the deadline, and assert every capturer/helper termination attempt and any reported survivor; the contract promises bounded attempts and truthful survivor reporting, not impossible proof that an unkillable process vanished.
- Introduce immutable `RecorderSessionConfig`, constructed exactly once immediately after the instance lock and config/preflight read and passed explicitly by both entrypoints. It owns the selected STT backend, endpoint, launchd label, diarization settings, and display label consumed by service construction, `stt_banner`, telemetry initialisation, diarization/preflight, reconnect/kickstart authority, and `log_stt_server_rss`. Recorder paths may not call load-on-demand variants; only named standalone diagnostics may omit the snapshot. Grep every `load_config`, `_create_stt_service`, `stt_banner`, preflight/recovery, and RSS call site. Change config after capture and prove both arms, banner, telemetry, diarization, preflight, recovery, and shutdown probe retain the captured values.
- Bind native liveness caches to the complete recorder generation. Cache keys include PID, stored command fingerprint, and positive start epoch; an absent/malformed epoch or changed command/epoch invalidates rather than hits the cache. On owned-process exit, `terminationStatus == 0` is the only clean stopped result. A requested Stop with non-zero exit remains failed and may consume `exit_reason`/`last_error` only from status matching PID, command, start epoch, spawn floor, and active directory. Cover complete/incomplete generations, same-PID/new-epoch, same-PID/new-command, requested/unrequested zero/non-zero exits, and matching/stale/absent diagnostics.
- Create one immutable `ReleaseIdentity` record containing canonical bundle identifier, designated requirement, cdhash, staged app tree hash, main-executable hash/inode evidence, and CLI git identity. The staged installer derives it from the descriptor-backed candidate and rejects `ONOATS_INSTALL_BUNDLE_ID` unless it is an exact redundant match before LaunchServices queries or mutation. Install and smoke consume the same record. Before launch, smoke verifies every field; after obtaining the app PID, it uses Security.framework dynamic-code APIs to bind the running process to the expected designated requirement and cdhash, while descriptor/tree checks cover resources and helpers. After recording it re-verifies dynamic executable identity and the tree. Add prelaunch and post-record mismatch matrices for each field, path replacement, same-path tree replacement, unrelated LaunchServices namespace, and signature-only impostors.
- Make telemetry exact-field validation one atomic cross-language checkpoint. Python owns explicit root/audio/STT/branch/transcript key-set constants used by the producer; Swift owns matching sets used by the decoder; parity tests extract and compare each set. Generate table-driven Python snapshots for every optional/conditional shape, feed them to the Swift harness, and inject one unknown key independently at every nesting level. Mutations removing Python production use, Swift enforcement, or any parity set must fail. Preserve schema v2 and deliberate unknown-field rejection.
- Preserve the accepted macOS limitation: installed-smoke TERM/KILL still performs the strongest available `proc_pidinfo` generation recheck immediately before delivery, but signal delivery is not generation-atomic. Keep this one design conflict quarantined; do not claim that userspace can eliminate the kernel check-to-signal race.
- Implement Phase 11 as ordered, independently verified checkpoints: (1) failing lifecycle/controller and signal-handoff matrices; (2) `RecorderSessionConfig` and the complete STT consumer sweep; (3) lifecycle controller plus fail-open status entrypoint wiring; (4) bounded supervisor hard-exit boundary; (5) native generation cache/exit truth; (6) `ReleaseIdentity` installer/smoke closure; (7) atomic Python/Swift telemetry field parity; (8) documentation and sibling-plan corrections. Each checkpoint gets focused tests and a meaningful commit before the next; checkpoint 7 lands Python constants, Swift enforcement, generated fixtures, and parity tests together.
- Add a real Python 3.13 CI job for control socket, lifecycle, supervisor, status, and STT snapshot suites while retaining the existing Python 3.12 full lane. Release CI must not publish unless both supported-runtime contract lanes pass. Update `AGENTS.md`, `docs/control-socket-contract.md`, `README.md`, and `native/README.md` with the single lifecycle owner, fail-open status boundary, immutable session snapshot, generation-keyed native cache, and release-identity contract. Grep sibling plans and append dated corrections only where Phase 11 materially supersedes a recorded decision.
- Run the focused test, then the full validation command. Inspect explicitly staged files for secrets, PII, private transcripts, audio, credentials, and unintended binary metadata before committing. Conduct then restarts the full gauntlet from the Phase 11 boundary; signing and installation remain blocked until that gauntlet converges.

### Post-Gauntlet Release Steps (manual; not a `/conduct` phase)

**Goal:** The bytes that pass smoke are exactly the bytes that are installed, and they are the post-gauntlet bytes. A failed install can only roll back to a verified prior baseline. These steps change no code; any fix goes back through the current final Phase 11 contract and the gauntlet, then re-smokes.

- After the gauntlet converges, rerun Phase 7's documentation and historic-decision audit plus deterministic recording integration and installed-smoke unit/mutation tests against the final diff, then run Ruff format/check, review-marker validation, the full pytest suite, `native/test_ui_telemetry.sh`, and `make -C native build` on the post-gauntlet tree before signing. Do not launch the real microphone/TCC smoke before signing. Run focused tests for every file the gauntlet touched. If the documentation audit changes any file, rerun the marker check and gauntlet; repeat until both code and documentation reach a fixed point.
- Assemble and sign a staged `Onoats.app`. Verify the resource allowlist, signature, designated requirement, and canonical app identity with `make -s -C native app-tree-hash`, plus `make -C native cdhash`. Record the CLI identity: `git rev-parse HEAD` with an empty `git status --porcelain`, plus `command -v onoats`. Resolve the shim interpreter and installed distribution metadata, then run an import probe and require `Path(onoats.__file__).resolve()` to belong to this checkout before accepting the CLI identity. Before staged smoke, snapshot `config.toml` bytes and every touched Onoats `UserDefaults` key, including `cliPath` and transcript-preview visibility, and use a dedicated temporary smoke data directory. Restore and byte/value-verify all settings in a finally-style cleanup before `install-staged` and again after post-install smoke.
- Run the complete manual native matrix against the signed staged app before baseline preflight or installation. First produce the mandatory immutable identity with `python3 native/install_staged.py release-identity <staged-app> --cli-git-identity "$(git rev-parse HEAD)" --output <release-identity.json>`. Then run `python3 native/installed_recording_smoke.py --app <staged-app> --data-dir <temporary-smoke-dir> --release-identity <release-identity.json>`. This is the first real microphone/TCC smoke. Record the app tree hash and CLI identity beside the smoke result. A staged smoke that lacks non-zero Me evidence and a durable Me utterance fails even when process, sockets, and STT are ready.
- Hash and byte-copy claims apply to `Onoats.app` only. The CLI is `uv tool install --editable` of the working tree and has no hashable bytes of its own. Any commit or edit between staged smoke and install forces a re-smoke.
- Preflight any installed app and CLI before treating them as a rollback baseline: require every Phase 1 field, a valid app signature, the expected designated requirement, an installed-app cdhash equal to the recorded baseline, and a prior CLI SHA that still resolves (`git cat-file -e <sha>`). There is no version-string check: the version is 1.5.1 on both sides and the CLI has no `--version` flag. This run's Phase 1 record lacks the exact prior `EXTRAS` and branch/detached state, so it is explicitly a clean-install case and must not claim or attempt rollback. Future runs may use rollback only when every field was recorded before implementation.
- Immediately before installation, revalidate the recorded CLI SHA, empty worktree, shim path and interpreter/distribution metadata, and checkout-owned `onoats.__file__` import path; every value must equal the staged-smoke record. Install the exact staged app identity that passed smoke using the documented clean-install flags, or the separately documented rollback-approval and baseline identity flags when a complete baseline passed preflight. If post-install smoke fails, run `make -C native rollback` for the app, then `git switch --detach <recorded prior SHA>` and `make -C native install-cli` with the same EXTRAS, but only when the baseline passed preflight. Because the editable CLI is the working tree, rollback leaves it detached at the prior SHA by design: report this and print `git switch <recorded branch>`, and do not switch back automatically. For a clean install, report the failure and do not claim rollback.
- Run the post-install smoke only after the staged bundle passes. Installation is not the first acceptance test.

## Technical Specifications

### Files to Modify

- `native/onoats-menubar/Sources/OnoatsMenuBarApp.swift` — window-style scene, main-panel state, stateful template icon.
- `native/onoats-menubar/Sources/RecorderModel.swift` — `ProcessHandle` plus `SystemOps` seams, telemetry state, authoritative active/next directories for launch/read/signal/exit, pre-PID warning detail, per-run log files and log/folder actions, stable-tag category-error detection, validated refresh integration, retained lifecycle actions.
- `native/onoats-menubar/Sources/UiTelemetry.swift` — strict schema-v2 decoding whose run-ID validation consumes the same shared pattern pinned by parity tests.
- `native/onoats-menubar/Sources/RecordingPanelView.swift` — compact `Oats - Organized Transcript System` identity, grouped Seminar switch, truthful activity labels, and existing lifecycle surfaces.
- `native/onoats-menubar/Sources/ConfigStore.swift` — only if settings navigation needs an existing-key read/write helper; do not broaden it into a full TOML parser.
- `native/onoats-menubar/Info.plist` — app icon metadata only.
- `native/Makefile` and `native/install_staged.py` — explicit resource assembly and verification inputs (Phase 5), a canonical `app-tree-hash` producer, failure-atomic `install-staged`/`rollback` targets, and an environment-only operator-input boundary that never expands path/identity data through Make. `install` and `setup` stay unchanged.
- `src/onoats/transports/socket_audio.py` — optional keyword-only `on_level` callback on the `UnixSocketAudioTransport` facade and its inner input transport, called after frame validation.
- `src/onoats/dual.py` — telemetry state holder carrying `TranscriptBuffer.run_id`, publisher task created after running status and cancelled before stopped status, branch wiring through `_build_socket_transports(cfg, *, on_mic_level, on_system_level)`, STT state wiring, and owner-checked cleanup through a new keyword-only `telemetry_cleanup` callable on `_finalize_shutdown_status`.
- `src/onoats/processors/transcript_buffer.py` — optional finalized-utterance observation hook.
- `src/onoats/runtime.py` — backend-neutral STT state publication around service creation/preflight, and the one caller-minted run epoch: one epoch helper, `_write_pid_file` takes a required `start_epoch`, `_write_status_running` forwards `start_time`, both return success; new keyword-only `on_ready`/`on_recovering`/`on_failed` parameters on `_create_stt_service`.
- `src/onoats/cli.py` and `src/onoats/_vendor/pid.py` — Stop/Flush control-socket client routing plus exact-record compare-and-unlink cleanup; no normal CLI PID-signal fallback.
- `src/onoats/__main__.py` — `run_onoats` (`bot-single`) mints its own epoch and passes it to `_write_pid_file`; it builds no telemetry.
- `src/onoats/stt/websocket_stt_service.py` — keyword-only `on_ready`, `on_recovering`, and `on_failed` callbacks without changing reconnect semantics.
- `tests/test_socket_audio_transport.py` — callback isolation, branch identity, invalid PCM and backpressure regression coverage.
- `tests/test_dual_socket_source.py` — Me/Them telemetry routing without audio mixing, exact `TranscriptBuffer.run_id` wiring; PortAudio and `bot-single` build no publisher.
- `tests/test_websocket_stt_reconnect.py` — confirmed health, recovery, failure, lost-connection `on_recovering`, and redaction transitions.
- `tests/test_stt_config_wiring.py` — `_create_stt_service` forwards the new `on_ready`/`on_recovering`/`on_failed` keywords and sets the initial state per backend.
- `tests/test_runtime_preflight.py` — preflight and recovery paths behave as before with the new callbacks absent, and a redacted detail reaches the failure callback.
- `tests/test_speaker_labels.py` — transcript preview source/speaker rendering parity where applicable.
- `tests/test_native_contract_parity.py` — separate schema-v2 telemetry and Swift-decoder parity, including required `transcript_run_id`, the Seminar literal `seminars` across Swift `seminarCategory`, the `--category` argv, and the telemetry `category` field, the Swift `ONOATS-ERROR invalid-category` matcher against the Python constant (landing with its Swift constant in Phase 6), the `ONOATS_DATA_DIR` and `--data-dir` literals, and a ban on `URLSession`/`NWConnection` in the new Swift files.
- `tests/test_status_file.py` — lifecycle status remains v2 and warning rendering remains intact.
- `tests/test_socket_supervisor.py` — shared run epoch, identity publication order, `_finalize_shutdown_status` `telemetry_cleanup` ordering, and paired status/PID evidence.
- `tests/test_release_meta.py` — resource, Info.plist, canonical identity, archive allocation, and failure-atomic `install-staged`/`rollback` contract matrices.
- `tests/test_recording_integration.py` — deterministic full supervisor-to-durable-Me path plus an opt-in real-STT fixture variant.
- `tests/test_cli.py`, `tests/test_status_file.py`, and `tests/test_socket_supervisor.py` — control-channel routing, exact-record stale cleanup, publication/teardown ordering, and no-PID-signal mutation coverage.
- `native/installed_recording_smoke.py` — signed-app smoke with `proc_pidinfo` generation teardown, finite telemetry validation, recorder-attested run identity, bounded secret-safe JSONL reads, and durable Me proof.
- `README.md`, `native/README.md` — user and maintainer documentation.
- `AGENTS.md` — Leaf modules entry for `ui_telemetry.py` and the telemetry invariants.
- `docs/dev_plans/20260609-feature-milestone-b-macos-capture-menubar.md` — dated correction under its existing `## Findings` (below its marker), only if its menu-presentation decision is stale.
- `docs/dev_plans/20260925-feature-menubar-seminar-toggle.md` — a new dated `## Findings` section appended at its end (the plan has no marker and no existing `## Findings`), preserving Seminar semantics while relocating the control.

### New Files to Create

- `src/onoats/recorder_lifecycle.py` — stdlib-only shared lifecycle state machine and single serialization authority for signal/control Flush, Stop, terminal rotation, and shutdown admission.
- `tests/test_recorder_lifecycle.py` — full intent-order, rotation, acknowledgement, error, duplicate, and mutation matrix for both recorder entrypoints.
- `src/onoats/ui_telemetry.py` — stdlib-only leaf module: ephemeral schema, bounded publisher, atomic writer, tolerant reader, and owner-checked cleanup.
- `src/onoats/control_socket.py` — stdlib-only Unix control server/client and bounded versioned protocol, with no runtime/status/dual imports.
- `tests/test_control_socket.py` — protocol, peer permission, generation authentication, concurrency, acknowledgement, replacement, cleanup, and mutation tests.
- `tests/test_control_socket_contract_parity.py` — parity between the versioned control contract and code constants, limits, response fields, state machine, and rejection rules.
- `docs/control-socket-contract.md` — versioned Stop/Flush wire contract, ownership, lifecycle, limits, and compatibility behavior.
- `tests/test_ui_telemetry.py` — contract, freshness, throttling, ownership, atomicity, permissions, failure, and cleanup tests.
- `native/onoats-menubar/Sources/UiTelemetry.swift` — strict Swift decoding and freshness/ownership validation.
- `native/onoats-menubar/Sources/RecordingPanelView.swift` — main recording-control panel.
- `native/onoats-menubar/Sources/SettingsPanelView.swift` — in-panel settings destination.
- `native/onoats-menubar/Tests/UiTelemetryDecoderHarness.swift` — executable strict-decoder harness for shared JSON fixtures.
- `native/onoats-menubar/Tests/RecorderModelHarness.swift` — executable model harness for directory authority, lifecycle routing, and pre-PID warning detail.
- `tests/fixtures/ui_telemetry/` — shared valid, malformed, stale, foreign-owner, boundary, and non-finite vectors, each snapshot with a `now` field (non-finite snapshots are raw text carrying `NaN`/`Infinity` tokens), plus the boundary verdict table with string-encoded level inputs.
- `native/test_ui_telemetry.sh` — compile and run the decoder harness on macOS without introducing Swift Package Manager.
- `native/test_recorder_model.sh` — compile and run the model harness on macOS without introducing Swift Package Manager.
- `assets/brand/onoats-mark.svg` — canonical standalone mark.
- `assets/brand/onoats-wordmark.svg` — canonical mark plus wordmark.
- `assets/brand/README.md` — palette, clear space, small-size, export, and template-icon rules.
- `native/onoats-menubar/Resources/OnoatsHeader.png` and `OnoatsHeader@2x.png` — panel header exports.
- `native/onoats-menubar/Resources/OnoatsMenuBarTemplate<State>.png` and `@2x` for Idle, Recording, Warning, Busy, and Failed — monochrome template exports, one per state.
- `native/onoats-menubar/Resources/Onoats.icns` — Finder, permissions, and app identity icon.

### UI Telemetry Contract

- Path: `<data_dir>/.active/onoats.ui.json`.
- Schema: independent integer `2`; it does not share `STATUS_SCHEMA_VERSION`. Schema 2 requires `transcript_run_id` matching `^[0-9a-f]{32}$`.
- Ownership tuple: recorder PID plus one caller-minted run epoch shared byte-for-byte with PID line 4 and lifecycle `start_time`.
- Clock: every timestamp (snapshot time, per-branch observation times, run epoch) is wall-clock epoch seconds, `time.time()` in Python and the injected clock's `Date().timeIntervalSince1970` in Swift, matching `status.py`. A wall-clock jump can mark a live snapshot stale or fresh for at most one expiry window; accepted, because telemetry is display-only.
- Freshness: maximum 4 writes per second, heartbeat at least every 2 seconds, and snapshot expiry after 5 seconds. Snapshot acceptance checks only schema, ownership (PID plus start epoch), snapshot freshness, numeric ranges, optional-string bounds, and aggregate STT consistency. Each activity branch carries its own observation timestamp and is judged per branch after acceptance: it expires after 1 second without a new frame and renders as a visible greyed `No signal` row, without dropping the other branch or STT state. An advancing observation timestamp counts as a changed snapshot; a branch with continuing frames refreshes its observation time at least every 500 ms inside the 4 Hz cap. Swift also requires the authoritative PID record to be live.
- Activity: optional normalized peaks plus observation timestamps for `me` and `them`. Missing or branch-stale means unknown (`No signal`), not zero and not healthy; a snapshot heartbeat never refreshes branch age.
- STT state: mic/system each use `starting | ready | recovering | failed | unknown`; aggregate precedence is `failed > recovering > starting > unknown > ready`, and ready requires both branches. Swift recomputes and rejects disagreement with the published aggregate.
- STT state definitions (per branch instance): `starting` = before that websocket instance's first successful handshake. `ready` = that instance's own latest handshake succeeded; `start()` already connects eagerly, so no audio is needed, and mic and system each confirm from their own connection, never from the shared preflight cache. `recovering` = a reconnect or kickstart is in flight, or the reader has detected a lost established connection (`_read_events` teardown clears `_connected`), until the next handshake succeeds. `failed` = `_ensure_connected` has exhausted its backoff; an auth or protocol error is retried through the same backoff, so it surfaces as `failed` only at exhaustion, never earlier. It is not terminal: the next connect attempt moves it to `recovering` and a successful handshake to `ready`, and the state-pair tests pin `failed → recovering → ready`. `unknown` = non-websocket backends (`whisper`, `deepgram`), set at construction and permanent, shown as neutral grey `status not reported (<service>)` and never green. The confirmed `transcript.*` signal stays the kickstart-cooldown concept and is not a UI readiness gate.
- Transcript preview: optional text capped at 240 Unicode code points plus canonical source and render-time speaker label. It never replaces the durable JSONL entry. With explicit user approval, it may show either Me or Them text already present in the session JSONL; the panel always labels the source, the default-off opt-in is its privacy control, and no extra redaction applies.
- Transcript run attestation: required `transcript_run_id` comes from the same `TranscriptBuffer.run_id` written in JSONL line-1 `session_meta`. Swift validates and carries it without using it as lifecycle authority; installed smoke uses it only after PID/start-epoch ownership and freshness pass.
- STT detail: optional, redacted, and capped at 256 Unicode code points.
- File mode: `0o600`; level values are finite and within `[0.0, 1.0]`; out-of-range or non-finite observations are rejected at the producer (no clamping), and a rejected observation advances neither the level nor the branch observation time.
- Category: optional locked category. The Seminar literal is `seminars` (Swift `seminarCategory`), pinned by a parity test across that constant, the `--category` argv, and this field. The app-owned in-memory lock remains authoritative for its run; an external session uses only fresh same-owner telemetry. The panel renders a Seminar badge only for an exact match with `seminars` and otherwise shows `Category unavailable` when external category authority is absent. The CLI remains the only authority on whether `seminars` is in the config `[categories]` set.
- Durability: none. Writes are atomic but deliberately not fsynced. The file may disappear or be stale after a crash.
- Failure policy: best-effort and warning-only. Repeated failures are rate-limited.

### Architecture Decisions

- Keep lifecycle status and UI telemetry separate. Status remains low-frequency and authoritative; telemetry remains ephemeral and display-only.
- **Decision (grilled):** Scope new meters and telemetry to Onoats.app's CoreAudio/socket session. Preserve PortAudio and `bot-single` without new UI telemetry requirements; show unavailable for unsupported external sessions.
- **Decision (grilled):** Combine the menu-bar app's two STT instances into one row. Green requires both ready; unhealthy states expose branch detail.
- **Decision (grilled):** Publish at 4 Hz maximum, heartbeat every 2 seconds, and expire after 5 seconds to balance meter response and filesystem load.
- **Decision (grilled):** Snapshot the configured data directory synchronously at Start and use that pinned path for supervisor environment, status/PID/telemetry reads, exit handling, external Stop, and Flush until the recorder exits. When settings change it mid-run, show active and next-recording paths.
- **Decision (grilled):** Show Python's redacted running STT label on the main panel. Keep the service picker and config-file endpoint access in settings.
- **Decision (grilled):** Offer Open Log plus Show Logs Folder. The canonical log is the app-captured stdout/stderr file of the most recent app-owned run: one `onoats-bot-<spawnEpoch>.log` per run, newest 10 kept. Open Log appears on warning and failure surfaces only for app-owned sessions and failed app-owned starts, with folder fallback; an external session's warning says `Log not captured (started outside the menu bar)`. Settings → Diagnostics keeps both Open Log and Show Logs Folder. The popover never tails logs. The absent-file smoke case deletes the log while the app is idle.
- **Decision (grilled):** Treat the app-owned Seminar selection as badge authority even if telemetry disappears. External sessions require fresh same-owner category telemetry and otherwise display `Category unavailable`.
- **Decision (grilled):** Show a pre-PID permission-wait warning only for the matching retained app-owned supervisor. It decorates `.starting` but never establishes recorder liveness.
- **Decision (grilled):** Carry per-branch audio-observation timestamps. Judge branch age per branch after snapshot acceptance, so one stale branch never drops the other branch or STT state. A stale branch renders as a visible greyed `No signal` row, not a hidden row, so panel height stays stable. A branch with continuing frames publishes an observation-time refresh at least every 500 ms inside the 4 Hz cap; heartbeats never advance branch age. The five-second snapshot expiry continues to govern the overall telemetry record. Producers reject, never clamp, out-of-range levels.
- **Decision (grilled):** Permit a clean install after staged app verification when no trustworthy prior app and CLI baseline exists. Use rollback only when the prior baseline passes preflight.
- **Decision (updated after the 2026-11-04 seam escalation):** Phase 11 is the last conduct-driven phase. `Review Gates: full` auto-chains the review gauntlet only after Phase 11 reaches its implementation-and-verification commit. The Post-Gauntlet Release Steps remain unnumbered and manual, so signing, staged smoke, exact-byte installation, and rollback preflight happen only after convergence.
- **Decision (grilled and accepted after gauntlet seam escalation):** Every normal CLI Stop and Flush request uses a recorder-owned Unix control socket and carries the complete expected PID/start-epoch generation; there is no identity-free request mode. The server verifies the generation and same effective UID via Darwin `LOCAL_PEERCRED`, then acknowledges only at the command-specific acceptance point. Missing/legacy servers, absent peer credentials, and malformed or mismatched identities fail closed. Direct OS signals remain compatibility inputs, not the CLI transport. Apple's [`kill(2)` contract](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/kill.2.html) targets a PID and exposes no process-generation argument, so normal CLI control never signals a PID after userspace verification.
- **Decision (grilled and accepted after gauntlet seam escalation):** Control commands serialize through one recorder-loop lock. Flush-first completes and acknowledges rotation before Stop; Stop-first marks graceful shutdown and acknowledges before rejecting later Flush. Duplicate Stop is idempotent. Both recorder entrypoints bind the server before identity publication and close/remove it before exact-record PID teardown.
- **Decision (approved after gauntlet seam escalation):** Stale PID cleanup compares the complete captured record immediately before unlink, and staged-installer operator values enter only through dedicated environment variables read by Python. Make never evaluates operator path or identity data.
- **Decision (grilled):** Installed-smoke transcript authority is recorder-owned schema-v2 telemetry carrying `TranscriptBuffer.run_id`, accepted only with the exact recorder PID/start epoch. Filenames, corpus novelty, inode changes, mtime, and size are discovery or defence-in-depth hints only. Durable Me acceptance requires the telemetry-attested run ID in JSONL line 1 and a canonical Me utterance in that session.
- **Decision (grilled):** Installed-smoke process ownership becomes immutable evidence once captured. Live app ancestry authorises discovery; exact macOS kernel process birth from `proc_pidinfo(PROC_PIDTBSDINFO)` plus PID/command/PGID/member evidence authorises teardown after the app disappears. Capture all generations immediately after ancestry proof and recheck before every signal. Second-resolution `ps lstart` is never authority.
- **Decision (grilled):** Pre-sign validation runs deterministic recording tests and installed-smoke unit/mutation tests only. The real microphone/TCC smoke runs only after the staged app is signed, and installation copies those exact verified bytes without rebuilding or re-signing.
- **Decision (grilled):** The CLI is `uv tool install --editable` of the working tree and has no hashable bytes. Its verified identity is `git rev-parse HEAD` with an empty `git status --porcelain`, plus `command -v onoats` resolving to the editable-install shim, recorded beside the `Onoats.app` tree digest. No `--version` flag is added. Hash, byte-copy, and post-copy-hash claims apply to `Onoats.app` only. Any commit or edit between staged smoke and install forces a re-smoke. CLI rollback means `git switch --detach` to the recorded prior SHA and running `make install-cli` with the same EXTRAS; the tree stays detached by design and the recorded branch is reported, not auto-restored.
- **Decision (grilled):** Back up the installed app as a zip archive outside LaunchServices-scanned folders (`~/Library/Application Support/Onoats/app-backups/`), never as a second launchable `.app` sharing the bundle identifier. `rollback` unpacks it with `ditto -x -k` and re-verifies signature and designated requirement.
- **Decision (grilled):** STT state definitions live once in the UI Telemetry Contract. `ready` is this instance's own latest successful handshake, never the shared preflight cache; `unknown` is permanent for non-websocket backends and renders neutral grey `status not reported (<service>)`, never green. The confirmed `transcript.*` signal stays the kickstart-cooldown concept and is not a UI readiness gate. STT callbacks enter through new keyword-only `_create_stt_service` parameters; the internally built `on_recovery` is not one of them.
- **Decision (grilled):** The Seminar literal is `seminars`, pinned by a parity test across Swift `seminarCategory`, the `--category` argv, and the telemetry `category` field. The CLI is the only authority on whether it is configured; Swift adds no TOML parser. A failed app-owned Start on the category-membership error shows "Seminar needs 'seminars' in config.toml [categories]" with an `Open Config` action next to `Open Log`.
- **Decision (grilled):** Swift recomputes aggregate STT state from the two branches and rejects telemetry when the published aggregate disagrees.
- **Decision (grilled):** STT `starting` is set at construction for websocket instances only; non-websocket instances are `unknown` from construction. The new `on_ready`/`on_recovering`/`on_failed` callbacks are keyword-only parameters of `_create_stt_service`; `on_recovery` stays internal.
- **Decision (grilled):** A rollback-capable baseline includes the prior CLI SHA, branch/detached state, exact install command and `EXTRAS`, shim path, and verified installed-app identity. This run did not capture every field before implementation, so it is a clean-install run with no rollback claim.
- **Decision (grilled):** No commit holds a parity pin without its Swift target, so the stable category-error tag's Swift parity pin lives in Phase 6 with its Swift constant.
- **Decision (grilled):** `start_epoch` is a required `_write_pid_file` parameter. All three callers (`dual.py` twice, `__main__.py` once) mint and pass it; no fallback clock read.
- **Decision (grilled):** The Phase 5 signing is a throwaway verification sign; the no-fix-after-signing rule applies to the post-gauntlet staged app.
- **Decision (grilled):** Preserve completed Phase 2 as immutable evidence. A new Phase 3 owns accepted runtime review remediation, and Conduct resumes from that first unfinished phase.
- **Decision (grilled):** `SystemOps.spawn` returns a retained `ProcessHandle` abstraction; the model never depends directly on concrete `Process` lifecycle APIs.
- **Decision (grilled):** Swift detects invalid Seminar configuration through the stable `ONOATS-ERROR invalid-category` tag, never through human-facing log prose.
- **Decision (grilled):** The opt-in transcript preview may show Me or Them text, always with its source label. It remains off by default.
- **Decision (grilled):** Open Log opens the in-memory path of this process's most recent app-owned run, falling back to the folder; only after a relaunch with no in-memory path does it scan for the newest file first.
- **Decision (grilled):** The 20260925 plan has no marker and no `## Findings`; its correction is a new dated `## Findings` section at its end.
- Compute Me/Them peaks after socket-frame validation and before staging. The callback receives its source from branch construction, not from mutable frame metadata.
- Keep audio callbacks memory-only and coalesce persistence through one bounded background task. UI animation interpolates between bounded snapshots.
- Let Python publish STT state. Swift performs no network probe and does not duplicate authentication, URI, reconnect, or launchd logic.
- Keep endpoint editing in `config.toml` for this release. The main panel uses the existing runtime-produced `sttLabel`; settings expose the configured service and Open Config.
- Keep transcript preview off by default in `UserDefaults`; this preference affects presentation only and never changes transcript persistence.
- Keep advanced/settings navigation inside the menu-bar popover. Do not create a separate Dock-visible window or move common actions out of the panel.
- Treat the website as an external consumer of canonical SVG exports. Do not invent a website path in this checkout.
- Preserve the direct-`swiftc` build. New Swift source files remain covered by the existing source glob; resources require explicit copy rules.

### Dependencies

- No new Python, Swift-package, or web dependencies.
- Existing relevant versions: Python `>=3.12`, `pipecat-ai` `>=1.3.0,<2.0.0`, `websockets>=13.0`, `pytest>=8`, and `ruff>=0.15,<0.16` from `pyproject.toml`.
- Native compilation continues to use macOS system SwiftUI, AppKit, CoreAudio, AVFoundation, and AudioToolbox frameworks with a macOS 14.4 deployment floor.
- There is no `Package.swift`; native code continues to compile through `native/Makefile`.

### Integration Seams

| Seam | Writer | Caller | Contract |
|---|---|---|---|
| Audio activity | Socket input transport | UI telemetry publisher | Validated PCM, fixed branch source through the keyword-only `on_level` hook, no await/backpressure mutation, normalized finite peak plus producer observation time; 1 s branch expiry |
| Transcript preview | `TranscriptBuffer` | UI telemetry publisher | Final non-empty utterance, bounded copy only, durable JSONL unchanged |
| STT health | Two runtime STT instances | UI telemetry publisher | Branch-labelled states, fixed aggregate precedence, both-ready green rule, Swift aggregate consistency check, redacted details |
| Transcript run attestation | `TranscriptBuffer.run_id` | UI telemetry publisher, Swift decoder, installed smoke | Required schema-v2 `transcript_run_id`; exact recorder PID/start-epoch ownership; JSONL line-1 match |
| UI snapshot | UI telemetry publisher | Swift telemetry reader | Atomic schema-v2 JSON, 4 Hz cap, 2 s heartbeat, 5 s expiry, shared PID/epoch, mode 0600 |
| Lifecycle state | Status/PID files | `RecorderModel` | PID identity remains authoritative; telemetry cannot change recorder state; matching pre-PID detail may decorate app-owned starting only |
| Recorder lifecycle authority | `RecorderLifecycleController` | Control socket, signal adapters, both entrypoints, pipeline finalisation | One lock and state machine own continuation/terminal rotation plus graceful-stop admission; adapters never set shutdown state or rotate independently |
| Startup-signal handoff | Supervisor startup handlers | Recorder lifecycle controller | Controller readiness transfers first-signal ownership in one loop turn; no handler gap or overlapping first-signal owners; second signal retains force-exit authority |
| Recorder control | Recorder-owned Unix socket | CLI Stop/Flush and external native controls | Bounded JSON v1 request; required PID/start epoch; same-UID `LOCAL_PEERCRED`; authentication precedes delegation to the lifecycle controller; acknowledgement follows the delegated action; no PID-signal fallback |
| Session configuration | Immutable `RecorderSessionConfig` created after instance lock/config read | STT construction, banner, telemetry, diarization/preflight, recovery, RSS probe | Every active-session consumer receives the same explicit snapshot; load-on-call is standalone-diagnostics-only |
| Stale identity cleanup | Complete captured `PidRecord` | Runtime and CLI teardown | Compare PID, cmdline, and start epoch immediately before unlink; preserve unreadable, foreign, replaced, or same-PID successor records |
| Recorder settings | Swift `ConfigStore` | Python config loader and CLI actions | Existing narrow TOML edits preserve unrelated content; one Start-time directory snapshot governs launch/read/signal/exit while changes apply next Start |
| Diagnostics | Per-run app-captured log (`onoats-bot-<spawnEpoch>.log`) of the most recent app-owned run | Panel and settings | Open this process's most recent run file when present, otherwise reveal folder (newest-file scan only after relaunch); Open Log on warning surfaces only for app-owned sessions; never tail content in the popover |
| Log output authority | Exclusively reserved append descriptor | App-owned recorder and owned Flush helpers | Every child receives a duplicate of the retained descriptor; owned Stop terminates the retained supervisor; category-tag reads use the retained sink; no writer or machine reader reopens the pathname |
| Category-error detection | App-captured run log | `RecorderModel` | Bounded tail read, matched against the stable `ONOATS-ERROR invalid-category` tag only on a non-zero exit with no recorder PID ever published; human prose is display-only |
| Native assets | Canonical SVG/exports | Makefile and Swift panel | Explicit filenames, template semantics, resource copy before signing |
| Install pairing | Verified staged app hash and CLI identity (SHA, clean tree, shim metadata, import path) | Native install workflow (`install-staged`, `rollback`) | App bytes stay unchanged after smoke; rollback requires a complete prior baseline and the exact extracted-and-verified archive before installed-app removal |
| Release identity | Immutable `ReleaseIdentity` from the descriptor-backed staged candidate | Installer, LaunchServices checks, dynamic-code smoke verification, post-record verification | Bundle ID, designated requirement, cdhash, tree hash, executable evidence, and CLI identity are one record; dynamic PID code identity plus descriptor/tree checks bind the launched generation |
| Installer operator input | Dedicated `ONOATS_INSTALL_*` environment values | `install_staged.py` | Python owns defaults/derivation; fixed Make recipes do not expand or transform operator-controlled path/identity bytes |
| Telemetry field parity | Python producer key-set constants | Swift exact-field decoder and parity harness | Every root/nested key set and optional shape is table-tested; an unknown field at any level is rejected in both languages |
| Recording proof | Fixture-fed mic socket or signed app-owned microphone | Full recorder pipeline and session JSONL | Readiness is insufficient; acceptance requires fresh non-zero Me evidence and a durable canonical `source=me` utterance |
| Smoke process identity | `proc_pidinfo(PROC_PIDTBSDINFO)` plus process topology | Installed-smoke teardown | Kernel `(tvsec, tvusec)` birth is signal authority; PID/command/PGID are secondary and every signal rechecks the captured generation |
| Historic plans | Current plan | Prior decision records | Dated correction and cross-link only when a prior decision is materially superseded |

## Architecture & Call Flow

```mermaid
graph LR
    U["User"] -->|"Start, Stop, Seminar, settings"| M["Onoats menu-bar app"]
    M -->|"owned Start and Stop"| P["Python CLI and supervisor"]
    M -->|"generation-authenticated external Stop and Flush"| Q["Recorder control socket"]
    Q -->|"authenticated intent"| LC["RecorderLifecycleController"]
    P -->|"signal intent and terminal outcome"| LC
    LC -->|"ordered continuation or terminal rotation"| R
    M <-->|"selected settings"| C["config.toml"]
    C -->|"one immutable snapshot per Start"| SC["RecorderSessionConfig"]
    SC -->|"backend, endpoint, display, recovery, and shutdown probe"| R
    P -->|"spawn and supervise"| N["Native capturer"]
    N -->|"framed Me and Them PCM"| R["Recorder pipelines"]
    R -->|"lifecycle and warnings"| S["Status file"]
    R -->|"throttled levels, STT health, transcript preview, and TranscriptBuffer.run_id via one publisher task started after running status and cancelled before stopped status"| T["Schema-v2 UI telemetry snapshot"]
    R -->|"durable utterances with matching session_meta.run_id"| J["Session JSONL"]
    M -->|"poll authoritative state"| S
    M -->|"poll display-only live data"| T
    T -->|"attested transcript_run_id"| X["Installed smoke"]
    J -->|"line-1 run ID plus canonical Me utterance"| X
    RI["ReleaseIdentity"] -->|"static tree plus dynamic PID code identity"| X
    RI -->|"same approved identity"| M
    P -->|"stdout and stderr of app-owned runs"| L["Per-run app log"]
    M -->|"Open Log and stable category-error tag read"| L
```

```mermaid
sequenceDiagram
    participant U as User
    participant M as Menu-bar app
    participant P as CLI and supervisor
    participant N as Native capturer
    participant R as Recorder pipelines
    participant F as Status and telemetry files

    U->>M: Select Seminar if needed
    U->>M: Start recording
    M->>P: Spawn onoats bot
    P->>N: Spawn capturer
    N->>R: Stream separate Me and Them PCM
    R->>F: Publish lifecycle and throttled UI snapshots
    M->>F: Poll state, warnings, health, and activity
    F-->>M: Current run-scoped snapshot
    U->>M: Stop recording
    M->>P: Terminate retained owned Process handle
    M->>P: Or CLI Stop via generation-authenticated control socket
    P->>R: Drain and finalise
    R->>F: Persist final status and invalidate live telemetry
    P-->>M: Exit
```

| Step | Trigger | Enters context | Cleared/persisted | Turn boundary |
|---|---|---|---|---|
| 1 | App launches or panel opens | PID identity, status, config, telemetry freshness | Model persists for app lifetime | Panel renders |
| 2 | User starts | Seminar choice and shared configuration; a new per-run log file opens | Session/run identity persists until exit; the log persists (newest 10 kept) | Recorder becomes live |
| 3 | Capture proceeds | Separate Me/Them audio, STT state, utterances | JSONL persists; UI telemetry is throttled and session-scoped | Each atomic snapshot |
| 4 | User stops | Graceful-stop request and drain state | Transcript persists; the publisher task is cancelled and joined before the stopped status, then owner-checked cleanup invalidates live telemetry; final status remains | Supervisor exits |

## Testing Notes

### Automated Test Approach

- Unit-test UI telemetry schema, numeric validation, truncation, permissions, atomic replacement, throttling, freshness, owner-checking, cleanup, and fail-open writes.
- Unit-test the control socket's byte/message bounds, peer and file permissions, expected-generation authentication, command acknowledgement, successor rejection, cleanup races, and missing/legacy-server failure. Mutations restoring PID signalling or weakening the complete-record comparison must fail.
- Unit-test the installer environment boundary with hostile Make syntax and exact-byte paths; prove fixed recipes do not evaluate operator input. Pin Swift decoding to the shared run-ID pattern with a mutation that fails when validation duplicates the grammar.
- Mutation-test ownership/freshness by substituting stale PID, same PID with a different start epoch, stale update time, schema drift, malformed JSON, and non-finite or out-of-range levels (`-0.0`, `0.0`, `1.0`, `1.0000001`, `inf`, `nan`; rejected, never clamped). Assert the one minted epoch is identical in PID, status, and telemetry. Assert a branch observation older than 1 second yields a per-branch `No signal` while the snapshot, the other branch, and STT state stay accepted, and that a branch emitting only rejected values for more than 1 second does too. Read the boundary verdict table from `tests/fixtures/ui_telemetry/` in both Python and Swift; it stores levels as strings because JSON cannot carry `inf`/`nan`, and the writer's `allow_nan=False` is asserted too.
- Fake-clock changed writes, unchanged 2-second heartbeats, 4 Hz suppression, 1-second branch expiry, 5-second snapshot expiry, repeated-failure warning suppression, recovery logging, and fail-open cleanup. Assert heartbeats do not advance branch observation times, and that a constant level with continuous frames writes at least every 500 ms.
- Exercise the menu-bar source matrix: Me/Them, socket telemetry present/absent, unsupported external capture, transcript preview enabled/disabled, and owned/external session. PortAudio and `bot-single` remain outside new telemetry scope: assert `portaudio` builds no publisher and `run_onoats` never constructs one.
- Assert level callbacks preserve one socket to one branch and do not alter staging, byte caps, drop policy, or fatal error behaviour; a raising observer neither stops the reader nor changes staging.
- Assert every mic/system STT state pair and callback ordering against the fixed aggregate precedence, including startup, recovery, confirmation, auth/protocol failure, unknown backend support (a non-websocket instance is `unknown` from construction and never `starting`), a lost established connection firing `on_recovering`, and every aggregate/branch mismatch.
- Assert no telemetry file exists before `_write_status_running` returns and a raising `write_running` leaves none; assert PID line 4, status `start_time`, and the telemetry epoch are identical (`tests/test_socket_supervisor.py`).
- Exercise a cancellation-resistant telemetry publisher and both normal and force-exit timeout paths. Assert shutdown stays bounded and preserves stopped-status → cleanup → pid-removal ordering.
- Assert URI userinfo/query secrets are stripped before STT detail reaches the telemetry writer: the Python side asserts the `_redact` output at the caller (`tests/test_websocket_stt_reconnect.py`, `tests/test_runtime_preflight.py`) and in the written file. Swift performs no redaction, so the decoder harness only pins the 256-scalar detail cap (256 accepted, 257 rejected); what the display shows is not a Swift-testable secrecy property.
- Source-read Swift contracts and execute the native decoder harness against shared JSON vectors for telemetry fields, allowed states, exact bounds, freshness, ownership, aggregate mismatches, and rejection cases. Include 239/240/241-scalar strings using combining marks, astral characters, and ZWJ sequences. Also source-read the new Swift files and assert they contain no `URLSession` or `NWConnection` use.
- Execute the native model harness across directory changes, PID reuse, old-run exit, next-run adoption, owned termination, external Stop, Flush, pre-PID permission detail including stale/equal timestamps and both clearing transitions, drain-state convergence, per-run log naming and newest-10 pruning, stable category-error tag detection, missing-CLI behavior, and the Open Log resolution rule.
- Keep existing status, CLI, socket, supervisor, diarization, release metadata, and parity suites green.
- Exercise cleanup against FIFO, device/non-regular entry, symlink, oversized file, malformed owner, and same-path inode replacement; every case stays bounded and preserves a replacement.
- Exercise archive pathname replacement between verification and extraction; symlink, FIFO, device, and directory archive inputs; unsafe ACL/mode metadata; wrong resource entry types; pre-commit failure; post-commit cleanup failure; and leftover temporary names. Each case stays bounded, and LaunchServices reports only the canonical app path.
- Exercise descriptor-owned logging by replacing the reserved pathname before recorder and Flush spawns and before invalid-category detection. Assert every duplicate carries `O_APPEND`, all output and the bounded tag read target the originally reserved inode, the owner survives overlapping children and graceful Stop, and all descriptors close after the final writer exits.
- Exercise recording in two layers: deterministic full-path persistence in automated tests, then real signed-app mic/TCC persistence in the installed smoke.
- Exercise installed-smoke run identity across startup recovery, historical-file renames, copies and hard links, continuation files, multiple directory-new IDs, malformed headers, telemetry/JSONL mismatch, exact/over-limit/unterminated lines, diagnostic secrecy, and source variants. Only the telemetry-attested run with a canonical Me utterance may pass.
- Exercise captured-identity teardown across app-first exit, recorder-first exit, reaped group leaders, recycled PIDs/PGIDs, same-second PID collisions, partial signal failures, and cleanup-error aggregation. Every case stays bounded; every signal requires the captured identity to pass the immediate kernel birth-tuple and secondary-evidence recheck. Tests prove the userspace gate, not impossible atomicity with delivery.
- Exercise microphone telemetry with `-0.0`, `0.0`, `1.0`, finite out-of-range values, stale/fresh windows, future timestamps, non-finite JSON constants, ownership mismatch, and invalid floor arguments. Acceptance requires finite in-range values and non-negative ages before the floor comparison; invalid floors fail before any app launch or discovery seam.
- Exercise `RecorderLifecycleController` across both entrypoints with intent-order, admitted-handler cancellation, client disconnect, duplicate request, terminal error, signal handoff, and second-signal force matrices. Every test names the sole lock/event/rotation owner; mutations restoring a raw event set, second lock, or out-of-controller rotation fail.
- Exercise fail-open status with present/absent status crossed with success/error exit. Assert exactly one redacted warning, no telemetry publisher when initial status fails, durable transcripts, working control, and unchanged recorder result. Status-absent failures use process exit plus the run log, never a fabricated late status record.
- Exercise supervisor timeout with a cancellation-suppressing task and blocked default-executor worker. The injected hard-exit hook fires by `_RECORDER_DRAIN_GRACE_SEC + _CAPTURER_TERM_GRACE_SEC + _CAPTURER_KILL_REAP_GRACE_SEC + 1.0`; every captured process receives the bounded teardown attempts and any survivor is reported before the hook.
- Exercise `RecorderSessionConfig` by mutating config after capture and asserting identical values at both STT arms, banner, telemetry, diarization, preflight, reconnect/kickstart, RSS probe, and shutdown. Run the focused contract on real Python 3.12 and 3.13 CI interpreters.
- Exercise native generation truth across complete/incomplete PID records, same-PID/new-command and same-PID/new-epoch cache entries, requested/unrequested zero/non-zero exits, and matching/stale/absent diagnostic status. Non-zero always renders failed.
- Exercise every `ReleaseIdentity` field at prelaunch and post-record boundaries, including path replacement, same-path tree replacement, wrong bundle ID/requirement/cdhash, dynamic PID code mismatch, helper/resource change, unrelated LaunchServices namespace, and signature-only impostor. Installation and smoke must reject different identity-record bytes.
- Generate every optional telemetry object shape from the Python producer, feed it to Swift, then inject one unknown key at root, audio, STT aggregate, STT branch, and transcript levels. Mutations that disconnect a declared key set from either producer or decoder fail parity.

### Manual Native Smoke Matrix

- Stopped: panel opens at a stable size; Seminar is editable; Start is dominant; settings are reachable and return preserves state. With an unavailable CLI, Start is disabled and Diagnostics explains the missing command.
- Starting/running/stopping/failed: every transition has a truthful label and the main action cannot double-fire.
- Owned and external sessions: Stop follows the correct path; Flush still works; draining never renders as stopped early.
- Seminar: selected before Start, shown read-only during an app-owned recording even if telemetry disappears, passed as `--category seminars`, and reset after exit; external sessions without fresh category telemetry show `Category unavailable`.
- Me/Them: speak/play each side independently; only its meter responds; silence decays to a zero level (the capturer's silence pacer keeps frames flowing, so silence alone never produces `No signal`); `kill -STOP <capturer pid>` then `kill -CONT` makes both branches show a greyed `No signal` row after one second and recover while STT state stays live (one capturer feeds both branches, so independent per-branch expiry is covered by the fixtures and fake-clock tests, not by smoke); `kill -STOP <recorder pid>` for about 7 seconds then `kill -CONT` shows telemetry unavailable after five seconds. Keep each pause under the transport's 10 s default read-idle timeout.
- STT: with the websocket backend, confirmed ready, recovering, failed, and starting render distinct states without false green. With `whisper` or `deepgram`, the row stays neutral grey `status not reported (<service>)` and never green.
- Seminar config: remove `seminars` from `[categories]`, select Seminar, and Start; the failure surface says "Seminar needs 'seminars' in config.toml [categories]" with `Open Config` and `Open Log`.
- Transcript preview: off by default; enabling shows one bounded latest Me or Them line with its source label; disabling removes it immediately; stale prior-run text never appears.
- Warnings: all-zero input, STT recovery, login-item hint, schema drift, Flush/Stop failure, and failed-session detail wrap without panel expansion.
- Settings: microphone, system-output display, STT service, data directory, config open, CLI diagnostics, and Quit/Stop & Quit retain current behaviour. The system-output label remains visible across settings navigation.
- Directory transition: change the configured directory during a run; confirm the panel keeps launching/reading/signalling/exiting through the active directory, shows active/next paths, and adopts the new directory only after the next Start.
- Diagnostics: run two app-owned sessions, then Open Log opens the second run's file labelled with its start time; Show Logs Folder reveals the folder; with the app idle and that second run's log file deleted, Open Log falls back to the folder (not the first run's file: the in-memory path rule); after quitting and relaunching the app, Open Log opens the newest surviving file; an external-session warning shows `Log not captured (started outside the menu bar)` and no Open Log; no transcript/log tail appears in-panel.
- Dismissal: Escape and click-away close the panel; reopen and settings/back preserve correct state while recording continues.
- Accessibility (precondition: Keyboard navigation on): Tab/Shift-Tab focus order, Space/Return activation, VoiceOver labels, tooltip text, contrast, Reduce Motion, and light/dark menu bars.
- Branding: header and app icon render sharply; custom menu icon remains legible at supported scales and shows each state.
- Panel refinement: the header visibly reads `Oats - Organized Transcript System`; the Seminar option renders as a grouped native switch; no transparent header padding creates a blank region.
- Recording proof: speak during an app-owned signed session; Me becomes non-zero and the stopped run creates a new durable Me utterance. A connected process with no Me utterance fails smoke.
- Bundle (post-gauntlet release steps): staged signed app passes resource allowlist and codesign checks; the app tree digest matches the installed bytes and the CLI git identity (SHA, clean tree, `command -v onoats`) matches the one recorded at smoke; a valid baseline preserves byte-identical designated requirements and grants, while a missing baseline follows the documented clean-install path.

### Test Results

- [ ] Focused telemetry and parity tests pass.
- [ ] `uv run ruff format` and `uv run ruff check` pass.
- [ ] Full `uv run pytest` passes.
- [ ] `make -C native build` passes.
- [ ] `native/test_ui_telemetry.sh` passes on macOS.
- [ ] `native/test_recorder_model.sh` passes on macOS.
- [ ] Resource-allowlist negative checks (extra file, missing file) fail the verifier.
- [ ] Signed bundle verification passes (the post-gauntlet staged app; the Phase 5 sign is a throwaway).
- [ ] Designated-requirement bytes match the verified installed baseline when one exists; clean-install evidence is recorded otherwise.
- [ ] Manual native smoke matrix passes.
- [ ] Deterministic recording integration persists a canonical Me utterance.
- [ ] Signed installed-app recording smoke observes non-zero Me audio and persists a canonical Me utterance.

## Acceptance Criteria

- The menu-bar item opens a window-style panel whose dominant action is Start when stopped and Stop when running.
- Seminar is one interaction away before Start, remains next-session-only, and displays read-only during an app-owned locked run even without telemetry. External sessions require fresh matching category telemetry or show `Category unavailable`.
- Every existing control remains available either on the main panel or in its settings destination.
- Owned Stop, external Stop, Flush, Quit, liveness, warnings, and drain semantics remain covered. Retained app-owned Process handles may terminate their exact child; external Stop/Flush receive acceptance from the matching recorder generation over the control socket and never fall back to PID signalling.
- Me/Them indicators use separate fresh CoreAudio/socket values with producer observation times; a branch older than one second renders a visible greyed `No signal` row, and any stale, missing, malformed, or unsupported external telemetry renders unavailable.
- The primary combined STT row never shows green unless both menu-bar branches confirm ready; non-ready states identify unhealthy branches, and Swift rejects a published aggregate that disagrees with its branches.
- Transcript preview is opt-in, bounded, source-labelled for Me or Them, and never leaks a previous run.
- Telemetry writes are versioned, atomic, mode 0600, capped at 4 Hz, heartbeated every 2 seconds, expired after 5 seconds, owner-checked, asynchronously coalesced, and fail open.
- PID, lifecycle status, and telemetry carry one identical caller-minted run epoch; telemetry begins only after authoritative identity publication.
- Changing the data directory during a run leaves one Start-time path authoritative for launch, reads, external Stop, Flush, and exit handling; the panel shows active/next paths and applies the change on the next recording.
- A matching pre-PID permission-wait status may decorate only the app-owned starting state and never creates recorder liveness.
- Warning/failure surfaces of app-owned sessions and failed app-owned starts can open the log of the most recent app-owned run (the newest surviving file only after an app relaunch); external-session warnings say the log was not captured; Settings → Diagnostics can open the file or reveal its folder without embedding log content.
- `STATUS_SCHEMA_VERSION` remains 2 and all existing Swift/Python parity tests pass.
- A user-approved canonical SVG brand, app icon, and monochrome template menu icon are committed with reuse guidance.
- The assembled clean staged app contains only allowlisted resources and passes signature verification. Staged smoke and installation use the exact same hashed app bytes and the same verified CLI identity (SHA, clean tree, shim metadata, and import path); rollback uses only the exact extracted-and-verified archive from a complete prior baseline. This run is explicitly clean-install because its Phase 1 baseline is incomplete.
- README/native docs describe the final UI and telemetry meanings; only materially superseded historic decisions receive dated corrections.
- After accepted review fixes, focused tests, full pytest, Ruff format/check, review-marker validation, native compile, signed-app verification, and manual smoke all pass on the final bytes.
- Pre-merge documentation, code, security, and full review gates (the auto-chained gauntlet after Phase 11) report no unresolved findings before the Post-Gauntlet Release Steps sign anything.
- The STT row shows `ready` only for the websocket backend with both branches' own handshakes confirmed; `whisper` and `deepgram` sessions show neutral grey `status not reported`.
- A failed app-owned Start on a missing `seminars` category explains the cause and offers `Open Config`.
- The panel visibly identifies itself as `Oats - Organized Transcript System`, uses a grouped Seminar switch, and distinguishes `Signal`, `Silent`, and stale `No signal` states.
- Telemetry cleanup is bounded and compare-and-unlink safe; a non-regular or replaced snapshot cannot block shutdown or be removed.
- App-owned log writers retain descriptor/inode authority from reservation through exit; pathname replacement cannot retarget recorder, Flush, or Stop output.
- Installer verification and extraction consume one immutable archive snapshot, enforce entry/metadata policy, and leave at most one LaunchServices-visible Onoats app across every failure point.
- Automated recording integration and signed installed-app smoke both require durable canonical `source=me` evidence; process, socket, or STT readiness alone cannot pass.
- Installed-app smoke transcript proof belongs to the recorder-owned schema-v2 telemetry `transcript_run_id` and a matching JSONL first line; path movement, corpus novelty, or historical-file recovery cannot satisfy it.
- Installed-app smoke cleanup attempts every still-matching captured recorder/group identity after app-first exit; each delivery requires an immediate kernel birth-tuple and secondary-evidence match. Kernel process birth, not second-resolution start text, is userspace signal authority, and the residual recheck-to-delivery race remains an explicit accepted limitation.
- Installed-app smoke rejects non-finite or future-dated telemetry and validates its configured Me floor before launch.
- Stop and Flush are accepted only by the recorder generation named in the request; missing, legacy, mismatched, malformed, oversized, or replaced control endpoints fail closed without signalling a PID.
- Stale PID and control-socket cleanup preserve successor generations, including same-PID/new-epoch records and replacement sockets.
- Staged installer paths and identities cross Make as environment data only; hostile Make syntax is never evaluated, while exact path bytes reach Python.
- One lifecycle controller orders control and signal Flush/Stop plus terminal rotation; no second lock, event writer, or finally path can reopen a continuation after stopping begins.
- Failure to publish rich running status never aborts capture or transcript persistence after PID/control authority exists; startup cancellation still has a process-level upper bound.
- Every STT service, banner, telemetry state, recovery action, and shutdown probe in one recording consumes the same immutable session configuration.
- Native liveness caches and exit reporting bind to the complete recorder generation; a requested Stop never hides a non-zero exit.
- Installer and signed-app smoke accept only the approved bundle identifier, designated requirement, cdhash, and tree hash; telemetry field sets remain Python/Swift parity-checked.
- Startup first-signal authority transfers from supervisor to recorder controller without a gap or overlap; a cancellation-suppressing task cannot keep the process alive beyond the documented grace-sum deadline and hard-exit tolerance.
- Running-status failure emits one redacted warning, creates no telemetry publisher, preserves recording/control/transcript behavior, and uses process exit plus run log for later failure detail.
- Python 3.12 and 3.13 contract lanes pass before release publication.
- The active app PID's dynamic code identity matches the same immutable `ReleaseIdentity` used for staged installation; static path/signature checks alone cannot pass smoke.
- Every conditional telemetry shape and unknown-key nesting level passes Python-producer to Swift-decoder parity coverage.

<!-- reviewed: 2026-11-05 @ 3b6273d0c4624a78012c19e9915a69a4dc9a6bb1 -->

## Progress

- [x] Phase 1: Brand Direction and Canonical Assets
- [x] Phase 2: Ephemeral UI Telemetry Contract and Runtime Producer
- [x] Phase 3: Accepted Review Remediation for Completed Runtime Work
- [x] Phase 4: Swift Telemetry Consumer
- [x] Phase 5: Native Resource Pipeline and Stateful Menu Icon
- [x] Phase 6: Window-Style Recording Panel and In-Panel Settings
- [x] Phase 7: Documentation, Historic Decision Corrections, and Pre-Gauntlet Verification
- [x] Phase 8: Contract Redesign, Panel Refinement, and Recording Proof
- [x] Phase 9: Run-Bound Recording Proof and Captured-Identity Teardown
- [x] Phase 10: Recorder-Owned Control Channel and Boundary Hardening
- [x] Phase 11: Unified Recorder Lifecycle Authority and Release Identity Closure

## Findings

- 2026-11-02 — Phase 9 completed the recorder-attested schema-v2 run-ID flow,
  descriptor-bound JSONL proof, kernel-birth process identity, and captured
  teardown matrix. The canonical focused gate passed 226 tests with one opt-in
  skip plus the Swift decoder harness; full validation passed 7,348 tests with
  four opt-in skips, Ruff, review-marker validation, and the native build.

- 2026-11-02 — Phase 9 review (`20261101T195750Z-phase9-amendment`) produced
  11 unique findings and two reported contradictions. All were accepted and
  reconciled into the contract: telemetry now attests the transcript run,
  `proc_pidinfo` process birth is signal authority, and the real microphone/TCC
  smoke occurs only after signing the staged app.

- 2026-11-01 — The Round 3 gauntlet seam tripwire halted after three independent
  findings reproduced the installed-smoke lifecycle defect after its Phase 8
  contract redesign. Two further findings showed path-based transcript proof
  accepting recovered historical JSONL and permissive float parsing accepting
  non-finite or future telemetry. Phase 9 replaces those authorities before any
  signing or installation step.

- 2026-10-30 — Phase 1 approved the grain-wave direction committed at
  `e19323cdc544dabf298179f1df1830d1ce4a0538`. Rollback baseline before any
  Phase 2 code change: `git status --porcelain` was empty; `command -v onoats`
  resolved to `/Users/vr000m/.local/bin/onoats`; the installed app was
  `/Users/vr000m/Applications/Onoats.app`; its designated requirement was
  `identifier "net.varunsingh.onoats" and certificate leaf =
  H"aac7e2b994e42bed55fad8e292cf3ba1a4629cee"`; and its cdhash was
  `4491f6d250ae237fed30a388a5a76aa35f7468ac`.
- 2026-10-30 — Phase 2 completed at
  `099270e9515d2f86bc9d2740dc36752fcd7b3904`; its focused gate passed with
  462 tests before the plan was restructured for Conduct-owned native phases.
- 2026-10-30 — The post-Phase-2 plan review produced 14 accepted findings and
  no contradictions. Phase 2 remains immutable; new Phase 3 owns its runtime
  remediation, and the former Phases 3–6 are now Phases 4–7.
- 2026-10-30 — This run is a clean-install release. The Phase 1 record did not
  capture the exact prior CLI `EXTRAS` or branch/detached state before work
  began, so the installed baseline must not be used for rollback.
- 2026-10-30 — Phase 3 added the stable invalid-category machine tag and a
  synchronous telemetry stop-request seam. The focused gate passed 120 tests;
  Ruff validation passed. Its one-shot reviewer found two shutdown-boundary
  defects, both fixed before the phase boundary.
- 2026-10-30 — Phase 4 added strict Swift telemetry decoding and a model seam
  that keeps PID identity and liveness authoritative. The focused gate passed
  both native harnesses and 34 parity tests; the native app build validated.
  Its one-shot reviewer found six ownership, lifecycle, cleanup, isolation,
  fixture, and polling findings, all fixed before the phase boundary.
- 2026-10-30 — Phase 5 added clean resource assembly, exact allowlist checks,
  state-specific template glyphs, strict signing verification, and staged
  install/rollback targets. The focused gate passed 65 tests and the signed
  bundle validation. Missing and unexpected resource probes both failed as
  required. Its one-shot reviewer found two rollback-boundary defects, both
  fixed before the phase boundary.
- 2026-10-30 — Phase 6 replaced the flat menu with the window-style recording
  panel, in-panel settings/diagnostics, per-run logs, and bounded stable-tag
  diagnostics. The native harness, 75 focused tests, Ruff checks, and the
  native build passed. Its one-shot reviewer found four lifecycle, logging,
  append, and wording defects; all were fixed before the phase boundary. The
  interaction, VoiceOver, and visual matrix remains a post-gauntlet manual gate.
- 2026-10-30 — Phase 7 synchronized the user and maintainer documentation,
  appended narrow corrections to the two superseded historic UI decisions,
  and preserved every reviewed marker. The focused gate passed 153 tests; the
  native-access full suite passed 7,193 tests with 3 skips; Ruff and the unsigned
  native build passed. Its one-shot reviewer found three documentation/test
  scope issues, all fixed before the phase boundary. The full review gauntlet
  and post-gauntlet manual release steps remain pending.
- 2026-10-31 — The first gauntlet produced two fix commits, `7439623` and
  `9a6ef3d`, whose complete suites passed 7,200 and 7,215 tests respectively.
  Round 3 approved the native code-review and adversarial gates but found 13
  deep-review issues. The seam tripwire stopped a third automatic fix because
  the redesigned RecorderModel filesystem seam and operator workflow moved
  again. Phase 8 is the operator-approved contract redesign for those findings.
- 2026-10-31 — Installed-branch feedback found that the panel lacked visible
  product identity, presented Seminar as a bare checkbox, and produced no
  durable Me utterance during an app-owned recording. The run log showed the
  signed app bound `MacBook Pro Microphone`, connected both socket transports,
  and reached STT readiness, but readiness created no session artifact. A
  terminal-launched raw capturer check was denied microphone access; that is
  terminal TCC evidence only and does not diagnose the signed app. Phase 8 adds
  full-path and installed-app recording proofs that cannot pass on readiness.
- 2026-10-31 — The Phase 8 contract review ran five clean-context lenses plus
  a post-reconciliation contradiction pass. It produced 17 unique findings
  with no dropped records: two Critical entries for the same phase/gauntlet
  dependency cycle and 15 Important ownership, installer, logging, smoke, and
  test-matrix gaps. All recommended resolutions were incorporated before the
  marker refresh. Phase 8 now completes before the auto-chained gauntlet;
  accepted gauntlet fixes reopen the phase rather than creating a cycle.
- 2026-10-31 — Phase 8 closed the four advisory findings without a contract
  contradiction: telemetry cleanup remains descriptor-owned through removal,
  installer uniqueness is global and failure-atomic, installed smoke rejects a
  recorder crash, and the full recording test requires paced microphone audio
  to produce a durable `source=me` utterance before graceful shutdown. The
  focused gate passed 211 tests with 1 skip plus RecorderModelHarness; Ruff,
  the 7,255-test full suite with 4 skips, review-marker verification, and the
  native build passed. Post-gauntlet signing, installation, and installed-app
  microphone smoke remain manual gates.

## Issues & Solutions

- A cancellation-resistant arbitrary `publisher.stop()` wrapper could survive
  until `asyncio.run` teardown. The fix requests stop synchronously and bounds
  only the publisher-owned task.
- A second signal arriving during the normal 0.5-second telemetry wait could
  still be delayed. The helper now races the owned task against the live
  `force_exit_event` and cancels immediately when force-exit wins.
- The first Swift consumer could reclaim `.stopping`, accept an unrelated PID
  as app-owned, and poll identity at telemetry cadence. Lifecycle refresh now
  preserves draining state and caches only a current-attempt PID/start tuple;
  the 4 Hz telemetry poll decodes against that cache without probing liveness.
- The native process callback retained its handle and the harness wrote host
  preferences. The callback now captures weakly and clears on spawn failure;
  the harness runs with an isolated home and removes every test suite domain.
- Designated-requirement command output includes a path-bearing `Executable=`
  line and may prefix the requirement with `# `. The installer now extracts
  exactly one normalized requirement expression and rejects zero or multiple
  matches; ad-hoc signed execution tests cover three distinct app paths.
- An existing app is not sufficient evidence for rollback approval. The
  staged installer now defaults to clean-install mode and clears stale approval;
  archive creation requires explicit expected requirement and cdhash evidence,
  while approved mode fails closed before removing the installed app.
- A recorder can publish and tear down between menu polls; teardown removes the
  PID file before process exit. Exit classification now accepts only matching
  current-attempt, recorder-written status evidence, identified by the
  non-empty STT label that distinguishes it from the pre-recorder sentinel.
- Run logs are reserved with exclusive creation and all process writers use
  kernel `O_APPEND`, preventing timestamp collisions, stale-tag reuse, and
  separate recorder/Flush descriptors from overwriting one another.

### Review gauntlet round 1 remediation (2026-11-02)

- Exit diagnostics now require the owned PID and the published epoch, or the
  existing current-attempt epoch bound before publication. Start enablement and
  its action share the retained recorder/log-sink lifetime gate, including Flush.
- Pruning first moves a candidate into a private same-directory quarantine and
  verifies the moved inode against the opened descriptor. A replacement is
  restored without overwriting a new arrival; an occupied restoration retains
  the candidate in quarantine rather than deleting it.
- Installed-smoke control reads share a 64 KiB, nonblocking, no-follow regular
  descriptor reader. Recorder discovery also verifies the identity marker and
  live command fingerprint; app discovery and cleanup use kernel executable
  paths (`proc_pidpath`) alongside captured process generations.
- Matrix regressions cover publication/PID/epoch, pruning replacements and
  restoration collisions, auxiliary lifetimes, control-file kinds, PID identity,
  and kernel-path/argv combinations. All six mechanism mutations failed their
  new tests and were restored; the smoke suite (142 tests), both native harnesses,
  lint/format and review markers pass. The model harness's pre-existing schema-1
  telemetry fixture failure was reproduced from pre-fix HEAD in isolation, then
  updated to schema 2 with its required run ID. Panel parity now pins `canStart`.
- `just ci` has no justfile. The sandbox full suite produced 7344 passes,
  52 failures and 9 errors (socket/ps/Metal restrictions plus the now-corrected
  panel parity assertion); the full native build reached a sandbox-denied Swift
  macro plugin after redirecting its module cache. Parent validation reruns these
  full gates outside the sandbox. Sibling-plan review found no superseded decision.

### Review gauntlet round 2 remediation (2026-11-02)

- Remote `session.closed` now revokes STT readiness just like EOF or a reader
  error. Root cause: the recovery callback lived inside the abnormal-close
  logging branch, although an orderly remote close also ends the handshake.
- The reader's current-client and prior-connected guards own the transition;
  local drain/close detaches the client before awaiting teardown. A 24-case
  matrix covers three remote endings, current/superseded/draining/closing
  ownership, and both connected states, including repeated teardown. Restoring
  the old transition fails exactly the established current `session.closed`
  case; restoring the fix passes all 24 cases. The reconnect suite passes 71.
- Callback and teardown call-site audit found no additional transition owner
  requiring changes. Sibling plans retain their historical decisions. Native
  code and contracts are unchanged; no native build is required for this fix.
- `just ci` has no justfile. Format, lint, and review-marker checks pass.
- Full sandbox suite: 7369 passed, 51 failed, 9 errors, 3 skipped; socket/ps
  restrictions and unavailable Metal prevent a green broad gate here. Combined
  reconnect/config/telemetry run: 181 passed, 2 Metal-dependent failures. Parent
  validation must rerun the broad gate outside the sandbox.

### 2026-11-02: Gauntlet round 3 lifecycle and telemetry corrections

- Readiness requires the exact negotiated client, a connected state, and a live
  reader after the acknowledgement wait. A newer remote-close revocation cannot
  be overwritten by readiness or kickstart-recovery callbacks.
- Quit retains owned recorder/auxiliary handles until exit. The 15-second error
  fallback leaves the app alive rather than bypassing supervisor teardown.
  Early supervisor signal ownership covers spawn, socket/permission waiting,
  and preflight; running handlers retain their existing drain semantics.
- The publisher now captures immutable snapshots on the loop and awaits one
  dedicated daemon I/O worker through a one-slot queue. New observations coalesce
  in memory. The shutdown cleanup callback requests worker-owned cleanup only
  after stopped status; a late write is followed by owner-checked cleanup.
  A blocked filesystem cannot block the recorder loop or executor shutdown.
- These changes refine the existing fail-open and ownership contracts without
  changing schemas, process ownership, external-session signalling, or STT
  reconnect policy. Sibling-plan audit found no superseded historical decision.
- Restored focused gates: 147 Python tests and the RecorderModel native harness
  pass. Four mutations fail as expected: removed readiness gate (4 cells),
  synchronous telemetry writes (3 cells), removed startup signal owner (8 cells),
  and immediate Quit termination (native startup-ownership assertion).
- Format, lint, review-marker and whitespace gates pass; `just ci` has no
  justfile. Full sandbox suite: 7393 passed, 51 failed, 9 errors, 3 skipped;
  AF_UNIX/ps restrictions and unavailable Metal still block broad validation.
  Production native compilation is blocked by the sandboxed Swift macro plugin;
  the isolated model harness compiles and passes. Parent must repeat those
  environment-blocked gates outside the sandbox.

### 2026-11-03: Unloaded STT launch-agent recovery

- Installed testing showed the configured websocket endpoint could remain
  unreachable when its launch-agent plist existed but the job was unloaded.
  The existing self-healing path only kickstarted already-loaded jobs.
- A configured `launchd_label` now also authorises one fixed bootstrap target:
  `~/Library/LaunchAgents/<label>.plist`. Onoats rejects symlinks, non-regular or
  oversized files, wrong ownership, group/world-writable files, mismatched
  labels, and plists without `RunAtLoad=true`; it never infers authority from a
  port or accepts an arbitrary path. Bootstrap uses the existing bounded,
  secret-free `launchctl` subprocess policy and tolerates a concurrent loader.
- The live configured job was booted out and recovered through the production
  helper. Launchd reported it running and TCP port 9865 was reachable after the
  bootstrap. Focused recovery/config/preflight coverage passed (160 tests).

### 2026-11-04: Gauntlet round 2 boundary redesigns

- Control endpoint ownership now starts at a private bind and reaches the public
  pathname through an exclusive no-replace rename. Python 3.13 automatic socket
  cleanup is disabled by runtime-version contract, and cleanup uses exclusive
  restoration so no raced pathname can become owned or be deleted as a successor.
- Installed-smoke failure cleanup now applies one complete TERM, bounded wait,
  KILL, final bounded wait contract to the recorder, every exact captured group
  member, and the app. Gone and recycled identities remain non-targets; survivors
  and independent cleanup errors are aggregated and fail the smoke.
- The staged installer passes bundle paths to resource verification only as
  child environment data consumed by the recipe shell. Operator paths no longer
  enter Make command-line assignments or Make expansion.
- Dual recording and `bot-single` each load one immutable session config and pass
  it to STT construction. Dual telemetry plus both STT arms therefore select one
  backend even if the config changes during startup; direct helper calls retain
  the deliberate load-on-call boundary.

### 2026-11-05: Linux endpoint-identity correction

- Hosted CI exposed that Linux can immediately recycle an unlinked Unix-socket
  inode. Device/inode comparison alone could therefore mistake a raced successor
  for the recorder-owned endpoint and remove it during quarantine cleanup.
- Linux now retains an `O_PATH` descriptor from the private bind through final
  cleanup, pinning the original inode while the quarantined entry is verified.
  Darwin retains the existing device/inode path because it cannot open socket
  nodes through an equivalent descriptor. The no-replace restore and
  newer-successor preservation rules are unchanged.
- The two replacement-at-final-boundary regressions now exercise the corrected
  invariant on Linux; the control-socket contract and its source-parity gate
  explicitly require the pinned identity.

## Final Results

### Summary

- (fill when complete)

### Outcomes

- (fill when complete)

### Learnings

- (fill when complete)

### Follow-up Work

- Integrate the approved canonical brand assets into the onoats.app website after its repository/path is identified.
