# Task: Onoats Menu-Bar Control Panel and Brand Mark

**Status**: Not Started
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
- Expose `Open Log` on warning/failure surfaces of app-owned sessions and failed app-owned starts only, and expose both `Open Log` and `Show Logs Folder` under Settings → Diagnostics. The canonical log is the app-captured stdout/stderr file of the most recent app-owned run: one `onoats-bot-<startEpoch>.log` per run in the existing Logs folder, newest 10 kept. App-owned Flush and Stop output appends to that run's file; external Stop and Flush output is not written to a run log. An external session's warning says `Log not captured (started outside the menu bar)` and offers no `Open Log`. Fall back to the folder when the log file does not exist; do not embed a live log tail.
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

### Phase 2: Ephemeral UI Telemetry Contract and Runtime Producer

**Impl files:** `src/onoats/ui_telemetry.py, src/onoats/transports/socket_audio.py, src/onoats/dual.py, src/onoats/processors/transcript_buffer.py, src/onoats/runtime.py, src/onoats/stt/websocket_stt_service.py`
**Test files:** `tests/test_ui_telemetry.py, tests/test_socket_audio_transport.py, tests/test_dual_socket_source.py, tests/test_websocket_stt_reconnect.py, tests/test_speaker_labels.py, tests/test_status_file.py, tests/test_socket_supervisor.py`
**Test command:** `uv run pytest tests/test_ui_telemetry.py tests/test_socket_audio_transport.py tests/test_dual_socket_source.py tests/test_websocket_stt_reconnect.py tests/test_speaker_labels.py tests/test_status_file.py tests/test_socket_supervisor.py -q`
**Goal:** Runtime telemetry is display-only, branch-safe, stale-rejectable, bounded, and incapable of affecting recorder success or transcript durability.

