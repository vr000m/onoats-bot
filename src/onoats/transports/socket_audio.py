"""Unix-domain-socket PCM16 audio input transport.

Phase 1 of the CoreAudio socket-audio transport (see
``docs/dev_plans/20260607-feature-menubar-coreaudio-socket-transport.md``).

This module gives onoats an ``AUDIO_SOURCE=socket`` capture path: instead of
reading from a PortAudio device, a recorder branch reads framed PCM16 LE / 16 kHz
/ mono audio from a unix *stream* socket that some external capturer writes. Each
branch (``me`` / mic, ``them`` / system) gets its own socket and its own
``UnixSocketAudioInputTransport`` instance — the never-mix invariant is preserved
because nothing in this module fans one socket to two branches.

The pipecat seam (verified against pipecat 1.3.0 source):

  - Subclass :class:`pipecat.transports.base_input.BaseInputTransport`.
  - Override ``async start(self, frame)`` (the reference hook —
    ``LocalAudioInputTransport`` overrides ``start``, not the ``pass``-stub
    ``start_audio_in_streaming``). Order is load-bearing:
      1. ``await super().start(frame)``
      2. connect the socket + validate the handshake header
      3. ``await self.set_transport_ready(frame)`` — this is what creates
         ``_audio_in_queue`` (``base_input.py:152``)
      4. ONLY THEN spawn the read-loop task.
    Spawning the reader before step 3 races queue creation: ``push_audio_frame``
    does ``self._audio_in_queue.put(frame)`` (``base_input.py:170``) against a
    ``None`` queue.
  - Push PCM with the public ``push_audio_frame()`` (never ``_push_audio_frame``).
  - ``BaseInputTransport.stop()`` / ``cancel()`` only cancel the framework's own
    drain task; the subclass must tear down ``self._read_task`` itself, so
    ``stop`` / ``cancel`` / ``cleanup`` each close the socket and
    cancel-and-await the reader.
  - Construct the base with ``TransportParams(audio_in_enabled=True, ...)`` —
    without it the base never drains pushed frames (``push_audio_frame`` no-ops).

Wire format (Phase 1 starting point; pinned in the Phase 3 contract doc):

  - A 1-line JSON handshake header terminated by ``\n``, e.g.
    ``{"rate":16000,"width":2,"channels":1,"v":1,"nonce":"..."}``. The transport
    validates ``rate`` / ``width`` / ``channels`` against what it expects and
    refuses to start loudly on a mismatch or unknown ``v``. ``nonce`` is the
    generation token a supervisor uses to invalidate stale sockets (consumed in
    Phase 3); Phase 1 captures it for inspection but does not yet gate on it.
  - Each subsequent frame is length-prefixed: a 4-byte big-endian unsigned
    payload length, then that many bytes of a JSON object
    ``{"seq": int, "captured_monotonic_ns": int, "pcm_b64": str}`` where
    ``pcm_b64`` is base64-encoded PCM16 LE mono. Length-prefixing (not
    fixed-size) is deliberate: a unix *stream* socket has no message boundaries,
    so a fixed-size reader silently desyncs on a partial write. ``seq`` and
    ``captured_monotonic_ns`` are copied into ``InputAudioRawFrame.metadata`` and
    ``pts`` so drops are observable and ``me``/``them`` drift is measurable.

Backpressure: the read loop hands frames to the pipeline as fast as it can. The
base's ``_audio_in_queue`` is unbounded, so to keep memory bounded under a
faster-than-consumer writer this transport maintains its *own* bounded staging
buffer between the socket reader and the base queue, with a configurable policy
(default ``drop-oldest`` with a queue-depth WARNING). The policy is deliberately
*not* frozen — see Open Question 4 in the plan.

No self-reconnect: EOF, a broken handshake, a version mismatch, or a read-idle
timeout each surface a **fatal** ``ErrorFrame`` (pushed upstream via
``push_error(..., fatal=True)``) that cancels the pipeline and ends the branch
*and* the recorder. The supervisor (Phase 3) owns capturer restart.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from dataclasses import dataclass
from enum import Enum

from loguru import logger

from pipecat.frames.frames import InputAudioRawFrame, StartFrame
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import BaseTransport, TransportParams

# ---------------------------------------------------------------------------
# Wire-format constants. These are the Phase-1 starting point; the canonical
# contract is pinned in docs/audio-socket-contract.md in Phase 3. Treat any
# change here as a wire-contract version bump.
# ---------------------------------------------------------------------------

WIRE_VERSION = 1
"""Protocol version advertised in / required from the handshake header."""

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_SAMPLE_WIDTH = 2  # PCM16 -> 2 bytes/sample, little-endian
DEFAULT_CHANNELS = 1

LENGTH_PREFIX_BYTES = 4
"""Each framed payload is preceded by a 4-byte big-endian unsigned length."""

MAX_FRAME_PAYLOAD_BYTES = (
    1 << 20
)  # 1 MiB ceiling guards against a runaway length prefix

DEFAULT_MAX_BUFFERED_BYTES = 16 << 20
"""Byte ceiling for the staging buffer, independent of the frame-count cap.

