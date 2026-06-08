# Task: macOS Menu-bar Launcher + CoreAudio Socket Audio Transport (retire BlackHole)

**Status**: Proposed (draft — not yet `/review-plan`'d)
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

### The pipecat transport seam (verified via pipecat-context-hub, pipecat 1.x)

- `pipecat.transports.base_input.BaseInputTransport(FrameProcessor)` is the base
  class for all input transports. Subclasses:
  - override **`async start_audio_in_streaming(self)`** to begin transport-specific
    capture, and
  - call **`await self.push_audio_frame(InputAudioRawFrame(audio=…, sample_rate=…, num_channels=…))`**
    to feed PCM into the pipeline (gated internally on `audio_in_enabled`).
- `pipecat.transports.local.audio.LocalAudioInputTransport(BaseInputTransport)` is
  the **reference implementation** (PyAudio → `InputAudioRawFrame`). The new
  socket transport mirrors it, replacing the PyAudio read loop with a socket read
  loop.
- `LocalAudioTransport.input()` returns the `LocalAudioInputTransport`
  (a `FrameProcessor`) that `dual.py` puts at the head of each arm. The new
  `UnixSocketAudioTransport.input()` returns a `UnixSocketAudioInputTransport`.

So the Python seam is **confirmed**, not assumed: subclass `BaseInputTransport`,
read PCM16/16 kHz/mono off a unix socket, `push_audio_frame`. One instance per
branch.

### Why retire BlackHole

BlackHole is a virtual-audio-driver kext-adjacent install the user must set up
out-of-band, and "them" capture silently fails if the system output isn't routed
through it (a documented koda failure mode). ScreenCaptureKit (macOS 13+) and the
CoreAudio process-tap API (macOS 14.4+) can capture system/process audio
**without** a virtual device, removing that setup step and failure class.

## Requirements

- **Socket transport (Python, CI-testable without native code):**
  - A `UnixSocketAudioInputTransport(BaseInputTransport)` that reads framed PCM16
    LE mono @ 16 kHz from a unix socket and `push_audio_frame`s `InputAudioRawFrame`s.
  - A `UnixSocketAudioTransport(BaseTransport)` wrapper exposing `.input()` so it
    drops into `_build_dual_pipeline` unchanged.
  - `AUDIO_SOURCE` selector (`portaudio` default | `socket`) wired in `dual.py`’s
    transport construction (the only site that builds `LocalAudioTransport`), with
    per-branch socket paths (`ONOATS_MIC_SOCKET` / `ONOATS_SYSTEM_SOCKET`, or
    `[audio] mic_socket/system_socket` in `config.toml`).
  - **Never-mix guarantee preserved**: exactly one socket per branch; the
    transport refuses to start if both branches resolve to the same socket path.
  - Clean teardown + reconnect: if the capturer dies / the socket closes, the
    branch surfaces an `ErrorFrame` and reconnects on the next connection (mirror
    the STT-client reconnect ergonomics already in `runtime.py`).
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
    source of truth.
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

> Phases 1–3 are the **portable, CI-testable core** and the recommended first
> milestone. Phase 4 is the native capturer (macOS, harder to CI). Phase 5 (menu
> bar) is independent and can be done before or after 4.

### Phase 1: `UnixSocketAudioInputTransport` + wire framing  *(Python)*

**Impl files:** `src/onoats/transports/socket_audio.py` (new), `src/onoats/transports/__init__.py`
**Test files:** `tests/test_socket_audio_transport.py`
**Test command:** `uv run pytest tests/test_socket_audio_transport.py -v`

- Subclass `BaseInputTransport`; in `start_audio_in_streaming()` open the unix
  socket, read fixed-size PCM16 frames, `push_audio_frame(InputAudioRawFrame(...))`.
  Mirror `LocalAudioInputTransport`'s frame sizing / sample-rate params.
- Define the wire framing (start with raw length-prefixed PCM16 LE mono @ 16 kHz;
  a 1-line JSON handshake header for format negotiation is the forward-compatible
  option — decide in review).
- Tests feed the socket from a pure-Python writer (a recorded PCM fixture) — **no
  native code** — and assert the transport emits `InputAudioRawFrame`s with the
  right rate/width/channels and clean EOF/reconnect handling.

### Phase 2: `UnixSocketAudioTransport` wrapper + `AUDIO_SOURCE` wiring in `dual.py`

**Impl files:** `src/onoats/transports/socket_audio.py`, `src/onoats/dual.py`, `src/onoats/config/__init__.py`
**Test files:** `tests/test_dual_socket_source.py`
**Test command:** `uv run pytest tests/test_dual_socket_source.py -v`

- `UnixSocketAudioTransport(BaseTransport)` with `.input()` → the Phase-1 input
  transport. Per-branch socket paths from env/config.
- In `dual.py`, branch the transport construction on `AUDIO_SOURCE`: `portaudio`
  (today, `LocalAudioTransport`) | `socket` (two `UnixSocketAudioTransport`). Keep
  everything downstream (`_build_dual_pipeline`) untouched.
- **Never-mix guard:** refuse to start if `mic_socket == system_socket`.
- Test: drive `_build_dual_pipeline` with two socket transports fed by two
  fixtures; assert mic-socket audio only ever exits tagged `me`, system-socket
  only `them` (the invariant).

### Phase 3: Capturer↔recorder lifecycle (CLI supervisor) + wire-contract doc

**Impl files:** `src/onoats/cli.py` (supervisor for `AUDIO_SOURCE=socket`), `docs/audio-socket-contract.md` (new)
**Test files:** `tests/test_socket_supervisor.py`
**Test command:** `uv run pytest tests/test_socket_supervisor.py -v`

- A supervisor (used when `onoats bot` runs with `AUDIO_SOURCE=socket`): spawn the
  capturer (path via `ONOATS_CAPTURER_BIN`), wait for both sockets to appear,
  start the recorder; on shutdown, stop the recorder then the capturer; on
  capturer death, tear down cleanly (session still flushes + rotates).
- Document the wire contract (framing, format, handshake, backpressure policy)
  the way the queue contract is documented.
- Test the supervisor against a **fake capturer** (a Python script that writes the
  fixtures to the sockets) — still no Swift needed.

### Phase 4: Swift CoreAudio / ScreenCaptureKit capturer  *(macOS native)*

**Impl files:** `native/onoats-capturer/` (Swift package), build integration
**Test files:** native unit tests + a manual macOS smoke checklist
**Test command:** _(native; document the `swift build` + manual capture check — not in Python CI)_

- Swift binary: mic via CoreAudio input; system audio via ScreenCaptureKit (macOS
  13+) or the CoreAudio process-tap API (macOS 14.4+) — **decide the minimum
  macOS target in review** (it gates the API). Resample each to 16 kHz PCM16 mono;
  write framed PCM to the two sockets per the Phase-3 contract.
- Permission handling (Screen Recording / audio capture) with clear denials.
- **Open**: how the Swift binary is built, signed, and **distributed** inside a
  `pip`/`uv`-installed Python package (a pip wheel can't easily ship a notarized
  mac binary). Options to weigh: separate Homebrew formula, a `onoats capturer
  install` downloader, or build-from-source via `[macos]` extra. This is the
  biggest unknown — see Open Questions.

### Phase 5: macOS menu-bar launcher  *(macOS native; independent of 1–4)*

**Impl files:** `native/onoats-menubar/` (Swift, e.g. SwiftUI `MenuBarExtra`), status-file reader
**Test files:** manual macOS checklist + a status-file schema round-trip test (Python side)
**Test command:** `uv run pytest tests/test_status_file.py -v`

- Status-bar app: Start / Stop / Flush (shell out to `onoats {bot,flush}` or
  signal the pid), running indicator, **profiles** (device + STT config sets),
  device pickers (reuse `onoats devices`).
- A **status file** (JSON under the state dir) written by the recorder and read by
  the menu bar, so GUI and `onoats status` share one truth. Define + test its
  schema on the Python side even though the consumer is Swift.
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
# src/onoats/transports/socket_audio.py  (sketch)
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

class UnixSocketAudioInputTransport(BaseInputTransport):
    async def start_audio_in_streaming(self):
        # connect self._socket_path; loop: read one PCM16 frame; then
        await self.push_audio_frame(
            InputAudioRawFrame(audio=pcm_bytes, sample_rate=16000, num_channels=1)
        )

class UnixSocketAudioTransport(BaseTransport):
    def input(self):  # what dual.py calls
        return self._input  # a UnixSocketAudioInputTransport
```

`dual.py` swap site (today builds `LocalAudioTransport(LocalAudioTransportParams(
input_device_index=…))`): branch on `AUDIO_SOURCE`, construct two
`UnixSocketAudioTransport`s instead, leave `_build_dual_pipeline` unchanged.

### Wire format (Phase 1 starting point — finalize in review)

- PCM16 LE, 16 kHz, **mono**, one socket per branch.
- Framing: fixed-size frames matching pipecat's audio-in chunking, OR length-
  prefixed; a leading 1-line JSON handshake (`{"rate":16000,"width":2,"channels":1}`)
  lets the transport validate/negotiate before the stream — recommended for
  forward-compat, decide in review.
- Backpressure: bounded buffer; on overflow drop oldest with a logged WARNING
  (audio realtime > completeness) — confirm policy in review.

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
      ⇒ only `them`).
- [ ] Wire-format contract documented + round-trip tested.
- [ ] Capturer crash / permission-denied / slow-reader paths fail loud and leave
      the queue consistent (session still rotates).
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
   semantics.
4. **Backpressure policy** — drop-oldest vs bounded-block vs adaptive, and how to
   keep `me`/`them` timestamps from drifting under load.
5. **Echo/duplication** — when capturing system output, does the user's own voice
   (played back through speakers + re-captured) leak into `them`? May need the
   process-tap to exclude onoats's own output, or AEC. Validate during Phase 4
   smoke.

## Notes for whoever picks this up

- onoats `main` was at `7f33e72` when this was written (koda pins `023e0e0`; the
  pin is independent of this doc).
- There was **uncommitted staged work** in onoats's working tree at authoring time
  touching `_vendor/pid.py` / `cli.py` / `test_cli.py` (looked flush/pid-related)
  — this plan file was left **untracked and uncommitted** to avoid entangling with
  it. Commit it on its own feature branch.
- Related koda follow-up (separate): upstream koda's `./koda flush` cmdline-vs-`ps`
  identity verification into `onoats flush` (today it validates only the pid-file
  marker before `SIGUSR1`), after which koda can revert to a thin `onoats flush`
  pass-through. See koda PR #104.
