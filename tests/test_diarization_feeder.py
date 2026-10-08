"""Phase 3 - `onoats.processors.diarization_feeder`: pass-through feeder +
single daemon worker thread. Stub diarizers only; `mlx_audio` is never imported.

Interface assumptions (the plan pins behaviour, not names; a mismatch is an
interface finding): `DiarizationFeeder(timeline, diarizer_factory=None, *,
worker=None, join_timeout_sec=...)`, `DiarizerWorker(timeline, factory, *,
queue_max, overrun_consecutive, clock)` with `start/enqueue/finish/abort/join`
and `.thread`, `feeder.counter` (a `SampleCounter`), and the model contract
`set_streaming_config("low")`, `config.num_speakers`, `init_streaming_state()`,
`feed(audio, state, *, sample_rate, final=False) -> (output, state)`.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import threading

import numpy as np
import pytest
from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    InputAudioRawFrame,
    StartFrame,
    TextFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from onoats.config.audio_devices import PIPELINE_SAMPLE_RATE
from onoats.processors import diarization_feeder as df
from onoats.stt import diarization_timeline as dt
from onoats.stt.diarization_timeline import DiarizationTimeline

CHUNK = 1600  # samples (100 ms = 10 diarizer frames)
JOIN = 5.0


# --------------------------------------------------------------------- stubs


class _State:
    def __init__(self, frames_processed=0):
        self.frames_processed = frames_processed


class _Out:
    def __init__(self, speaker_probs):
        self.speaker_probs = speaker_probs


class _Cfg:
    num_speakers = 8


class StubDiarizer:
    """Model stub. The active slot for a chunk is chosen by its first int16
    sample (marker value): 1000 -> slot 0, 2000 -> slot 1, else slot 2."""

    def __init__(self, log, *, delay_cb=None, raise_on_feed=None, block=None):
        self.log = log
        self.config = _Cfg()
        self.sample_rate = PIPELINE_SAMPLE_RATE
        self.feed_calls = []  # (audio, sample_rate, final, thread_id)
        self.thread_ids = {}
        self._delay_cb = delay_cb
        self._raise = raise_on_feed
        self._block = block
        self.log.append("load")

    def set_streaming_config(self, preset):
        self.thread_ids["config"] = threading.get_ident()
        self.log.append(f"set_streaming_config({preset})")

    def init_streaming_state(self):
        return _State(0)

    def feed(self, audio, state, *, sample_rate=16000, final=False, **kw):
        self.thread_ids.setdefault("feed", set()).add(threading.get_ident())
        self.log.append("feed")
        self.feed_calls.append(
            (audio.copy(), sample_rate, final, threading.get_ident())
        )
        if self._block is not None:
            self._block.wait()
        if self._raise is not None:
            raise self._raise
        if self._delay_cb is not None:
            self._delay_cb()
        n_frames = len(audio) // dt.FRAME_SAMPLES
        probs = np.zeros((n_frames, 8), dtype=np.float32)
        if n_frames:
            marker = int(round(float(audio[0]) * 32768.0))
            slot = {1000: 0, 2000: 1}.get(marker, 2)
            probs[:, slot] = 0.99
        return _Out(probs), _State(state.frames_processed + n_frames)


class RecordingTimeline(DiarizationTimeline):
    """Records the thread + order of every write call."""

    def __init__(self, log=None):
        super().__init__()
        self.log = log if log is not None else []
        self.write_threads = set()

    def _w(self, name):
        self.write_threads.add(threading.get_ident())
        self.log.append(name)

    def set_slot_count(self, n):
        self._w(f"set_slot_count({n})")
        super().set_slot_count(n)

    def record(self, start_sample, probs):
        self._w("record")
        super().record(start_sample, probs)

    def record_gap(self, start, end):
        self._w("record_gap")
        super().record_gap(start, end)

    def mark_flushed(self):
        self._w("mark_flushed")
        super().mark_flushed()


def pcm(marker: int, n=CHUNK) -> bytes:
    a = np.full(n, marker, dtype="<i2")
    return a.tobytes()


def audio_frame(marker=1000, n=CHUNK):
    return InputAudioRawFrame(
        audio=pcm(marker, n), sample_rate=PIPELINE_SAMPLE_RATE, num_channels=1
    )


@pytest.fixture
def warnings_seen():
    msgs = []
    hid = logger.add(lambda m: msgs.append(m.record["message"]), level="WARNING")
    yield msgs
    logger.remove(hid)


def make_feeder(timeline, model, *, worker=None, join=JOIN, **kw):
    """Feeder whose factory runs `model_factory` (returns `model`)."""
    return df.DiarizationFeeder(
        timeline,
        (lambda: model) if not callable(model) else model,
        worker=worker,
        join_timeout_sec=join,
        **kw,
    )


def drive(feeder, frames, monkeypatch, *, cleanup=True):
    """Push `frames` through the feeder; return the frames it pushed on.

    `cleanup` mirrors the pipeline task reaping processors after the terminal
    frame (the feeder joins its worker there, not on EndFrame/CancelFrame).
    """
    out = []

    async def _base(self, frame, direction):
        return None

    async def _push(frame, direction=FrameDirection.DOWNSTREAM):
        out.append(frame)

    monkeypatch.setattr(df.FrameProcessor, "process_frame", _base)
    feeder.push_frame = _push  # type: ignore[method-assign]

    async def _run():
        for f in frames:
            await feeder.process_frame(f, FrameDirection.DOWNSTREAM)
        if cleanup:
            await feeder.cleanup()

    asyncio.run(_run())
    return out


def start_frame():
    return StartFrame(audio_in_sample_rate=PIPELINE_SAMPLE_RATE)


# --------------------------------------------------------------- module hygiene


def test_import_does_not_pull_mlx_audio():
    code = (
        "import sys, onoats.processors.diarization_feeder, onoats.dual;"
        "assert 'mlx_audio' not in sys.modules, 'mlx_audio imported'"
    )
    r = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr


def test_mlx_audio_absent_after_stub_run(monkeypatch):
    log = []
    tl = RecordingTimeline()
    feeder = make_feeder(tl, StubDiarizer(log))
    drive(feeder, [start_frame(), audio_frame(), EndFrame()], monkeypatch)
    assert "mlx_audio" not in sys.modules


def test_pipeline_rate_matches_model_rate():
    assert PIPELINE_SAMPLE_RATE == 16000 == dt.SAMPLE_RATE_HZ


def test_preflight_and_loader_share_one_model_owner():
    """The startup preflight must probe exactly what the worker's loader needs.

    Both sides use the single owner, ``onoats.stt.diarization_model`` (a
    leaf, so ``runtime`` need not import the feeder and its pipecat/numpy
    graph): same repo-id object, same cache probe.
    """
    from onoats import runtime
    from onoats.stt import diarization_model as dm

    assert runtime.DIARIZATION_REPO_ID is dm.REPO_ID
    assert not hasattr(df, "DIARIZATION_MODEL_ID")  # no second name to drift
    assert runtime._diarization_weights_cached is dm.weights_cached
    assert df.diarization_model is dm
    assert not hasattr(df, "_cached_snapshot_dir")
    assert not hasattr(runtime, "_DIARIZATION_WEIGHT_FILES")


def test_model_rate_mismatch_degrades(monkeypatch, warnings_seen):
    log = []
    m = StubDiarizer(log)
    m.sample_rate = 8000
    tl = RecordingTimeline()
    feeder = make_feeder(tl, m)
    out = drive(feeder, [start_frame(), audio_frame(), EndFrame()], monkeypatch)
    assert tl.is_degraded
    assert not any(isinstance(f, ErrorFrame) for f in out)
    assert m.feed_calls == []


# --------------------------------------------------------------- observer contract


def test_frames_pass_through_unchanged_in_order(monkeypatch):
    tl = RecordingTimeline()
    feeder = make_feeder(tl, StubDiarizer([]))
    start = start_frame()
    a1, a2 = audio_frame(), audio_frame(2000)
    snap = (a1.audio, a2.audio)
    txt = TextFrame(text="hi")
    end = EndFrame()
    out = drive(feeder, [start, a1, txt, a2, end], monkeypatch)
    assert out == [start, a1, txt, a2, end]
    assert all(x is y for x, y in zip(out, [start, a1, txt, a2, end]))
    assert (a1.audio, a2.audio) == snap  # not mutated


def test_non_audio_frames_are_not_counted_or_enqueued(monkeypatch):
    tl = RecordingTimeline()
    m = StubDiarizer([])
    feeder = make_feeder(tl, m)
    drive(feeder, [start_frame(), TextFrame(text="x"), EndFrame()], monkeypatch)
    assert feeder.counter.samples == 0
    assert [c for c in m.feed_calls if not c[2]] == []


def test_sample_counting_and_start_sample_tags(monkeypatch):
    tl = RecordingTimeline()
    m = StubDiarizer([])
    feeder = make_feeder(tl, m)
    sizes = [CHUNK, 800, CHUNK, 320]
    frames = [start_frame()] + [audio_frame(1000, n) for n in sizes] + [EndFrame()]
    drive(feeder, frames, monkeypatch)
    assert feeder.counter.samples == sum(sizes)
    # Worker consumed every chunk contiguously: coverage reaches the total.
    assert tl.coverage_high_water() >= sum(sizes) - dt.FRAME_SAMPLES


def test_counter_resets_on_start_frame(monkeypatch):
    tl = RecordingTimeline()
    feeder = make_feeder(tl, StubDiarizer([]))
    drive(feeder, [start_frame(), audio_frame()], monkeypatch)
    assert feeder.counter.samples == CHUNK
    drive(feeder, [start_frame()], monkeypatch)
    assert feeder.counter.samples == 0
    feeder.worker.abort()
    feeder.worker.join(JOIN)


# --------------------------------------------------------------- worker contract


def test_worker_owns_all_model_calls_and_timeline_writes(monkeypatch):
    log = []
    ids = {}

    def factory():
        ids["factory"] = threading.get_ident()
        return model

    model = StubDiarizer(log)
    tl = RecordingTimeline(log)
    feeder = make_feeder(tl, factory)
    drive(
        feeder,
        [start_frame(), audio_frame(), audio_frame(), EndFrame()],
        monkeypatch,
    )
    worker_id = ids["factory"]
    assert worker_id != threading.get_ident()
    assert model.thread_ids["config"] == worker_id
    assert model.thread_ids["feed"] == {worker_id}  # incl. final flush
    assert tl.write_threads == {worker_id}
    assert feeder.worker.thread.daemon is True


def test_call_sequence(monkeypatch):
    log = []
    model = StubDiarizer(log)
    tl = RecordingTimeline(log)
    feeder = make_feeder(tl, model)
    drive(feeder, [start_frame(), audio_frame(), EndFrame()], monkeypatch)
    assert log[:4] == [
        "load",
        "set_streaming_config(low)",
        "set_slot_count(8)",
        "feed",
    ]


def test_set_streaming_config_before_first_feed(monkeypatch):
    log = []
    feeder = make_feeder(RecordingTimeline(log), StubDiarizer(log))
    drive(
        feeder, [start_frame(), audio_frame(), audio_frame(), EndFrame()], monkeypatch
    )
    assert log.index("set_streaming_config(low)") < log.index("feed")


def test_feed_receives_1d_float32_scaled(monkeypatch):
    model = StubDiarizer([])
    feeder = make_feeder(RecordingTimeline(), model)
    drive(feeder, [start_frame(), audio_frame(1000), EndFrame()], monkeypatch)
    audio, rate, final, _ = model.feed_calls[0]
    assert final is False
    assert audio.ndim == 1 and audio.dtype == np.float32
    assert audio.shape[0] == CHUNK
    np.testing.assert_allclose(audio, 1000 / 32768.0, rtol=0, atol=1e-9)
    assert rate == PIPELINE_SAMPLE_RATE


def test_negative_pcm_scaling(monkeypatch):
    model = StubDiarizer([])
    feeder = make_feeder(RecordingTimeline(), model)
    drive(feeder, [start_frame(), audio_frame(-32768), EndFrame()], monkeypatch)
    np.testing.assert_allclose(model.feed_calls[0][0], -1.0)


# --------------------------------------------------------------- gap / alignment


def _run_worker(tl, model, chunks, **wkw):
    """Direct worker use: chunks = [(start_sample, pcm)], then finish."""
    w = df.DiarizerWorker(tl, lambda: model, **wkw)
    for s, p in chunks:
        assert w.enqueue(s, p)
    w.start()
    w.finish()
    assert w.join(JOIN)
    return w


def test_missing_start_sample_range_records_gap_and_realigns():
    tl = RecordingTimeline()
    model = StubDiarizer([])
    # chunk A @0 (slot0), chunk B @1600 MISSING (dropped), chunk C @3200 (slot1)
    _run_worker(tl, model, [(0, pcm(1000)), (3200, pcm(2000))])
    assert "record_gap" in [x for x in tl.log]
    # Later window labels against the feeder clock, not the model's.
    lab_c = tl.label_for(3200, 4800)
    assert lab_c is not None and lab_c.speakers == ["S2"] and not lab_c.overlap
    lab_a = tl.label_for(0, 1600)
    assert lab_a is not None and lab_a.speakers == ["S1"]
    # The dropped range is resolved-but-unlabelled.
    assert tl.label_for(1600, 3200) is None
    assert tl.coverage_high_water() >= 4800 - dt.FRAME_SAMPLES


def test_real_queue_overflow_drop_oldest_realigns():
    tl = RecordingTimeline()
    model = StubDiarizer([])
    w = df.DiarizerWorker(tl, lambda: model, queue_max=2)
    # Not started yet, so nothing consumes: the first chunk is dropped.
    for i, marker in enumerate((1000, 2000, 2000)):
        w.enqueue(i * CHUNK, pcm(marker))
    w.start()
    w.finish()
    assert w.join(JOIN)
    assert tl.label_for(CHUNK, 2 * CHUNK).speakers == ["S1"]  # slot 1 first seen -> S1
    assert tl.label_for(0, CHUNK) is None  # dropped range = GAP / uncovered


def test_contiguous_chunks_record_no_gap():
    tl = RecordingTimeline()
    _run_worker(
        tl,
        StubDiarizer([]),
        [(0, pcm(1000)), (CHUNK, pcm(1000)), (2 * CHUNK, pcm(1000))],
    )
    assert "record_gap" not in tl.log


# --------------------------------------------------------------- degrade


def test_loader_raises_degrades_once_and_feeder_keeps_passing(
    monkeypatch, warnings_seen
):
    def boom():
        raise RuntimeError("no weights")

    tl = RecordingTimeline()
    feeder = make_feeder(tl, boom)
    frames = [
        start_frame(),
        audio_frame(),
        TextFrame(text="x"),
        audio_frame(),
        EndFrame(),
    ]
    out = drive(feeder, frames, monkeypatch)
    assert out == frames
    assert tl.is_degraded
    assert not any(isinstance(f, ErrorFrame) for f in out)
    assert len(warnings_seen) == 1, warnings_seen


def test_feed_raises_degrades_exactly_one_warning_no_error_frame(
    monkeypatch, warnings_seen
):
    model = StubDiarizer([], raise_on_feed=RuntimeError("metal exploded"))
    tl = RecordingTimeline()
    feeder = make_feeder(tl, model)
    frames = [start_frame()] + [audio_frame() for _ in range(5)] + [EndFrame()]
    out = drive(feeder, frames, monkeypatch)
    assert out == frames
    assert tl.is_degraded and not tl.is_flushed
    assert not any(isinstance(f, ErrorFrame) for f in out)
    assert len(warnings_seen) == 1, warnings_seen
    assert feeder.worker.join(JOIN)


def test_no_enqueue_after_degrade_but_frames_still_pass(monkeypatch):
    tl = RecordingTimeline()
    model = StubDiarizer([])
    feeder = make_feeder(tl, model)
    enq = []
    real = feeder.worker.enqueue
    feeder.worker.enqueue = lambda s, p: (enq.append(s), real(s, p))[1]  # type: ignore[method-assign]
    tl.mark_degraded("test")
    frames = [start_frame(), audio_frame(), audio_frame(), EndFrame()]
    out = drive(feeder, frames, monkeypatch)
    assert out == frames
    assert enq == []
    assert feeder.counter.samples == 2 * CHUNK  # clock still advances
    assert not any(isinstance(f, ErrorFrame) for f in out)


def test_worker_stops_accepting_after_runtime_failure():
    tl = RecordingTimeline()
    model = StubDiarizer([], raise_on_feed=RuntimeError("x"))
    w = df.DiarizerWorker(tl, lambda: model)
    w.start()
    w.enqueue(0, pcm(1000))
    assert w.join(JOIN)
    assert tl.is_degraded
    assert w.enqueue(CHUNK, pcm(1000)) is False


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _overrun_model(clock, delay_for):
    """Each feed() call ``i`` (0-based, incl. final flush) costs ``delay_for(i)``."""
    calls = iter(range(10**9))

    def delay():
        clock.t += delay_for(next(calls))

    return StubDiarizer([], delay_cb=delay)


#: CHUNK-sized calls per budget window (100 ms chunks, 1 s window -> 10).
PER_WIN = int(df.BUDGET_WINDOW_SEC * PIPELINE_SAMPLE_RATE) // CHUNK


def _chunks(n):
    return [(i * CHUNK, pcm(1000)) for i in range(n)]


def test_budget_window_constant():
    assert df.BUDGET_WINDOW_SEC == 1.0
    assert PER_WIN == 10


def test_single_slow_window_does_not_degrade():
    clock = _FakeClock()
    # One 10 s call in window 0, then fast windows.
    model = _overrun_model(clock, lambda i: 10.0 if i == 0 else 0.001)
    tl = RecordingTimeline()
    _run_worker(tl, model, _chunks(PER_WIN * 4), clock=clock)
    assert not tl.is_degraded
    assert tl.is_flushed


def test_consecutive_overrun_windows_degrade_once_no_error_frame(warnings_seen):
    clock = _FakeClock()
    n = df.BUDGET_OVERRUN_CONSECUTIVE
    model = _overrun_model(clock, lambda i: 10.0)
    tl = RecordingTimeline()
    w = df.DiarizerWorker(tl, lambda: model, clock=clock)
    for s, p in _chunks(PER_WIN * (n + 2)):
        w.enqueue(s, p)
    w.start()
    w.finish()
    assert w.join(JOIN)
    assert tl.is_degraded
    assert not tl.is_flushed
    assert len(warnings_seen) == 1, warnings_seen
    # Degraded exactly at the end of the n-th window, not per call.
    assert len(model.feed_calls) == PER_WIN * n


def test_overrun_windows_must_be_consecutive():
    clock = _FakeClock()
    n = df.BUDGET_OVERRUN_CONSECUTIVE
    # (n-1) over-budget windows then one in-budget window, repeated.
    pattern = [10.0] * (n - 1) + [0.001]

    def delay_for(i):
        return pattern[(i // PER_WIN) % n] / PER_WIN

    model = _overrun_model(clock, delay_for)
    tl = RecordingTimeline()
    _run_worker(tl, model, _chunks(PER_WIN * n * 3), clock=clock)
    assert not tl.is_degraded
    assert tl.is_flushed


def test_bursty_slow_model_degrades_despite_cheap_buffering_calls(warnings_seen):
    """Regression: the model buffers most feed() calls cheaply and computes
    in bursts. Per-call budgeting let each cheap call reset the overrun count,
    so a slower-than-real-time model never degraded."""
    clock = _FakeClock()
    n = df.BUDGET_OVERRUN_CONSECUTIVE
    # 9 cheap calls + 1 compute call (1.0 s) per 1.0 s of audio: 1.009x RT.
    model = _overrun_model(
        clock, lambda i: 1.0 if i % PER_WIN == PER_WIN - 1 else 0.001
    )
    tl = RecordingTimeline()
    _run_worker(tl, model, _chunks(PER_WIN * (n + 3)), clock=clock)
    assert tl.is_degraded
    assert not tl.is_flushed
    assert len(warnings_seen) == 1, warnings_seen


def test_bursty_model_single_slow_compute_window_does_not_degrade():
    clock = _FakeClock()
    # Only one compute call is slow; the rest of the session is real-time-safe.
    model = _overrun_model(
        clock,
        lambda i: 1.0 if i == PER_WIN - 1 else (0.05 if i % PER_WIN == 9 else 0.001),
    )
    tl = RecordingTimeline()
    _run_worker(tl, model, _chunks(PER_WIN * 8), clock=clock)
    assert not tl.is_degraded
    assert tl.is_flushed


# --------------------------------------------------------------- lookahead + gap


class LookaheadDiarizer(StubDiarizer):
    """Mimics the real ``low`` preset: no output until FIRST frames are
    buffered, then bursts of >= BURST frames while holding HOLD frames of
    lookahead; ``final=True`` drains everything. ``frames_processed`` counts
    emitted rows (row r describes model-input frame r)."""

    FIRST = 110
    HOLD = 100
    BURST = 72

    def __init__(self):
        super().__init__([])
        self.slots: list[int] = []  # per model-input frame
        self.batches: list[tuple[int, int]] = []  # (first row, n rows)

    def feed(self, audio, state, *, sample_rate=16000, final=False, **kw):
        self.feed_calls.append((audio.copy(), sample_rate, final, 0))
        n_frames = len(audio) // dt.FRAME_SAMPLES
        if n_frames:
            marker = int(round(float(audio[0]) * 32768.0))
            self.slots += [{1000: 0, 2000: 1}.get(marker, 2)] * n_frames
        emitted = state.frames_processed
        buffered = len(self.slots) - emitted
        if final:
            k = buffered
        elif emitted == 0:
            k = buffered - self.HOLD if buffered >= self.FIRST else 0
        else:
            k = buffered - self.HOLD if buffered - self.HOLD >= self.BURST else 0
        probs = np.zeros((k, 8), dtype=np.float32)
        for j in range(k):
            probs[j, self.slots[emitted + j]] = 0.99
        if k:
            self.batches.append((emitted, k))
        return _Out(probs), _State(emitted + k)


def test_lookahead_rows_for_pre_gap_audio_land_before_gap():
    """Regression: with model lookahead, rows emitted AFTER a gap re-anchor
    still describe PRE-gap audio. They must map through the anchor in effect
    when that input was fed, not the post-gap anchor (which shifted them onto
    the GAP segment, left the true range unresolved, and stalled coverage)."""
    n_chunks = 40
    drop = 20  # first chunk of the second half
    chunks = [
        (i * CHUNK, pcm(1000 if i < drop else 2000))
        for i in range(n_chunks)
        if i != drop
    ]
    gap = (drop * CHUNK, (drop + 1) * CHUNK)
    total = n_chunks * CHUNK
    model = LookaheadDiarizer()
    tl = RecordingTimeline()
    _run_worker(tl, model, chunks)
    assert tl.is_flushed and not tl.is_degraded

    # The model really did hold lookahead and emit a batch spanning the
    # pre/post-gap boundary in model-input frames.
    boundary_row = drop * CHUNK // dt.FRAME_SAMPLES
    assert model.batches[0][0] == 0 and model.batches[0][1] < model.FIRST
    assert any(r0 < boundary_row < r0 + k for r0, k in model.batches)

    # Every pipeline sample resolved exactly once: segments tile [0, total).
    segs = sorted(tl._segments, key=lambda s: s.start)
    pos = 0
    for seg in segs:
        assert seg.start == pos, (seg.start, pos)
        if seg.probs is not None:
            assert seg.end - seg.start == seg.probs.shape[0] * dt.FRAME_SAMPLES
        pos = seg.end
    assert pos == total
    gaps = [(s.start, s.end) for s in segs if s.probs is None]
    assert gaps == [gap]
    for seg in segs:
        if seg.probs is not None:
            assert seg.end <= gap[0] or seg.start >= gap[1]
            # Pre-gap rows carry slot 0, post-gap rows slot 1.
            want = 0 if seg.end <= gap[0] else 1
            assert (seg.probs[:, want] >= 0.5).all()

    assert tl.coverage_high_water() == total
    # Labels align with the feeder clock on both sides of the gap.
    assert tl.label_for(0, gap[0]).speakers == ["S1"]
    assert tl.label_for(gap[0] - CHUNK, gap[0]).speakers == ["S1"]
    assert tl.label_for(gap[1], gap[1] + CHUNK).speakers == ["S2"]
    assert tl.label_for(gap[1], total).speakers == ["S2"]
    assert tl.label_for(*gap) is None


def test_lookahead_coverage_advances_past_gap_before_flush():
    """Coverage must pass the gap while streaming, not only at final flush."""
    model = LookaheadDiarizer()
    tl = RecordingTimeline()
    drop = 20
    chunks = [(i * CHUNK, pcm(1000)) for i in range(60) if i != drop]
    hw_at_final = []
    real_feed = model.feed

    def feed(audio, state, *, final=False, **kw):
        if final:
            hw_at_final.append(tl.coverage_high_water())
        return real_feed(audio, state, final=final, **kw)

    model.feed = feed  # type: ignore[method-assign]
    _run_worker(tl, model, chunks)
    assert tl.coverage_high_water() == 60 * CHUNK
    # Streaming rows (before the final flush) already moved coverage past it.
    assert hw_at_final and hw_at_final[0] > (drop + 1) * CHUNK


# --------------------------------------------------------------- lifecycle


def test_endframe_final_flush_marks_flushed_and_thread_exits(monkeypatch):
    model = StubDiarizer([])
    tl = RecordingTimeline()
    feeder = make_feeder(tl, model)
    frames = [start_frame(), audio_frame(), audio_frame(), EndFrame()]
    out = drive(feeder, frames, monkeypatch)
    assert out == frames
    assert tl.is_flushed and not tl.is_degraded
    finals = [c for c in model.feed_calls if c[2]]
    assert len(finals) == 1 and finals[0][1] == PIPELINE_SAMPLE_RATE
    assert model.feed_calls[-1][2] is True  # flush is last
    assert not feeder.worker.thread.is_alive()


def test_endframe_drains_queued_chunks_before_flush(monkeypatch):
    model = StubDiarizer([])
    feeder = make_feeder(RecordingTimeline(), model)
    drive(
        feeder,
        [start_frame()] + [audio_frame() for _ in range(6)] + [EndFrame()],
        monkeypatch,
    )
    assert [c[2] for c in model.feed_calls] == [False] * 6 + [True]


def test_cancelframe_aborts_without_flush_and_thread_exits(monkeypatch):
    model = StubDiarizer([])
    tl = RecordingTimeline()
    feeder = make_feeder(tl, model)
    cancel = CancelFrame()
    out = drive(feeder, [start_frame(), audio_frame(), cancel], monkeypatch)
    assert out[-1] is cancel
    assert not feeder.worker.thread.is_alive()
    assert not tl.is_flushed
    assert not any(c[2] for c in model.feed_calls)  # no final flush on abort


def test_blocked_feed_cannot_hang_endframe(monkeypatch, warnings_seen):
    release = threading.Event()
    model = StubDiarizer([], block=release)
    tl = RecordingTimeline()
    feeder = make_feeder(tl, model, join=0.3)
    frames = [start_frame(), audio_frame(), EndFrame()]
    done = {}

    def run():
        done["out"] = drive(feeder, frames, monkeypatch)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(8)
    try:
        assert not t.is_alive(), "EndFrame hung behind a blocked feed()"
        assert done["out"] == frames
        assert feeder.worker.thread.daemon is True
        assert tl.is_degraded and not tl.is_flushed
        assert not any(isinstance(f, ErrorFrame) for f in done["out"])
    finally:
        release.set()
        feeder.worker.join(JOIN)


def test_blocked_feed_cannot_hang_cancelframe(monkeypatch):
    release = threading.Event()
    model = StubDiarizer([], block=release)
    feeder = make_feeder(RecordingTimeline(), model, join=0.3)
    frames = [start_frame(), audio_frame(), CancelFrame()]
    done = {}

    t = threading.Thread(
        target=lambda: done.setdefault("out", drive(feeder, frames, monkeypatch)),
        daemon=True,
    )
    t.start()
    t.join(8)
    try:
        assert not t.is_alive()
        assert done["out"] == frames
    finally:
        release.set()
        feeder.worker.join(JOIN)


def test_terminal_frames_are_forwarded_without_waiting_for_the_worker(monkeypatch):
    """EndFrame/CancelFrame must reach the STT while the worker is still stuck
    in ``feed()``: the join is deferred to ``cleanup()`` so it cannot eat the
    shutdown budget."""
    for terminal in (EndFrame, CancelFrame):
        release = threading.Event()
        model = StubDiarizer([], block=release)
        tl = RecordingTimeline()
        feeder = make_feeder(tl, model, join=30.0)  # a join here would hang
        out = []

        async def _base(self, frame, direction):
            return None

        async def _push(frame, direction=FrameDirection.DOWNSTREAM):
            out.append(frame)

        monkeypatch.setattr(df.FrameProcessor, "process_frame", _base)
        feeder.push_frame = _push  # type: ignore[method-assign]
        term = terminal()

        async def go():
            await feeder.process_frame(start_frame(), FrameDirection.DOWNSTREAM)
            await feeder.process_frame(audio_frame(), FrameDirection.DOWNSTREAM)
            while not model.feed_calls:  # worker is now parked inside feed()
                await asyncio.sleep(0.005)
            await asyncio.wait_for(
                feeder.process_frame(term, FrameDirection.DOWNSTREAM), 5.0
            )
            assert out[-1] is term
            assert feeder.worker.thread.is_alive()
            assert not tl.is_degraded

        try:
            asyncio.run(go())
        finally:
            release.set()
            feeder.worker.join(JOIN)


def test_cleanup_without_terminal_frame_aborts_the_worker(monkeypatch):
    model = StubDiarizer([])
    tl = RecordingTimeline()
    feeder = make_feeder(tl, model)
    drive(feeder, [start_frame(), audio_frame()], monkeypatch)  # cleanup only
    assert not feeder.worker.thread.is_alive()
    assert not tl.is_flushed and not tl.is_degraded
    assert not any(c[2] for c in model.feed_calls)  # aborted, no final flush
