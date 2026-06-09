# Milestone B — Native macOS System-Audio Capture + Menu-Bar Launcher

**Status**: Not Started
**Component**: macos, transport, recorder, packaging
**Assignee**: Varun Singh
**Priority**: Medium
**Branch**: `feat/socket-audio-transport-milestone-b`
**Created**: 2026-06-09
**Objective**: Replace the BlackHole-based macOS system-audio path with a native
Swift capturer that feeds the Unix-socket audio transport shipped in Milestone A,
and add a SwiftUI menu-bar launcher backed by a shared status file. Native phases
build from source and run on the author's own machine; public (notarized)
distribution is explicitly out of scope.

## Context

Milestone A (Phases 1–3 of
`docs/dev_plans/20260607-feature-menubar-coreaudio-socket-transport.md`, merged in
PR #4) shipped the portable, CI-testable core: a `UnixSocketAudioInputTransport`
with length-prefixed PCM16-LE framing and a JSON handshake header, an
`AUDIO_SOURCE=socket` wiring path, and a CLI supervisor that spawns a capturer
binary (`ONOATS_CAPTURER_BIN`) into a per-generation private socket dir. The wire
contract is documented in `docs/audio-socket-contract.md`.

Milestone B is the **native macOS half** that was deliberately deferred: it can't
be verified in headless/Python CI, so it was split into its own PR and plan. This
plan covers Phases 4–6:

- **Phase 4** — the Swift capturer that actually produces the two socket streams.
- **Phase 5** — a SwiftUI menu-bar launcher plus the Python-side status file it
  reads.
- **Phase 6** — retiring BlackHole from the default macOS story + docs.

**Open Question 2 (binary distribution) is RESOLVED** for this plan's scope (see
the parent plan's Open Questions §2, resolved 2026-06-09): build the native
artifacts **from source locally** and sign with a **stable self-signed
code-signing certificate** (login-keychain "Code Signing" cert, *not* ad-hoc) so
the Screen Recording / audio-capture TCC grant survives rebuilds — $0, free Apple
ID, **no notarization**. Public distribution (Developer ID notarize + Homebrew
cask) is deferred until a paid ($99/yr) membership and is **out of scope here**.

## Requirements

- **No BlackHole on the happy path.** A fresh macOS user records me+them system
  audio with no virtual-audio driver installed, only the native capturer + granted
  Screen Recording permission.
- **Same queue files, same tags.** The socket path must produce `me`/`them` queue
  files identical in tagging to the existing PortAudio path (the keystone never-mix
  invariant: mic ⇒ only `me`, system ⇒ only `them`). An **A/B parity check** (same
  source via PortAudio and socket, diff the resulting queue files) is the
  acceptance evidence.
- **Stable TCC identity** *(assumption — verify first via the Phase 4 Pre-req
  spike).* The capturer/menu-bar bundle is signed with a stable self-signed
  identity *on the premise that* this obtains and persists the Screen Recording
  grant across rebuilds without re-prompt. This is an unverified Apple-platform
  behavior, not an in-repo fact — the Pre-req spike must confirm it before any
  Swift is written. Ad-hoc signing (`codesign -s -`) is explicitly disallowed for
  the shipped build target. The one-time self-signed-cert creation is **step 0** of
  Phase 4, documented in `native/README.md` before the first `make sign`.
- **One native bundle.** The menu-bar `.app` is the single native artifact; the
  capturer is embedded at `Onoats.app/Contents/MacOS/onoats-capturer`. One bundle
  ID ⇒ one TCC identity. The Python wheel **never** bundles native code; it
  discovers the binary via `ONOATS_CAPTURER_BIN` (already wired).
- **Version-safe decoupling.** Because the Python recorder (pip) and the native
  capturer (local build) version independently, the capturer **must** emit the
  handshake version the recorder expects, and the recorder **must** reject a
  mismatch loudly (the guard already exists — see Technical Specifications).
- **Fail loud, leave state consistent.** Denied Screen Recording, no system-audio
  device, or a capturer crash mid-session must fail loud and still rotate a partial
  session — never hang (mirrors Milestone A's supervisor teardown).
- **Status file is single-source-of-truth for liveness display.** The recorder
  writes a JSON status file on start/stop; `onoats status` reads it with the pid
  file kept as a liveness backstop; the menu bar consumes the same file.

## Implementation Checklist

> **⛔ AUTONOMOUS-RUN SCOPE:** Only the Python status-file slice (Phase 5a) is
> `/conduct`-runnable — it has a real `Test command`. Phases 4, 5b (Swift), and 6
> are **native macOS** with no Python test command and **cannot be verified in
> headless CI**; their Test slot is a **manual macOS smoke checklist**. Do **not**
> point `/conduct` at the Swift phases — it would generate unverifiable native
> code. Build those interactively with the self-signed cert + manual smoke test.
>
> **Open Question 1 (system-audio API) RESOLVED: Core Audio process-tap, macOS
> 14.4+.** The **Phase 4 Pre-req spike** (machine `sw_vers ≥ 14.4` + the
> self-signed-cert/TCC verification) must pass before any Phase 4 Swift is written.

### Phase 4 Pre-req: TCC/self-signed-cert spike + environment gate  *(do FIRST)*

**This is a verification spike, not coding — it de-risks the whole $0 path before
any Swift is written.** The plan's premise that a *self-signed* cert obtains and
*persists* a Screen Recording grant across rebuilds is an **unverified Apple-platform
assumption** (see Open Questions / Assumptions), not an in-repo fact. Resolve it first:

1. Confirm the target machine meets the OS floor: `sw_vers -productVersion` **≥ 14.4**
   (Core Audio process-tap requirement). If below, Phase 4 is blocked — fall back to
   ScreenCaptureKit (13+) or BlackHole.
2. Create a stable self-signed **"Code Signing"** certificate in the login keychain
   (Keychain Access → Certificate Assistant). Document the one-time step in
   `native/README.md`. Record its designated requirement: `codesign -dvvv` on a
   signed stub.
3. Sign a minimal stub `.app` with it, grant Screen Recording once, then **rebuild +
   re-sign 3×** and confirm macOS does **not** re-prompt and the grant persists
   (compare `codesign -dvvv` designated requirement before/after — it must be byte-
   stable; bundle ID + identity must not drift across `make app`).
4. **If the grant cannot be obtained or does not persist under self-signed:** STOP —
   the "no notarization / $0" conclusion (Open Question 2) is reopened and needs the
   paid Developer ID path. Do not proceed to Phase 4 coding on a false premise.

### Phase 4: Swift system-audio capturer  *(macOS native; NOT conduct-runnable)*

**Impl files:** `native/onoats-capturer/` (new Swift package/target), build script
`native/Makefile` (`make build` / `make sign` / `make app`), codesign-with-self-signed-cert step
**Test files:** manual macOS smoke checklist (below) — no Python test
**Test command:** _n/a — manual macOS smoke checklist; not in Python CI_

- **System-audio API (Open Question 1 — RESOLVED 2026-06-09): Core Audio
  process-tap** (`AudioHardwareCreateProcessTap` / `CATapDescription`, macOS
  **14.4+**). ScreenCaptureKit (13+) is explicitly deferred — revisit only if a
  13.x floor is later required. **Assumption (not in-repo-verified):** "tap all
  processes via empty process list + `isExclusive`, feeding an aggregate device"
  is the intended recipe — confirm against Apple's "Capturing system audio with
  Core Audio taps" sample during the Phase-4 smoke (step 1).
- Capture system output (Core Audio tap) **and** mic; resample each to **16 kHz
  PCM16 mono LE**.
- **Wire format — emit JSON-object frames, NOT raw PCM** (per
  `docs/audio-socket-contract.md`): after the handshake line, each frame is a
  **4-byte big-endian length prefix** followed by a JSON object
  `{"seq": <int>, "captured_monotonic_ns": <int>, "pcm_b64": "<base64 PCM16 LE>"}`.
  `seq` is a **monotonic per-stream** counter (drives drop/drift detection — OQ4);
  `captured_monotonic_ns` is the capturer's monotonic capture timestamp (drives
  `pts`). PCM payload MUST be a whole number of samples. Reference chunk ~20 ms /
  **640 bytes** (`SHOULD`, keeps VAD/STT cadence identical to PortAudio).
- **Stream routing (keystone — never-mix):** the capturer MUST create **exactly
  two** sockets and write **mic capture only to `--mic-socket`** and **system-tap
  capture only to `--system-socket`** — never the reverse, never both fanned onto
  one socket (`audio-socket-contract.md` "exactly one socket per branch").
- **Socket role:** the capturer is the **server** — it `bind()`+`listen()`s and
  **creates both socket files** (this is what unblocks the supervisor's bounded
  `_wait_for_sockets`). The recorder is the client.
- **Handshake** (one line UTF-8 JSON + `\n`, before any frame):
  `{"rate":16000,"width":2,"channels":1,"v":WIRE_VERSION,"nonce":"<echoed>"}`. The
  capturer MUST **echo the `--nonce`/`ONOATS_CAPTURER_NONCE` value verbatim** in
  `nonce`, or the supervisor path rejects it (`SocketHandshakeError`). If framing
  semantics ever change, bump `WIRE_VERSION` in `socket_audio.py` **and**
  `docs/audio-socket-contract.md` in lockstep (`tests/test_audio_socket_contract_parity.py`
  guards the drift) — the recorder rejects a mismatched `v`.
- **Device-change survival (contract MUST):** the capturer MUST survive a
  default-input-device change mid-session (e.g. AirPods disconnect) and MUST NOT
  exit on a recoverable device change — keep streaming to the same sockets.
- Screen Recording / audio-capture **permission handling** with a clear denial
  message and non-hanging exit (so the supervisor rotates a partial session).
- Sign the build output with the **stable self-signed cert** from the Pre-req spike
  (never ad-hoc).
- **Manual macOS smoke checklist** (the Test slot for this phase):
  1. Capture me+them in a real session; verify two independent STT streams (this
     transitively proves the handshake `v`/nonce were accepted — the transport
     refuses to start otherwise) **and** confirms the Core Audio tap recipe works.
  2. **Asymmetric keystone routing test** — play a known signal to **mic only**
     (silence to system): assert `them` is silent and `me` has the signal; then
     **invert** (signal to system only): assert `me` silent, `them` has it. (A
     symmetric source + tag-parity diff alone can pass under a stream swap — this
     content-routing test is the real keystone evidence.)
  3. Confirm the capturer created **two** socket files, one source per branch.
  4. **A/B check** — record the same source via PortAudio and socket paths; compare
     `me`/`them` **transcription quality** (not just tag parity) to also catch a
     resample bug (declared 16 kHz but feeding 48 kHz passes the handshake).
  5. **Deny Screen Recording** → confirm the 4-part fail-loud observable:
     `ErrorFrame` logged, supervisor exit code ≠ 0, WARNING/ERROR line, partial
     session rotated into `pending/`. No hang.
  6. **Kill the capturer mid-session** → same 4-part observable; supervisor tears
     down the whole process group, partial session rotates.
  7. **Default-device change mid-session** (disconnect AirPods) → capturer keeps
     streaming, `me`/`them` timeline stays continuous, no exit.
  8. **(OQ4)** drive under load and inspect `metadata["socket_seq"]` / drift to
     inform the final backpressure policy.
  9. **(OQ5)** listen for own-voice echo leaking into `them` (note: the A/B tag
     check passes even if echo contaminates content — this listen-test is the only
     content-correctness coverage for echo).

### Phase 5a: Python status file  *(pure Python; conduct-runnable)*

**Impl files:** `src/onoats/runtime.py`, `src/onoats/dual.py` (recorder *writes*
status file on start + stop/rotation), `src/onoats/cli.py` (`onoats status` reads
it; pid file kept as backstop), `src/onoats/status.py` (new — schema + read/write helpers)
**Test files:** `tests/test_status_file.py`
**Test command:** `uv run pytest tests/test_status_file.py -v`

- Define a JSON status-file schema (under the state dir): a **`schema`/`v` integer**
  (so the Swift menu-bar consumer can't silently drift — same independent-versioning
  argument as the audio handshake), recorder pid, start time, audio source, STT
  config label, last-rotation time, running flag.
- **Producer:** `runtime.py`/`dual.py` write the file on startup and on
  shutdown/rotation. This is the load-bearing slice — the schema round-trip test
  alone does not deliver it. **Writes MUST be atomic (temp file + `os.replace`)** so
  a crash mid-write never leaves half-JSON for a reader. **Write ordering:** pid file
  first then status on start; status-stopped then pid removed on stop — so the pid
  backstop is always consistent with what `onoats status` reads.
- **CLI rewire:** `onoats status` (today `cli.py:777-798`, reads pid only) reads
  the status file, **with the pid file as the liveness backstop**. Pin the
  precedence as a truth table over {status `running`?, pid alive?}: pid-alive
  overrides a missing/stopped status (report live); a `running=true` status with a
  **dead** pid reports **stopped** (stale status must never report a dead recorder
  as live). Test all four cells.
- **Tests** (`tests/test_status_file.py`): (a) schema round-trip (write→read→assert);
  (b) **producer test** — drive the start path, assert file exists with
  `running=true`; drive shutdown/rotation, assert `running=false` + last-rotation
  updated (this fills the load-bearing gap the round-trip test does not); (c) the
  4-cell backstop truth table; (d) atomic-write (no half-JSON observable).

### Phase 5b: SwiftUI menu-bar launcher  *(macOS native; NOT conduct-runnable)*

**Impl files:** `native/onoats-menubar/` (SwiftUI `MenuBarExtra`, `LSUIElement`),
embeds the Phase-4 capturer at `Contents/MacOS/onoats-capturer`
**Test files:** manual macOS smoke checklist (below)
**Test command:** _n/a — manual macOS smoke checklist; not in Python CI_

- Menu-bar app: Start / Stop / Flush (shell out to `onoats {bot,flush}` or signal
  the pid), running indicator, **profiles** (device + STT config sets), device
  pickers (reuse `onoats devices`).
- **Consumer:** reads the same status file defined in Phase 5a for its
  running/stopped indicator.
- Package as the single notarization-ready `.app` bundle with the capturer
  embedded; signed with the stable self-signed cert.
- **Manual smoke:** launch from menu bar, start/stop a session, confirm indicator
  tracks the status file, confirm Flush works.

### Phase 6: Retire BlackHole from the default macOS story + docs

> **⛔ GATE: do not start Phase 6 until Phase 4 acceptance (asymmetric keystone
> routing + A/B check) has PASSED on the author's machine and is recorded in
> `## Findings`.** Demoting BlackHole in the docs before the native path is proven
> would point a fresh user at a non-working capture path. Hard precondition.

**Impl files:** `src/onoats/init.py` (drop/soften the BlackHole loopback warning at
`init.py:133`), `src/onoats/processors/dual_silence_detector.py` (update the
BlackHole comment at `:32`), `README.md`, `docs/audio-socket-contract.md`,
`pyproject.toml` `[macos]` extra note (line 28–29)
**Test files:** existing suite (Phase 6 edits `init.py` runtime code, not only docs)
**Test command:** `uv run pytest`

- Make the native capturer the documented default macOS capture path; demote
  BlackHole to a fallback/legacy note (do not remove the code paths that still
  work, just stop recommending it).
- **Document the minimum macOS version (14.4+, from OQ1).** Because the native path
  requires 14.4+, **keep BlackHole as the documented fallback for macOS 13.x–14.3**
  rather than pure legacy — a user below the floor otherwise has no working path.
- Update `[macos]` extra docs to point at the local build (`native/README.md`),
  not BlackHole install.

## Technical Specifications

### Verified codebase facts (from Explore, 2026-06-09)

- **Handshake already versioned.** `socket_audio.py:83-87` defines `WIRE_VERSION = 1`;
  the handshake JSON uses key `"v"` (e.g.
  `{"rate":16000,"width":2,"channels":1,"v":1,"nonce":"..."}`), and
  `parse_handshake()` (`socket_audio.py:167-213`) reads `obj.get("v")` and rejects
  `!= WIRE_VERSION`. **No new protocol-version field is needed** — the drift guard
  exists; the capturer just has to emit the right `v`.
- **Capturer discovery already wired.** `dual.py:308-369` reads
  `os.environ.get("ONOATS_CAPTURER_BIN", "")` and spawns it; it raises if
  `AUDIO_SOURCE=socket` but the var is empty.
- **`onoats status` reads pid only today.** `cli.py:777-797` (`_cmd_status`) reads
  the pid file via `_read_pid(data_dir)` + `_process_alive(pid)`; no status file
  exists yet. Phase 5a adds the status file and keeps the pid path as backstop.
- **`AUDIO_SOURCE` wiring.** `config/__init__.py:141-156` — `audio_source` resolves
  env `AUDIO_SOURCE` > `config.toml [audio].source` > default; the socket-path
  properties are **`mic_socket`** (`:159`) / **`system_socket`** (`:168`) — *not*
  `me_socket_path`/`them_socket_path*` (corrected per codebase-claims lens).
- **No status file written today.** `runtime.py` writes only a pid file
  (`_write_pid_file` ~`:993`, removed ~`:1027`). Phase 5a is net-new.
- **BlackHole references** to retire/soften: `init.py:133` (loopback-device
  warning), `processors/dual_silence_detector.py:32` (routing comment). `pyaudio`
  is imported lazily (`cli.py:750`, `init.py:80`) and is transitive via
  `pipecat-ai`, not a direct dep.
- **Dependencies** (`pyproject.toml`): `requires-python >=3.12`;
  `pipecat-ai[...]>=1.0.0,<2.0.0`; `websockets>=13.0`; `[macos]` extra =
  `mlx-whisper>=0.4.0`, `kokoro-onnx>=0.4.0` (no Swift entry yet; comment at
  line 28–29 reserves space for the CoreAudio bridge); dev: `pytest>=8`,
  `ruff>=0.15,<0.16`.

### Native artifact layout

```
native/
  README.md              # one-time self-signed cert setup + build instructions
  Makefile               # `make build`, `make sign`, `make app`
  onoats-capturer/       # Swift: Core Audio tap + mic → two sockets
  onoats-menubar/        # SwiftUI MenuBarExtra; embeds capturer in the .app
```

- The pip package contains **no** native code. On macOS, `ONOATS_CAPTURER_BIN`
  points at the locally-built `onoats-capturer` (either the standalone build output
  or the one embedded in `Onoats.app/Contents/MacOS/`).
- Signing: a single stable self-signed "Code Signing" certificate in the login
  keychain signs both the capturer and the `.app`. Documented in `native/README.md`.

### Distribution (resolved scope)

- **In scope:** build-from-source + self-signed-cert, local to the author's
  machine. No notary service, no Homebrew.
- **Out of scope (deferred to paid membership):** Developer ID cert, `xcrun
  notarytool` + `stapler`, Homebrew cask / `onoats capturer install` downloader.
  When that lands it is purely additive — the decoupled architecture (pure-Python
  wheel + separately-built native bundle + `v` handshake guard) does not change.

## Testing Notes

- **Python (CI-able):** only Phase 5a — `uv run pytest tests/test_status_file.py`.
  Covers schema round-trip and the dead-pid-beats-stale-status backstop.
- **Native (manual only):** Phases 4 and 5b use the per-phase manual macOS smoke
  checklists above. The A/B parity check (Phase 4 step 3) is the load-bearing
  acceptance evidence for "no BlackHole, same queue files."
- Full existing suite (153 tests as of PR #4) must stay green after Phase 5a /
  Phase 6 Python edits: `uv run pytest`.

## Issues & Solutions

_(to be filled during implementation)_

## Acceptance Criteria

- [ ] **Phase 4 Pre-req spike passed:** `sw_vers ≥ 14.4` confirmed; self-signed
      cert obtains the Screen Recording grant and it survives 3 rebuilds without
      re-prompt (`codesign -dvvv` designated requirement byte-stable). *(If failed,
      Open Question 2 is reopened — paid Developer ID path.)*
- [ ] Native capturer produces me+them socket streams with **no BlackHole
      installed**, emitting **JSON-object frames** (`seq`/`captured_monotonic_ns`/
      `pcm_b64`) per the wire contract, echoing the supervisor nonce.
- [ ] **Keystone never-mix proven by the asymmetric routing test** (mic-only signal
      ⇒ `them` silent + `me` has it; inverted ⇒ vice-versa), not just tag parity.
- [ ] Capturer survives a default-input-device change mid-session without exiting.
- [ ] `onoats status` reads the status file with the pid backstop (4-cell truth
      table); recorder writes the file atomically on start/stop;
      `tests/test_status_file.py` green (incl. producer test); full suite green.
- [ ] Menu-bar `.app` launches, embeds the capturer, tracks the status file,
      Start/Stop/Flush work.
- [ ] BlackHole demoted to fallback in docs (kept for <14.4); `[macos]` extra
      points at the native build; min macOS version documented.
- [ ] Deny-permission and capturer-crash paths show the 4-part fail-loud observable
      (ErrorFrame + rc≠0 + WARNING/ERROR + `pending/` rotation), no hang.

## Review Focus

- **Keystone never-mix invariant** (mic ⇒ `me`, system ⇒ `them`) — the A/B parity
  check is the evidence; scrutinize the capturer's per-stream socket routing.
- **Handshake `v` guard** — confirm the capturer emits `v: WIRE_VERSION` and that
  any framing change bumps `WIRE_VERSION` on both sides (`socket_audio.py:83-87`,
  `167-213`). This is the only thing protecting the pip/local-build decoupling.
- **Status-file liveness backstop** — a stale status file must never report a dead
  recorder as live; the pid check must win (`cli.py:777-797`).
- **TCC stability** — verify the build target uses the stable self-signed cert, not
  ad-hoc, so the Screen Recording grant persists.
- **Fail-loud teardown parity** with Milestone A's supervisor (denied permission /
  crash / no device → rotate partial, no hang).

## Open Questions

1. **Minimum macOS target / system-audio API** — **RESOLVED 2026-06-09: Core Audio
   process-tap, macOS 14.4+ floor** (Apple guidance: audio-only ⇒ Core Audio tap,
   not ScreenCaptureKit). ScreenCaptureKit (13+) deferred — "if we ever need it, we
   can get that later." *Still requires the trivial `sw_vers ≥ 14.4` machine check
   in the Phase 4 Pre-req before locking.*
2. **Self-signed cert grants + persists the Screen Recording TCC grant** *(NEW —
   from assumptions lens)* — the load-bearing premise of the $0 path is an
   unverified Apple-platform behavior. **Must be confirmed by the Phase 4 Pre-req
   spike**; if it fails, the paid Developer ID path is reopened.
4. **Final backpressure policy** (carried from parent plan) — drop-oldest vs
   drop-newest vs bounded-block. Milestone A ships configurable drop-oldest +
   WARNING + monotonic seq. **Decide after a short STT-artifact + drift comparison
   test under real capturer load** — do not freeze drop-oldest as an invariant.
5. **Echo / duplication** (carried) — does the user's own voice (played through
   speakers + re-captured) leak into `them`? May need the process-tap to exclude
   onoats's own output, or AEC. **Validate during the Phase 4 smoke checklist.**

## Final Results

_(to be filled on completion)_

<!-- reviewed: YYYY-MM-DD @ <hash> -->

## Progress

- [ ] Phase 4 — Swift capturer (manual smoke)
- [ ] Phase 5a — Python status file (`tests/test_status_file.py`)
- [ ] Phase 5b — SwiftUI menu-bar launcher (manual smoke)
- [ ] Phase 6 — retire BlackHole + docs

## Findings

_(durable findings recorded here during implementation)_
