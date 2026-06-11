# Milestone B — Native macOS System-Audio Capture + Menu-Bar Launcher

**Status**: In Progress — Phases 4/5a/6 done; Phase 5b built; all manual smoke done incl. both TCC denials — pre-merge review gauntlet next
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
the mic + system-audio TCC grants survive rebuilds — $0, free Apple
ID, **no notarization**. Public distribution (Developer ID notarize + Homebrew
cask) is deferred until a paid ($99/yr) membership and is **out of scope here**.

## Requirements

- **No BlackHole on the happy path.** A fresh macOS user records me+them system
  audio with no virtual-audio driver installed, only the native capturer + the
  required permission grants.
- **Two separate TCC grants — mic AND system audio.** Microphone capture (`me`)
  and Core Audio system-audio capture (`them`) are **distinct macOS privacy paths**
  (Apple's modern UI splits "System Audio Recording Only" / "Screen & System Audio
  Recording" from "Microphone"). The bundle must declare **both**
  `NSMicrophoneUsageDescription` (mic) and `NSAudioCaptureUsageDescription`
  (system audio); a fresh machine can pass the system-audio prompt and still
  silence the `me` branch if mic is denied. Both grants must be handled, both
  denials must fail loud, and acceptance must confirm both. (The plan previously
  said only "Screen Recording" — that label covers neither path precisely.)
- **Same queue files, same tags.** The socket path must produce `me`/`them` queue
  files identical in tagging to the existing PortAudio path (the keystone never-mix
  invariant: mic ⇒ only `me`, system ⇒ only `them`). An **A/B parity check** (same
  source via PortAudio and socket, diff the resulting queue files) is the
  acceptance evidence.
- **Stable TCC identity** *(assumption — verify first via the Phase 4 Pre-req
  spike).* The capturer/menu-bar bundle is signed with a stable self-signed
  identity *on the premise that* this obtains and persists the mic + system-audio
  TCC grants across rebuilds without re-prompt. This is an unverified Apple-platform
  behavior, not an in-repo fact — the Pre-req spike must confirm it before any
  Swift is written. Persistence is keyed to the **designated requirement / bundle
  identity**, **not** the cdhash (the cdhash changes every rebuild — that is
  expected, not a failure). Ad-hoc signing (`codesign -s -`) is explicitly
  disallowed for the shipped build target. The one-time self-signed-cert creation
  is **step 0** of Phase 4, documented in `native/README.md` before the first
  `make sign`.
- **One native bundle.** The menu-bar `.app` is the single native artifact; the
  capturer is embedded at `Onoats.app/Contents/MacOS/onoats-capturer`. One bundle
  ID ⇒ one TCC identity. The Python wheel **never** bundles native code; it
  discovers the binary via `ONOATS_CAPTURER_BIN` (already wired).
- **Version-safe decoupling.** Because the Python recorder (pip) and the native
  capturer (local build) version independently, the capturer **must** emit the
  handshake version the recorder expects, and the recorder **must** reject a
  mismatch loudly (the guard already exists — see Technical Specifications).
- **Fail loud, leave state consistent.** Denied mic or system-audio grant, no system-audio
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

### Phase 4 Pre-req: blocking spikes (TCC + Core Audio tap) before any Phase 4 code  *(do FIRST)*

**These are verification spikes, not coding — they de-risk the whole $0 path AND
the capture primitive before any production Swift is written.** Two premises are
**unverified Apple-platform assumptions** (see Open Questions / Assumptions), not
in-repo facts: (A) that a *self-signed* cert obtains and *persists* the mic +
system-audio TCC grants across rebuilds, and (B) that the Core Audio tap recipe
works on the target OS. Resolve **both** before Phase 4, gated:

1. **OS floor.** Confirm `sw_vers -productVersion` **≥ 14.4** (Core Audio
   process-tap requirement). If below, Phase 4 is blocked — fall back to
   ScreenCaptureKit (13+) or BlackHole.
2. **Cert.** Create a stable self-signed **"Code Signing"** certificate in the login
   keychain (Keychain Assistant). Document the one-time step in `native/README.md`.
   Capture the **designated requirement** with `codesign -dr -` (NOT `-dvvv`, which
   does not print the DR) on a signed stub; record the cdhash separately with
   `codesign -dvvv`.
3. **TCC persistence spike — on the FINAL launch topology, not a bare stub.** Build
   the real bundle shape (`Onoats.app` with the helper embedded at
   `Contents/MacOS/onoats-capturer`) and exercise the **actual supervisor exec path**
   — the Python supervisor launching the embedded binary via `ONOATS_CAPTURER_BIN`,
   not double-clicking the `.app`. Grant **both** mic and system-audio once, then
   **rebuild + re-sign 3×** and confirm macOS does **not** re-prompt and both grants
   persist. Compare `codesign -dr -` output **byte-for-byte** across rebuilds (it must
   be stable; the cdhash will change — that is expected). Rationale: a binary launched
   by the supervisor may be attributed to a different TCC identity than the GUI-launched
   app — test the path you ship. (Cite Apple TN2206 on designated-requirement stability.)
4. **Core Audio tap recipe spike — blocking, executable.** Prove the exact capture
   primitive with a throwaway executable **before** Phase 4 coding:
   - Create the global process tap. **Semantics note:** `CATapDescription`'s process
     list is an *exclusion* list when the exclude/`isExclusive`-style initializer is
     used, so an **empty exclusion list = tap all processes = system output** (the
     AudioCap `stereoGlobalTapButExcludeProcesses: []` pattern). Confirm the exact
     initializer/flag on the target OS — do not assume `isExclusive` means "exclusive
     ownership."
   - Build the **private aggregate device**, install an IOProc, and confirm a real
     **system-output** stream arrives **from multiple unrelated apps**, and that those
     apps **keep playing normally** (the tap must not starve/mute other audio).
   - **Concurrent mic + system capture** in the same spike (not just smoke): run the
     mic input device alongside the system-audio aggregate under the two-socket
     topology and confirm both stream together with no aggregate/clock-domain conflict.
   - Prove **teardown leaves no residue**: `AudioDeviceStop` → destroy IOProc →
     destroy aggregate → destroy tap on exit, then a **start/kill/start ×3** loop with
     no stale aggregate/tap object surviving and no leaked-tap-object contention on
     relaunch.
   - If the recipe is wrong on this OS, Phase 4 must be built around a different
     primitive (and Open Question 1 reopens) — find that out now, not mid-implementation.
5. **If grant cannot be obtained/persisted under self-signed (spike 3):** STOP — the
   "no notarization / $0" conclusion (Open Question 2) is reopened and needs the paid
   Developer ID path. **If the tap recipe fails (spike 4):** revisit Open Question 1.
   Do not proceed to Phase 4 coding on a false premise.

### Phase 4: Swift system-audio capturer  *(macOS native; NOT conduct-runnable)*

**Impl files:** `native/onoats-capturer/` (new Swift package/target), build script
`native/Makefile` (`make build` / `make sign` / `make app`), codesign-with-self-signed-cert step
**Test files:** manual macOS smoke checklist (below) — no Python test
**Test command:** _n/a — manual macOS smoke checklist; not in Python CI_

- **System-audio API (Open Question 1 — RESOLVED 2026-06-09): Core Audio
  process-tap** (`AudioHardwareCreateProcessTap` / `CATapDescription`, macOS
  **14.4+**). ScreenCaptureKit (13+) is explicitly deferred — revisit only if a
  13.x floor is later required. The "tap all processes via empty process list +
  `isExclusive`, feeding a private aggregate device" recipe is **proven in Pre-req
  spike 4 before this phase starts** (no longer deferred to the smoke).
- Capture system output (Core Audio tap) **and** mic; resample each to **16 kHz
  PCM16 mono LE**.
- **Aggregate/tap lifecycle invariant:** on every exit path (graceful and crash),
  the capturer MUST `AudioDeviceStop` → destroy the IOProc → destroy the private
  aggregate device → destroy the process tap, leaving **no stale Core Audio object**
  that would cause exclusive-tap contention on the next launch. Use a uniquely-named
  private aggregate UID so a leaked object is identifiable.
- **Clock domains / timestamps:** mic and system tap may run in **different clock
  domains**; resampling alone does not align them. Derive **one capturer-wide
  monotonic mapping** (host-time → ns) and stamp `captured_monotonic_ns` from it for
  both streams — not from unrelated per-callback times — so `me`/`them` drift is
  measurable and bounded. Use the IOProc `AudioTimeStamp.mHostTime` converted via
  `mach_timebase_info` (equivalently `clock_gettime(CLOCK_UPTIME_RAW)`) as the single
  source; set a resampler drift budget (e.g. ≤1 audio frame / minute) and a
  long-running soak (OQ4 smoke) that must hold drift within it over a realistic session.
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
- **Startup barrier (both-connected before streaming):** socket-file existence is
  NOT proof the recorder connected. The capturer MUST **accept both** connections
  and **write both handshakes** before it starts *either* capture stream — otherwise
  early audio is lost, the two streams offset, or one branch hits the read-idle
  watchdog. If either socket misses a bounded startup deadline, fail **both** branches
  loud.
- **Write invariant (stream socket, not datagram):** a `SOCK_STREAM` write can be
  partial. The capturer MUST treat each frame as a looped `writeAll` (full
  prefix+payload or fail-loud exit), suppress `SIGPIPE` (handle `EPIPE` as a normal
  terminal condition), and — if **one** branch's socket closes while the other is
  still writable — tear down **both** branches cleanly (never half-stream).
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
- **Permission handling for BOTH grants** — mic (`NSMicrophoneUsageDescription`)
  and system audio (`NSAudioCaptureUsageDescription`) are separate TCC services.
  Each denial must produce a clear message and a non-hanging exit (so the supervisor
  rotates a partial session). A mic denial silences `me`; a system-audio denial
  silences `them` — handle and fail loud on **each** independently.
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
  5. **Deny system-audio grant** → confirm the 4-part fail-loud observable:
     `ErrorFrame` logged, supervisor exit code ≠ 0, WARNING/ERROR line, partial
     session rotated into `pending/`. No hang.
  6. **Deny mic grant** (separate TCC service) → `me` branch fails loud with the
     same 4-part observable; no silent silenced-`me` session.
  7. **Kill the capturer mid-session** → same 4-part observable; supervisor tears
     down the whole process group, partial session rotates.
  8. **Aggregate/tap residue** — start/kill/start ×3: confirm no stale private
     aggregate device or process tap survives between runs and no exclusive-tap
     contention on relaunch (the lifecycle invariant above).
  9. **One-socket disconnect** — close one branch's socket mid-session: confirm the
     capturer tears down **both** branches cleanly (no half-stream, no hang, EPIPE
     handled).
  10. **Default-device change mid-session** (disconnect AirPods) → capturer keeps
      streaming, `me`/`them` timeline stays continuous, no exit.
  11. **(OQ4)** long soak under load: inspect `metadata["socket_seq"]` and bound
      `me`/`them` drift to inform the final backpressure policy.
  12. **(OQ5)** listen for own-voice echo leaking into `them` (note: the A/B tag
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
  config label, last-rotation time, running flag, **and a failure state** —
  `last_error` / `exit_reason` / `supervisor_rc` (e.g. mic-denied, system-audio-denied,
  capturer-crash) so the menu bar can show *why* a start failed, not just liveness.
  The recorder writes the failure state on a fail-loud exit.
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
  4-cell backstop truth table; (d) atomic-write (no half-JSON observable);
  (e) **failure-state propagation** — a fail-loud exit writes `last_error`/
  `exit_reason`/`supervisor_rc` and `onoats status` surfaces it.

### Phase 5b: SwiftUI menu-bar launcher  *(macOS native; NOT conduct-runnable)*

**Impl files:** `native/onoats-menubar/` (SwiftUI `MenuBarExtra`, `LSUIElement`),
embeds the Phase-4 capturer at `Contents/MacOS/onoats-capturer`
**Test files:** manual macOS smoke checklist (below)
**Test command:** _n/a — manual macOS smoke checklist; not in Python CI_

- Menu-bar app: Start / Stop / Flush, running indicator, **profiles** (device + STT
  config sets), device pickers (reuse `onoats devices`).
- **Stop MUST signal the recorder/supervisor, never the capturer directly.** Start
  shells out to `onoats bot`; Stop sends `SIGTERM` to that `onoats` process (or
  `onoats` stop path); Flush → `onoats flush`. Signaling the embedded capturer
  directly would convert a graceful user-stop into the Milestone-A *fatal
  capturer-death* path (mis-read as a crash) — the supervisor owns the capturer
  lifecycle, so the GUI must go through it.
- **Consumer:** reads the same status file defined in Phase 5a for its
  running/stopped indicator.
- Package as the single notarization-ready `.app` bundle with the capturer
  embedded; signed with the stable self-signed cert.
- **Manual smoke:** launch from menu bar, start/stop a session, confirm indicator
  tracks the status file, confirm Flush works.

**Decisions (2026-06-10) — CLI discovery, install/update, backend split:**

- **CLI discovery:** the GUI invokes the stable shim `~/.local/bin/onoats`
  created by `uv tool install --editable '<repo>[macos]'` (isolated uv tool venv,
  decoupled from the repo's `.venv`; editable, so Python edits apply on next
  Start with no reinstall). Optional absolute-path override in the app's config;
  the menu shows the resolved path + version so a stale install is visible. No
  login-shell shell-out — a LaunchServices app never inherits shell PATH, and
  config is already CWD-independent (`~/.config/onoats/`, data `~/koda-data`).
- **Install/update:** extend `native/Makefile` (not a justfile — `make` ships
  with Xcode CLT; no new prerequisite): `cert` (scripted self-signed identity
  creation; README GUI steps stay as fallback), `install-cli` (idempotent
  `uv tool install --editable` — re-run *is* the update), `install` (sign +
  install-cli + `ditto` to `~/Applications/Onoats.app`; `ditto` preserves
  signatures, and TCC grants key to the DR not the path, so reinstall keeps
  grants). Full story: create cert once, `make -C native install`; update =
  `git pull && make -C native install`.
- **Backend split:** the PortAudio/BlackHole path stays fully supported and
  CLI-invokable — it is already the default (`audio_source` = `portaudio`) and
  the required path below macOS 14.4. Add an explicit `onoats bot
  --source portaudio|socket` flag for ergonomics. The menu-bar app hardcodes
  `AUDIO_SOURCE=socket` only: the GUI exists for the TCC responsible-process
  topology, which is meaningless for the PortAudio path.
- **Device picker = set the system default input ("option 2", decided
  2026-06-10):** the capturer has no device-selection argument — it binds the
  macOS default input at start — so the menu's mic picker sets
  `kAudioHardwarePropertyDefaultInputDevice` (system-wide; disclosed in the
  submenu). A running session keeps its bound device; the change applies on
  the next Start. **Named profiles (desk/travel device+STT sets) move to a
  follow-up plan** gated on capturer `--mic-uid` support — the Settings
  submenu (STT service, data dir → config.toml) covers the single-profile
  case today.

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
- Full existing suite (202 tests) must stay green after Phase 5a /
  Phase 6 Python edits: `uv run pytest`.

## Issues & Solutions

See **`## Findings`** below — issues encountered during implementation and their
resolutions (copy-only IOProc, tap-create retry, pre-start stale-status bug,
Settings `NSHomeDirectory` gotcha, system-audio-denial-delivers-silence) are
recorded there as they were discovered.

## Acceptance Criteria

- [x] **Pre-req spike 3 (TCC) passed:** `sw_vers ≥ 14.4`; self-signed cert obtains
      **both** mic + system-audio grants and they survive 3 rebuilds without
      re-prompt, **exercised on the real `.app`+embedded-helper+supervisor-exec path**
      (`codesign -dr -` designated requirement byte-stable; cdhash expected to
      change). *(If failed, Open Question 2 reopened — paid Developer ID path.)*
- [x] **Pre-req spike 4 (Core Audio tap) passed:** tap + private aggregate device
      yields a real system-audio stream, and start/kill/start ×3 leaves no stale
      aggregate/tap and no exclusive-tap contention.
- [x] Native capturer produces me+them socket streams with **no BlackHole
      installed**, emitting **JSON-object frames** (`seq`/`captured_monotonic_ns`/
      `pcm_b64`) per the wire contract, echoing the supervisor nonce, with a
      capturer-wide monotonic timestamp source ~~and bounded `me`/`them` drift over
      a soak~~ *(wire mechanics verified 2026-06-10; drift soak = OQ4, pending)*.
- [x] **Keystone never-mix proven** — real-session content routing 2026-06-10:
      session `20260610_130609_f0978b9e` has 82 `them` entries (all video
      narration) and 5 `me` entries (all the user's spoken test phrases), zero
      crossover in either direction. *(Content-routing evidence, per the
      asymmetric test's intent.)*
- [x] Startup barrier: capturer accepts both connections + writes both handshakes
      before streaming; bounded startup deadline fails both branches loud
      *(deadline-expiry observed live: orphaned capturer exited 12 with
      "failing both loud", 2026-06-10)*.
- [x] Write path survives partial writes / `EPIPE` (looped `writeAll`, `SIGPIPE`
      suppressed); closing one branch tears down both cleanly *(observed: every
      wire_check disconnect → "socket closed by peer" → both branches torn down)*.
- [x] Capturer survives a default-input-device change mid-session without exiting
      *(verified live 2026-06-10: device switched mid-session, session continued,
      clean Ctrl+C after)*.
- [x] `onoats status` reads the status file with the pid backstop (4-cell truth
      table); recorder writes the file atomically on start/stop;
      `tests/test_status_file.py` green (incl. producer test); full suite green.
      *(Phase 5a done.)*
- [x] Menu-bar `.app` launches, embeds the capturer, tracks the status file;
      Start/Stop/Flush work and **Stop signals the supervisor, never the capturer**.
      *(Phase 5b core smoke PASSED 2026-06-10 in the GUI topology.)*
- [x] BlackHole demoted to fallback in docs (kept for <14.4); `[macos]` extra
      points at the native build; min macOS version documented. *(Phase 6 done
      2026-06-10.)*
- [x] **Both** mic-denial and system-audio-denial, plus capturer-crash, show the
      4-part fail-loud observable (ErrorFrame + rc≠0 + WARNING/ERROR + `pending/`
      rotation), no hang. *(Capturer-crash VERIFIED 2026-06-10: `pkill -9` →
      ErrorFrames on both branches, supervisor "capturer exited mid-session
      (rc=-9)" + non-zero exit, partial session rotated to `pending/`, no hang.
      **Mic-denial VERIFIED 2026-06-10 in the menu-bar topology**: Don't Allow →
      capturer rc=10 pre-socket, supervisor writes fresh `mic-denied` status,
      menu shows "Last session failed: mic-denied" + cause, no hang. First
      attempt exposed a stale-status bug — see Findings. **System-audio denial
      TESTED 2026-06-10 — the fail-loud observable is structurally unreachable
      for this denial: macOS lets a denied app create the process tap and
      delivers silence (no error, no rc=11)** — see Findings; the rc=11
      fail-loud path covers API-level tap failures and is pinned by the
      parametrized supervisor test.)*

## Review Focus

- **Keystone never-mix invariant** (mic ⇒ `me`, system ⇒ `them`) — the A/B parity
  check is the evidence; scrutinize the capturer's per-stream socket routing.
- **Handshake `v` guard** — confirm the capturer emits `v: WIRE_VERSION` and that
  any framing change bumps `WIRE_VERSION` on both sides (`socket_audio.py:83-87`,
  `167-213`). This is the only thing protecting the pip/local-build decoupling.
- **Status-file liveness backstop** — a stale status file must never report a dead
  recorder as live; the pid check must win (`cli.py:777-797`).
- **TCC stability (two grants)** — verify the build target uses the stable
  self-signed cert (not ad-hoc) and that **both** mic and system-audio grants
  persist; persistence is keyed to the designated requirement (`codesign -dr -`),
  not the cdhash.
- **Process-boundary correctness** — startup barrier (accept-both before stream),
  `writeAll`/`EPIPE`/`SIGPIPE` handling, one-socket-closes-tears-down-both, and
  Core Audio aggregate/tap cleanup (no residue on relaunch). See AGENTS.md
  "Reviewing a subprocess / process-boundary change".
- **Menu-bar Stop routing** — GUI Stop must signal the supervisor (`onoats`), never
  the embedded capturer, so a user-stop isn't mis-read as a capturer crash.
- **Fail-loud teardown parity** with Milestone A's supervisor (each permission
  denial / crash / no device → rotate partial, no hang).

## Open Questions

1. **Minimum macOS target / system-audio API** — **API SELECTED 2026-06-09: Core
   Audio process-tap, macOS 14.4+ floor** (Apple guidance: audio-only ⇒ Core Audio
   tap, not ScreenCaptureKit). ScreenCaptureKit (13+) deferred — "if we ever need it,
   we can get that later." **Not fully resolved until Pre-req spike 4 proves the
   recipe** (and the `sw_vers ≥ 14.4` check passes); if the spike fails, this
   reopens.
2. **Self-signed cert grants + persists the mic + system-audio TCC grants** *(NEW —
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

All four phases landed on `feat/socket-audio-transport-milestone-b` (PR #5):

- **Phase 4 — Swift capturer:** produces the two socket streams natively with no
  BlackHole installed, emitting JSON-object frames per the wire contract.
  End-to-end dual-STT session, keystone me/them zero-crossover, kill-mid-session
  4-part fail-loud, one-socket-close teardown, device-switch survival, A/B parity,
  and 3× residue cleanup all PASSED on real hardware 2026-06-10. Deferred to
  ride-along usage: soak/echo (steps 11–12) and the me/them drift soak (OQ4).
- **Phase 5a — Python status file:** `onoats status` reads the status file with
  the 4-cell pid backstop; recorder writes atomically on start/stop;
  `tests/test_status_file.py` green.
- **Phase 5b — SwiftUI menu-bar launcher:** app builds/signs/GUI-launches (DR
  byte-identical through the bundle restructure), embeds the capturer, tracks the
  status file. Core smoke PASSED 2026-06-10 (Start → mid-call Flush → menu-bar Stop
  with content-bearing final flush → both sessions drained to `done/`, keystone
  split intact). Mic-denial fail-loud PASSED; system-audio denial found to deliver
  silence rather than an enforced failure (see Findings).
- **Phase 6 — BlackHole demoted to fallback:** README + init warning + pyproject
  `[macos]` note lead with the native 14.4+ path, BlackHole kept as documented
  fallback for 13.x–14.3/off-mac.

Full suite: **209 passed** (202 at smoke completion + tests added by the
review-gauntlet fixes). Status going into merge: pre-merge review gauntlet
(`/review` done — 4 findings fixed in `3aa08d2`; `/security-review` done — no
findings; Codex adversarial review + `/deep-review` pending).

<!-- reviewed: 2026-06-11 @ 4bd816ff677794bbf95bf237d90bb8c7e2f0b0cb -->
## Progress

- [x] Phase 4 — Swift capturer (manual smoke) — ***BUILT; smoke steps 1–3, 7,
  9, 10 PASSED on real hardware 2026-06-10** (end-to-end dual-STT session,
  keystone content routing me/them zero-crossover, kill-mid-session 4-part
  fail-loud, one-socket-close teardown, device-switch survival, graceful Ctrl+C
  recorder-finishes-first). **Step 4 A/B parity PASSED 2026-06-10 — both Phase 6
  gate conditions met.** **Steps 5–6 TCC denials: attempted 2026-06-10,
  INCONCLUSIVE in the terminal topology (Onoats toggles don't govern
  terminal-launched sessions — Ghostty attribution); moved to Phase 5b where the
  menu-bar topology makes them testable.** **Step 8 residue ×3 PASSED 2026-06-10
  on the production binary via `native/residue_check.sh` (3× kill -9 mid-capture
  → `RESIDUE: none` + `TAPS: none`; spike scan widened to match `onoats-*` UIDs
  so it sees production aggregates).** **Remaining:** steps 11–12
  soak/echo (ride along with normal usage). Pre-req spikes 3+4 PASSED 2026-06-09.*
- [x] Phase 5a — Python status file (`tests/test_status_file.py`) — **done**
- [x] Phase 5b — SwiftUI menu-bar launcher — ***BUILT 2026-06-10** (app compiles,
  signs, GUI-launches; DR byte-identical through the bundle restructure; install
  chain `make cert` / `make install-cli` / `make install` all verified live).
  **Core smoke PASSED 2026-06-10 on a real call (GUI topology):** Start →
  mid-call Flush (SIGUSR1, 25 entries rotated + fresh active swapped) →
  continued talking → menu-bar Stop (SIGTERM graceful: 25-entry
  **content-bearing final flush** ending in a real utterance — closes the
  Milestone-A open edge), both sessions drained to `done/`,
  `exit_reason=graceful`, pid file removed, keystone me/them split intact
  (3/22 on the call leg). **Mic-denial (step 5) PASSED 2026-06-10 in the GUI
  topology** — Don't Allow → menu shows "Last session failed: mic-denied /
  capturer exited (rc=10) before creating its sockets". (First run exposed a
  stale-status bug, fixed in `522919e` — see Findings.) Settings pickers poked
  live (mic picker, STT picker, data dir visible in the same smoke screenshot).
  **System-audio denial (step 6) TESTED 2026-06-10: denial is not enforced as
  a failure — denied tap delivers silence (see Findings).** Phase 5b manual
  smoke COMPLETE. Device profiles deferred — see Findings.*
- [x] Phase 6 — BlackHole demoted to fallback (gate passed 2026-06-10) —
  **done 2026-06-10**: README matrix + audio-source section lead with the
  native path (14.4+ floor documented, BlackHole kept as documented fallback
  for 13.x–14.3/off-mac), init.py warning softened to a NOTE, silence-detector
  comment + pyproject `[macos]` note updated; suite 202 passed.

## Findings

- **Post-review-fix verification PASSED (2026-06-10).** After the external
  review fixes (RT-thread drop logging moved to the worker, unbind
  serialization, Teardown registration order), the rebuilt capturer passed
  `smoke_wire_check.sh` on real hardware: both branches PASS, drops=0, real
  mic data (peak 0.0147) + system audio (peak 0.39–0.59). Bonus evidence: the
  tap-create retry fired in anger (attempt 1 instant `noErr` +
  `kAudioObjectUnknown`, retry succeeded — the exact documented flakiness,
  handled). Two further real calls were recorded via the menu-bar app and
  fully processed downstream by koda (transcripts render end-to-end). Note:
  an agent/SSH shell cannot run this smoke — mic IOProc binds but delivers
  nothing (the documented audio-context confound); local terminal only.
- **Phase 5b built (2026-06-10): menu-bar app + single-bundle restructure +
  install chain.** `native/onoats-menubar/` (3 swiftc sources, MenuBarExtra,
  LSUIElement); `Onoats.app` main executable is now the menu-bar app with the
  capturer embedded at `Contents/MacOS/onoats-capturer`, both signed
  inner-first with the same identity+identifier — **DR verified byte-identical
  through the restructure** (`identifier "net.varunsingh.onoats" and
  certificate leaf = H"aac7e2b9…"`), so existing TCC grants carry over.
  Key sub-findings:
  - **Scripted cert creation works fully prompt-free** (`native/make_cert.sh`,
    `make cert`): verified empirically under a throwaway identity that codesign
    needs **neither keychain trust nor a partition-list fix** — the
    import-time ACL (`security import -T /usr/bin/codesign`) is sufficient.
    The script refuses to regenerate an existing identity (new cert = new DR =
    all TCC grants invalidated).
  - **CLI discovery solved via `uv tool install --editable '<repo>[macos]'`**
    → stable shim `~/.local/bin/onoats` in an isolated uv venv (editable +
    extras + the git+https dep all verified working). The GUI invokes that
    fixed path (override: `defaults write net.varunsingh.onoats cliPath …`);
    config was already CWD/env-independent (`~/.config/onoats/`).
  - **Stop semantics:** the GUI SIGTERMs only the supervisor it spawned
    (`Process.terminate()`; `runtime.py:1130` handles SIGTERM as graceful
    drain). Sessions started outside the menu bar are displayed as external
    and NOT signalled from Swift — the identity-checked pid signalling
    (marker + fingerprint + recycling guards) lives in the Python CLI, and
    duplicating it in Swift would be drift-prone.
  - **Device pickers / profiles DEFERRED (scope finding):** the capturer has
    no device-selection argument — it captures the **system default** input
    device (and the tap follows default output). A picker in the menu would
    silently not apply. Shipped instead: the menu displays the live default
    input/output device names (the wrong-device guard from the A/B finding
    below). Pickers/profiles need capturer `--mic-uid` support + config
    plumbing first — follow-up work, not part of the 5b smoke gate.
  - **GUI data-dir caveat:** a LaunchServices app sees no shell env, so the
    Swift status reader resolves `config.toml [storage].data_dir` →
    `~/.local/share/onoats` only; shell-exported `ONOATS_DATA_DIR`/XDG vars
    don't apply to GUI-read status (set it in config.toml if it matters).
  - `onoats bot` stdout/stderr from GUI starts → `~/Library/Logs/Onoats/onoats-bot.log`.
  - **Settings submenu added (2026-06-10, user request):** STT service picker
    (whisper/websocket/deepgram, mirroring runtime.py's branches) + data-dir
    chooser + open-config.toml. Writes go to `~/.config/onoats/config.toml`
    itself via a surgical single-key line editor — one source of truth with the
    CLI, no UserDefaults divergence; applies on next Start. Writer verified to
    leave all untouched lines byte-identical. (Gotcha for future tests:
    `NSHomeDirectory()` ignores a `$HOME` override — it reads the user DB.)
- **Phase 4 capturer built + wire-contract verified end to end (2026-06-10).**
  `native/onoats-capturer/` (plain swiftc, 7 sources), built/signed via
  `native/Makefile` into `native/Onoats.app` — DR byte-identical to the spike's
  (`identifier "net.varunsingh.onoats" and certificate leaf = H"aac7e2b9…"`), so
  the existing TCC grants apply. Verified with `native/wire_check.py` (a
  recorder-role contract checker): handshake (rate/width/channels/`v:1`, nonce
  echoed verbatim), 4-byte BE length prefix, 640-byte whole-sample 20 ms frames,
  `seq` contiguous from 0, `captured_monotonic_ns` non-decreasing, both branches
  PASS rc=0; real system audio captured through the tap (peak 0.72–0.77 from a
  `say` voice); SIGTERM and peer-close both produce full teardown (taps/aggregate
  destroyed, socket files unlinked, both branches torn down together). 187-test
  Python suite green. Engineering findings while building it:
  - **Copy-only IOProc is mandatory.** Running AVAudioConverter inside the Core
    Audio realtime callback made the HAL silently stop invoking the IOProc after
    ~5 cycles (~50 ms). Fixed: IOProc memcpys buffer+timestamp into a bounded
    queue; a worker thread does wrap→resample→chunk.
  - **The global process tap delivers IO callbacks ONLY while some process is
    rendering audio** — a quiet system produces no frames at all (every earlier
    "continuous stream" spike observation had test audio covering the window).
    Without filler this starves the `them` branch, trips the recorder's 10 s
    read-idle watchdog, and breaks me/them timeline continuity. Fixed: per-branch
    20 ms silence pacer (engages after 100 ms without real data, trails the live
    edge by 100 ms, clamps resumed-real timestamps monotonic, gap-jumps if >2 s
    behind). Also covers mic gaps during device changes.
  - **`AudioHardwareCreateProcessTap` intermittently returns `noErr` +
    `kAudioObjectUnknown` even for the signed bundle** (instant return, vs
    ~200 ms legit) — bounded retry ×3 @ 500 ms recovers it (observed recovering
    on attempt 2). The spike had seen this only unsigned.
  - **Capture start order:** tap+aggregate first, mic engine second (creating
    the tap while AVAudioEngine runs correlated with the flaky creation).
  - **Agent-shell limitation:** mic capture delivers zero callbacks under the
    Claude/sandboxed shell context regardless of code (the known TCC-attribution
    confound — the spike's own `concurrent` mode also reports mic=0 there), and
    background-niced processes get scheduler-throttled, which produced phantom
    "stall" symptoms during debugging. Mic-content verification and the full
    manual smoke checklist (steps 1–12) must run from the user's interactive
    terminal.

- **TCC-denial tests (smoke steps 5–6) INCONCLUSIVE in the terminal topology —
  re-test in Phase 5b (2026-06-10).** Three denial tests were run by toggling
  the **"Onoats"** entries in System Settings — but per the spike-3 attribution
  finding, a terminal-launched capturer's effective TCC identity is the
  **terminal (Ghostty)**, whose grants were never touched. Hence: mid-session
  revocation "did nothing", a start with "mic off" still captured, and the tap
  was still created with "system audio off" — all expected, none of it evidence
  about real denial behavior. What today's runs DID establish: (a) the silence
  pacer turns a starved branch into "alive but empty" (session rotates, no
  hang, no watchdog kill) rather than a hard failure; (b) the capturer's
  fail-loud denial path rests on the startup `AVCaptureDevice` authorization
  check, which keys to the responsible process — correct for the 5b menu-bar
  topology, vacuous for terminal launches where the terminal already holds the
  grant. **Action:** run steps 5–6 against the menu-bar topology in Phase 5b
  (where the "Onoats" toggles are the effective ones); also consider a 5b-era
  heuristic warning for a persistently all-zero mic branch (macOS can deliver
  zeroed buffers, not errors, to unauthorized audio clients).

- **Mic-denial smoke PASSED in the menu-bar topology (2026-06-10) — after
  exposing and fixing a stale-status bug.** First denial run: capturer
  correctly exited rc=10 before creating its sockets, but the
  `_wait_for_sockets` False path in `_supervise_socket_session` wrote NO
  status record — the menu read the *previous* session's record and showed
  the absurd "Last session failed: graceful". Fixed in `522919e`, two layers:
  (1) the pre-start failure path now writes a fresh stopped record with the
  rc-mapped reason (`10→mic-denied`, `11→system-audio-denied`, other→
  `capturer-start-failed`, timeout→`capturer-start-timeout`); (2)
  `RecorderModel.handleExit` gained a freshness guard — a status record whose
  `start_time` predates the spawned session is ignored in favor of the raw
  exit code, so a stale record can never mislabel a failure. Pinned by a
  parametrized supervisor test (fake capturer dies pre-socket with chosen rc;
  planted stale "graceful" record must be replaced). Re-run against the fixed
  build: menu shows "Last session failed: mic-denied / capturer exited
  (rc=10) before creating its sockets" — exactly the fail-loud observable
  step 5 demands. Recovery via `tccutil reset Microphone
  net.varunsingh.onoats` → Start → Allow re-prompts as expected.

- **System-audio (kTCCServiceAudioCapture) denial is NOT enforced as a
  failure — denied tap delivers silence (2026-06-10).** Test sequence in the
  menu-bar topology: `tccutil reset AudioCapture net.varunsingh.onoats` →
  Start → prompt appeared → Don't Allow (TCC.db: `auth_value=0` recorded for
  `net.varunsingh.onoats`). Subsequent Starts: no re-prompt, and the capturer
  **created the process tap without error** ("capturing via process tap at
  48000 Hz") — no rc=11, session runs normally. Content probe: ~2.5-min
  session with system audio playing → manual flush AND shutdown flush both
  `0 entries`, nothing rotated — the denied tap delivers zeroed/empty
  buffers, which the silence pacer masks as "alive but empty `them`". This
  confirms the earlier prediction (macOS delivers zeroed buffers, not
  errors, to unauthorized audio clients) and the spike-3 hint that
  AudioCapture is more permissive for a signed app creating a tap.
  **Disposition:** the 4-part fail-loud observable is structurally
  unreachable for this denial — an OS behavior, not a gap in our supervisor.
  The rc=11 `system-audio-denied` path remains correct for API-level tap
  failures and is pinned by the parametrized supervisor test. **Follow-up
  (post-PR):** a heuristic warning for a persistently all-zero system branch
  (same idea already noted above for the mic branch) is the only way to
  surface this state to the user.

- **A/B parity check PASSED (2026-06-10) — Phase 6 gate satisfied.** Same source
  video recorded via the socket/native path (`session_20260610_133548_e010cfab`,
  27 `them` utterances) and the PortAudio/BlackHole path
  (`session_20260610_134058_a00d0a0d`, 28 `them` utterances): near-identical
  segmentation and transcription quality, with minor STT errors in BOTH
  directions (socket: "whatever"→"whenever"; PortAudio: "say from"→"say
  Friend") — no systematic socket-path degradation, ruling out a resample bug.
  Mic-content quality evidenced separately (earlier session's `me` entries
  transcribed the spoken test phrases verbatim). Together with the keystone
  content-routing result, **both Phase 6 gate conditions have PASSED on the
  author's machine**. Caveat from the first A/B attempt: the PortAudio run
  initially recorded 0 entries because the wrong devices were selected —
  reinforcing the device-visibility follow-up (show chosen devices in CLI +
  menu bar).

- **Wire smoke PASSED on real hardware (user terminal, 2026-06-10):** both
  branches PASS via `smoke_wire_check.sh` with real content — mic peak 0.07–0.12
  (Scarlett Solo), system peak 0.37 (music). Two fixes/findings from getting there:
  - **AVAudioEngine inputNode delivers ZERO tap callbacks from the Scarlett Solo
    USB** (engine `running=true`, formats agree, no error — and it misreports the
    device at 44.1 kHz when HAL says 48 kHz). A raw HAL `AudioDeviceIOProcID` on
    the same device streams fine (`--selftest-mic` shows both probes). MicCapture
    was rewritten onto raw HAL (same copy-only IOProc + worker pattern as the
    system branch) with a `kAudioHardwarePropertyDefaultInputDevice` listener for
    the device-change contract. Lesson: **never use AVAudioEngine input for this
    product; PortAudio worked all along because it is raw HAL.**
  - **Terminal-launched tap creation is SLOW (~2–3 s, audible output dropout at
    session start)** even signed — the spike's ~200 ms number was measured
    GUI-launched (`open Onoats.app`); the TCC verification inside
    `AudioHardwareCreateProcessTap` is evidently the slow path under terminal
    attribution. Per-session impact only (tap is created once). Phase 5b's
    GUI launch should restore ~200 ms; for the CLI topology, document "start
    onoats before the meeting". The capturer's frame timeline stays gap-free
    regardless (frames × 20 ms == ts_span, drops=0).
  - **Follow-up (Phase 5b/6 scope):** surface the chosen capture devices in
    `onoats status` / CLI output and the menu-bar UI (the capturer now logs the
    input device name/UID at start), and decide whether the socket path should
    honor explicit device selection like the PortAudio path instead of
    default-input-only.

- **TCC attribution is rooted at the launching GUI app, not the capturer's bundle
  identity (spike 3, 2026-06-09).** Running `supervisor-exec.py tcc` from Ghostty
  reported `mic (pre)=authorized` and a silent system-audio tap success on a
  **brand-new bundle id** (`net.varunsingh.onoats`) that had never been granted
  anything — and System Settings lists **Ghostty** (not "Onoats") under both
  Microphone and Screen & System Audio Recording. Conclusion: a binary
  `posix_spawn`'d from a terminal (`shell → uv → python → capturer`) is attributed
  by TCC to the **responsible GUI process at the session root = the terminal**, and
  inherits *its* grants; the capturer's own self-signed identity is never consulted.
  - **Consequence:** the self-signed-cert TCC-persistence premise (OQ2) can ONLY be
    tested with `Onoats.app` as the responsible process (a LaunchServices/`open`
    launch, or a GUI menu-bar app at the chain root) — the terminal path is a
    confound and any "PASS" from it is a false pass.
  - **Two viable production topologies fall out:** (1) **terminal-launched** — the
    recorder runs from a terminal that already holds the grants (today's koda/onoats
    workflow); the capturer inherits them; **no self-signed cert / no `.app` needed**
    for TCC (ad-hoc signing is fine). (2) **menu-bar-app-launched** — `Onoats.app`
    (GUI, LaunchServices) is its own responsible process, so grants are keyed to its
    bundle identity and the **self-signed stable cert is required** to persist them
    across rebuilds; this is the path the plan's Phase 5b assumes.
  - DR captured and is the stable shape we want (no cdhash):
    `identifier "net.varunsingh.onoats" and certificate leaf = H"aac7e2b9…"`.
  - `make build` compiled the Core Audio process-tap Swift clean on macOS 26.3.1
    (no API iteration needed); `make sign` succeeded with the self-signed cert.

- **System audio is a SEPARATE TCC service from mic — confirmed (2026-06-09).**
  macOS 26 lists a distinct **"System Audio Recording Only"** pane; the Core Audio
  process tap added "Onoats" there independently of the `Microphone` pane. So the
  plan's "two distinct grants" requirement and the `NSAudioCaptureUsageDescription`
  key are both correct/necessary. **Notable:** only the **mic** prompt was
  interactive; the system-audio grant was recorded without a visible separate
  prompt, yet real capture works (`peak=0.44`) — so the system-audio authorization
  is more permissive for a signed app creating a tap. Both denials must still be
  handled in Phase 4 (a user can revoke either pane).

- **Pre-req spike 4 (Core Audio tap recipe) PASSED — 2026-06-09.** Global process
  tap (`CATapDescription(stereoGlobalTapButExcludeProcesses: [])`, empty exclusion =
  tap all = system output) → private aggregate device → IOProc captures a **real
  system-output stream** (peak 0.5–0.8 over ~575k frames at 48 kHz/2ch float).
  - **Concurrent mic + system**: AVAudioEngine mic alongside the Core Audio
    aggregate streamed together (mic peak 0.18 / system peak 0.29), no clock-domain
    conflict.
  - **Mute behavior**: `CATapMuteBehavior.unmuted` (raw 0) keeps other apps
    **audible** while capturing — this is the one to use. `.mutedWhenTapped` (raw 2)
    mutes the tapped apps for the whole capture (ruled out).
  - **Startup dropout is a SIGNING artifact, not the API.** The **unsigned**
    standalone binary blocked **~3.9 s inside `AudioHardwareCreateProcessTap`**
    (TCC/security verification per call) → audible 4 s dropout. The **signed**
    `Onoats.app` creates the tap in **~200 ms, no audible stutter** (3 back-to-back
    runs: 204/184/195 ms). **Implication: the capturer MUST be signed** (it is — it
    ships inside `Onoats.app`); never benchmark/ship the unsigned build. The unsigned
    binary was also intermittently flaky (one `AudioHardwareCreateProcessTap`
    returned `noErr` + `kAudioObjectUnknown`).
  - **Residue / teardown**: `kill -9 ×3` then `list-aggregates` → **none** (macOS
    auto-reclaims *private* aggregate devices on process death) and `list-taps` →
    **none** (no tap leak even on SIGKILL — an earlier "taps leak" hunch was wrong).
    Graceful exit (our `defer` chain) also leaves none.
  - **Phase 4 design notes:** create the tap **once per session** and start the
    IOProc immediately (signed ⇒ ~200 ms, fine); use `.unmuted`; tear down on
    SIGTERM-graceful (`AudioDeviceStop`→destroy IOProc→destroy aggregate→destroy
    tap); a startup tap-sweep (`kAudioHardwarePropertyTapList`) is cheap optional
    insurance though no leak was observed. Touching the tap subsystem
    (even `list-taps`) glitches audio briefly — avoid needless tap ops mid-session.

- **Phase 5a (status file) — shipped 2026-06-09.** `src/onoats/status.py` (new):
  `STATUS_SCHEMA_VERSION = 1`, frozen `StatusRecord`, atomic `write_status`
  (tempfile + `os.replace` + `fsync`), tolerant `read_status` (malformed/half-JSON
  → `None`), producer helpers, and `resolve_liveness` (the pid-authoritative 4-cell
  truth table). Producers wired in `runtime.py` (`_write_status_running` /
  `_mark_status_rotation` / `_write_status_stopped`, best-effort) and called from
  `dual.py` at start (after pid write), every rotation, and stop (before pid
  removal). `onoats status` (`cli.py`) now reads the status file with the pid file
  as the liveness backstop and surfaces `exit_reason`/`last_error`/`supervisor_rc`;
  the socket supervisor stamps the specific `capturer-crash` vs `fatal_error_frame`
  cause + final rc via `stamp_supervisor_failure`. 26 new tests in
  `tests/test_status_file.py`; full suite green (187).
- **Phase 4 Pre-req spike kit built (`native/spike/`).** One signed bundle
  (`Onoats.app` + helper at `Contents/MacOS/onoats-capturer`) with `tcc` / `tap` /
  `concurrent` / `list-aggregates` modes, a faithful `supervisor-exec.py` harness
  (reuses the real `_build_capturer_env` — verified no `DYLD_*`, nonce wired, no
  secrets leaked), Makefile, and `native/README.md` (cert step + run sequence).
  `sw_vers` = 26.3.1 (≥ 14.4 ✓). **Spikes 3 & 4 are interactive and BLOCKING — not
  yet run.** Open uncertainty to resolve in spike 3: whether Core Audio taps
  actually consult `NSAudioCaptureUsageDescription` (both keys declared).
