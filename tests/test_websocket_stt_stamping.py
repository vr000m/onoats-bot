"""Phase 4 - `WebSocketSTTService` segment-sample stamping.

Design intent: each them-arm ``TranscriptionFrame`` is stamped with its VAD span
(``onoats_segment_start_sample`` / ``onoats_segment_end_sample``) via an ordered
queue of pending spans (one entry per VAD-stop, appended *before* delegating the
frame to ``super()`` and popped at the head of ``run_stt``); the me arm
(``stamp_segment_samples=False``) is never stamped.

Tests drive the REAL path (``process_frame(VADUserStoppedSpeakingFrame)``
-> ``SegmentedSTTService._handle_user_stopped_speaking`` -> ``run_stt``, inline
on pipecat <= 1.8 and via its segment-queue task on 1.9+) through
pipecat's ``run_test`` harness, with a stubbed deferred decode: a fake
``TranscriptionClient`` whose ``commit()`` queues a scripted
``transcript.completed`` event. Names asserted here come from plan Requirement 3
/ the Phase 4 block: ctor kwarg ``stamp_segment_samples``; frame attributes
``onoats_segment_start_sample`` / ``onoats_segment_end_sample``.
"""

from __future__ import annotations

import asyncio
import functools
import inspect

import pytest
from loguru import logger
from pipecat.frames.frames import (
    InputAudioRawFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.tests.utils import SleepFrame, run_test
from stt_server import protocol as P

from onoats.config.audio_devices import PIPELINE_SAMPLE_RATE
from onoats.processors.diarization_feeder import DiarizationFeeder
from onoats.stt import launchd
from onoats.stt import websocket_stt_service as wss_module
from onoats.stt.diarization_timeline import (
    DiarizationTimeline,
    SampleCounter,
    segmented_stt_orders_spans,
)
from onoats.stt.websocket_stt_service import WebSocketSTTService

START_ATTR = "onoats_segment_start_sample"
END_ATTR = "onoats_segment_end_sample"
CHUNK = 1600  # samples (100 ms @ 16 kHz)


@pytest.fixture(autouse=True)
def _clean_registry():
    launchd.REGISTRY.clear()
    yield
    launchd.REGISTRY.clear()


# ------------------------------------------------------------------ stubs


class _ScriptedClient:
    """Fake ``TranscriptionClient``: ``commit()`` is the deferred decode - it
    queues the next scripted transcript (``None`` entry -> empty text)."""

    instances: list[_ScriptedClient] = []
    script: list[str] = []

    def __init__(self, **kwargs):
        self._events: asyncio.Queue = asyncio.Queue()
        self.committed = 0
        _ScriptedClient.instances.append(self)

    async def connect(self):
        return {"backend": {"name": "test", "model": "test-model"}}

    async def update_session(self, **kwargs):
        await self._events.put({"type": P.EVT_SESSION_UPDATED})

    async def events(self):
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev

    async def send_audio(self, data):
        pass

    async def commit(self):
        self.committed += 1
        text = _ScriptedClient.script.pop(0) if _ScriptedClient.script else "hello"
        await self._events.put({"type": P.EVT_TRANSCRIPT_COMPLETED, "transcript": text})

    async def cancel(self):
        pass

    async def close_session(self):
        await self._events.put({"type": P.EVT_SESSION_CLOSED})

    async def close(self):
        await self._events.put(None)


@pytest.fixture(autouse=True)
def _fake_client(monkeypatch):
    _ScriptedClient.instances = []
    _ScriptedClient.script = []
    monkeypatch.setattr(wss_module, "TranscriptionClient", _ScriptedClient)
    monkeypatch.setattr(
        wss_module, "_RECONNECT_BACKOFF_SECONDS", (0.0, 0.0, 0.0, 0.0, 0.0)
    )
    yield
    _ScriptedClient.instances = []
    _ScriptedClient.script = []


def _svc(stamp: bool = True, **kw) -> WebSocketSTTService:
    return WebSocketSTTService(
        socket_path="/tmp/stt-stamping-test.sock",
        stamp_segment_samples=stamp,
        **kw,
    )


def _audio(n: int = CHUNK) -> InputAudioRawFrame:
    return InputAudioRawFrame(
        audio=b"\x01\x00" * n, sample_rate=PIPELINE_SAMPLE_RATE, num_channels=1
    )


def _vad_start() -> VADUserStartedSpeakingFrame:
    return VADUserStartedSpeakingFrame(start_secs=0.0, timestamp=0.0)


def _vad_stop() -> VADUserStoppedSpeakingFrame:
    return VADUserStoppedSpeakingFrame(stop_secs=0.0, timestamp=0.0)


def _segment(pre: int, speech: int) -> list:
    """``pre`` chunks of silence/pre-roll, VAD start, ``speech`` chunks, VAD stop."""
    return (
        [_audio() for _ in range(pre)]
        + [_vad_start()]
        + [_audio() for _ in range(speech)]
        + [_vad_stop()]
    )


@functools.cache
def _warm_pipecat_imports() -> None:
    """pipecat 1.9+ warms its deferred imports in a worker thread on the first
    pipeline start, which can outlast ``run_test``'s start timeout (1 s) and
    stall the event loop for minutes in a fresh venv. Do it once up front, in
    this thread, so the first test starts like every other."""
    try:
        from pipecat.utils.prewarm import warm_deferred_imports
    except ImportError:  # pipecat < 1.9
        return
    warm_deferred_imports()


def _run(svc, frames, *, errors: list | None = None):
    """Drive ``frames`` through the real service; returns the downstream
    ``TranscriptionFrame``s in order. ``errors`` collects ERROR log messages."""
    _warm_pipecat_imports()
    sink = None
    if errors is not None:
        sink = logger.add(
            lambda m: errors.append(m.record["message"]),
            level="ERROR",
        )
    try:
        down, _up = asyncio.run(
            asyncio.wait_for(
                run_test(
                    svc,
                    frames_to_send=[*frames, SleepFrame(0.2)],
                    send_end_frame=True,
                ),
                timeout=20,
            )
        )
    finally:
        if sink is not None:
            logger.remove(sink)
    return [f for f in down if isinstance(f, TranscriptionFrame)]


# ------------------------------------------------------------ stamping


def test_stamp_equals_vad_span_with_preroll_excluded():
    """Span == [counter at VAD start, counter at VAD stop). The 3 pre-roll
    chunks before VAD start (and any pipecat pre-roll buffer) are NOT in it."""
    svc = _svc()
    frames = _segment(pre=3, speech=2)
    out = _run(svc, frames)
    assert len(out) == 1
    assert getattr(out[0], START_ATTR) == 3 * CHUNK
    assert getattr(out[0], END_ATTR) == 5 * CHUNK
    assert out[0].text == "hello"


def test_two_consecutive_segments_each_get_their_own_span():
    svc = _svc()
    _ScriptedClient.script = ["one", "two"]
    frames = _segment(pre=1, speech=2) + _segment(pre=2, speech=1)
    out = _run(svc, frames)
    assert [f.text for f in out] == ["one", "two"]
    # seg1: start 1 chunk, end 3 chunks; seg2: 3 + 2 pre = 5, end 6.
    assert (getattr(out[0], START_ATTR), getattr(out[0], END_ATTR)) == (
        1 * CHUNK,
        3 * CHUNK,
    )
    assert (getattr(out[1], START_ATTR), getattr(out[1], END_ATTR)) == (
        5 * CHUNK,
        6 * CHUNK,
    )


def test_startframe_resets_the_sample_clock_and_open_state():
    """A ``StartFrame`` resets the sample clock (the feeder resets too) and any
    open VAD start / queued span. Driven directly: pipecat 1.9+ handles a
    ``StartFrame`` out of band, ahead of queued data frames, so it cannot be
    interleaved mid-stream through a pipeline."""
    svc = _svc()
    for f in [_audio(), _audio(), _vad_start(), _audio()]:
        svc._track_segment_span(f)
    svc._span_queue.append((1, 2))
    assert svc._sample_counter.samples == 3 * CHUNK

    svc._track_segment_span(StartFrame(audio_in_sample_rate=PIPELINE_SAMPLE_RATE))
    assert svc._sample_counter.samples == 0
    assert svc._vad_start_sample is None
    assert len(svc._span_queue) == 0

    for f in _segment(pre=1, speech=1):
        svc._track_segment_span(f)
    assert list(svc._span_queue) == [(1 * CHUNK, 2 * CHUNK)]


def test_kwarg_false_sets_no_attributes():
    svc = _svc(stamp=False)
    out = _run(svc, _segment(pre=2, speech=2))
    assert len(out) == 1
    assert not hasattr(out[0], START_ATTR)
    assert not hasattr(out[0], END_ATTR)


def test_default_kwarg_is_false():
    sig = inspect.signature(WebSocketSTTService.__init__)
    assert sig.parameters["stamp_segment_samples"].default is False
    out = _run(
        WebSocketSTTService(socket_path="/tmp/stt-stamping-test.sock"),
        _segment(pre=1, speech=1),
    )
    assert len(out) == 1
    assert not hasattr(out[0], START_ATTR)


# ----------------------------------------------- fail-loud slot discipline


def test_spans_stay_aligned_when_run_stt_is_deferred(monkeypatch):
    """pipecat 1.9+ queues each segment and a task runs ``run_stt`` later, so
    several spans are outstanding before the first ``run_stt`` starts. The stop
    handler is replaced by that queued shape (all three VAD-stops are seen,
    then the segments are decoded in order): each transcript must still carry
    its own segment's span."""
    svc = _svc()
    _ScriptedClient.script = ["one", "two", "three"]
    deferred: list[bytes] = []

    async def _queued(frame):
        svc._user_speaking = False
        deferred.append(bytes(svc._audio_buffer))
        svc._audio_buffer.clear()
        if len(deferred) == 3:
            for audio in deferred:
                await svc.process_generator(svc.run_stt(audio))

    monkeypatch.setattr(svc, "_handle_user_stopped_speaking", _queued)
    out = _run(
        svc,
        _segment(pre=1, speech=1)
        + _segment(pre=2, speech=2)
        + _segment(pre=0, speech=3),
    )
    assert [f.text for f in out] == ["one", "two", "three"]
    # seg1 [1,2); seg2 [2+2, 4+2)=[4,6)... counted in chunks from the clock.
    assert [(getattr(f, START_ATTR), getattr(f, END_ATTR)) for f in out] == [
        (1 * CHUNK, 2 * CHUNK),
        (4 * CHUNK, 6 * CHUNK),
        (6 * CHUNK, 9 * CHUNK),
    ]


def test_segment_dropped_by_an_unusable_service_leaves_no_span(monkeypatch):
    """pipecat 1.9+ drops the segment of a service that is not ``is_usable``;
    no ``run_stt`` will pop for it, so no entry may be queued."""
    monkeypatch.setattr(
        WebSocketSTTService, "is_usable", property(lambda self: False), raising=False
    )
    svc = _svc()
    for f in _segment(pre=1, speech=1):
        svc._track_segment_span(f)
    assert len(svc._span_queue) == 0
    assert svc._vad_start_sample is None


def test_run_stt_with_empty_slot_is_unstamped_without_crash():
    """``run_stt`` called with no captured span (no VAD frames at all) must
    yield an unstamped frame and not raise."""
    svc = _svc()

    async def _go():
        frames = []
        async for f in svc.run_stt(b"\x01\x00" * CHUNK):
            frames.append(f)
        return frames

    # run through the harness so the client/lock machinery is live: a bare
    # run_stt via a pipeline-less call still works because _ensure_connected is
    # self-contained.
    out = asyncio.run(_go())
    texts = [f for f in out if isinstance(f, TranscriptionFrame)]
    assert len(texts) == 1
    assert not hasattr(texts[0], START_ATTR)


def test_vad_stop_without_vad_start_is_unstamped_not_guessed():
    svc = _svc()
    errors: list[str] = []
    out = _run(svc, [_audio(), _audio(), _vad_stop()], errors=errors)
    assert len(out) == 1
    assert not hasattr(out[0], START_ATTR)
    assert errors


def test_double_vad_start_keeps_first_start_and_logs_error():
    svc = _svc()
    errors: list[str] = []
    frames = [_audio(), _vad_start(), _audio(), _vad_start(), _audio(), _vad_stop()]
    out = _run(svc, frames, errors=errors)
    assert len(out) == 1
    assert getattr(out[0], START_ATTR) == CHUNK  # first start, not the second
    assert getattr(out[0], END_ATTR) == 3 * CHUNK
    assert any("already open" in e for e in errors)


# ------------------------------------------------------- queue clearing


def test_queue_cleared_on_startframe():
    svc = _svc()
    svc._span_queue.append((123, 456))
    out = _run(svc, [_audio(), _audio()])
    assert out == []
    assert len(svc._span_queue) == 0


def test_reconnect_keeps_queued_spans_and_each_run_stt_pops_one():
    """A reconnect/discard must not touch queued spans (their segments have not
    reached ``run_stt`` yet); each ``run_stt`` pops exactly its own."""
    svc = _svc()
    svc._span_queue.extend([(1, 2), (3, 4)])

    async def _go():
        await svc._ensure_connected()
        await svc._discard_stale()
        assert list(svc._span_queue) == [(1, 2), (3, 4)]
        out = []
        for _ in range(2):
            async for f in svc.run_stt(b"\x01\x00" * CHUNK):
                out.append(f)
        return [f for f in out if isinstance(f, TranscriptionFrame)]

    frames = asyncio.run(_go())
    assert [(getattr(f, START_ATTR), getattr(f, END_ATTR)) for f in frames] == [
        (1, 2),
        (3, 4),
    ]
    assert len(svc._span_queue) == 0


# --------------------------------------------------------- clock parity


def test_clock_parity_with_feeder_counter(monkeypatch):
    """The same frame stream through the feeder's counter and the STT service's
    counter yields equal sample positions at every VAD edge."""
    feeder = DiarizationFeeder(DiarizationTimeline(), worker=_NullWorker())

    out_frames: list = []

    async def _base(self, frame, direction):
        return None

    async def _push(frame, direction=FrameDirection.DOWNSTREAM):
        out_frames.append(frame)

    ctx = monkeypatch.context()
    m = ctx.__enter__()
    m.setattr(type(feeder).__mro__[1], "process_frame", _base, raising=False)
    feeder.push_frame = _push  # type: ignore[method-assign]

    frames = (
        [StartFrame(audio_in_sample_rate=PIPELINE_SAMPLE_RATE)]
        + _segment(pre=2, speech=3)
        + _segment(pre=1, speech=1)
    )
    # Feeder positions at each VAD edge.
    edges: list[int] = []

    async def _drive_feeder():
        for f in frames:
            if isinstance(
                f, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
            ):
                edges.append(feeder.counter.samples)
            await feeder.process_frame(f, FrameDirection.DOWNSTREAM)

    try:
        asyncio.run(_drive_feeder())
    finally:
        ctx.__exit__(None, None, None)  # restore the real FrameProcessor

    svc = _svc()
    out = _run(svc, [f for f in frames if not isinstance(f, StartFrame)])
    assert len(out) == 2
    # edges = [start1, stop1, start2, stop2]
    assert (getattr(out[0], START_ATTR), getattr(out[0], END_ATTR)) == (
        edges[0],
        edges[1],
    )
    assert (getattr(out[1], START_ATTR), getattr(out[1], END_ATTR)) == (
        edges[2],
        edges[3],
    )
    # And the shared helper class is literally the same implementation.
    assert isinstance(feeder.counter, SampleCounter)
    assert isinstance(svc._sample_counter, SampleCounter)


class _NullWorker:
    thread = None
    stopped = True

    def start(self):
        pass

    def enqueue(self, *a, **k):
        return True

    def finish(self):
        pass

    def abort(self):
        pass

    def join(self, timeout=None):
        return True


# -------------------------------------------------- pipecat hook shape


def test_pipecat_hook_shape():
    """Pins the pipecat shape the stamping depends on, so a future pipecat that
    reorders segments or adds a reconnect-replay fails loudly."""
    # private attributes / signatures used
    assert hasattr(SegmentedSTTService, "_handle_user_stopped_speaking")
    assert hasattr(SegmentedSTTService, "_handle_user_started_speaking")
    assert hasattr(SegmentedSTTService, "process_audio_frame")
    sig = inspect.signature(SegmentedSTTService.process_frame)
    assert list(sig.parameters) == ["self", "frame", "direction"]
    assert list(inspect.signature(WebSocketSTTService.run_stt).parameters) == [
        "self",
        "audio",
    ]
    assert list(
        inspect.signature(SegmentedSTTService._handle_user_stopped_speaking).parameters
    ) == ["self", "frame"]

    # Segments reach run_stt in VAD-stop order (inline, or one in-order queue
    # task): the property the span queue depends on, probed the same way the
    # startup preflight probes it.
    assert segmented_stt_orders_spans() is True

    # ...and process_audio_frame has no reconnect-replay branch.
    paf = inspect.getsource(SegmentedSTTService.process_audio_frame).lower()
    for word in ("replay", "reconnect", "resend"):
        assert word not in paf


# --------------------------------------------------- decode-timeout ratio owner


def test_decode_timeout_uses_the_timeline_ratio(monkeypatch):
    """``run_stt`` scales its decode timeout by the timeline's
    ``DECODE_TIMEOUT_RATIO`` (one owner; the ring capacity is sized from the
    same constant), not a local literal: patching the imported binding changes
    the timeout actually passed to ``asyncio.wait_for``."""
    from onoats.stt import diarization_timeline as dt

    assert wss_module.DECODE_TIMEOUT_RATIO is dt.DECODE_TIMEOUT_RATIO

    ratio = 7.25
    monkeypatch.setattr(wss_module, "DECODE_TIMEOUT_RATIO", ratio)
    monkeypatch.setattr(wss_module, "_DECODE_TIMEOUT_SECONDS", 0.001)
    audio = b"\x01\x00" * CHUNK
    audio_seconds = len(audio) / (P.AUDIO_SAMPLE_RATE_HZ * P.AUDIO_SAMPLE_WIDTH_BYTES)
    timeouts: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def _spy(aw, timeout=None):
        timeouts.append(timeout)
        return await real_wait_for(aw, timeout)

    monkeypatch.setattr(wss_module.asyncio, "wait_for", _spy)
    svc = _svc(stamp=False)

    async def _go():
        return [f async for f in svc.run_stt(audio)]

    out = asyncio.run(_go())
    assert any(isinstance(f, TranscriptionFrame) for f in out)
    assert pytest.approx(ratio * audio_seconds) in timeouts
