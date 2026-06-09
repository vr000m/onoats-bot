# Task: macOS Menu-bar Launcher + CoreAudio Socket Audio Transport (retire BlackHole)

**Status**: Proposed (draft — `/review-plan` findings incorporated 2026-06-08)
**Component**: recorder, transport, macos, packaging
**Assigned to**: vr000m
**Priority**: Medium (quality-of-life + dependency reduction; not blocking the recorder)
**Branch**: _(none yet — author on a feature branch when picked up)_
**Created**: 2026-06-07
**Completed**: (fill when done)

> **Provenance.** This work was scoped out of koda's recorder-extraction plan
> (`koda-pipecat: docs/dev_plans/20260606-refactor-extract-onoats-recorder.md`,
> "Deferred to a FUTURE onoats-repo dev plan") and deliberately left for the
> onoats repo. It is **not** part of the koda dep-back PR. This file is the
> forward-pointer made concrete.

---

## Objective

Two related, independently-shippable improvements to the onoats recorder on macOS:

1. **CoreAudio socket audio transport** — an `AUDIO_SOURCE=socket` capture mode in
   which the two recorder branches (`me` / mic, `them` / system) read 16 kHz
   PCM16 mono audio from **unix domain sockets** instead of PortAudio devices.
   A small native (Swift) **capturer** taps the microphone and system output via
   CoreAudio / ScreenCaptureKit and writes each stream to its own socket. This
   **retires the BlackHole loopback dependency** (today's only way to capture
   "them" on Apple Silicon — see the README cross-platform matrix).

2. **macOS menu-bar launcher** — a lightweight status-bar app that starts/stops
   the recorder, shows running state, triggers a flush, switches between
   device/STT **profiles** (desk vs travel), and picks input devices — wrapping
   the existing `onoats {bot,flush,status}` CLI rather than re-implementing it.

These are **two features in one plan** because they share the macOS-native
surface and the capturer's lifecycle is naturally owned by the menu-bar app; but
the **socket transport (Phase 1–3) is the higher-value, CI-testable core** and
can ship without the menu bar. The menu bar (Phase 5) can ship independently on
top of today's PortAudio path too. Sequence so the risky native work is gated by
a pure-Python, fully-tested seam first.

## Context

### Today's dual-stream capture (verified against `src/onoats/dual.py`)

onoats records two **independent** branches that must never mix:

```
mic    ("me")   → LocalAudioTransport(input_device_index=mic_dev).input()
                    → VAD → [optional processors] → STT #1 → SourceTagger(source="me",  source_order=0)
system ("them") → LocalAudioTransport(input_device_index=system_dev).input()
                    → VAD → [optional processors] → STT #2 → SourceTagger(source="them", source_order=1)
                                                            (both arms = one ParallelPipeline)
```

`_build_dual_pipeline(mic_transport, system_transport, …)` (`dual.py`) takes the
two transport **objects** as parameters and only calls `.input()` on each. That
is the swap seam: a different transport class with a compatible `.input()` drops
in with **no pipeline change**. Each branch has its own STT session
(`_create_stt_service()` is called twice).

**The load-bearing invariant** (from koda's `reference_koda_audio_routing` notes,
and why the dual design exists): **dual-stream, two STT connections, the `me` and
`them` audio MUST NEVER be mixed.** One transport instance = one branch = one
socket = one STT session. A capturer that interleaved the two streams onto one
socket, or a transport that fanned one socket to both branches, would silently
destroy speaker attribution (koda's classifier keys on the canonical `me`/`them`
`source` enum; mixing collapses it).

> **Assumption (verify before relying on it).** That koda's classifier keys on
> the `me`/`them` `source` enum *and that those values are a frozen contract* is
> sourced from koda's `reference_koda_audio_routing` notes, not from this repo:
> koda is an external codebase not vendored here. What onoats can verify is only
> that it **emits** `source="me"/"them"` (`dual.py:186-187` via `SourceTagger`).
> Before this plan leans on "never mix or attribution breaks," confirm the enum
> values and their frozen status against koda's queue-contract doc.

### The pipecat transport seam (verified via pipecat-context-hub, pipecat 1.3.0)

The override hook below is **verified against the pipecat 1.3.0 source**, not
inferred from the method name — an earlier draft of this plan said to override
`start_audio_in_streaming()` "mirroring `LocalAudioInputTransport`," which is
wrong: the reference does **not** use that hook.