- Add a separate schema-v1 `.active/onoats.ui.json` snapshot. Do not add fields to `StatusRecord` or bump `STATUS_SCHEMA_VERSION`.
- Define one exact record shape shared by Python tests and Swift decoding: schema, recorder PID/start epoch, update time, optional Me/Them levels with per-branch observation timestamps, per-branch plus aggregate STT state/detail, optional bounded transcript preview/source/speaker label, and locked category.
- Write snapshots by same-directory temporary file plus `os.replace`, with owner-only permissions. Because the file is ephemeral, do not `fsync` every meter update.
- Define Python-owned constants (the Swift mirror and the telemetry parity tests land together in Phase 3's first commit, so no commit holds a parity test without its Swift target): schema `1`, file name `onoats.ui.json`, mode `0o600`, maximum write rate `4 Hz`, unchanged-state heartbeat `2 s`, snapshot expiry `5 s`, per-branch audio-observation expiry `1 s`, per-branch observation refresh `500 ms` while frames continue, the Seminar category literal `seminars`, transcript limit `240` Unicode code points, detail limit `256` Unicode code points, and level range `[0.0, 1.0]`.
- Let audio/STT/transcript callbacks update memory only. Coalesce changed snapshots and heartbeats through one bounded background task; no callback performs filesystem I/O or awaits persistence.
- Suppress redundant intermediate snapshots, but write an unchanged heartbeat every 2 seconds so healthy telemetry never expires. A heartbeat updates only snapshot time; it never advances either branch's audio-observation timestamp. An advancing branch observation timestamp counts as a changed snapshot: a branch with continuing frames publishes an observation-time refresh at least every 500 ms, inside the 4 Hz cap.
- Reject non-finite or out-of-range level values at the publisher boundary: drop that observation and keep the last good value. A rejected observation advances neither the level nor that branch's observation time, so a branch emitting only rejected values for more than 1 s renders `No signal`. There is no clamping. Boundary verdicts: `-0.0` accepted and published as `0.0`, `0.0` and `1.0` accepted, `1.0000001`, `inf`, and `nan` rejected. The verdict table lives in `tests/fixtures/ui_telemetry/` and both the Python tests and the Swift harness read it.
- Construct telemetry only for `audio_source == "socket"`. Inject source-labelled level observers into the two existing socket transports; keep the transport generic and preserve one socket to one branch. PortAudio and `bot-single` remain unchanged. Compute each frame's peak from the validated PCM16 bytes with `array('h')` max-abs divided by 32768 (`audioop` is removed in Python 3.13 and the project allows `>=3.12`; no new dependency). Wrap the observer so an exception is caught, logged once and rate-limited, and never raised into the reader.
- Publish transcript preview data from final `TranscriptionFrame` handling without changing JSONL entries or render-time speaker composition.
- Track mic and system STT state independently. Aggregate by fixed precedence `failed > recovering > starting > unknown > ready`; `ready` requires both branches ready. Use `unknown` for backends without a trustworthy confirmation hook. Treat branch states as the aggregate's source of truth. Per-state definitions live once in the UI Telemetry Contract. Runtime sets `starting` at construction. `WebSocketSTTService` gains keyword-only `on_ready()`, `on_recovering()`, and `on_failed(detail)` callbacks that enter through `_create_stt_service` keyword arguments beside the existing `on_recovery`, without changing reconnect semantics.
- Sanitize STT detail through existing URI/exception redaction before it reaches telemetry. Redaction happens in the caller (`runtime.py` or the STT service); `ui_telemetry.py` receives already-redacted text and only caps its length.
- Mint one run epoch in `runtime.py` before PID/status publication. `_write_pid_file` takes the caller-minted `start_epoch` instead of reading the clock itself, `_write_status_running` forwards it as `start_time` (`status.write_running` already accepts it, so `status.py` needs no change), and both return success so `dual.py` can gate telemetry. Build the in-memory telemetry state holder before transports and STT services so their callbacks have a target, but enable file writes only after PID then running-status publication succeeds.
- Create the one publisher task in `dual.py` right after `_write_status_running` succeeds. In `_run_shutdown`'s single-writer tail, cancel and join it (bounded) before `_write_status_stopped`, run owner-checked cleanup between `_write_status_stopped` and `_remove_pid_file`, and never let it delay `force_exit_event` or `wait_or_force`.
- Make producer and cleanup errors warning-only. Cleanup may unlink only a snapshot that still belongs to the same PID/start epoch.
- Rate-limit repeated write/cleanup warnings, log one recovery after writes resume, and test both with a fake clock.
- Invalidate or remove live telemetry after the recorder writes its stopped status; a leftover file remains harmless because consumers reject stale ownership/freshness.
- Keep `ui_telemetry.py` a leaf module: stdlib only, no `onoats` imports. `socket_audio.py`, `TranscriptBuffer`, and `WebSocketSTTService` receive plain callables, never the module.
- Add pinning tests: a raising level observer does not disturb the socket reader or staging; `audio_source == "portaudio"` builds no publisher and `run_onoats` (`bot-single`) never constructs one; no telemetry file exists before `_write_status_running` returns and a raising `write_running` leaves none; PID line 4, status `start_time`, and the telemetry epoch are identical.

### Phase 3: Swift Telemetry Consumer (manual native phase; not `/conduct`-driven)

**Goal:** The menu app accepts only fresh telemetry for the same live recorder and degrades to neutral UI without changing recorder liveness.

- Add a Swift mirror of the UI telemetry schema without modifying the existing `StatusRecord` mirror.
- Resolve and snapshot the configured directory synchronously before an app-owned Start. Export it as `ONOATS_DATA_DIR`, retain it until that PID/start epoch exits, read status/PID/telemetry and exit status only from it, and pass `--data-dir <active>` to external Stop and Flush. For an externally discovered session, pin the directory that supplied the verified identity.
- Retain the changed configured directory as `nextDataDir` and publish active/next display values when they differ.
- Accept telemetry only when schema, PID, start epoch, snapshot freshness, numeric ranges, and optional-string bounds pass validation. Recompute aggregate STT state from the two branches and reject any mismatch with the published aggregate. Judge per-branch observation age (1 s) per branch after acceptance, so one stale branch renders `No signal` without dropping the other branch or STT state.
- Keep PID identity/liveness authoritative. Telemetry must never create `.running`, `.stopped`, or `.failed` state.
- Clear activity, STT health, telemetry-derived category, and preview when telemetry is missing, stale, malformed, or owned by another run. Preserve the app-owned in-memory Seminar lock as its badge authority; for an external session without trustworthy category telemetry, show `Category unavailable`.
- While the retained app-owned supervisor is alive but no recorder PID exists, accept `write_prestart_waiting` detail only when its status PID matches `Process.processIdentifier` and its start time is no older than that Start attempt. It may decorate `.starting` but must never create `.running` or override PID liveness; clear it on supervisor exit or authoritative recorder publication. The match depends on the CLI exec chain preserving the supervisor pid (the uv shim execs Python in place); a forking `cliPath` wrapper never matches and gets no decoration, by design.
- Poll live telemetry at 4 Hz and animate between samples; keep status/PID polling at its existing cadence.
- Persist only the transcript-preview visibility preference in `UserDefaults`, defaulting to off.
- Add source-reading parity tests for the telemetry schema, allowed STT states, aggregate precedence, bounds, freshness constants, and file name. Pin the Seminar literal `seminars` across Swift `seminarCategory`, the `--category` argv, and the telemetry `category` field. Also pin the Swift category-error matcher literal (`--category must be one of`) as a substring of the `InvalidCategoryError` message that `categories.validate_category` produces.
- Add a small native decoder harness with shared JSON fixtures. Every fixture carries a `now` field (or offsets relative to it) that both the Python tests and the harness inject, so freshness cases cannot rot. Execute valid, malformed, stale, foreign-owner, boundary, non-finite, aggregate-mismatch, and non-ASCII 239/240/241-scalar cases instead of relying only on source-reading tests.
- Introduce one `SystemOps` protocol (process spawn, `kill`, file read/write/delete, clock) injected through `RecorderModel.init` with a production default, and route the existing `Process()` calls (owned Start, Stop, Flush), `kill()`, `FileManager`, and `Timer` call sites through it. Add a native `RecorderModel` harness over that seam; `native/test_recorder_model.sh` compiles `Sources/*.swift` without the `@main` file `OnoatsMenuBarApp.swift`, moving any symbol the model needs out of that file. Exercise directory changes, PID reuse, old-run exit, next-run adoption, owned termination, external `onoats stop --data-dir`, `onoats flush --data-dir`, pre-PID permission detail including a PID mismatch, and drain-state convergence.
- Compile with `make -C native build/Onoats` after each model change.

### Phase 4: Native Resource Pipeline and Stateful Menu Icon (manual native phase; not `/conduct`-driven)

**Goal:** The brand assets are copied and signed predictably, while the menu icon remains a valid macOS template image with distinct recorder states.

- Recreate the staging `Onoats.app` bundle before assembly so stale resources cannot survive an earlier build.
- Extend `native/Makefile` to create `Contents/Resources` and copy each named resource explicitly before signing.
- Verify the staged Resources directory against an explicit allowlist and fail on missing or unexpected entries. Exercise the failure branch once in this phase: plant an extra file, remove an expected one, confirm the verifier fails each time, and record the result.
- Add `CFBundleIconFile` without changing `CFBundleIdentifier`, executable names, deployment floor, or TCC usage strings.
- Load each state's menu glyph as a template image. Template rendering discards colour, so express idle, recording, warning, busy (starting/stopping), and failed as separate pre-rendered monochrome variants selected by state, legible in light and dark menu bars. Do not rely on runtime colour overlays.
- Use the full brand mark only in the panel header and app icon, never as a detailed colour menu-bar image.
- Add release-metadata/source-reading assertions for resource names, bundle-copy steps, template handling, and Info.plist icon metadata.
- Verify the assembled bundle contains every resource before codesigning and that `codesign --verify --deep --strict` succeeds after signing.
- If an installed app exists, validate its signature and designated requirement before treating it as a comparison or rollback baseline. A missing or invalid baseline does not block a clean install after staged verification and must never be used for rollback.
- Add two targets to `native/Makefile` here, so the review gauntlet covers them, and assert their presence and semantics in `tests/test_release_meta.py`. `install-staged` has no `sign` or `build` prerequisite: it archives the currently installed app with `ditto -c -k --keepParent` to `~/Library/Application Support/Onoats/app-backups/Onoats-<timestamp>.zip`, never a second launchable `.app` that would share the bundle identifier and could be picked by LaunchServices or `SMAppService.mainApp`, then copies the staged app. `rollback` unpacks a preflight-approved archive with `ditto -x -k` and re-verifies signature and designated requirement. `install` and `setup` stay unchanged.

### Phase 5: Window-Style Recording Panel and In-Panel Settings (manual native phase; not `/conduct`-driven)

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
- Settings → Diagnostics: Open Log, Show Logs Folder, CLI path/status, active data directory, and next-recording directory. `Open Log` opens the newest `onoats-bot-<startEpoch>.log` (the app-captured log of the most recent app-owned run, labelled with its start time) or falls back to the folder when absent.
- Warning and failure surfaces of app-owned sessions and failed app-owned starts include an `Open Log` action. An external session's warning says `Log not captured (started outside the menu bar)` and offers no `Open Log`. Do not tail or render log contents inside the popover.
- Change `RecorderModel` to write one `onoats-bot-<startEpoch>.log` per app-owned run, replacing the single appended `onoats-bot.log`. Keep the newest 10 and route app-owned Flush and Stop output into the active run's file. External Stop and Flush output is not written to a run log; their failures surface through the existing warning and failure surfaces.
- When an app-owned Start fails with the CLI's category-membership error (`--category must be one of`), the failure surface says "Seminar needs 'seminars' in config.toml [categories]" and offers `Open Config` next to `Open Log`. The CLI stays the only authority on whether the category is configured; Swift adds no TOML parser. Swift detects the error by a bounded read of the tail of the run log it just wrote, matched against the pinned literal, only on a non-zero exit when no recorder PID was ever published. Python's category handling and status files do not change.
- Render a stale Me/Them branch as a visible greyed `No signal` row so panel height stays stable.
- The main panel shows the existing Python-produced, redacted running `sttLabel`. While stopped, settings show only the configured service; advanced endpoint editing and resolution remain config/runtime work.
- Treat advanced STT endpoint editing as config-file work in this release; do not add a partial editor that can leave `ws_socket`, `ws_uri`, `ws_host`, and `ws_port` in conflict.
- Keep “changes apply on next Start” next to controls that write recorder configuration.
- Spike first on macOS 14.4 and record the result: in the `.window`-style panel, Escape dismisses it (fallback `.onExitCommand` or a key monitor); Tab and Shift-Tab traverse the controls (fallback explicit `.focused` ordering; this needs System Settings → Keyboard → Keyboard navigation on, a smoke-matrix precondition); Space and Return activate the focused control (`.keyboardShortcut(.defaultAction)`); and a `MenuBarExtra` label shows the per-state template images. Narrow the acceptance wording to whatever the spike shows is supported.
- Give every icon-only control an accessibility label and tooltip. Define a deterministic focus order and keyboard activation for Start/Stop, Seminar, settings/back, and Flush.
- Render long warnings in a bounded, wrapping region without changing the delimiter grammar pinned by Python tests.

### Phase 6: Documentation, Historic Decision Corrections, and Pre-Gauntlet Verification

**Impl files:** `README.md, native/README.md, AGENTS.md, docs/dev_plans/20260609-feature-milestone-b-macos-capture-menubar.md, docs/dev_plans/20260925-feature-menubar-seminar-toggle.md`
**Test files:** `tests/test_native_contract_parity.py, tests/test_status_file.py, tests/test_release_meta.py`
**Test command:** `uv run pytest tests/test_native_contract_parity.py tests/test_status_file.py tests/test_release_meta.py -q`
**Validation cmd:** `python3 scripts/check_review_markers.py && make -C native build`
**Goal:** Documentation describes the final control hierarchy and telemetry contract without rewriting historical implementation logs or weakening reviewed-plan markers.

- Update README/native documentation for the window panel, common Start/Stop/Seminar flow, settings/diagnostics location, telemetry meanings, privacy default, active/next directory disclosure, log actions, and logo asset ownership.
- Grep `docs/dev_plans/` for menu-bar, Seminar, status, telemetry, and branding references.
- Append concise dated corrections to historic plans whose recorded UI decisions are materially superseded, only below the plan's review marker (under `## Findings`; the 20260609 plan keeps its marker below `## Final Results`), never beside the superseded decision above it. No marker hash changes, and `scripts/check_review_markers.py` must still pass.
- Register `ui_telemetry.py` in AGENTS.md's Leaf modules list and note the telemetry invariants (display-only, fail-open, one publisher task, levels rejected not clamped).
- Run `uv run ruff format` and `uv run ruff check` before the full suite.
- Run `uv run pytest` in full.
- Run `python3 scripts/check_review_markers.py`.
- Build the unsigned compile-smoke binaries with `make -C native build`.
- Before any commit, inspect only explicitly staged paths for secrets, credentials, private transcripts, PII, and unintended binary metadata.
- This phase ends before any signing. `Review Gates: full` then auto-chains the review gauntlet once over the finished branch. The Post-Gauntlet Release Steps start only after the gauntlet converges, so no accepted fix can land after bytes have been signed, smoked, or installed.
- Operator workflow: Phases 1, 3, 4, and 5 are done by hand. Tick each `[x]` in Progress when finished so `/conduct` (one phase per `--resume`) skips it and walks only Phases 2 and 6, in document order. The gauntlet fires after Phase 6, the last parsed phase.

### Post-Gauntlet Release Steps (manual; not a `/conduct` phase)

**Goal:** The bytes that pass smoke are exactly the bytes that are installed, and they are the post-gauntlet bytes. A failed install can only roll back to a verified prior baseline. These steps change no code; any fix goes back through Phase 6 and the gauntlet, then re-smokes.

- After the gauntlet converges, run Ruff format/check, review-marker validation, the full pytest suite, and `make -C native build` once more on the post-gauntlet tree before signing, then focused tests for any file the gauntlet touched.
- Assemble and sign a staged `Onoats.app`. Verify the resource allowlist, signature, designated requirement, and the app tree digest: sha256 over the sorted per-file sha256 list of the bundle, plus `make -C native cdhash`. Record the CLI identity: `git rev-parse HEAD` with an empty `git status --porcelain`, plus `command -v onoats` resolving to the `uv tool install --editable` shim. Record the existing `cliPath` UserDefaults value (the staged app shares the installed app's bundle identifier and domain), point the staged app at the CLI with an override, run the manual smoke matrix, then restore the recorded value before install smoke.
- Hash and byte-copy claims apply to `Onoats.app` only. The CLI is `uv tool install --editable` of the working tree and has no hashable bytes of its own. Any commit or edit between staged smoke and install forces a re-smoke.
- Preflight any installed app and CLI before treating them as a rollback baseline: require both, executable/version agreement, a valid app signature, and the expected designated requirement. A missing or invalid baseline is a clean-install case, not a rollback source.
- Install the exact staged app bytes that passed smoke using `install-staged`, and verify the post-copy app tree digest. If post-install smoke fails, run `rollback` for the app, then `git switch --detach <recorded prior SHA>` and `make install-cli` with the same EXTRAS, but only when the baseline passed preflight. Because the editable CLI is the working tree, rollback leaves it detached at the prior SHA by design: report this and print `git switch <recorded branch>`, and do not switch back automatically. For a clean install, report the failure and do not claim rollback.
- Run the post-install smoke only after the staged bundle passes. Installation is not the first acceptance test.

## Technical Specifications

### Files to Modify

- `native/onoats-menubar/Sources/OnoatsMenuBarApp.swift` — window-style scene, main-panel state, stateful template icon.
- `native/onoats-menubar/Sources/RecorderModel.swift` — `SystemOps` seam, telemetry state, authoritative active/next directories for launch/read/signal/exit, pre-PID warning detail, per-run log files and log/folder actions, category-error detection, validated refresh integration, retained lifecycle actions.
- `native/onoats-menubar/Sources/ConfigStore.swift` — only if settings navigation needs an existing-key read/write helper; do not broaden it into a full TOML parser.
- `native/onoats-menubar/Info.plist` — app icon metadata only.
- `native/Makefile` — explicit resource assembly and verification inputs (Phase 4), plus new `install-staged` (no `sign`/`build` prerequisite; archives the installed app to a zip under `~/Library/Application Support/Onoats/app-backups/` before copying the staged app) and `rollback` targets (Phase 4). `install` and `setup` stay unchanged.
- `src/onoats/transports/socket_audio.py` — optional source-labelled audio-observation callback after frame validation.
- `src/onoats/dual.py` — telemetry state holder, publisher task created after running status and cancelled before stopped status, branch wiring, STT state wiring, owner-checked cleanup.
- `src/onoats/processors/transcript_buffer.py` — optional finalized-utterance observation hook.
- `src/onoats/runtime.py` — backend-neutral STT state publication around service creation/preflight, and the one caller-minted run epoch: `_write_pid_file` takes `start_epoch`, `_write_status_running` forwards `start_time`, both return success.
- `src/onoats/stt/websocket_stt_service.py` — keyword-only `on_ready`, `on_recovering`, and `on_failed` callbacks without changing reconnect semantics.
- `tests/test_socket_audio_transport.py` — callback isolation, branch identity, invalid PCM and backpressure regression coverage.
- `tests/test_dual_socket_source.py` — Me/Them telemetry routing without audio mixing; PortAudio and `bot-single` build no publisher.
- `tests/test_websocket_stt_reconnect.py` — confirmed health, recovery, failure, and redaction transitions.
- `tests/test_speaker_labels.py` — transcript preview source/speaker rendering parity where applicable.
- `tests/test_native_contract_parity.py` — separate telemetry-schema and Swift-decoder parity, including the Seminar literal `seminars` across Swift `seminarCategory`, the `--category` argv, and the telemetry `category` field, and the Swift `--category must be one of` matcher against `validate_category`'s message.
- `tests/test_status_file.py` — lifecycle status remains v2 and warning rendering remains intact.
- `tests/test_socket_supervisor.py` — shared run epoch, identity publication order, shutdown cleanup, and paired status/PID evidence.
- `tests/test_release_meta.py` — resource, Info.plist, and `install-staged`/`rollback` Makefile contract.
- `README.md`, `native/README.md` — user and maintainer documentation.
- `AGENTS.md` — Leaf modules entry for `ui_telemetry.py` and the telemetry invariants.
- `docs/dev_plans/20260609-feature-milestone-b-macos-capture-menubar.md` — dated correction only if its menu-presentation decision is stale.
- `docs/dev_plans/20260925-feature-menubar-seminar-toggle.md` — dated correction preserving Seminar semantics while relocating the control.

### New Files to Create

- `src/onoats/ui_telemetry.py` — stdlib-only leaf module: ephemeral schema, bounded publisher, atomic writer, tolerant reader, and owner-checked cleanup.
- `tests/test_ui_telemetry.py` — contract, freshness, throttling, ownership, atomicity, permissions, failure, and cleanup tests.
- `native/onoats-menubar/Sources/UiTelemetry.swift` — strict Swift decoding and freshness/ownership validation.
- `native/onoats-menubar/Sources/RecordingPanelView.swift` — main recording-control panel.
- `native/onoats-menubar/Sources/SettingsPanelView.swift` — in-panel settings destination.
- `native/onoats-menubar/Tests/UiTelemetryDecoderHarness.swift` — executable strict-decoder harness for shared JSON fixtures.
- `native/onoats-menubar/Tests/RecorderModelHarness.swift` — executable model harness for directory authority, lifecycle routing, and pre-PID warning detail.
- `tests/fixtures/ui_telemetry/` — shared valid, malformed, stale, foreign-owner, boundary, and non-finite JSON vectors, each with a `now` field, plus the boundary verdict table.
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
- Schema: independent integer `1`; it does not share `STATUS_SCHEMA_VERSION`.
- Ownership tuple: recorder PID plus one caller-minted run epoch shared byte-for-byte with PID line 4 and lifecycle `start_time`.
- Freshness: maximum 4 writes per second, heartbeat at least every 2 seconds, and snapshot expiry after 5 seconds. Snapshot acceptance checks only schema, ownership (PID plus start epoch), snapshot freshness, numeric ranges, optional-string bounds, and aggregate STT consistency. Each activity branch carries its own observation timestamp and is judged per branch after acceptance: it expires after 1 second without a new frame and renders as a visible greyed `No signal` row, without dropping the other branch or STT state. An advancing observation timestamp counts as a changed snapshot; a branch with continuing frames refreshes its observation time at least every 500 ms inside the 4 Hz cap. Swift also requires the authoritative PID record to be live.
- Activity: optional normalized peaks plus observation timestamps for `me` and `them`. Missing or branch-stale means unknown (`No signal`), not zero and not healthy; a snapshot heartbeat never refreshes branch age.
- STT state: mic/system each use `starting | ready | recovering | failed | unknown`; aggregate precedence is `failed > recovering > starting > unknown > ready`, and ready requires both branches. Swift recomputes and rejects disagreement with the published aggregate.
- STT state definitions (per branch instance): `starting` = before that instance's first successful handshake. `ready` = that instance's own latest handshake succeeded; `start()` already connects eagerly, so no audio is needed, and mic and system each confirm from their own connection, never from the shared preflight cache. `recovering` = a reconnect or kickstart is in flight until the next handshake succeeds. `failed` = backoff exhausted, or an auth or protocol error; it is not terminal: the next connect attempt moves it to `recovering` and a successful handshake to `ready`, and the state-pair tests pin `failed → recovering → ready`. `unknown` = non-websocket backends (`whisper`, `deepgram`), permanently, shown as neutral grey `status not reported (<service>)` and never green. The confirmed `transcript.*` signal stays the kickstart-cooldown concept and is not a UI readiness gate.
- Transcript preview: optional text capped at 240 Unicode code points plus canonical source and render-time speaker label. It never replaces the durable JSONL entry. It is the user's own transcript text, already in the session JSONL; the default-off opt-in is its only privacy control and no extra redaction applies.
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
- **Decision (grilled):** Offer Open Log plus Show Logs Folder. The canonical log is the app-captured stdout/stderr file of the most recent app-owned run: one `onoats-bot-<startEpoch>.log` per run, newest 10 kept. Open Log appears on warning and failure surfaces only for app-owned sessions and failed app-owned starts, with folder fallback; an external session's warning says `Log not captured (started outside the menu bar)`. Settings → Diagnostics keeps both Open Log and Show Logs Folder. The popover never tails logs. The absent-file smoke case deletes the log while the app is idle.
- **Decision (grilled):** Treat the app-owned Seminar selection as badge authority even if telemetry disappears. External sessions require fresh same-owner category telemetry and otherwise display `Category unavailable`.
- **Decision (grilled):** Show a pre-PID permission-wait warning only for the matching retained app-owned supervisor. It decorates `.starting` but never establishes recorder liveness.
- **Decision (grilled):** Carry per-branch audio-observation timestamps. Judge branch age per branch after snapshot acceptance, so one stale branch never drops the other branch or STT state. A stale branch renders as a visible greyed `No signal` row, not a hidden row, so panel height stays stable. A branch with continuing frames publishes an observation-time refresh at least every 500 ms inside the 4 Hz cap; heartbeats never advance branch age. The five-second snapshot expiry continues to govern the overall telemetry record. Producers reject, never clamp, out-of-range levels.
- **Decision (grilled):** Permit a clean install after staged app verification when no trustworthy prior app and CLI baseline exists. Use rollback only when the prior baseline passes preflight.
- **Decision (grilled):** Split the old Phase 6. Phase 6 is conduct-driven documentation, historic-plan corrections, lint, tests, review-marker validation, and the unsigned native build. `Review Gates: full` auto-chains the review gauntlet once after it. The Post-Gauntlet Release Steps are an unnumbered manual section, not a `### Phase N:` heading, so `/conduct` and the gauntlet auto-chain treat Phase 6 as the last phase. They cover staged signing, hashed smoke, install of the exact staged bytes, and rollback preflight, all on post-gauntlet bytes. All code they need (`install-staged`, `rollback`) lands earlier in Phase 4, so the gauntlet reviews it and no accepted fix can invalidate signed or installed bytes.
- **Decision (grilled):** The CLI is `uv tool install --editable` of the working tree and has no hashable bytes. Its verified identity is `git rev-parse HEAD` with an empty `git status --porcelain`, plus `command -v onoats` resolving to the editable-install shim, recorded beside the `Onoats.app` tree digest. No `--version` flag is added. Hash, byte-copy, and post-copy-hash claims apply to `Onoats.app` only. Any commit or edit between staged smoke and install forces a re-smoke. CLI rollback means `git switch --detach` to the recorded prior SHA and running `make install-cli` with the same EXTRAS; the tree stays detached by design and the recorded branch is reported, not auto-restored.
- **Decision (grilled):** Back up the installed app as a zip archive outside LaunchServices-scanned folders (`~/Library/Application Support/Onoats/app-backups/`), never as a second launchable `.app` sharing the bundle identifier. `rollback` unpacks it with `ditto -x -k` and re-verifies signature and designated requirement.
- **Decision (grilled):** STT state definitions live once in the UI Telemetry Contract. `ready` is this instance's own latest successful handshake, never the shared preflight cache; `unknown` is permanent for non-websocket backends and renders neutral grey `status not reported (<service>)`, never green. The confirmed `transcript.*` signal stays the kickstart-cooldown concept and is not a UI readiness gate. STT callbacks enter through `_create_stt_service` keyword arguments like `on_recovery`.
- **Decision (grilled):** The Seminar literal is `seminars`, pinned by a parity test across Swift `seminarCategory`, the `--category` argv, and the telemetry `category` field. The CLI is the only authority on whether it is configured; Swift adds no TOML parser. A failed app-owned Start on the category-membership error shows "Seminar needs 'seminars' in config.toml [categories]" with an `Open Config` action next to `Open Log`.
- **Decision (grilled):** Swift recomputes aggregate STT state from the two branches and rejects telemetry when the published aggregate disagrees.
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
| Audio activity | Socket input transport | UI telemetry publisher | Validated PCM, fixed branch source, no await/backpressure mutation, normalized finite peak plus producer observation time; 1 s branch expiry |
| Transcript preview | `TranscriptBuffer` | UI telemetry publisher | Final non-empty utterance, bounded copy only, durable JSONL unchanged |
| STT health | Two runtime STT instances | UI telemetry publisher | Branch-labelled states, fixed aggregate precedence, both-ready green rule, Swift aggregate consistency check, redacted details |
| UI snapshot | UI telemetry publisher | Swift telemetry reader | Atomic schema-v1 JSON, 4 Hz cap, 2 s heartbeat, 5 s expiry, shared PID/epoch, mode 0600 |
| Lifecycle state | Status/PID files | `RecorderModel` | PID identity remains authoritative; telemetry cannot change recorder state; matching pre-PID detail may decorate app-owned starting only |
| Recorder settings | Swift `ConfigStore` | Python config loader and CLI actions | Existing narrow TOML edits preserve unrelated content; one Start-time directory snapshot governs launch/read/signal/exit while changes apply next Start |
| Diagnostics | Per-run app-captured log (`onoats-bot-<startEpoch>.log`) of the most recent app-owned run | Panel and settings | Open file when present, otherwise reveal folder; Open Log on warning surfaces only for app-owned sessions; never tail content in the popover |
| Native assets | Canonical SVG/exports | Makefile and Swift panel | Explicit filenames, template semantics, resource copy before signing |
| Install pairing | Verified staged app hash and recorded CLI git identity (SHA, clean tree, `command -v onoats`) | Native install workflow (`install-staged`, `rollback`) | App bytes hashed and unchanged after smoke; no commit or edit between smoke and install without a re-smoke; rollback only to a preflight-approved baseline |
| Historic plans | Current plan | Prior decision records | Dated correction and cross-link only when a prior decision is materially superseded |

## Architecture & Call Flow

```mermaid
graph LR
    U["User"] -->|"Start, Stop, Seminar, settings"| M["Onoats menu-bar app"]
    M -->|"owned Start and Stop"| P["Python CLI and supervisor"]
    M -->|"identity-checked external Stop and Flush"| P
    M <-->|"selected settings"| C["config.toml"]
    P -->|"spawn and supervise"| N["Native capturer"]
    N -->|"framed Me and Them PCM"| R["Recorder pipelines"]
    R -->|"lifecycle and warnings"| S["Status file"]
    R -->|"throttled levels, STT health, transcript preview via one publisher task started after running status and cancelled before stopped status"| T["UI telemetry snapshot"]
    R -->|"durable utterances"| J["Session JSONL"]
    M -->|"poll authoritative state"| S
    M -->|"poll display-only live data"| T
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
    M->>P: SIGTERM owned process or identity-checked CLI Stop
    P->>R: Drain and finalise
    R->>F: Persist final status and invalidate live telemetry
    P-->>M: Exit
```

| Step | Trigger | Enters context | Cleared/persisted | Turn boundary |
|---|---|---|---|---|
| 1 | App launches or panel opens | PID identity, status, config, telemetry freshness | Model persists for app lifetime | Panel renders |
| 2 | User starts | Seminar choice and shared configuration | Session/run identity persists until exit | Recorder becomes live |
| 3 | Capture proceeds | Separate Me/Them audio, STT state, utterances | JSONL persists; UI telemetry is throttled and session-scoped | Each atomic snapshot |
| 4 | User stops | Graceful-stop request and drain state | Transcript persists; the publisher task is cancelled and joined before the stopped status, then owner-checked cleanup invalidates live telemetry; final status remains | Supervisor exits |

## Testing Notes

### Automated Test Approach

- Unit-test UI telemetry schema, numeric validation, truncation, permissions, atomic replacement, throttling, freshness, owner-checking, cleanup, and fail-open writes.
- Mutation-test ownership/freshness by substituting stale PID, same PID with a different start epoch, stale update time, schema drift, malformed JSON, and non-finite or out-of-range levels (`-0.0`, `0.0`, `1.0`, `1.0000001`, `inf`, `nan`; rejected, never clamped). Assert the one minted epoch is identical in PID, status, and telemetry. Assert a branch observation older than 1 second yields a per-branch `No signal` while the snapshot, the other branch, and STT state stay accepted, and that a branch emitting only rejected values for more than 1 second does too. Read the boundary verdict table from `tests/fixtures/ui_telemetry/` in both Python and Swift.
- Fake-clock changed writes, unchanged 2-second heartbeats, 4 Hz suppression, 1-second branch expiry, 5-second snapshot expiry, repeated-failure warning suppression, recovery logging, and fail-open cleanup. Assert heartbeats do not advance branch observation times, and that a constant level with continuous frames writes at least every 500 ms.
- Exercise the menu-bar source matrix: Me/Them, socket telemetry present/absent, unsupported external capture, transcript preview enabled/disabled, and owned/external session. PortAudio and `bot-single` remain outside new telemetry scope: assert `portaudio` builds no publisher and `run_onoats` never constructs one.
- Assert level callbacks preserve one socket to one branch and do not alter staging, byte caps, drop policy, or fatal error behaviour; a raising observer neither stops the reader nor changes staging.
- Assert every mic/system STT state pair and callback ordering against the fixed aggregate precedence, including startup, recovery, confirmation, auth/protocol failure, unknown backend support, and every aggregate/branch mismatch.
- Assert no telemetry file exists before `_write_status_running` returns and a raising `write_running` leaves none; assert PID line 4, status `start_time`, and the telemetry epoch are identical (`tests/test_socket_supervisor.py`).
- Assert URI userinfo/query secrets cannot reach the telemetry file or Swift display.
- Source-read Swift contracts and execute the native decoder harness against shared JSON vectors for telemetry fields, allowed states, exact bounds, freshness, ownership, aggregate mismatches, and rejection cases. Include 239/240/241-scalar strings using combining marks, astral characters, and ZWJ sequences.
- Execute the native model harness across directory changes, PID reuse, old-run exit, next-run adoption, owned termination, external Stop, Flush, pre-PID permission detail, and drain-state convergence.
- Keep existing status, CLI, socket, supervisor, diarization, release metadata, and parity suites green.

### Manual Native Smoke Matrix

- Stopped: panel opens at a stable size; Seminar is editable; Start is dominant; settings are reachable and return preserves state.
- Starting/running/stopping/failed: every transition has a truthful label and the main action cannot double-fire.
- Owned and external sessions: Stop follows the correct path; Flush still works; draining never renders as stopped early.
- Seminar: selected before Start, shown read-only during an app-owned recording even if telemetry disappears, passed as `--category seminars`, and reset after exit; external sessions without fresh category telemetry show `Category unavailable`.
- Me/Them: speak/play each side independently; only its meter responds; silence decays; a branch shows a greyed `No signal` row after one second without frames while the other branch and STT state stay live; stale snapshots become unknown after five seconds.
- STT: with the websocket backend, confirmed ready, recovering, failed, and starting render distinct states without false green. With `whisper` or `deepgram`, the row stays neutral grey `status not reported (<service>)` and never green.
- Seminar config: remove `seminars` from `[categories]`, select Seminar, and Start; the failure surface says "Seminar needs 'seminars' in config.toml [categories]" with `Open Config` and `Open Log`.
- Transcript preview: off by default; enabling shows one bounded latest line; disabling removes it immediately; stale prior-run text never appears.
- Warnings: all-zero input, STT recovery, login-item hint, schema drift, Flush/Stop failure, and failed-session detail wrap without panel expansion.
- Settings: microphone, STT service, data directory, config open, CLI diagnostics, and Quit/Stop & Quit retain current behaviour.
- Directory transition: change the configured directory during a run; confirm the panel keeps launching/reading/signalling/exiting through the active directory, shows active/next paths, and adopts the new directory only after the next Start.
- Diagnostics: run two app-owned sessions, then Open Log opens the second run's file labelled with its start time; Show Logs Folder reveals the folder; with the app idle and the log file deleted, Open Log falls back to the folder; an external-session warning shows `Log not captured (started outside the menu bar)` and no Open Log; no transcript/log tail appears in-panel.
- Dismissal: Escape and click-away close the panel; reopen and settings/back preserve correct state while recording continues.
- Accessibility (precondition: Keyboard navigation on): Tab/Shift-Tab focus order, Space/Return activation, VoiceOver labels, tooltip text, contrast, Reduce Motion, and light/dark menu bars.
- Branding: header and app icon render sharply; custom menu icon remains legible at supported scales and shows each state.
- Bundle (post-gauntlet release steps): staged signed app passes resource allowlist and codesign checks; the app tree digest matches the installed bytes and the CLI git identity (SHA, clean tree, `command -v onoats`) matches the one recorded at smoke; a valid baseline preserves byte-identical designated requirements and grants, while a missing baseline follows the documented clean-install path.

### Test Results

- [ ] Focused telemetry and parity tests pass.
- [ ] `uv run ruff format` and `uv run ruff check` pass.
- [ ] Full `uv run pytest` passes.
- [ ] `make -C native build` passes.
- [ ] `native/test_ui_telemetry.sh` passes on macOS.
- [ ] `native/test_recorder_model.sh` passes on macOS.
- [ ] Resource-allowlist negative checks (extra file, missing file) fail the verifier.
- [ ] Signed bundle verification passes.
- [ ] Designated-requirement bytes match the verified installed baseline when one exists; clean-install evidence is recorded otherwise.
- [ ] Manual native smoke matrix passes.

## Acceptance Criteria

- The menu-bar item opens a window-style panel whose dominant action is Start when stopped and Stop when running.
- Seminar is one interaction away before Start, remains next-session-only, and displays read-only during an app-owned locked run even without telemetry. External sessions require fresh matching category telemetry or show `Category unavailable`.
- Every existing control remains available either on the main panel or in its settings destination.
- Owned Stop, external Stop, Flush, Quit, liveness, warnings, and drain semantics remain unchanged and covered.
- Me/Them indicators use separate fresh CoreAudio/socket values with producer observation times; a branch older than one second renders a visible greyed `No signal` row, and any stale, missing, malformed, or unsupported external telemetry renders unavailable.
- The primary combined STT row never shows green unless both menu-bar branches confirm ready; non-ready states identify unhealthy branches, and Swift rejects a published aggregate that disagrees with its branches.
- Transcript preview is opt-in, bounded, and never leaks a previous run.
- Telemetry writes are versioned, atomic, mode 0600, capped at 4 Hz, heartbeated every 2 seconds, expired after 5 seconds, owner-checked, asynchronously coalesced, and fail open.
- PID, lifecycle status, and telemetry carry one identical caller-minted run epoch; telemetry begins only after authoritative identity publication.
- Changing the data directory during a run leaves one Start-time path authoritative for launch, reads, external Stop, Flush, and exit handling; the panel shows active/next paths and applies the change on the next recording.
- A matching pre-PID permission-wait status may decorate only the app-owned starting state and never creates recorder liveness.
- Warning/failure surfaces of app-owned sessions and failed app-owned starts can open the newest per-run app-captured log; external-session warnings say the log was not captured; Settings → Diagnostics can open the file or reveal its folder without embedding log content.
- `STATUS_SCHEMA_VERSION` remains 2 and all existing Swift/Python parity tests pass.
- A user-approved canonical SVG brand, app icon, and monochrome template menu icon are committed with reuse guidance.
- The assembled clean staged app contains only allowlisted resources and passes signature verification. Staged smoke and installation use the exact same hashed app bytes and the same recorded CLI git identity (SHA, clean tree, `command -v onoats`); rollback uses only a preflight-verified prior baseline, while an absent baseline follows the clean-install path.
- README/native docs describe the final UI and telemetry meanings; only materially superseded historic decisions receive dated corrections.
- After accepted review fixes, focused tests, full pytest, Ruff format/check, review-marker validation, native compile, signed-app verification, and manual smoke all pass on the final bytes.
- Pre-merge documentation, code, security, and full review gates (the auto-chained gauntlet after Phase 6) report no unresolved findings before the Post-Gauntlet Release Steps sign anything.
- The STT row shows `ready` only for the websocket backend with both branches' own handshakes confirmed; `whisper` and `deepgram` sessions show neutral grey `status not reported`.
- A failed app-owned Start on a missing `seminars` category explains the cause and offers `Open Config`.

<!-- reviewed: 2026-10-29 @ 2e4c3d9c1cea1649dc43997b5309cbf96018b52c -->

## Progress

- [ ] Phase 1: Brand Direction and Canonical Assets
- [ ] Phase 2: Ephemeral UI Telemetry Contract and Runtime Producer
- [ ] Phase 3: Swift Telemetry Consumer
- [ ] Phase 4: Native Resource Pipeline and Stateful Menu Icon
- [ ] Phase 5: Window-Style Recording Panel and In-Panel Settings
- [ ] Phase 6: Documentation, Historic Decision Corrections, and Pre-Gauntlet Verification

## Findings

- (append findings here as work proceeds)

## Issues & Solutions

- (append issues and resolutions here as work proceeds)

## Final Results

### Summary

- (fill when complete)

### Outcomes

- (fill when complete)

### Learnings

- (fill when complete)

### Follow-up Work

- Integrate the approved canonical brand assets into the onoats.app website after its repository/path is identified.