The per-frame ceiling (``MAX_FRAME_PAYLOAD_BYTES``, 1 MiB) times the frame-count
cap (``max_buffered_frames``, default 200) would otherwise allow ~200 MiB of
transient staging under a misbehaving capturer sending maximal frames. Bounding
total staged bytes as well caps that footprint regardless of frame size. At the
640-byte 20 ms reference frame the count cap always bites first, so this only
engages for pathologically large frames.
"""


def frame_size_bytes(sample_rate: int) -> int:
    """Return the 20 ms PCM16-mono frame size in bytes for ``sample_rate``.

    Mirrors ``LocalAudioInputTransport``'s chunking: pipecat's
    ``num_frames = int(sample_rate / 100) * 2`` is the PyAudio frames-per-buffer
    count (20 ms @ 16 kHz = 320). At 2 bytes/sample, mono, one 20 ms chunk is
    320 * 2 = 640 bytes @ 16 kHz. Exposed so tests and the contract doc can
    assert the framing matches the reference.
    """
    num_frames = int(sample_rate / 100) * 2
    return num_frames * DEFAULT_SAMPLE_WIDTH


class BackpressurePolicy(str, Enum):
    """How the staging buffer behaves when the consumer falls behind.

    Configurable, not frozen: the final choice (drop-oldest vs drop-newest vs
    bounded-block) is deferred to the Open-Question-4 drift comparison. The
    default is ``DROP_OLDEST`` (realtime audio favours freshness over
    completeness).
    """

    DROP_OLDEST = "drop_oldest"
    DROP_NEWEST = "drop_newest"
    BLOCK = "block"


class SocketHandshakeError(Exception):
    """Raised when the capturer handshake is missing, malformed, or mismatched.

    Surfaced as an ``ErrorFrame`` / refuse-to-start — never silently coerced.
    """


@dataclass(frozen=True)
class HandshakeHeader:
    """Parsed + validated handshake header from the capturer."""

    rate: int
    width: int
    channels: int
    version: int
    nonce: str | None


def parse_handshake(
    line: bytes,
    *,
    expected_rate: int,
    expected_width: int,
    expected_channels: int,
) -> HandshakeHeader:
    """Parse and validate the 1-line JSON handshake header.

    Raises :class:`SocketHandshakeError` on malformed JSON, an unknown protocol
    version, or a rate/width/channels mismatch — loudly, never coercing.
    """
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SocketHandshakeError(f"handshake is not valid UTF-8 JSON: {exc}") from exc

    if not isinstance(obj, dict):
        raise SocketHandshakeError(
            f"handshake must be a JSON object, got {type(obj).__name__}"
        )

    version = obj.get("v")
    if version != WIRE_VERSION:
        raise SocketHandshakeError(
            f"unsupported wire version {version!r} (this transport speaks v{WIRE_VERSION})"
        )

    rate = obj.get("rate")
    width = obj.get("width")
    channels = obj.get("channels")
    if (rate, width, channels) != (expected_rate, expected_width, expected_channels):
        raise SocketHandshakeError(
            "handshake format mismatch: capturer offered "
            f"rate={rate} width={width} channels={channels}, "
            f"transport requires rate={expected_rate} width={expected_width} "
            f"channels={expected_channels}"
        )

    nonce = obj.get("nonce")
    if nonce is not None and not isinstance(nonce, str):
        raise SocketHandshakeError(
            f"handshake nonce must be a string, got {type(nonce).__name__}"
        )

    return HandshakeHeader(
        rate=rate, width=width, channels=channels, version=version, nonce=nonce
    )


class UnixSocketAudioInputTransport(BaseInputTransport):
    """Read framed PCM16 LE / mono audio from a unix stream socket.

    One instance == one capture branch == one socket == one STT session. Pushes
    :class:`InputAudioRawFrame` s into the pipeline via the public
    ``push_audio_frame``; surfaces an ``ErrorFrame`` and ends the branch on EOF,
    handshake failure, or read-idle timeout (no self-reconnect — the supervisor
    owns restart).
    """

    def __init__(
        self,
        socket_path: str,
        params: TransportParams,
        *,
        read_idle_timeout: float = 10.0,
        max_buffered_frames: int = 200,
        max_buffered_bytes: int = DEFAULT_MAX_BUFFERED_BYTES,
        backpressure_policy: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST,
        expected_nonce: str | None = None,
        **kwargs,
    ):
        """Initialize the socket input transport.

        Args:
            socket_path: Filesystem path of the unix socket to connect to.
            params: Pipecat transport params. MUST have ``audio_in_enabled=True``
                or the base never drains pushed frames; enforced here.
            read_idle_timeout: Seconds to wait for the next frame before
                declaring the (alive-but-silent) capturer dead and surfacing an
                ``ErrorFrame``. ``<= 0`` disables the watchdog.
            max_buffered_frames: Frame-count bound on the internal staging buffer
                between the socket reader and the base audio queue.
            max_buffered_bytes: Total-byte bound on the staging buffer, enforced
                alongside ``max_buffered_frames`` so a few maximal frames cannot
                blow the footprint past the count cap × the 1 MiB per-frame ceiling.
            backpressure_policy: What the staging buffer does when full
                (configurable — see Open Question 4).
            expected_nonce: If set, the handshake nonce must equal this value or
                the transport refuses to start (generation-token gating; the
                supervisor in Phase 3 supplies it). Phase 1 leaves it ``None``.
        """
        if not params.audio_in_enabled:
            raise ValueError(
                "UnixSocketAudioInputTransport requires TransportParams("
                "audio_in_enabled=True): without it BaseInputTransport never "
                "drains pushed frames (push_audio_frame silently no-ops)."
            )

        super().__init__(params, **kwargs)

        self._socket_path = socket_path
        self._read_idle_timeout = read_idle_timeout
        self._max_buffered_frames = max_buffered_frames
        self._max_buffered_bytes = max(1, max_buffered_bytes)
        self._backpressure_policy = backpressure_policy
        self._expected_nonce = expected_nonce

        self._expected_rate = params.audio_in_sample_rate or DEFAULT_SAMPLE_RATE
        # Width is pinned to PCM16: the handshake validates width == 2 and refuses
        # to start otherwise, so there is no TransportParams field that can make
        # this diverge from the wire format today.
        self._expected_width = DEFAULT_SAMPLE_WIDTH
        self._expected_channels = params.audio_in_channels

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._handshake: HandshakeHeader | None = None
        self._read_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None

        # Bounded staging buffer: the socket reader puts frames here; a pump task
        # forwards them to the base queue. Bounding it by BOTH frame count and
        # total bytes (plus the drop policy) caps memory under a
        # faster-than-consumer writer regardless of frame size.
        self._stage: asyncio.Queue[InputAudioRawFrame] = asyncio.Queue(
            maxsize=max(1, max_buffered_frames)
        )
        self._staged_bytes = 0
        # Set by the pump after each ``get`` so a BLOCK-policy producer waiting on
        # the byte cap (which the count-based Queue cannot express) is woken.
        # Single-reader / single-pump invariant: exactly one task waits on this
        # Event (the reader in _block_until_room) and exactly one sets it (the
        # pump). If a future refactor adds a second reader or pump, replace this
        # Event with an asyncio.Condition — two concurrent BLOCK waiters would
        # otherwise race past _has_room_for on one set().
        self._room_freed = asyncio.Event()
        self._dropped_frames = 0
        self._tearing_down = False

    # -- lifecycle ----------------------------------------------------------

    async def start(self, frame: StartFrame):
        """Connect, validate the handshake, go ready, then spawn the reader.

        The order is load-bearing (see module docstring). ``set_transport_ready``
        is what creates ``_audio_in_queue``; the reader must not run before it.
        """
        await super().start(frame)

        # Idempotent: a second StartFrame must not open a second connection.
        if self._reader is not None:
            return

        await self._connect_and_handshake()
        await self.set_transport_ready(frame)

        # Only now is _audio_in_queue guaranteed to exist. Spawn the staging
        # pump first, then the socket reader.
        self._pump_task = self.create_task(self._pump_loop())
        self._read_task = self.create_task(self._read_loop())

    async def stop(self, frame):
        """Stop: tear down our own reader/pump, then defer to the base."""
        await self._teardown()
        await super().stop(frame)

    async def cancel(self, frame):
        """Cancel: tear down our own reader/pump, then defer to the base."""
        await self._teardown()
        await super().cancel(frame)

    async def cleanup(self):
        """Cleanup: tear down our own reader/pump, then defer to the base."""
        await self._teardown()
        await super().cleanup()

    # -- connection + handshake --------------------------------------------

    async def _connect_and_handshake(self) -> None:
        """Open the unix socket and validate the 1-line JSON handshake header."""
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                self._socket_path
            )
        except OSError as exc:
            raise SocketHandshakeError(
                f"could not connect to capturer socket {self._socket_path!r}: {exc}"
            ) from exc

        # Bound the handshake read by the same idle watchdog as the frame loop:
        # a capturer that accepts the connection but never writes the header
        # would otherwise hang start() forever — the socket-exists check has
        # already passed and the child is still alive, so neither the read-idle
        # ErrorFrame path nor the capturer-death path can fire here.
        try:
            if self._read_idle_timeout and self._read_idle_timeout > 0:
                header_line = await asyncio.wait_for(
                    self._reader.readline(), timeout=self._read_idle_timeout
                )
            else:
                header_line = await self._reader.readline()
        except asyncio.TimeoutError as exc:
            raise SocketHandshakeError(
                f"timed out after {self._read_idle_timeout}s waiting for the "
                f"capturer handshake on {self._socket_path!r} "
                "(connected but silent)"
            ) from exc
        except (OSError, asyncio.IncompleteReadError) as exc:
            raise SocketHandshakeError(
                f"failed reading handshake from {self._socket_path!r}: {exc}"
            ) from exc

        if not header_line:
            raise SocketHandshakeError(
                f"capturer closed {self._socket_path!r} before sending a handshake"
            )

        self._handshake = parse_handshake(
            header_line,
            expected_rate=self._expected_rate,
            expected_width=self._expected_width,
            expected_channels=self._expected_channels,
        )

        if (
            self._expected_nonce is not None
            and self._handshake.nonce != self._expected_nonce
        ):
            raise SocketHandshakeError(
                f"stale/foreign capturer on {self._socket_path!r}: handshake nonce "
                f"{self._handshake.nonce!r} != expected {self._expected_nonce!r}"
            )

        # Truncate the nonce in logs (it is a generation token; same-user logs,
        # but keep it consistent with cli.py's nonce[:8] truncation).
        nonce_log = f"{self._handshake.nonce[:8]}…" if self._handshake.nonce else None
        logger.info(
            f"Socket transport connected: path={self._socket_path} "
            f"rate={self._handshake.rate} width={self._handshake.width} "
            f"channels={self._handshake.channels} nonce={nonce_log}"
        )

    # -- read loop ----------------------------------------------------------

    async def _read_loop(self) -> None:
        """Read length-prefixed frames and stage them for the pump.

        Ends the branch on EOF, a malformed frame, or a read-idle timeout by
        surfacing a fatal ``ErrorFrame`` (upstream). Never self-reconnects.
        """
        assert self._reader is not None
        try:
            while True:
                frame = await self._read_one_frame()
                if frame is None:
                    # Clean EOF.
                    await self._surface_error(
                        f"capturer closed socket {self._socket_path!r} (EOF); ending branch"
                    )
                    return
                await self._stage_frame(frame)
        except asyncio.CancelledError:
            raise
        except _ReadIdleTimeout:
            await self._surface_error(
                f"read-idle timeout ({self._read_idle_timeout}s) on "
                f"{self._socket_path!r}: capturer alive but silent; ending branch"
            )
        except _ConsumerStallTimeout:
            await self._surface_error(
                f"backpressure BLOCK stalled (> {self._read_idle_timeout}s) on "
                f"{self._socket_path!r}: downstream consumer is not draining; "
                "ending branch"
            )
        except SocketHandshakeError as exc:
            await self._surface_error(
                f"socket framing error on {self._socket_path!r}: {exc}"
            )
        except OSError as exc:
            await self._surface_error(
                f"socket read error on {self._socket_path!r}: {exc}; ending branch"
            )

    async def _read_one_frame(self) -> InputAudioRawFrame | None:
        """Read one length-prefixed framed payload.

        Returns ``None`` on clean EOF. Raises :class:`_ReadIdleTimeout` if no
        bytes arrive within the watchdog window, or
        :class:`SocketHandshakeError` on a malformed/oversized frame.
        """
        assert self._reader is not None

        prefix = await self._read_exactly_or_eof(LENGTH_PREFIX_BYTES)
        if prefix is None:
            return None

        payload_len = int.from_bytes(prefix, "big")
        if payload_len <= 0 or payload_len > MAX_FRAME_PAYLOAD_BYTES:
            raise SocketHandshakeError(
                f"frame length {payload_len} out of bounds "
                f"(1..{MAX_FRAME_PAYLOAD_BYTES}); stream desynced"
            )

        payload = await self._read_exactly_or_eof(payload_len)
        if payload is None:
            # EOF mid-frame is a desync/truncation, not a clean close.
            raise SocketHandshakeError(
                f"capturer closed mid-frame on {self._socket_path!r} "
                f"(wanted {payload_len} payload bytes)"
            )

        return self._decode_frame(payload)

    async def _read_exactly_or_eof(self, n: int) -> bytes | None:
        """``readexactly(n)`` with the read-idle watchdog applied.

        Returns ``None`` only on a clean EOF at a frame boundary (zero bytes
        read). Raises :class:`_ReadIdleTimeout` if nothing arrives in time, or
        :class:`SocketHandshakeError` on a truncated (partial-then-EOF) read.
        """
        assert self._reader is not None
        try:
            if self._read_idle_timeout and self._read_idle_timeout > 0:
                data = await asyncio.wait_for(
                    self._reader.readexactly(n), timeout=self._read_idle_timeout
                )
            else:
                data = await self._reader.readexactly(n)
        except asyncio.TimeoutError as exc:
            raise _ReadIdleTimeout() from exc
        except asyncio.IncompleteReadError as exc:
            if not exc.partial:
                # Clean EOF at a boundary.
                return None
            raise SocketHandshakeError(
                f"truncated read on {self._socket_path!r}: got {len(exc.partial)}/{n} bytes"
            ) from exc
        return data

    def _decode_frame(self, payload: bytes) -> InputAudioRawFrame:
        """Decode one JSON frame payload into an ``InputAudioRawFrame``.

        Copies ``seq`` and ``captured_monotonic_ns`` into ``metadata`` (and sets
        ``pts`` from the capture timestamp) so drops are observable and drift is
        measurable.
        """
        try:
            obj = json.loads(payload.decode("utf-8"))
            pcm = base64.b64decode(obj["pcm_b64"], validate=True)
            seq = int(obj["seq"])
            captured_ns = int(obj["captured_monotonic_ns"])
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
            binascii.Error,
        ) as exc:
            raise SocketHandshakeError(f"malformed frame payload: {exc}") from exc

        # A PCM payload must be a whole number of samples for every channel;
        # an odd byte count (PCM16) would silently misalign every following
        # sample. Treat it as a framing/desync signal, not as audio to coerce.
        bytes_per_sample_frame = self._expected_width * self._expected_channels
        if bytes_per_sample_frame and len(pcm) % bytes_per_sample_frame != 0:
            raise SocketHandshakeError(
                f"PCM payload {len(pcm)} bytes is not a multiple of "
                f"width*channels ({bytes_per_sample_frame}); stream is desynced"
            )

        audio_frame = InputAudioRawFrame(
            audio=pcm,
            sample_rate=self._expected_rate,
            num_channels=self._expected_channels,
        )
        audio_frame.metadata["socket_seq"] = seq
        audio_frame.metadata["captured_monotonic_ns"] = captured_ns
        # PTS is nanoseconds in pipecat; the capturer's monotonic clock is a fine
        # presentation timestamp for downstream drift measurement.
        audio_frame.pts = captured_ns
        return audio_frame

    # -- staging buffer / backpressure -------------------------------------

    def _has_room_for(self, frame_bytes: int) -> bool:
        """Whether a frame of ``frame_bytes`` fits within BOTH staging caps."""
        return (
            not self._stage.full()
            and self._staged_bytes + frame_bytes <= self._max_buffered_bytes
        )

    def _put_staged(self, frame: InputAudioRawFrame, frame_bytes: int) -> None:
        """Put a frame on the staging queue and account its bytes.

        Caller MUST have checked :meth:`_has_room_for` first (no ``await`` runs
        between the check and here, so the room cannot vanish under us)."""
        self._stage.put_nowait(frame)
        self._staged_bytes += frame_bytes

    async def _stage_frame(self, frame: InputAudioRawFrame) -> None:
        """Enqueue a frame into the bounded staging buffer per the drop policy.

        The buffer is bounded by BOTH a frame count and a total-byte ceiling; it
        is "full" when adding this frame would breach either. The fast path is a
        non-blocking put. Only when full does the backpressure policy kick in:
        ``BLOCK`` awaits free space (applying flow control back to the capturer),
        bounded by the read-idle timeout so a stalled *consumer* surfaces a fatal
        error rather than parking the reader forever; the drop policies never
        block the reader.
        """
        frame_bytes = len(frame.audio)

        if self._has_room_for(frame_bytes):
            self._put_staged(frame, frame_bytes)
            return

        policy = self._backpressure_policy

        if policy is BackpressurePolicy.BLOCK:
            await self._block_until_room(frame, frame_bytes)
            return

        if policy is BackpressurePolicy.DROP_NEWEST:
            self._dropped_frames += 1
            logger.warning(
                f"Socket transport {self._socket_path!r} buffer full "
                f"(depth={self._stage.qsize()}/{self._max_buffered_frames} "
                f"frames, {self._staged_bytes}/{self._max_buffered_bytes} bytes); "
                f"dropping NEWEST frame seq={frame.metadata.get('socket_seq')} "
                f"(total dropped={self._dropped_frames})"
            )
            return

        # Default: DROP_OLDEST. Evict oldest frames until this one fits within
        # both caps (one eviction suffices when only the count cap is breached;
        # a large frame against the byte cap may evict several).
        while not self._has_room_for(frame_bytes) and not self._stage.empty():
            try:
                dropped = self._stage.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._staged_bytes -= len(dropped.audio)
            self._dropped_frames += 1
            logger.warning(
                f"Socket transport {self._socket_path!r} buffer full "
                f"(depth={self._stage.qsize() + 1}/{self._max_buffered_frames} "
                f"frames, bytes cap {self._max_buffered_bytes}); dropping OLDEST "
                f"frame seq={dropped.metadata.get('socket_seq')} "
                f"(total dropped={self._dropped_frames})"
            )

        if self._has_room_for(frame_bytes):
            self._put_staged(frame, frame_bytes)
        else:
            # The queue is empty yet the frame still doesn't fit: it alone
            # exceeds the byte cap. Drop it loudly rather than spin forever.
            self._dropped_frames += 1
            logger.warning(
                f"Socket transport {self._socket_path!r}: frame "
                f"seq={frame.metadata.get('socket_seq')} is {frame_bytes} bytes, "
                f"larger than the entire staging cap ({self._max_buffered_bytes}); "
                f"dropping it (total dropped={self._dropped_frames})"
            )

    async def _block_until_room(
        self, frame: InputAudioRawFrame, frame_bytes: int
    ) -> None:
        """BLOCK policy: wait until both caps admit the frame, then stage it.

        Bounded by the read-idle timeout: a downstream pipeline that stops
        draining must surface a fatal error (via ``_ConsumerStallTimeout``)
        instead of parking the reader — the socket-read watchdog cannot fire
        while we are blocked here, so this is the only stall guard on this path.
        """
        bounded = self._read_idle_timeout and self._read_idle_timeout > 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._read_idle_timeout if bounded else None

        while not self._has_room_for(frame_bytes):
            self._room_freed.clear()
            if bounded:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise _ConsumerStallTimeout()
                try:
                    await asyncio.wait_for(self._room_freed.wait(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise _ConsumerStallTimeout() from exc
            else:
                # Watchdog disabled (read_idle_timeout <= 0): wait unbounded.
                # Liveness here rests on external cancellation — if the pump dies
                # (its own _surface_error), its fatal ErrorFrame cancels the
                # pipeline and this reader task, so the wait cannot hang forever.
                await self._room_freed.wait()

        self._put_staged(frame, frame_bytes)

    async def _pump_loop(self) -> None:
        """Forward staged frames into the base audio queue.

        Decoupling the socket reader from ``push_audio_frame`` via the bounded
        staging buffer is what makes the drop policy effective: the base queue is
        unbounded, so without this buffer a faster-than-consumer writer would
        grow it without limit.

        Fail-loud: an unexpected exception from ``push_audio_frame`` surfaces a
        fatal ``ErrorFrame`` and ends the branch, mirroring ``_read_loop``. If the
        pump died silently the reader would keep staging into a full buffer and
        drop every frame with no audio reaching the pipeline — a silent failure.
        """
        try:
            while True:
                frame = await self._stage.get()
                self._staged_bytes -= len(frame.audio)
                self._room_freed.set()
                await self.push_audio_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._surface_error(
                f"staging pump failed on {self._socket_path!r}: {exc}; ending branch"
            )

    # -- teardown / errors --------------------------------------------------

    async def _surface_error(self, message: str) -> None:
        """Push a FATAL ``ErrorFrame`` upstream to terminate the recorder.

        Uses the framework's ``push_error(..., fatal=True)`` helper, which pushes
        the error **upstream**. The pipeline worker only cancels the pipeline for
        a *fatal* error arriving upstream (``worker.py``: ``frame.fatal`` →
        queue ``CancelFrame``). A plain/downstream ``ErrorFrame`` would log but
        leave the recorder running — so a silent-but-alive capturer would hang
        the session instead of failing loud. ``fatal=True`` is what makes the
        EOF / read-idle / framing failures end the branch AND the recorder.
        """
        logger.error(f"Socket transport: {message}")
        await self.push_error(message, fatal=True)

    async def _teardown(self) -> None:
        """Close the socket and cancel-and-await the reader/pump tasks.

        Idempotent. The base only cancels its own drain task, so the subclass
        must do this or the reader/pump leak past ``EndFrame`` / ``CancelFrame``.
        """
        if self._tearing_down:
            return
        self._tearing_down = True

        for task_attr in ("_read_task", "_pump_task"):
            task = getattr(self, task_attr)
            if task is not None:
                await self.cancel_task(task)
                setattr(self, task_attr, None)

        if self._writer is not None:
            try:
                self._writer.close()
                # Drain the close so the event loop fully releases the socket;
                # skipping this can emit "transport not closed" warnings at
                # shutdown on some loops. Best-effort and bounded.
                await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
            except (OSError, asyncio.TimeoutError):
                pass
            self._writer = None
        self._reader = None


class UnixSocketAudioTransport(BaseTransport):
    """Input-only transport wrapping a :class:`UnixSocketAudioInputTransport`.

    This is the ``dual.py`` swap seam: it exposes ``.input()`` (what
    ``_build_dual_pipeline`` calls) returning a socket input transport, so it
    drops in where ``LocalAudioTransport`` was with no pipeline change. One
    instance == one branch == one socket; nothing here fans a socket to two
    branches, preserving the never-mix invariant.

    The transport is built with
    ``TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)`` —
    ``audio_in_enabled=True`` is required or the base never drains pushed
    frames. There is no output side: ``.output()`` raises ``NotImplementedError``.
    """

    def __init__(
        self,
        socket_path: str,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        params: TransportParams | None = None,
        read_idle_timeout: float = 10.0,
        max_buffered_frames: int = 200,
        max_buffered_bytes: int = DEFAULT_MAX_BUFFERED_BYTES,
        backpressure_policy: BackpressurePolicy = BackpressurePolicy.DROP_OLDEST,
        expected_nonce: str | None = None,
        name: str | None = None,
        input_name: str | None = None,
        **kwargs,
    ):
        """Initialize the input-only socket transport.

        Args:
            socket_path: Filesystem path of the unix socket to connect to.
            sample_rate: Audio-in sample rate; used to build the default
                ``TransportParams`` when ``params`` is not supplied.
            params: Optional pre-built transport params. Defaults to
                ``TransportParams(audio_in_enabled=True,
                audio_in_sample_rate=sample_rate)``.
            read_idle_timeout: Forwarded to the input transport's idle watchdog.
            max_buffered_frames: Forwarded to the input transport's staging buffer.
            max_buffered_bytes: Forwarded to the input transport's staging buffer.
            backpressure_policy: Forwarded to the input transport.
            expected_nonce: Forwarded to the input transport (Phase-3 generation
                token; ``None`` in Phase 1/2).
            name: Optional transport instance name.
            input_name: Optional name for the input frame processor.

        Any extra ``**kwargs`` are forwarded verbatim to
        :class:`UnixSocketAudioInputTransport` (and on to ``BaseInputTransport``).
        They MUST NOT duplicate the keys this wrapper already forwards explicitly
        (``name``/``input_name``, ``params``, ``read_idle_timeout``,
        ``max_buffered_frames``, ``max_buffered_bytes``, ``backpressure_policy``,
        ``expected_nonce``) or the input transport raises a duplicate-keyword
        ``TypeError``.
        """
        super().__init__(name=name, input_name=input_name)

        if params is None:
            params = TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=False,
                audio_in_sample_rate=sample_rate,
            )

        self._socket_path = socket_path
        self._input: UnixSocketAudioInputTransport = UnixSocketAudioInputTransport(
            socket_path,
            params,
            read_idle_timeout=read_idle_timeout,
            max_buffered_frames=max_buffered_frames,
            max_buffered_bytes=max_buffered_bytes,
            backpressure_policy=backpressure_policy,
            expected_nonce=expected_nonce,
            name=input_name,
            **kwargs,
        )

    def input(self) -> UnixSocketAudioInputTransport:
        """Return the socket input transport (the frame processor ``dual.py`` uses)."""
        return self._input

    def output(self):
        """Input-only transport: there is no output side."""
        raise NotImplementedError("UnixSocketAudioTransport is input-only")


class _ReadIdleTimeout(Exception):
    """Internal: no frame arrived within the read-idle watchdog window."""


class _ConsumerStallTimeout(Exception):
    """Internal: BLOCK backpressure could not stage a frame within the watchdog
    window because the downstream consumer stopped draining."""