- `pipecat.transports.base_input.BaseInputTransport(FrameProcessor)` is the base
  class for all input transports. Verified lifecycle:
  - `__init__(self, params: TransportParams, **kwargs)` — **requires** a
    `TransportParams`. The base only creates the audio-in queue + drain task
    (`_create_audio_task`) when `params.audio_in_enabled` is `True`
    (`base_input.py:223-227`); with it `False`, `push_audio_frame` silently
    no-ops. The socket transport MUST construct
    `TransportParams(audio_in_enabled=True, audio_in_sample_rate=…)`.
  - The **reference `LocalAudioInputTransport` overrides `async start(self, frame:
    StartFrame)`** (not `start_audio_in_streaming`): it calls `await
    super().start(frame)`, opens its stream / registers its read source, then
    calls `await self.set_transport_ready(frame)` — and `set_transport_ready`
    is what triggers `_create_audio_task` (`base_input.py:152-159`). It pushes
    PCM via `await self.push_audio_frame(InputAudioRawFrame(audio=…,
    sample_rate=…, num_channels=…))` from its capture source
    (`audio.py:68-113`).
  - `async start_audio_in_streaming(self)` exists as a base hook but is a `pass`
    stub the reference never overrides (`base_input.py:85-90`). **Mirror
    `start()` + `set_transport_ready()`, not `start_audio_in_streaming`.**
- `LocalAudioTransport.input()` returns the `LocalAudioInputTransport`
  (a `FrameProcessor`) that `dual.py` puts at the head of each arm. The new
  `UnixSocketAudioTransport.input()` returns a `UnixSocketAudioInputTransport`.

So the Python seam is **verified**: subclass `BaseInputTransport`, override
`start(frame)` to connect the socket, **call `set_transport_ready(frame)` first,
then** spawn an async read loop that `push_audio_frame`s PCM16/16 kHz/mono, and
pass `TransportParams(audio_in_enabled=True, …)`. One instance per branch.

> **Ordering matters — set ready BEFORE spawning the reader (queue-init race).**
> `push_audio_frame()` does `self._audio_in_queue.put(frame)` (`base_input.py:170`),
> but `_audio_in_queue` is created only inside `set_transport_ready()` →
> `_create_audio_task()` (`base_input.py:152`). If the reader task starts and
> pushes a frame before `set_transport_ready(frame)` has run, it hits a `None`
> queue. Connect + validate the handshake, call `set_transport_ready(frame)`,
> *then* start the reader (or gate the reader on an `asyncio.Event` set after
> readiness). Keep `push_audio_frame()` as the public call — do not reach for
> `_push_audio_frame`.
>
> **The subclass must tear down its own reader task.** `BaseInputTransport.stop()`
> / `cancel()` only cancel the framework's internal drain task via
> `_cancel_audio_task()` (`base_input.py:116,140`) — they do **not** touch a task
> the subclass spawned. Store `_read_task`, and in `stop()` / `cancel()` /
> `cleanup()` close the socket and cancel-and-await `_read_task`, or it leaks past
> `EndFrame` / `CancelFrame`.

### Why retire BlackHole

BlackHole is a virtual-audio-driver kext-adjacent install the user must set up
out-of-band, and "them" capture silently fails if the system output isn't routed
through it (a documented koda failure mode). The *premise* of this plan is that
ScreenCaptureKit and/or the CoreAudio process-tap API can capture system/process
audio **without** a virtual device, removing that setup step and failure class.

> **Assumption (validate in a Phase 4 spike, do not treat as settled).** The
> specific capability — system-audio capture with no virtual device — and the
> macOS version floors cited below (ScreenCaptureKit ≈ macOS 13+, CoreAudio
> process-tap ≈ macOS 14.4+) are Apple-framework behaviour that cannot be
> confirmed from this codebase. Validate them against current Apple docs and a
> capture spike before committing the BlackHole-retirement story; the chosen API
> and its OS floor are also Open Question 1.

## Requirements

