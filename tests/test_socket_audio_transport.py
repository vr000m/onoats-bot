"""Phase 1 tests: ``UnixSocketAudioInputTransport`` + the wire framing.

These cover the Phase-1 acceptance criteria from
``docs/dev_plans/20260607-feature-menubar-coreaudio-socket-transport.md``:

  - audio frames surface through the base with the right rate/width/channels
    (guards the silent ``audio_in_enabled=False`` no-op);
  - start ordering — readiness precedes the first ``push_audio_frame`` (no
    ``None``-queue race);
  - teardown — ``EndFrame`` *and* ``CancelFrame`` each cancel ``_read_task`` and
    close the socket (no leak);
  - endianness — PCM16 **LE** bytes round-trip to the expected samples;
  - handshake validation — a valid header is accepted; a mismatched
    rate/width/channels or unknown version is rejected loudly;
  - backpressure — a faster-than-consumer writer caps memory and drops-oldest
    with a queue-depth WARNING rather than growing unbounded;
  - read-idle — a connected-but-silent writer trips the watchdog and surfaces an
    ``ErrorFrame`` rather than hanging;
  - clean EOF — socket close surfaces an ``ErrorFrame`` and ends the branch (no
    self-reconnect).

The socket is fed by a pure-Python writer (an ``asyncio`` unix server) — **no
native code**. Async tests run under anyio's pytest plugin (a transitive dep);
there is no ``pytest-asyncio`` requirement. Every wait is bounded so a hang fails
the test instead of blocking CI.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import shutil
import struct
import tempfile
import uuid
from pathlib import Path

import pytest
from loguru import logger

from pipecat.clocks.system_clock import SystemClock
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    InputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.transports.base_transport import TransportParams
from pipecat.utils.asyncio.task_manager import TaskManager, TaskManagerParams

from onoats.transports.socket_audio import (
    LENGTH_PREFIX_BYTES,
    WIRE_VERSION,
    BackpressurePolicy,
    HandshakeHeader,
    SocketHandshakeError,
    UnixSocketAudioInputTransport,
    frame_size_bytes,
    parse_handshake,
)

# A generous-but-bounded ceiling for any single async wait. If an assertion
# would otherwise hang, this turns it into a failure instead.
WAIT_TIMEOUT = 5.0


@pytest.fixture
def anyio_backend():
    # Pin anyio to asyncio; the transport uses asyncio unix sockets/tasks.
    return "asyncio"


@pytest.fixture
def path():
    """A short unix-socket path under the system temp root.

    ``AF_UNIX`` paths are capped (~104 bytes on macOS); pytest's ``tmp_path``
    lives under a long ``/var/folders/...`` prefix that blows that limit. Build a
    deliberately short directory + filename instead, and clean it up.
    """
    base = Path(tempfile.gettempdir()) / f"oa{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "s.sock"
    try:
        yield str(path)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# Wire-format helpers (the pure-Python "capturer" side of the contract)
# ---------------------------------------------------------------------------


def make_header(
    *,
    rate: int = 16000,
    width: int = 2,
    channels: int = 1,
    v: int = WIRE_VERSION,
    nonce: str | None = "gen-1",
) -> bytes:
    """Build the 1-line JSON handshake header, ``\\n``-terminated."""
    obj: dict = {"rate": rate, "width": width, "channels": channels, "v": v}
    if nonce is not None:
        obj["nonce"] = nonce
    return (json.dumps(obj) + "\n").encode("utf-8")


def make_frame(seq: int, pcm: bytes, *, captured_ns: int | None = None) -> bytes:
    """Build one length-prefixed framed PCM payload per the wire contract."""
    payload = json.dumps(
        {
            "seq": seq,
            "captured_monotonic_ns": seq if captured_ns is None else captured_ns,
            "pcm_b64": base64.b64encode(pcm).decode("ascii"),
        }
    ).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


def pcm_from_samples(samples: list[int]) -> bytes:
    """Encode signed 16-bit samples as PCM16 **little-endian** bytes."""
    return array.array("h", samples).tobytes()


def samples_from_pcm(pcm: bytes) -> list[int]:
    """Decode PCM16 LE bytes back into signed 16-bit samples."""
    a = array.array("h")
    a.frombytes(pcm)
    return list(a)


class _SocketWriterServer:
    """A pure-Python unix-socket server that plays a scripted capturer.

    The ``feed`` coroutine receives the connection's :class:`asyncio.StreamWriter`
    and is responsible for writing the handshake + frames (and optionally closing
    or stalling). One connection only; the server is closed on ``aclose``.
    """

    def __init__(self, path: str, feed):
        self._path = path
        self._feed = feed
        self._server: asyncio.AbstractServer | None = None
        self._conns: list[asyncio.StreamWriter] = []

    async def start(self) -> None:
        async def _on_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            self._conns.append(writer)
            try:
                await self._feed(writer)
            except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                pass

        self._server = await asyncio.start_unix_server(_on_conn, path=self._path)

    async def aclose(self) -> None:
        for w in self._conns:
            try:
                w.close()
            except OSError:
                pass
        if self._server is not None:
            self._server.close()
            try:
                await asyncio.wait_for(self._server.wait_closed(), timeout=WAIT_TIMEOUT)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - best-effort teardown
                pass


# ---------------------------------------------------------------------------
# Manual single-processor harness (fine-grained lifecycle control)
# ---------------------------------------------------------------------------


class _CapturingSink(FrameProcessor):
    """Records every frame that flows downstream into it."""

    def __init__(self):
        super().__init__()
        self.frames: list = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        self.frames.append(frame)
        await self.push_frame(frame, direction)

    def audio_frames(self) -> list[InputAudioRawFrame]:
        return [f for f in self.frames if isinstance(f, InputAudioRawFrame)]

    def error_frames(self) -> list[ErrorFrame]:
        return [f for f in self.frames if isinstance(f, ErrorFrame)]


async def _setup_processor(proc: FrameProcessor, tm: TaskManager) -> None:
    await proc.setup(
        FrameProcessorSetup(
            clock=SystemClock(),
            task_manager=tm,
            pipeline_worker=None,
            observer=None,
            tool_resources=None,
        )
    )


class _ManualHarness:
    """Drives a transport + capturing sink directly via ``process_frame``.

    Lets a test inspect ``transport._read_task`` / socket state across
    ``StartFrame`` / ``EndFrame`` / ``CancelFrame`` — finer control than
    ``run_test`` gives.
    """

    def __init__(self, transport: UnixSocketAudioInputTransport):
        self.transport = transport
        self.sink = _CapturingSink()
        self._tm: TaskManager | None = None

    async def setup(self) -> None:
        tm = TaskManager()
        tm.setup(TaskManagerParams(loop=asyncio.get_running_loop()))
        self._tm = tm
        await _setup_processor(self.transport, tm)
        await _setup_processor(self.sink, tm)
        self.transport.link(self.sink)

    async def start(self) -> None:
        await self.transport.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)

    async def send(self, frame) -> None:
        await self.transport.process_frame(frame, FrameDirection.DOWNSTREAM)


async def _wait_until(
    predicate, *, timeout: float = WAIT_TIMEOUT, interval: float = 0.01
):
    """Poll ``predicate`` until true or ``timeout`` elapses (bounded)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if predicate():
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(interval)


