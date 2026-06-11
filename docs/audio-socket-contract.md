# onoats audio-socket wire contract (v1)

Status: **versioned contract — pinned**. This is the capturer↔recorder framing for
`AUDIO_SOURCE=socket`. Changing any value here is a wire-version bump (`v` in the
handshake) — bump `WIRE_VERSION` in `src/onoats/transports/socket_audio.py` and
this doc together. The transport refuses to start on a version it does not speak,
so a mismatched capturer fails loud rather than emitting silently-misframed audio.

This is the third versioned contract in the system, alongside the JSONL queue
contract (the `me`/`them` `source` enum — see `processors/source_tagger.py`) and
the status-file schema (`src/onoats/status.py`, `STATUS_SCHEMA_VERSION` — shipped
in Phase 5a; see [Status-file schema](#status-file-schema-v2) below). The
constants below are read
from `src/onoats/transports/socket_audio.py`; that module is the source of truth
and this doc mirrors it.

## Topology / invariant

Dual-stream, **two** STT sessions, `me` and `them` **never mixed**:

```
one capturer source → one unix socket → one transport instance → one branch
                    → one STT session → one SourceTagger
```

- `me` (mic) → `mic.sock` → `UnixSocketAudioInputTransport` → STT #1 → `source="me"`
- `them` (system) → `system.sock` → `UnixSocketAudioInputTransport` → STT #2 → `source="them"`

Exactly **one socket per branch**. The capturer MUST NOT interleave the two
streams onto one socket, and the recorder fans no socket to both branches.
Collapsing the two onto one socket silently destroys downstream speaker
attribution (the classifier keys on the canonical `me`/`them` enum).

## Transport (socket type)

- **Unix domain `SOCK_STREAM`** socket, one path per branch.
- The **capturer is the server** (it `bind()` + `listen()`s and creates the socket
  file); the **recorder transport is the client** (`asyncio.open_unix_connection`).
  The recorder waits for the socket file to appear, then connects.
- The recorder reads only; it never writes audio back. There is no output side.

## Format (PCM)

| Field         | Value        | Source constant                     |
|---------------|--------------|-------------------------------------|
| Encoding      | PCM signed   | —                                   |
| Sample width  | **2 bytes** (16-bit) | `DEFAULT_SAMPLE_WIDTH = 2`   |
| Endianness    | **little-endian (LE)** | (PCM16 LE)                |
| Sample rate   | **16000 Hz** | `DEFAULT_SAMPLE_RATE = 16000`       |
| Channels      | **1 (mono)** | `DEFAULT_CHANNELS = 1`              |

The transport validates `rate`/`width`/`channels` from the handshake against
these and refuses to start on a mismatch — never coerces.

A PCM payload MUST be a whole number of samples (`len(pcm) % (width*channels) == 0`).
An odd byte count is treated as a desync/framing error (loud `ErrorFrame`), not
coerced.

## Handshake (1-line JSON header)

Before any frames, the capturer writes **exactly one line** of UTF-8 JSON
terminated by `\n`:

```json
{"rate":16000,"width":2,"channels":1,"v":1,"nonce":"<hex>"}
```

| Key        | Type   | Meaning                                                        |
|------------|--------|---------------------------------------------------------------|
| `rate`     | int    | Sample rate; MUST equal the transport's expected rate (16000).|
| `width`    | int    | Sample width in bytes; MUST equal 2.                          |
| `channels` | int    | Channel count; MUST equal 1.                                  |
| `v`        | int    | Wire version; MUST equal `WIRE_VERSION` (currently **1**).    |
| `nonce`    | string | Generation token (see below). May be absent/`null` in v1.     |

The transport (`parse_handshake`) **refuses to start loudly** — raises
`SocketHandshakeError`, surfaced as refuse-to-start / `ErrorFrame` — on:
malformed/non-UTF-8 JSON, a non-object, an unknown `v`, a `rate`/`width`/`channels`
mismatch, or a non-string `nonce`. Nothing is silently coerced.

### Generation nonce (stale-socket defense)

Each supervisor launch mints a fresh nonce (`secrets.token_hex(16)`) and passes it
to the capturer. The capturer MUST echo it in the handshake's `nonce`.

The supervisor's **primary** stale-socket defense is structural: it mints a
**fresh private 0700 socket directory per generation**, so a leftover socket from
a previous generation lives at a path the new recorder never references and is
unreachable.

The nonce is the **end-to-end** belt-and-suspenders check on top of that. The
supervisor exports `ONOATS_CAPTURER_NONCE` into the recorder's environment; the
recorder resolves it via `OnoatsConfig.capturer_nonce` and threads it into both
branch transports as `expected_nonce`. A capturer whose handshake omits the nonce
or presents the wrong one is rejected with `SocketHandshakeError` (refuse-to-start
on the affected branch). When socket mode is driven **without** the supervisor
(no `ONOATS_CAPTURER_NONCE`), `expected_nonce` is `None` and the nonce is not
gated — only the wire-format fields are validated.

## Framing (length-prefixed)

After the handshake line, each frame is:

```
[ 4-byte big-endian unsigned length N ][ N bytes of JSON payload ]
```

- **Length prefix:** `LENGTH_PREFIX_BYTES = 4`, **big-endian**, unsigned. (Note the
  length prefix is big-endian / network order; the PCM *samples* inside the
  payload are little-endian — these are independent and intentional.)
- Length-prefixing (not fixed-size frames) is deliberate: a unix *stream* socket
  has no message boundaries, so a fixed-size reader silently desyncs on a partial
  write. The prefix makes a partial-write desync impossible to ignore.
- `N` MUST be in `1..MAX_FRAME_PAYLOAD_BYTES` (`MAX_FRAME_PAYLOAD_BYTES = 1 MiB`,
  `1 << 20`). A length of 0, negative, or over the ceiling is a desync → loud
  `ErrorFrame`. A guard against a runaway prefix.

### Frame payload (JSON object)

```json
{"seq": 0, "captured_monotonic_ns": 123456789, "pcm_b64": "<base64 PCM16 LE>"}
```

| Key                     | Type   | Meaning                                                    |
|-------------------------|--------|------------------------------------------------------------|
| `seq`                   | int    | Monotonic per-stream sequence number. Lets a drop be observed and `me`/`them` drift be measured. |
| `captured_monotonic_ns` | int    | Capture timestamp (capturer's monotonic clock, ns).        |
| `pcm_b64`               | string | base64 of the raw PCM16 LE mono samples for this frame.     |

The transport copies `seq` → `metadata["socket_seq"]`,
`captured_monotonic_ns` → `metadata["captured_monotonic_ns"]`, and sets the
Pipecat frame `pts = captured_monotonic_ns`, then pushes an
`InputAudioRawFrame(audio=pcm, sample_rate=16000, num_channels=1)`.

### Frame size

The reference chunking is **640 bytes** of PCM per 20 ms frame at 16 kHz
(`frame_size_bytes(16000) == int(16000/100)*2 samples * 2 bytes = 640`), mirroring
pipecat's `LocalAudioInputTransport`. The capturer SHOULD emit ~20 ms frames;
the transport does not hard-require 640 — any whole-sample payload within the
1 MiB ceiling is accepted — but matching the reference keeps VAD/STT cadence
identical to the PortAudio path.

## Backpressure policy

The recorder maintains a **bounded staging buffer** between the socket reader and
pipecat's (unbounded) audio queue. Bounding it caps memory under a
faster-than-consumer writer.

**The staging cap alone is not sufficient** — pipecat's `_audio_in_queue` is an
unbounded `asyncio.Queue`, so a pump that drained staging into it eagerly would let
a *stalled downstream consumer* (slow/blocked VAD/STT) grow the base queue without
limit while the staging buffer stays empty and the drop policy never fires. The pump
therefore **gates on the base queue's depth**: it only forwards a staged frame when
`_audio_in_queue` holds fewer than `max_buffered_frames`. Under a stall the pump
parks, the staging buffer fills, and the drop/`block` policy engages — so the frame
cap bounds **both** queues end-to-end. Consequence: worst-case buffered audio under a
sustained stall is ~2× `max_buffered_frames` (staging + base), and — since the base
queue exposes no per-byte hook — its bytes are bounded only by frame count × the
1 MiB per-frame ceiling (the `max_buffered_bytes` cap below governs staging only).

- **Default policy: `drop-oldest`** (`BackpressurePolicy.DROP_OLDEST`). On overflow
  the oldest staged frame is dropped and a **WARNING** is logged that includes the
  buffer depth and the dropped `seq` and a running total — so drops are observable.
  Realtime audio favours freshness over completeness.
- **Configurable, not frozen.** `drop-newest` and bounded-`block` are also
  implemented. The *final* choice (drop-oldest vs drop-newest vs bounded-block) is
  deferred to the OQ4 STT-artifact + drift comparison; do not treat drop-oldest as
  a frozen invariant.
- Default bound: `max_buffered_frames = 200` frames (~4 s at 20 ms/frame).
- Second bound: `DEFAULT_MAX_BUFFERED_BYTES = 16 MiB` total staged bytes, enforced
  *alongside* the frame count. Without it, 200 frames at the 1 MiB per-frame
  ceiling could stage ~200 MiB; the byte cap keeps the footprint bounded for any
  frame size (at the 640-byte reference frame the count cap always bites first).
- The monotonic `seq` is what makes a drop and any `me`/`them` drift measurable.

## Read-idle watchdog

EOF is not the only failure: a capturer that is alive but silent never closes the
socket, so EOF never fires. The transport applies a **read-idle timeout** (default
`read_idle_timeout = 10.0 s`; `<= 0` disables it). If no frame arrives within the
window, the transport surfaces an `ErrorFrame` and ends the branch — the same
terminal-for-session outcome as EOF, so the session rotates instead of hanging.

## Termination semantics (terminal-for-session; no self-reconnect)

Any of: clean EOF (capturer closed the socket), a truncated/desynced frame, a
malformed payload, a handshake/version mismatch, or a read-idle timeout — is
**terminal for the session**. The transport surfaces an `ErrorFrame` downstream
(which is fatal → the pipeline is cancelled) and **does not self-reconnect**.

**The supervisor owns restart.** A fresh capturer + transport pair (a new
generation: new private dir, new nonce, new sockets) is what a restart
establishes. The transport never latches a live branch back onto a respawned
capturer — this is the single-lifecycle-owner rule that keeps the two layers from
racing.

## Capturer launch contract (supervisor → capturer)

When `AUDIO_SOURCE=socket`, `onoats bot` runs the supervisor
(`cli._run_socket_supervisor`). The supervisor:

1. mints a **private 0700 socket directory** under the system temp root (short
   path to stay under the macOS ~104-byte `AF_UNIX` limit) containing
   `mic.sock` and `system.sock`;
2. mints a fresh generation **nonce**;
3. exports `ONOATS_MIC_SOCKET` / `ONOATS_SYSTEM_SOCKET` (pointing at those
   sockets) for the recorder;
4. **spawns the binary named by `ONOATS_CAPTURER_BIN`** **in its own
   session/process group** (`start_new_session=True`, the portable `setsid`),
   passing the socket paths and nonce **both** ways (read whichever you prefer):

   - **argv:** `--mic-socket <path> --system-socket <path> --nonce <hex>`
   - **env:** `ONOATS_MIC_SOCKET`, `ONOATS_SYSTEM_SOCKET`, `ONOATS_CAPTURER_NONCE`

5. waits (bounded, default 10 s) for **both** socket files to appear, then runs
   the recorder;
6. on shutdown stops the recorder, then the capturer's **entire process group**
   (SIGTERM → bounded wait → SIGKILL); on capturer death tears down cleanly.

**Signal isolation.** Because the capturer is spawned in its **own
session/process group**, a terminal `Ctrl+C`/`SIGTERM` (delivered by the OS to
the whole foreground process group) is **NOT relayed to the capturer**. The
supervisor owns the capturer's lifecycle end to end: it stops the capturer
**explicitly** (SIGTERM → bounded wait → SIGKILL, via `_stop_capturer`) **after
the recorder finishes**, never via an inherited terminal signal. This is what
makes a graceful Ctrl+C a *recorder-finishes-first* event (rc=0) rather than a
spurious *capturer-died-mid-session* event (rc=1): the recorder always wins the
shutdown race because the OS cannot kill the capturer out from under it.

**Process-group teardown.** Because the capturer is a process-group leader
(`start_new_session=True`), its PGID equals its PID, and `_stop_capturer`
signals the **whole process group** (`os.killpg` by that PID), not just the
leader — so any helper/child the capturer spawned (a wrapper script, a CoreAudio
helper) is torn down with it. Signalling only the leader would orphan such a
descendant, leaving it holding the audio device after the supervisor reports
success and removes the socket dir. This holds on the **crash path** too: even
once the capturer leader has exited and been reaped, the kernel keeps the PGID
reserved while the group is non-empty, so the group sweep still reaches a
surviving child (the teardown does not give up just because the leader is gone).
On platforms without process groups the teardown falls back to a single-PID
signal.

> **Residual race (accepted, same-UID; NOT closed in Milestone B).** The group is targeted
> by the leader's PID (`os.killpg(pid, …)`). If, on the crash path, the leader is
> reaped **and** every surviving group member also exits before a `killpg` fires,
> the PGID is released and that PID can be recycled — so a late `SIGTERM`/`SIGKILL`
> could land on an unrelated **same-user** process group. The window is sub-second
> and same-UID only (no privilege crossing), and macOS lacks a race-free
> `pidfd`-style handle. Phase 4 should narrow it — e.g. gate the final `SIGKILL`
> sweep on a confirmed live group member, or use `pidfd_send_signal` on Linux.

**Environment (deny-by-default allowlist).** The capturer is launched with an
**explicit, allowlisted** environment — **not** a copy of the recorder's full
env. It receives ONLY:

- the socket paths + generation nonce
  (`ONOATS_MIC_SOCKET` / `ONOATS_SYSTEM_SOCKET` / `ONOATS_CAPTURER_NONCE`, always
  set); and
- a fixed runtime/OS allowlist needed to launch a native macOS/Linux process:
  `PATH`, `HOME`, `TMPDIR`, `TMP`, `TEMP`, `USER`, `LOGNAME`, `LANG`, `SHELL`,
  plus any present `LC_*` (locale) and `__CF*` (CoreFoundation) vars.

The **entire `DYLD_*` family is excluded** — it is a dynamic-loader injection
surface end to end: `DYLD_INSERT_LIBRARIES` (dylib injection),
`DYLD_LIBRARY_PATH` / `DYLD_FRAMEWORK_PATH` / `DYLD_FALLBACK_*` (planted-dylib
search-path redirection), `DYLD_PRINT_TO_FILE` (arbitrary file write), etc. A
capturer that genuinely needs a specific `DYLD_*` var for framework resolution
must add it **explicitly** to the allowlist in source (see the limitation
below) — it is never forwarded by default. (The shipped Milestone B capturer
needed none.)

STT / application **secrets are never forwarded** to the capturer — anything not
on the allowlist (e.g. `DEEPGRAM_API_KEY`, any `*_API_KEY` / `*_TOKEN` /
`*_SECRET`, `STT_*`) is excluded **by construction**. The allowlist is the
auditable module-level constant `onoats.cli._CAPTURER_ENV_POLICY` (an
`exact` / `prefixes` / `deny` policy object); because the policy is
deny-by-default, a newly added secret can't leak by omission. This keeps a buggy
/ replaced / crash-reporting capturer from ever seeing credentials it doesn't
need.

> **Limitation.** If a capturer ever needs a non-secret env var outside this
> allowlist (e.g. a device index or a non-credential license token), it must be
> added to `_CAPTURER_ENV_POLICY` in source — there is no runtime override
> today. A blessed pass-through mechanism (e.g. an `ONOATS_CAPTURER_ENV_EXTRA`
> allowlist-extension var) remains **deferred indefinitely**: the shipped
> Milestone B capturer needed nothing beyond the allowlist, so adding one would
> still be speculative.

The capturer (`native/onoats-capturer/`, shipped in Milestone B / PR #5)
MUST: create both sockets, accept one connection each, write the v1 handshake
per connection as it is accepted (echoing the nonce), then stream
length-prefixed v1 frames per branch.

### Default-device changes (capturer requirement, verified live)

The capturer MUST survive a **default-input-device change** mid-session — e.g. the
user's AirPods disconnect and macOS switches the default input to the built-in
mic — by **re-binding to the new default device and continuing to stream to the
same sockets**. It MUST NOT exit on a recoverable device change.

This is deliberately the capturer's job, not the supervisor's: the recorder/
transport only ever see bytes on a socket and cannot distinguish a device switch
from any other gap. Handling it in the capturer keeps the session continuous and
the `me`/`them` timeline intact.

Conversely, any capturer exit **before** the recorder ends is treated as
fail-loud by the supervisor **regardless of exit code** (even `rc=0`): the
supervisor outlives the capturer by design, so a capturer-initiated exit means
the audio stream stopped mid-session and the recording is truncated. A deliberate
"clean stop" signal (e.g. a capturer `rc=0` that the supervisor would honour as
success) is **reserved for a future capturer exit-code contract** — adopting it
would also require redefining the transport's EOF-is-fatal rule, so it is out of
scope for v1.

## Status-file schema (v2)

`src/onoats/status.py` (`StatusRecord` / `STATUS_SCHEMA_VERSION`) is the source
of truth; the menu bar's `RecorderModel.swift` mirrors it (parity-pinned by
`tests/test_native_contract_parity.py`). **Both readers hard-reject any other
`schema` value** (Python returns "no status"; the menu bar shows schema drift)
— so a schema bump requires reinstalling the app and the CLI **together**
(`make -C native install`); the visible mixed-version symptom is schema drift,
never mis-rendered data.

Version history:

- **v1** (Phase 5a): `schema`, `pid`, `start_time`, `audio_source`,
  `stt_label`, `running`, `last_rotation_time`, `last_error`, `exit_reason`,
  `supervisor_rc`.
- **v2** (release-plan Phase 4): adds three **optional flat string** fields,
  all default `null`:
  - `warning` — live, non-fatal capture anomaly (today: the capturer's
    all-zero-input detector). Set/cleared by the supervisor from
    `ONOATS-EVENT` lines (above) while the session runs.
  - `mic_device`, `system_device` — `"<name> (uid=<uid>)"` for the devices the
    capturer actually bound. Defined in v2, populated from release-plan
    Phase 5 onward (`null` until then).

## Fail-loud observable (acceptance shape)

For every failure path — capturer crash, permission denied, slow/silent reader —
the contract requires all of:

1. an **`ErrorFrame`** on the affected branch (the transport emits it),
2. a **non-zero supervisor exit code**,
3. a **WARNING/ERROR log line**, and
4. the **partial session still rotates** into `pending/` (via the recorder's
   existing `flush_and_rotate` shutdown path) — **no hang**.

## Constants (mirror of `socket_audio.py`)

> **Parity is enforced.** `tests/test_audio_socket_contract_parity.py` parses the
> table below and asserts each value equals the live `socket_audio.py` constant.
> A `WIRE_VERSION` / constant bump that updates only the code (or only this doc)
> fails CI — change both together.

| Constant                  | Value     |
|---------------------------|-----------|
| `WIRE_VERSION`            | `1`       |
| `DEFAULT_SAMPLE_RATE`     | `16000`   |
| `DEFAULT_SAMPLE_WIDTH`    | `2`       |
| `DEFAULT_CHANNELS`        | `1`       |
| `LENGTH_PREFIX_BYTES`     | `4` (big-endian) |
| `MAX_FRAME_PAYLOAD_BYTES` | `1048576` (1 MiB) |
| `DEFAULT_MAX_BUFFERED_BYTES` | `16777216` (16 MiB) |
| 20 ms frame @ 16 kHz      | `640` bytes PCM (`frame_size_bytes(16000)`) |
| default `read_idle_timeout` | `10.0` s |
| default `max_buffered_frames` | `200` |
| default backpressure      | `drop-oldest` + WARNING (configurable) |