- **Socket transport (Python, CI-testable without native code):**
  - A `UnixSocketAudioInputTransport(BaseInputTransport)` that reads framed PCM16
    LE mono @ 16 kHz from a unix socket and `push_audio_frame`s `InputAudioRawFrame`s.
  - A `UnixSocketAudioTransport(BaseTransport)` wrapper exposing `.input()` so it
    drops into `_build_dual_pipeline` unchanged.
  - `AUDIO_SOURCE` selector (`portaudio` default | `socket`) wired in `dual.py`’s
    transport construction (the only site that builds `LocalAudioTransport`), with
    per-branch socket paths (`ONOATS_MIC_SOCKET` / `ONOATS_SYSTEM_SOCKET`, or
    `[audio] mic_socket/system_socket` in `config.toml`). **Resolve these through
    `OnoatsConfig` properties** (add `audio_source`, `mic_socket`, `system_socket`
    with a new `[audio]` section in `_DEFAULTS`), matching the existing
    env-over-toml `_env_or` device-resolution pattern (`config/__init__.py:99-116`)
    — not a raw `os.getenv` at the `dual.py` call site, which would fork the
    precedence rules.
  - **Never-mix guarantee preserved**: exactly one socket per branch; the
    transport refuses to start if both branches resolve to the same socket path.
    **Compare resolved paths, not raw strings** —
    `Path(mic_socket).expanduser().resolve() != Path(system_socket).expanduser().resolve()`
    — so a symlink or relative-path alias can't slip two branches onto one socket.
    The supervisor should also place the two sockets in a **private, supervisor-owned
    directory** (not a world-writable `/tmp` path) to foreclose symlink aliasing.
  - Clean teardown on socket loss: if the capturer dies / the socket closes, the
    branch surfaces an `ErrorFrame` and the session tears down cleanly (still
    flushes + rotates). **Specify this on the socket transport's own terms** — do
    *not* "mirror the STT-client reconnect ergonomics in `runtime.py`": that logic
    lives inside `WebSocketSTTService._ensure_connected` (a websockets client with
    ~15.5s retry, `runtime.py:340-356`), is not a transport-level helper, and a
    unix-socket reader has a different failure model (EOF / broken pipe / process
    death). There is no reusable reconnect routine in `runtime.py` to inherit.
  - **Read-idle watchdog (EOF is not the only failure).** A capturer that is alive
    but produces no data never closes the socket, so EOF never fires and the branch
    hangs silently (the base audio task just waits — `base_input.py:237`). Add a
    configurable read-idle timeout: if no frame arrives within it, surface an
    `ErrorFrame` and tear the session down the same way as EOF.
  - **Generation token / handshake nonce** so a supervisor restart invalidates
    stale socket fds: a restarted capturer presents a new nonce in its handshake,
    and the transport rejects (or refuses to bind to) a stale socket file left by
    the previous generation — preventing a new transport from latching onto an old
    half-open socket.
  - **Single lifecycle owner (resolve the Phase 1 ⇄ Phase 3 overlap).** Exactly
    one layer owns recovery so the two don't race (supervisor respawning the
    capturer → a new socket while the transport is mid-reconnect on the old path).
    Decision for this plan: **the Phase 3 supervisor owns the capturer process
    lifecycle; the transport treats socket EOF as terminal-for-this-session**
    (surface `ErrorFrame`, let the session rotate), and a fresh capturer +
    transport pair is what a restart establishes — the transport does not
    self-reconnect a live branch. (See Open Question 3.)
- **Native capturer (macOS):**
  - A Swift binary that opens two unix sockets (mic, system), captures each source
    via CoreAudio (mic) + ScreenCaptureKit / CoreAudio process-tap (system),
    resamples to 16 kHz PCM16 mono, and writes framed PCM to the matching socket.
  - Handles the Screen-Recording / audio-capture **permission** prompts and
    degrades with a clear message when denied.
  - A defined **wire framing** + handshake (format negotiation) between capturer
    and the Python transport.
- **Lifecycle:** something owns "start capturer → wait for sockets → start
  recorder; stop recorder → stop capturer". In CLI mode this can be a thin
  supervisor in `cli.py`; in GUI mode the menu-bar app owns it.
- **Menu-bar launcher:**
  - Status-bar item showing running/stopped + current profile; Start / Stop /
    Flush actions wrapping `onoats {bot,flush}` and reading `onoats status`.
  - **Profiles** (e.g. desk / travel) selecting device + STT config sets.
  - Device pickers (reuse `onoats devices` enumeration).
  - Reads recorder state from a **status file** (so GUI and CLI agree); no second
    source of truth. **Note this is a net-new artifact, not an existing one:**
    today `onoats status` derives state from the pid file + a liveness probe
    (`cli.py:187-207`, `_read_pid` + `_process_alive`) — there is no status file.
    To avoid creating a *second* source of truth, the recorder must **write** the
    status file (Phase 5 task below) and `onoats status` must be **rewired to read
    it**, with the pid file kept as the liveness backstop. The status-file schema
    is a third versioned contract alongside the wire format and the queue contract.
- **Cross-platform discipline:** the socket transport (Python) is portable; the
  capturer + menu bar are macOS-only and must live behind the existing `[macos]`
  extra / optional install, never imported on the baseline path. Linux/Windows
  keep PortAudio (+ their own loopback) — `AUDIO_SOURCE=socket` simply requires
  *a* feeder, which on macOS is the Swift capturer but could be anything writing
  the wire format.

## Review Focus (for the eventual `/review-plan`)

- **Never-mix invariant** — prove, with a test, that `AUDIO_SOURCE=socket`
  produces two independent branches: a frame written to the mic socket can only
  surface tagged `me`, and a frame to the system socket only `them`. No code path
  fans one socket to both arms or merges them pre-STT.
- **No native dependency on the Python/CI path** — `import onoats.dual` and the
  socket transport must import and unit-test with **no Swift binary present**
  (the test feeds the sockets from a pure-Python fixture). Assert the capturer is
  never imported/spawned on `AUDIO_SOURCE=portaudio`.
- **Wire-format contract** — the capturer↔transport framing (sample rate, width,
  channel count, endianness, frame size, handshake) is a versioned contract;
  pin it in a doc + a round-trip test, the way the queue contract is pinned.
- **Backpressure / drift** — define behaviour when the recorder reads slower than
  the capturer writes (drop vs block vs buffer cap) so a stall can't OOM or desync
  `me`/`them` timestamps.
- **Permissions & failure modes** — denied Screen Recording, no system-audio
  device, capturer crash mid-session: each must fail loud and leave the queue
  consistent (a partial session still rotates), not hang.