def _make_transport(path: str, **kw) -> UnixSocketAudioInputTransport:
    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    return UnixSocketAudioInputTransport(path, params, **kw)


# ---------------------------------------------------------------------------
# Pure-unit tests: wire framing (no socket, no event loop needed)
# ---------------------------------------------------------------------------


def test_frame_size_matches_reference_20ms():
    # 20 ms PCM16 mono @ 16 kHz = 320 samples * 2 bytes = 640 bytes,
    # mirroring LocalAudioInputTransport's chunking.
    assert frame_size_bytes(16000) == 640


def test_length_prefix_is_four_bytes():
    assert LENGTH_PREFIX_BYTES == 4


def test_parse_handshake_accepts_valid_header():
    header = make_header(rate=16000, width=2, channels=1, v=WIRE_VERSION, nonce="abc")
    line = header.rstrip(b"\n")
    parsed = parse_handshake(
        line, expected_rate=16000, expected_width=2, expected_channels=1
    )
    assert isinstance(parsed, HandshakeHeader)
    assert (parsed.rate, parsed.width, parsed.channels) == (16000, 2, 1)
    assert parsed.version == WIRE_VERSION
    assert parsed.nonce == "abc"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rate": 8000},  # mismatched rate
        {"width": 1},  # mismatched width
        {"channels": 2},  # mismatched channels
        {"v": WIRE_VERSION + 99},  # unknown version
    ],
    ids=["bad-rate", "bad-width", "bad-channels", "bad-version"],
)
def test_parse_handshake_rejects_mismatch_loudly(kwargs):
    line = make_header(**kwargs).rstrip(b"\n")
    with pytest.raises(SocketHandshakeError):
        parse_handshake(
            line, expected_rate=16000, expected_width=2, expected_channels=1
        )


def test_parse_handshake_rejects_malformed_json():
    with pytest.raises(SocketHandshakeError):
        parse_handshake(
            b"not json", expected_rate=16000, expected_width=2, expected_channels=1
        )


def test_pcm16_le_round_trip():
    # Values chosen to expose byte order: 1 (0x0001), 256 (0x0100), -1 (0xFFFF).
    samples = [1, 256, -1, 32767, -32768, 0]
    pcm = pcm_from_samples(samples)
    # Explicit LE byte layout for the first three samples.
    assert pcm[:6] == b"\x01\x00\x00\x01\xff\xff"
    assert samples_from_pcm(pcm) == samples


def test_decode_frame_accepts_whole_sample_pcm(path):
    """A PCM payload that is a whole number of PCM16 samples decodes cleanly."""
    transport = _make_transport(path)
    # _decode_frame takes the inner JSON payload; make_frame prefixes a 4-byte
    # length, so strip it.
    frame = transport._decode_frame(make_frame(7, pcm_from_samples([1, -2, 3]))[4:])
    assert isinstance(frame, InputAudioRawFrame)
    assert samples_from_pcm(frame.audio) == [1, -2, 3]
    assert frame.metadata["socket_seq"] == 7


def test_decode_frame_rejects_odd_byte_pcm_as_desync(path):
    """An odd PCM16 byte count would misalign every following sample.

    The transport treats it as a framing/desync signal (``SocketHandshakeError``),
    not as audio to coerce — guarding speaker-attribution integrity downstream.
    """
    transport = _make_transport(path)
    # 3 bytes is not a multiple of width*channels (2*1) for PCM16 mono.
    with pytest.raises(SocketHandshakeError):
        transport._decode_frame(make_frame(1, b"\x01\x00\x02")[4:])