## Implementation Checklist

> **⛔ AUTONOMOUS-RUN SCOPE (e.g. `/conduct`): implement Phases 1–3 ONLY, then
> STOP and report.** Do **not** begin Phase 4 (Swift/native capturer), Phase 5
> (menu bar), or Phase 6 (BlackHole retirement) in an unattended run: those are
> macOS-native, can't be verified in headless/Python CI, and Phase 4 is gated on
> the unresolved distribution unknown (Open Question 2). Two further guardrails
> for the autonomous run:
> - **Do not resolve Open Question 4** (final backpressure policy). Ship the
>   configurable default (drop-oldest + WARNING + monotonic seq); the *final*
>   drop-oldest vs drop-newest vs bounded-block choice is deferred to a drift
>   test and is out of scope here.
> - After Phase 2, surface the **keystone never-mix test** (mic-socket ⇒ only
>   `me`, system-socket ⇒ only `them`) for human review before continuing — it
>   protects downstream speaker attribution and warrants eyes even when green.
>
> **Two milestones, two PRs — do not bundle.** The plan covers two
> independently-shippable features; keep them in separate PRs so the CI-testable
> core is not held hostage by the hardest native unknown (Open Question 2,
> binary distribution).
>
> - **Milestone A (Phases 1–3): the portable, CI-testable core.** Pure Python +
>   a pure-Python fake capturer; no Swift, no native dependency. Mergeable on its
>   own behind `AUDIO_SOURCE=socket` (defaulting to `portaudio`).
>   **Caveat — Phase 3 is seam-complete, NOT user-runnable:** its supervisor
>   spawns the capturer named by `ONOATS_CAPTURER_BIN`, which does not exist until
>   Phase 4, so on a real machine `AUDIO_SOURCE=socket` cannot capture until
>   Phase 4 lands. A commit at the Phase-3 boundary is safe (default path intact)
>   but the socket path is exercisable only via the fake capturer / CI. Keep
>   `AUDIO_SOURCE=socket` undocumented-as-default and unflipped until Phase 6.
> - **Milestone B (Phases 4–6): the native capturer + menu bar + BlackHole
>   retirement.** macOS-native, harder to CI, and gated on resolving Open
>   Question 2. Phase 5 (menu bar) is independent of 4 and can ship on the
>   PortAudio path; it gains "no BlackHole" once Phase 4 lands.
>
> If this file grows unwieldy as Milestone B is fleshed out, split B into its own
> dev-plan file at that point; for now the two-milestone framing keeps the shared
> macOS context in one place.

### Phase 1: `UnixSocketAudioInputTransport` + wire framing  *(Python)*

**Impl files:** `src/onoats/transports/socket_audio.py` (new), `src/onoats/transports/__init__.py`
**Test files:** `tests/test_socket_audio_transport.py`
**Test command:** `uv run pytest tests/test_socket_audio_transport.py -v`

- **Create the `src/onoats/transports/` package** (new `__init__.py`) and check
  `tests/test_package_layout.py` doesn't need updating in the same commit (it
  enumerates packages) so the Phase-1 boundary stays green.
- Subclass `BaseInputTransport`. **Override `async start(self, frame)`** (the
  verified reference hook — see "The pipecat transport seam" above, *not*
  `start_audio_in_streaming`), in this **order** (the order is load-bearing):
  1. `await super().start(frame)`;
  2. connect the unix socket and validate the handshake;
  3. `await self.set_transport_ready(frame)` — this is what creates
     `_audio_in_queue` (`base_input.py:152`);
  4. **only then** spawn the async read-loop task (store it as `self._read_task`)
     that reads framed PCM16 and `push_audio_frame(InputAudioRawFrame(...))`.

  Spawning the reader before step 3 races the queue creation —
  `push_audio_frame` does `_audio_in_queue.put(frame)` (`base_input.py:170`) and
  would hit a `None` queue. (Equivalently, gate the reader on an `asyncio.Event`
  set after readiness.) Keep `push_audio_frame()` — do not call `_push_audio_frame`.
- **Own the reader-task teardown.** `BaseInputTransport.stop()` / `cancel()` only
  cancel the framework's drain task (`_cancel_audio_task`, `base_input.py:116,140`),
  not `self._read_task`. Override `stop(frame)` / `cancel(frame)` / `cleanup()` to
  close the socket and cancel-and-await `self._read_task`, so it doesn't leak past
  `EndFrame` / `CancelFrame`.
- Construct the base with `TransportParams(audio_in_enabled=True,
  audio_in_sample_rate=…)` — without `audio_in_enabled=True` the base never drains
  pushed frames. Mirror `LocalAudioInputTransport`'s 20 ms (`sample_rate/100 * 2`)
  frame sizing.