# ---------------------------------------------------------------------------
# Integration: frames surface through the base with correct format
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_frames_surface_with_correct_format(path):
    """Audio pushed by the reader actually surfaces downstream through the base.

    Guards the silent ``audio_in_enabled=False`` no-op: if the base never drained
    pushed frames, zero ``InputAudioRawFrame``s would surface and this fails.
    Also asserts rate/width/channels and that PCM16 LE decodes correctly.
    """
    samples = [1, 2, 3, -4, 5]
    pcm = pcm_from_samples(samples)
    n_frames = 5

    async def feed(writer: asyncio.StreamWriter):
        writer.write(make_header())
        for i in range(n_frames):
            writer.write(make_frame(i, pcm))
            await writer.drain()
            await asyncio.sleep(0.005)
        await writer.drain()
        # Keep the connection open; run_test ends the branch with its EndFrame.
        await asyncio.sleep(WAIT_TIMEOUT)

    server = _SocketWriterServer(path, feed)
    await server.start()
    try:
        transport = _make_transport(path)
        down, _up = await asyncio.wait_for(
            run_test(transport, frames_to_send=[SleepFrame(0.2)]),
            timeout=WAIT_TIMEOUT,
        )
    finally:
        await server.aclose()

    audio = [f for f in down if isinstance(f, InputAudioRawFrame)]
    assert audio, "no InputAudioRawFrame surfaced — base did not drain pushed frames"
    assert len(audio) == n_frames
    for f in audio:
        assert f.sample_rate == 16000
        assert f.num_channels == 1
        # width: PCM16 -> 2 bytes/sample, so byte length is even and decodes back.
        assert len(f.audio) % 2 == 0
        assert samples_from_pcm(f.audio) == samples
    # Monotonic sequence numbers are stamped into metadata (drops observable).
    assert [f.metadata.get("socket_seq") for f in audio] == list(range(n_frames))


# ---------------------------------------------------------------------------
# Start ordering: readiness precedes the first push (no None-queue race)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_start_ordering_no_none_queue_race(path):
    """A capturer that pushes immediately after connect must not hit a None queue.

    The reader pushes the very first frame the instant the connection is up; if
    ``start()`` spawned the reader before ``set_transport_ready`` created
    ``_audio_in_queue``, ``push_audio_frame`` would AttributeError on ``None``.
    The frame surfacing cleanly proves readiness preceded the first push.
    """
    pcm = pcm_from_samples([7, 7, 7])

    async def feed(writer: asyncio.StreamWriter):
        # Header then frames with NO pause — race the queue creation.
        writer.write(make_header())
        for i in range(3):
            writer.write(make_frame(i, pcm))
        await writer.drain()
        await asyncio.sleep(WAIT_TIMEOUT)

    server = _SocketWriterServer(path, feed)
    await server.start()
    harness = _ManualHarness(_make_transport(path))
    await harness.setup()
    try:
        await asyncio.wait_for(harness.start(), timeout=WAIT_TIMEOUT)
        # After start() returns, the queue exists and the reader is running.
        await _wait_until(lambda: len(harness.sink.audio_frames()) >= 3)
    finally:
        await asyncio.wait_for(harness.send(EndFrame()), timeout=WAIT_TIMEOUT)
        await server.aclose()

    # No ErrorFrame from a None-queue crash; frames surfaced.
    assert not harness.sink.error_frames()
    assert len(harness.sink.audio_frames()) >= 3


# ---------------------------------------------------------------------------
# Teardown: EndFrame AND CancelFrame each cancel _read_task + close socket
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "terminator", [EndFrame, CancelFrame], ids=["EndFrame", "CancelFrame"]
)
async def test_teardown_cancels_reader_and_closes_socket(path, terminator):
    """``EndFrame`` and ``CancelFrame`` must each cancel the reader and close the
    socket. The base only cancels its own drain task, so a non-overriding impl
    would leak ``_read_task`` past teardown — this asserts the subclass cleans up.
    """
    pcm = pcm_from_samples([1, 1])

    async def feed(writer: asyncio.StreamWriter):
        writer.write(make_header())
        writer.write(make_frame(0, pcm))
        await writer.drain()
        # Stay connected and silent-but-alive until torn down.
        await asyncio.sleep(WAIT_TIMEOUT)

    server = _SocketWriterServer(path, feed)
    await server.start()
    # Long idle timeout so the watchdog never fires during this test.
    harness = _ManualHarness(_make_transport(path, read_idle_timeout=WAIT_TIMEOUT * 2))
    await harness.setup()
    try:
        await asyncio.wait_for(harness.start(), timeout=WAIT_TIMEOUT)
        await _wait_until(lambda: len(harness.sink.audio_frames()) >= 1)

        read_task = harness.transport._read_task
        assert read_task is not None and not read_task.done()

        await asyncio.wait_for(harness.send(terminator()), timeout=WAIT_TIMEOUT)

        # Reader task torn down (no leak) and socket closed.
        assert harness.transport._read_task is None
        assert read_task.done()
        assert harness.transport._writer is None
        assert harness.transport._reader is None
    finally:
        await server.aclose()