- Define the wire framing. **Prefer length-prefixed frames** over fixed-size: a
  unix *stream* socket gives no message boundaries, so a fixed-size reader must
  `readexactly(N)` and the writer must handle partial writes — length-prefixing
  makes desync-on-partial-write impossible to ignore. If fixed-size is chosen
  anyway, `N = 640` bytes (20 ms PCM16/16 kHz mono) and the reader MUST use
  `readexactly(640)`. A 1-line JSON handshake header
  `{"rate":16000,"width":2,"channels":1,"v":1,"nonce":…}` for format negotiation +
  generation token is the forward-compatible option — decide in review, then pin
  it in the Phase-3 contract doc.
- **Read-idle watchdog:** a configurable timeout so a capturer that is alive but
  silent (no EOF ever fires — `base_input.py:237`) surfaces an `ErrorFrame` and
  ends the branch rather than hanging.
- **Backpressure policy (implement + test, but keep it configurable — see Open
  Question 4):** bounded read buffer; default policy drop-oldest with a logged
  WARNING that includes the queue depth (realtime audio > completeness). Stamp each
  frame with the capturer's monotonic sequence number so a drop is observable and
  `me`/`them` drift is measurable. Do **not** treat drop-oldest as a frozen
  invariant until the drift comparison in OQ4 is run.
- Tests feed the socket from a pure-Python writer (a recorded PCM fixture) — **no
  native code** — and assert:
  - the transport emits `InputAudioRawFrame`s with the right rate/width/channels,
    and that frames actually **surface through the base** (guards the silent
    `audio_in_enabled=False` no-op);
  - **start ordering**: a reader that pushes immediately after connect does not
    observe a `None` queue (readiness precedes the first `push_audio_frame`);
  - **teardown**: `EndFrame` *and* `CancelFrame` each cancel `self._read_task` and
    close the socket (no leaked task);
  - **endianness**: PCM16 **LE** bytes round-trip to the expected samples;
  - **handshake validation** (once the header is adopted): a valid header is
    accepted; a header with a mismatched rate/width/channels or unknown version is
    rejected loudly (`ErrorFrame` / refuse-to-start), not silently coerced;
  - **backpressure**: a writer faster than the consumer (or a stalled consumer)
    caps memory and drops-oldest with the WARNING, rather than growing unbounded;
  - **read-idle**: a connected-but-silent writer trips the idle watchdog and
    surfaces an `ErrorFrame` rather than hanging;
  - clean EOF handling: socket close surfaces an `ErrorFrame` and ends the branch
    (no self-reconnect — the supervisor owns restart; see Requirements).

### Phase 2: `UnixSocketAudioTransport` wrapper + `AUDIO_SOURCE` wiring in `dual.py`

**Impl files:** `src/onoats/transports/socket_audio.py`, `src/onoats/dual.py`, `src/onoats/config/__init__.py`
**Test files:** `tests/test_dual_socket_source.py`
**Test command:** `uv run pytest tests/test_dual_socket_source.py -v`

- `UnixSocketAudioTransport(BaseTransport)` with `.input()` → the Phase-1 input
  transport. Per-branch socket paths from env/config.
- In `dual.py`, branch on `AUDIO_SOURCE`: `portaudio` (today,
  `LocalAudioTransport`) | `socket` (two `UnixSocketAudioTransport`). Keep
  everything downstream (`_build_dual_pipeline`) untouched.
  - **The branch must also short-circuit PortAudio device resolution, not just
    the transport build.** Today `select_dual_input_devices(...)` and the
    `cfg.mic_device/system_device` lookups run *unconditionally upstream* of the
    `LocalAudioTransport(...)` construction (`dual.py:219-308`). For `socket` mode
    that PyAudio enumeration must be skipped entirely, or socket mode still
    imports/invokes PortAudio — violating the Review-Focus "no PortAudio on the
    socket path" mirror of the no-native assertion.
- **Never-mix guard:** refuse to start if the two sockets resolve to the same
  path — compare `Path(mic_socket).expanduser().resolve()` against
  `Path(system_socket).expanduser().resolve()`, not the raw strings, so a symlink
  or relative-path alias can't collapse both branches onto one socket.
- Tests: (a) drive `_build_dual_pipeline` with two socket transports fed by two
  fixtures; assert mic-socket audio only ever exits tagged `me`, system-socket
  only `them` (the keystone invariant); (b) assert the negative guard — both
  identical paths **and** a symlink/relative alias of one onto the other refuse to
  start *before* the pipeline runs; (c) assert `AUDIO_SOURCE=socket` neither
  imports nor calls the PortAudio device-enumeration path.

### Phase 3: Capturer↔recorder lifecycle (CLI supervisor) + wire-contract doc

**Impl files:** `src/onoats/cli.py` (supervisor for `AUDIO_SOURCE=socket`), `docs/audio-socket-contract.md` (new)
**Test files:** `tests/test_socket_supervisor.py`
**Test command:** `uv run pytest tests/test_socket_supervisor.py -v`