# ---------------------------------------------------------------------------
# Handshake validation: refuse to start loudly on a bad header
# ---------------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "header_kwargs",
    [
        {"rate": 8000},
        {"width": 1},
        {"channels": 2},
        {"v": WIRE_VERSION + 99},
    ],
    ids=["bad-rate", "bad-width", "bad-channels", "bad-version"],
)
async def test_start_refuses_on_bad_handshake(path, header_kwargs):
    """A mismatched/unknown header must refuse to start loudly — not coerce.

    Accepts either failure shape the plan allows: ``start()`` raises
    (refuse-to-start) OR an ``ErrorFrame`` surfaces. Either way the reader must
    not have been spawned and no audio may surface.
    """

    async def feed(writer: asyncio.StreamWriter):
        writer.write(make_header(**header_kwargs))
        await writer.drain()
        await asyncio.sleep(WAIT_TIMEOUT)

    server = _SocketWriterServer(path, feed)
    await server.start()
    harness = _ManualHarness(_make_transport(path))
    await harness.setup()
    raised = False
    try:
        try:
            await asyncio.wait_for(harness.start(), timeout=WAIT_TIMEOUT)
        except SocketHandshakeError:
            raised = True
        await asyncio.sleep(0.05)
    finally:
        await server.aclose()

    surfaced_error = bool(harness.sink.error_frames())
    assert raised or surfaced_error, "bad handshake was not rejected loudly"
    # No audio coerced through, reader never started.
    assert not harness.sink.audio_frames()
    assert harness.transport._read_task is None


@pytest.mark.anyio
async def test_start_accepts_valid_handshake(path):
    """The happy path: a valid header starts the branch and audio flows."""
    pcm = pcm_from_samples([9, 9, 9])

    async def feed(writer: asyncio.StreamWriter):
        writer.write(make_header())
        for i in range(2):
            writer.write(make_frame(i, pcm))
        await writer.drain()
        await asyncio.sleep(WAIT_TIMEOUT)

    server = _SocketWriterServer(path, feed)
    await server.start()
    harness = _ManualHarness(_make_transport(path))
    await harness.setup()
    try:
        await asyncio.wait_for(harness.start(), timeout=WAIT_TIMEOUT)
        await _wait_until(lambda: len(harness.sink.audio_frames()) >= 2)
        assert not harness.sink.error_frames()
    finally:
        await asyncio.wait_for(harness.send(EndFrame()), timeout=WAIT_TIMEOUT)
        await server.aclose()


# ---------------------------------------------------------------------------
# Backpressure: bounded buffer, drop-oldest, queue-depth WARNING
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_backpressure_bounded_and_drops_oldest_with_warning(path):
    """A faster-than-consumer writer must cap memory and drop-oldest, not grow.

    Drives the bounded staging buffer directly (no consuming pump) so the policy
    is exercised deterministically: more frames in than the bound, buffer stays
    capped, oldest are dropped, a queue-depth WARNING is logged, and the newest
    frames survive.
    """
    bound = 3
    transport = _make_transport(
        path,
        max_buffered_frames=bound,
        backpressure_policy=BackpressurePolicy.DROP_OLDEST,
    )

    def mkframe(seq: int) -> InputAudioRawFrame:
        f = InputAudioRawFrame(
            audio=pcm_from_samples([seq]), sample_rate=16000, num_channels=1
        )
        f.metadata["socket_seq"] = seq
        return f

    warnings: list[str] = []
    sink_id = logger.add(lambda m: warnings.append(str(m)), level="WARNING")
    try:
        total = 10
        for i in range(total):
            await asyncio.wait_for(
                transport._stage_frame(mkframe(i)), timeout=WAIT_TIMEOUT
            )
    finally:
        logger.remove(sink_id)

    # Memory is bounded.
    assert transport._stage.qsize() <= bound
    assert transport._stage.maxsize == bound
    # Drops happened and were counted.
    assert transport._dropped_frames >= total - bound
    # A queue-depth WARNING was logged.
    assert any("buffer full" in w and "depth=" in w for w in warnings)
    # Drop-OLDEST: the surviving frames are the newest ones.
    survivors = []
    while not transport._stage.empty():
        survivors.append(transport._stage.get_nowait().metadata["socket_seq"])
    assert survivors == [total - bound, total - bound + 1, total - 1]


# ---------------------------------------------------------------------------
# Read-idle watchdog: connected-but-silent capturer surfaces an ErrorFrame
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_read_idle_watchdog_surfaces_error(path):
    """A capturer that connects + handshakes but never sends a frame must trip
    the idle watchdog and surface an ``ErrorFrame`` rather than hang forever."""

    async def feed(writer: asyncio.StreamWriter):
        writer.write(make_header())
        await writer.drain()
        # Alive but silent — no EOF ever fires.
        await asyncio.sleep(WAIT_TIMEOUT)

    server = _SocketWriterServer(path, feed)
    await server.start()
    harness = _ManualHarness(_make_transport(path, read_idle_timeout=0.2))
    await harness.setup()
    try:
        await asyncio.wait_for(harness.start(), timeout=WAIT_TIMEOUT)
        await _wait_until(
            lambda: bool(harness.sink.error_frames()), timeout=WAIT_TIMEOUT
        )
        errs = harness.sink.error_frames()
        assert errs
        assert "idle" in errs[0].error.lower()
        # No audio was fabricated.
        assert not harness.sink.audio_frames()
    finally:
        await asyncio.wait_for(harness.send(EndFrame()), timeout=WAIT_TIMEOUT)
        await server.aclose()


# ---------------------------------------------------------------------------
# Clean EOF: socket close surfaces an ErrorFrame and ends the branch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_clean_eof_surfaces_error_and_ends_branch(path):
    """When the capturer closes the socket, the branch surfaces an ``ErrorFrame``
    and ends — no self-reconnect (the supervisor owns restart)."""
    pcm = pcm_from_samples([3, 3, 3])

    async def feed(writer: asyncio.StreamWriter):
        writer.write(make_header())
        for i in range(2):
            writer.write(make_frame(i, pcm))
        await writer.drain()
        # Clean close at a frame boundary -> EOF.
        writer.close()

    server = _SocketWriterServer(path, feed)
    await server.start()
    # Long idle timeout so EOF, not the watchdog, is what fires.
    harness = _ManualHarness(_make_transport(path, read_idle_timeout=WAIT_TIMEOUT * 2))
    await harness.setup()
    try:
        await asyncio.wait_for(harness.start(), timeout=WAIT_TIMEOUT)
        await _wait_until(
            lambda: bool(harness.sink.error_frames()), timeout=WAIT_TIMEOUT
        )
        errs = harness.sink.error_frames()
        assert errs
        assert "eof" in errs[0].error.lower() or "closed" in errs[0].error.lower()
        # The read loop returned (no self-reconnect): the reader task is done.
        read_task = harness.transport._read_task
        assert read_task is not None
        await _wait_until(lambda: read_task.done(), timeout=WAIT_TIMEOUT)
    finally:
        await asyncio.wait_for(harness.send(EndFrame()), timeout=WAIT_TIMEOUT)
        await server.aclose()