- A supervisor (used when `onoats bot` runs with `AUDIO_SOURCE=socket`): create a
  **private, supervisor-owned socket directory** (0700, not a shared `/tmp` path —
  forecloses symlink aliasing of the two branch sockets), spawn the capturer (path
  via `ONOATS_CAPTURER_BIN`) with a fresh **generation nonce**, wait for both
  sockets to appear, start the recorder; on shutdown, stop the recorder then the
  capturer; on capturer death, tear down cleanly (session still flushes + rotates).
  The supervisor owns the capturer lifecycle; the transport does not self-reconnect
  (see Requirements). On restart, a new nonce + fresh socket files invalidate any
  stale fd from the previous generation.
- **Define "fail loud" as a testable observable** so the acceptance criteria are
  checkable: for each failure path (capturer crash, permission denied, slow
  reader) the recorder MUST surface an `ErrorFrame` on the affected branch **AND**
  the supervisor MUST exit non-zero **AND** emit a WARNING/ERROR log line — and
  the partial session MUST still rotate (no hang). Mirror the existing STT-client
  `ErrorFrame` ergonomics for the frame shape.
- Document the wire contract (framing, format, handshake, endianness, backpressure
  policy, version) in `docs/audio-socket-contract.md` the way the queue contract
  is documented.
- Test the supervisor against a **fake capturer** (a Python script that writes the
  fixtures to the sockets) — still no Swift needed. Test paths:
  - **crash:** fake capturer writes N frames then dies / abruptly closes the socket
    → assert (per the "fail loud" definition) the branch surfaces an `ErrorFrame`,
    the recorder rotates the partial session into `pending/` (the existing
    `flush_and_rotate` path), the supervisor exits non-zero, and the process does
    not hang;
  - **hung-but-alive:** fake capturer connects but writes nothing → the read-idle
    watchdog fires, surfaces an `ErrorFrame`, and the session rotates (does not
    hang waiting for EOF that never comes);
  - **stale socket / restart:** a leftover socket file from a prior generation does
    not let a new transport latch onto it — the nonce mismatch is rejected.

### Phase 4: Swift CoreAudio / ScreenCaptureKit capturer  *(macOS native)*

**Impl files:** `native/onoats-capturer/` (Swift package), build integration
**Test files:** native unit tests + a manual macOS smoke checklist
**Test command:** _(native; document the `swift build` + manual capture check — not in Python CI)_

- Swift binary: mic via CoreAudio input; system audio via ScreenCaptureKit (macOS
  13+) or the CoreAudio process-tap API (macOS 14.4+) — **decide the minimum
  macOS target in review** (it gates the API). Resample each to 16 kHz PCM16 mono;
  write framed PCM to the two sockets per the Phase-3 contract.
- Permission handling (Screen Recording / audio capture) with clear denials.
- **Manual macOS smoke checklist** (can't run in headless CI): capture me+them;
  verify two independent STT streams; verify `source` tags; exercise the
  deny-permission path; and an **A/B parity check** — record the same source via
  both the PortAudio and socket paths and diff the resulting `me`/`them` queue
  files for tag parity (this is what the "no BlackHole, same queue files"
  acceptance criterion actually asserts).
- **Open**: how the Swift binary is built, signed, and **distributed** inside a
  `pip`/`uv`-installed Python package (a pip wheel can't easily ship a notarized
  mac binary). Options to weigh: separate Homebrew formula, a `onoats capturer
  install` downloader, or build-from-source via `[macos]` extra. This is the
  biggest unknown — see Open Questions.

### Phase 5: macOS menu-bar launcher  *(macOS native; independent of 1–4)*

**Impl files:** `src/onoats/runtime.py` / `src/onoats/dual.py` (recorder *writes* the status file on startup + shutdown), `src/onoats/cli.py` (rewire `onoats status` to read it, pid file as backstop), `native/onoats-menubar/` (Swift, e.g. SwiftUI `MenuBarExtra`), status-file reader
**Test files:** manual macOS checklist + a status-file schema round-trip test (Python side)
**Test command:** `uv run pytest tests/test_status_file.py -v`

- Status-bar app: Start / Stop / Flush (shell out to `onoats {bot,flush}` or
  signal the pid), running indicator, **profiles** (device + STT config sets),
  device pickers (reuse `onoats devices`).
- A **status file** (JSON under the state dir) so the GUI and `onoats status`
  share one truth. This has **two sides, and the writer must be built — it does
  not exist today**:
  - **Producer (recorder):** `runtime.py`/`dual.py` write the status file on
    startup and on shutdown/rotation. This is the load-bearing task; the schema
    round-trip test alone does not deliver it.
  - **CLI rewire:** `onoats status` reads the status file instead of deriving
    state from the pid file alone (`cli.py:187-207` today), with the pid file kept
    as the liveness backstop so a stale status file can't report a dead recorder
    as live.
  - **Consumer (menu bar):** reads the same file. Define + **test the schema on
    the Python side** (`tests/test_status_file.py`) even though the GUI consumer
    is Swift.
- Can ship on top of the PortAudio path; gains "no BlackHole" once Phase 4 lands.

### Phase 6: Retire BlackHole from the default macOS story + docs/packaging

**Impl files:** `README.md` (cross-platform matrix), `[macos]` extra / install docs
**Test files:** n/a
**Test command:** n/a

- Make `AUDIO_SOURCE=socket` the documented macOS default once the capturer is
  stable; keep PortAudio+BlackHole as a fallback. Update the cross-platform
  matrix; document the capturer install + permissions.

## Technical Specifications

### Pipecat seam (verified, pipecat 1.x)

```python
# src/onoats/transports/socket_audio.py  (sketch — verified against pipecat 1.3.0)
from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

class UnixSocketAudioInputTransport(BaseInputTransport):
    # NOTE: override start() (the reference hook), NOT start_audio_in_streaming
    # (a pass-stub the reference never uses). set_transport_ready() is what
    # triggers the base's audio drain task, and only when audio_in_enabled=True.
    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect_and_handshake(frame)   # 1. connect + validate header
        await self.set_transport_ready(frame)       # 2. creates _audio_in_queue
        self._read_task = self.create_task(         # 3. ONLY THEN spawn the reader
            self._read_loop())                      #    (else push_audio_frame hits
                                                    #     a None queue, base_input.py:170)

    async def _read_loop(self):
        # read framed PCM16; on each frame:
        #   await self.push_audio_frame(
        #       InputAudioRawFrame(audio=pcm, sample_rate=16000, num_channels=1))
        # EOF or read-idle timeout -> push ErrorFrame, end the branch.
        ...

    async def stop(self, frame):    # framework cancels only ITS drain task,
        await self._teardown()      # not self._read_task -> we must do it
        await super().stop(frame)

    async def cancel(self, frame):
        await self._teardown()
        await super().cancel(frame)

    async def cleanup(self):
        await self._teardown()
        await super().cleanup()

    async def _teardown(self):
        # close the socket; cancel-and-await self._read_task if set.
        ...

class UnixSocketAudioTransport(BaseTransport):
    # built with TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    def input(self):  # what dual.py calls
        return self._input  # a UnixSocketAudioInputTransport
    def output(self):  # input-only transport
        raise NotImplementedError("UnixSocketAudioTransport is input-only")
```

`dual.py` swap site (today builds `LocalAudioTransport(LocalAudioTransportParams(
input_device_index=…))`): branch on `AUDIO_SOURCE`, construct two
`UnixSocketAudioTransport`s instead, leave `_build_dual_pipeline` unchanged.

### Wire format (Phase 1 starting point — finalize in review)

- PCM16 LE, 16 kHz, **mono**, one socket per branch.
- Framing: **prefer length-prefixed** over fixed-size. A unix *stream* socket has
  no message boundaries, so a fixed-size reader must `readexactly(N)` and the
  writer must handle partial writes; length-prefixing makes a partial-write desync
  impossible to silently ignore. If fixed-size is chosen anyway, match the
  reference's chunking — `LocalAudioInputTransport` uses `int(sample_rate/100)*2`
  samples = **640 bytes** for 20 ms PCM16/16 kHz mono (`audio.py:68-93`; read the
  actual value from pipecat rather than assuming) and the reader MUST use
  `readexactly(640)`.
- Handshake: a leading 1-line JSON header
  `{"rate":16000,"width":2,"channels":1,"v":1,"nonce":…}` lets the transport
  validate/negotiate format and carry the generation nonce before the stream —
  recommended for forward-compat, decide in review.
- Each frame carries explicit timing metadata in the length-prefixed payload:
  `seq` (monotonic per stream), `captured_monotonic_ns`, and `pcm` bytes. The
  transport copies these into `InputAudioRawFrame.metadata` (and sets `pts` from
  `captured_monotonic_ns` if downstream processors use Pipecat PTS), so drops are
  observable and `me`/`them` drift is measurable after the Pipecat frame is
  created.
- Backpressure: bounded buffer; **default** policy drop-oldest with a logged
  WARNING that includes queue depth (audio realtime > completeness). **Keep the
  policy configurable, not frozen** — drop-oldest vs drop-newest vs bounded-block
  is decided by the OQ4 drift comparison, not pinned here.

### Invariant (do not violate)

Dual-stream, **two** STT sessions, `me` and `them` **never mixed**. One capturer
source → one socket → one transport instance → one branch → one STT → one
`SourceTagger`. The JSONL `source` stays the canonical `me`/`them` enum (the
frozen queue-contract value koda's classifier keys on).

## Dependencies

- Python: no new runtime deps for the socket transport (stdlib sockets + pipecat,
  already present). Tests add only fixtures.
- Native: a Swift toolchain (build-time); ScreenCaptureKit / CoreAudio (system
  frameworks). Distribution of the built binary is an open question (below).
- Keep all native bits behind the `[macos]` extra / optional install; the baseline
  `pip install onoats` and CI stay native-free.

## Testing Notes

- **CI covers Phases 1–3 fully** with pure-Python socket feeders — no Swift, no
  audio hardware, no permissions. This is the point of sequencing the transport
  before the capturer.
- Phase 4/5 native work needs a **manual macOS smoke checklist** (capture me+them,
  verify two STT streams, verify `source` tags, deny-permission path) — document
  it; it can't run in headless CI.
- Invariant test (Phase 2) is the keystone: socket→branch→tag isolation.

## Acceptance Criteria

- [ ] `AUDIO_SOURCE=socket` records a real dual session on macOS with **no
      BlackHole installed**, producing the same `me`/`them`-tagged queue files as
      the PortAudio path.
- [ ] The socket transport + `dual.py` wiring import and unit-test with **no
      native binary present**; CI is native-free.
- [ ] Never-mix invariant proven by test (mic socket ⇒ only `me`, system socket
      ⇒ only `them`), **plus** the negative guard (same socket path ⇒ refuse to
      start) and a test that `AUDIO_SOURCE=socket` touches no PortAudio path.
- [ ] Wire-format contract documented + round-trip tested, including **endianness
      (LE), handshake validation, and version-mismatch rejection** — not just a
      happy-path rate/width/channels round-trip.
- [ ] **Backpressure proven by test**: a faster-than-consumer writer caps memory
      under the configured policy (default drop-oldest) with a queue-depth WARNING
      (no unbounded growth); frames carry a monotonic sequence number.
- [ ] **Reader lifecycle proven by test**: readiness precedes the first
      `push_audio_frame` (no `None`-queue race), and `EndFrame`/`CancelFrame` each
      cancel the reader task and close the socket (no leak).
- [ ] **Hung-but-alive proven by test**: a connected-but-silent capturer trips the
      read-idle watchdog and rotates the session rather than hanging.
- [ ] Capturer crash / permission-denied / slow-reader paths **fail loud** —
      defined as `ErrorFrame` on the branch AND non-zero supervisor exit AND a
      WARNING/ERROR log line — and leave the queue consistent (partial session
      still rotates, no hang).
- [ ] Menu-bar app starts/stops/flushes via the CLI, shows status from the shared
      status file, and switches profiles — with `onoats status` and the GUI in
      agreement.
- [ ] README cross-platform matrix updated; BlackHole demoted to fallback on mac.

## Open Questions (resolve before/within `/review-plan`)

1. **Minimum macOS target** — ScreenCaptureKit audio (13+) vs CoreAudio
   process-tap (14.4+). Picks the system-audio API and the supported-OS floor.
2. **Native binary distribution** — how does a `pip`/`uv`-installed Python package
   ship a notarized Swift capturer + menu-bar app? (Homebrew formula? `onoats
   capturer install` downloader? build-from-source under `[macos]`?) **Biggest
   unknown; likely a small sub-plan of its own.**
3. **Capturer lifecycle owner** — CLI supervisor vs menu-bar app vs a launchd
   agent (mirrors the stt_server LaunchAgent pattern). Affects restart/crash
   semantics. **Partially resolved:** whichever layer is the supervisor *owns the
   capturer process lifecycle*, and the transport treats socket EOF as
   terminal-for-this-session (no self-reconnect) — so the two never race (see
   Requirements). Still open: which layer is the supervisor in CLI vs GUI mode.
4. **Backpressure policy** — drop-oldest vs bounded-block vs adaptive, and how to
   keep `me`/`them` timestamps from drifting under load. **Default, not frozen:**
   Phase 1 ships a bounded buffer with a *configurable* policy defaulting to
   drop-oldest + WARNING (queue depth logged), and frames carry a monotonic
   sequence number so drift is measurable. **Decide the final policy (drop-oldest
   vs drop-newest vs bounded-block) after a short STT-artifact + drift comparison
   test — do not treat drop-oldest as a frozen invariant** (it would otherwise
   contradict the "keep timestamps from drifting" goal of this very question).
5. **Echo/duplication** — when capturing system output, does the user's own voice
   (played back through speakers + re-captured) leak into `them`? May need the
   process-tap to exclude onoats's own output, or AEC. Validate during Phase 4
   smoke.

## Notes for whoever picks this up

- onoats `main` was at `7f33e72` when this was written (koda pins `023e0e0`; the
  pin is independent of this doc). **These are authoring-time snapshots — re-verify
  the current `main` HEAD and koda pin when the work is actually picked up; do not
  treat them as live.**
- There was **uncommitted staged work** in onoats's working tree at authoring time
  touching `_vendor/pid.py` / `cli.py` / `test_cli.py` (looked flush/pid-related)
  — this plan file was left **untracked and uncommitted** to avoid entangling with
  it. Commit it on its own feature branch.
- Related koda follow-up (separate): upstream koda's `./koda flush` cmdline-vs-`ps`
  identity verification into `onoats flush` (today it validates only the pid-file
  marker before `SIGUSR1`), after which koda can revert to a thin `onoats flush`
  pass-through. See koda PR #104.

<!-- reviewed: 2026-06-08 @ aa76f29d23b0a4133925d787e462ced0c25f4027 -->
