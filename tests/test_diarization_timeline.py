"""Phase 2 — `onoats.stt.diarization_timeline`: the leaf module owning the
correlation clock, the worker-written / loop-read `DiarizationTimeline`, the
label policy and its named constants.

Interface assumptions (from plan Req 1; the plan does not pin these, so a
mismatch here is an interface finding, not necessarily an impl bug):
  * `record(start_sample, probs)` takes a 2-D numpy array shaped
    `(frames, slots)`, one row per 10 ms (160-sample) frame.
  * `label_for(start, end)` returns `Label(speakers: list[str], overlap: bool)`
    or `None`.
  * the shared counter helper is `SampleCounter` with `.advance(n) -> start`
    (returns the position of the first sample of the chunk) and `.samples`.
"""

from __future__ import annotations

import ast
import asyncio
import threading
from pathlib import Path

import numpy as np
import pytest
from loguru import logger

from onoats.stt import diarization_timeline as dt
from onoats.stt.diarization_timeline import DiarizationTimeline

SR = 16000
HOP = 160
FPS = SR // HOP  # frames per second


def probs(n_frames, **slots):
    """(n_frames, SLOTS) array; `s0=0.95` sets slot 0 to that probability."""
    n_slots = 8
    arr = np.zeros((n_frames, n_slots), dtype=np.float32)
    for name, val in slots.items():
        arr[:, int(name[1:])] = val
    return arr


ON = 0.99  # comfortably above any sane ACTIVITY_THRESHOLD


def seconds(s):
    return int(s * SR)


def frames(s):
    return int(s * FPS)


@pytest.fixture
def tl():
    t = DiarizationTimeline()
    t.set_slot_count(8)
    return t


@pytest.fixture
def warnings_log():
    msgs = []
    sink_id = logger.add(lambda m: msgs.append(m), level="WARNING", format="{message}")
    yield msgs
    logger.remove(sink_id)


# ---------------------------------------------------------------------------
# Leaf-module boundary
# ---------------------------------------------------------------------------


def test_module_imports_no_onoats_or_mlx():
    src = Path(dt.__file__).read_text()
    for node in ast.walk(ast.parse(src)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names = ["<relative>"]
            elif node.module:
                names = [node.module]
        for n in names:
            assert not n.startswith("onoats"), n
            assert n != "<relative>"
            assert "mlx" not in n, n


def test_constants_exist_and_sane():
    for name in (
        "ACTIVITY_THRESHOLD",
        "DOMINANT_MARGIN",
        "OVERLAP_MIN_FRACTION",
        "MIN_COVERAGE_FRACTION",
        "HOLD_SEC",
        "MAX_TURN_SEC",
        "DECODE_TIMEOUT_RATIO",
        "RING_HEADROOM_SEC",
    ):
        assert hasattr(dt, name), name
    assert 0 < dt.ACTIVITY_THRESHOLD < ON
    assert 0 < dt.OVERLAP_MIN_FRACTION < 1
    assert 0 < dt.MIN_COVERAGE_FRACTION <= 1
    assert dt.HOLD_SEC > 0 and dt.RING_HEADROOM_SEC > 0


def test_max_turn_sec_parity_with_stt_server_protocol():
    protocol = pytest.importorskip("stt_server.protocol")
    assert dt.MAX_TURN_SEC == protocol.MAX_UNCOMMITTED_SECONDS


# ---------------------------------------------------------------------------
# Correlation clock
# ---------------------------------------------------------------------------


def test_sample_counter_is_contiguous_and_monotonic():
    c = dt.SampleCounter()
    assert c.advance(320) == 0
    assert c.advance(160) == 320
    assert c.advance(1600) == 480
    assert c.samples == 2080


def test_two_counters_over_same_stream_agree():
    a, b = dt.SampleCounter(), dt.SampleCounter()
    for n in (320, 160, 480, 640, 160):
        assert a.advance(n) == b.advance(n)
    assert a.samples == b.samples


# ---------------------------------------------------------------------------
# Label policy
# ---------------------------------------------------------------------------


def test_is_ready_tracks_slot_count():
    t = DiarizationTimeline()
    assert not t.is_ready  # worker still loading the model
    t.set_slot_count(8)
    assert t.is_ready


def test_label_none_until_slot_count_set():
    t = DiarizationTimeline()
    t.record(0, probs(frames(1), s0=ON))
    assert t.label_for(0, seconds(1)) is None
    t.set_slot_count(8)
    label = t.label_for(0, seconds(1))
    assert label is not None and label.speakers == ["S1"]


def test_dominant_speaker(tl):
    tl.record(0, probs(frames(2), s0=ON))
    label = tl.label_for(0, seconds(2))
    assert label.speakers == ["S1"]
    assert label.overlap is False


def test_overlap_above_threshold(tl):
    tl.record(0, probs(frames(2), s0=ON, s1=ON))
    label = tl.label_for(0, seconds(2))
    assert label.overlap is True
    assert sorted(label.speakers) == ["S1", "S2"]


def test_overlap_below_threshold_is_dominant_only(tl):
    n = frames(10)
    arr = probs(n, s0=ON)
    short = max(1, int(n * dt.OVERLAP_MIN_FRACTION / 2))
    arr[:short, 1] = ON
    tl.record(0, arr)
    label = tl.label_for(0, seconds(10))
    assert label.speakers == ["S1"]
    assert label.overlap is False


def _sole_candidate_window(a_on, a_on_b, b_on, b_on_a):
    """150-row window: slot 0 (A) active 40 rows (fraction 0.27 -> the sole
    candidate), then slot 1 (B) active 25 rows (fraction 0.17), then silence.
    ``a_on_b`` is A's probability on B's rows, ``b_on_a`` B's on A's rows."""
    n_a, n_b, n = 40, 25, 150
    arr = probs(n)
    arr[:n_a, 0] = a_on
    arr[:n_a, 1] = b_on_a
    arr[n_a : n_a + n_b, 1] = b_on
    arr[n_a : n_a + n_b, 0] = a_on_b
    assert n_a / n >= dt.OVERLAP_MIN_FRACTION > n_b / n
    return arr, n * HOP


def test_top_not_sole_candidate_requires_margin(tl):
    # A barely active (sole candidate, score ~0.31), B louder but brief
    # (score ~0.38): B tops the ranking but leads by < DOMINANT_MARGIN.
    arr, end = _sole_candidate_window(0.51, 0.0, 1.0, 0.0)
    tl.record(0, arr)
    assert tl.label_for(0, end) is None


def test_top_not_sole_candidate_with_margin_is_labelled(tl):
    # Same shape, but B sits just below threshold under A (score ~0.66 vs
    # ~0.31): B leads by >= DOMINANT_MARGIN, so it is labelled.
    arr, end = _sole_candidate_window(0.51, 0.0, 1.0, 0.45)
    tl.record(0, arr)
    label = tl.label_for(0, end)
    assert label is not None
    assert label.speakers == ["S2"]
    assert label.overlap is False


def test_sole_candidate_on_top_needs_no_margin(tl):
    # A is the sole candidate AND top-scored (~0.48 vs ~0.35, lead below
    # DOMINANT_MARGIN): labelled A with no margin check.
    arr, end = _sole_candidate_window(0.6, 0.3, 0.9, 0.0)
    tl.record(0, arr)
    label = tl.label_for(0, end)
    assert label is not None
    assert label.speakers == ["S1"]
    assert label.overlap is False


def test_slot_mapping_by_first_activity(tl):
    # Slot 3 speaks first -> S1; slot 0 later -> S2.
    tl.record(0, probs(frames(2), s3=ON))
    tl.record(seconds(2), probs(frames(2), s0=ON))
    assert tl.label_for(0, seconds(2)).speakers == ["S1"]
    assert tl.label_for(seconds(2), seconds(4)).speakers == ["S2"]


def _long_tl():
    t = DiarizationTimeline(capacity_sec=100_000)
    t.set_slot_count(8)
    return t


def test_idle_slot_gets_a_fresh_label_never_a_reused_one():
    t = _long_tl()
    idle = dt.SLOT_IDLE_REMINT_SEC
    t.record(0, probs(frames(2), s3=ON))
    t.record(seconds(10), probs(frames(2), s0=ON))
    t.record(seconds(idle + 100), probs(frames(2), s3=ON))  # slot 3 again, much later
    assert t.label_for(0, seconds(2)).speakers == ["S1"]
    assert t.label_for(seconds(10), seconds(12)).speakers == ["S2"]
    # S3 - not S1: slot 3 may have been recycled for a different person.
    again = t.label_for(seconds(idle + 100), seconds(idle + 102))
    assert again.speakers == ["S3"]


def test_slot_active_again_within_the_idle_threshold_keeps_its_label():
    t = _long_tl()
    t.record(0, probs(frames(2), s3=ON))
    t.record(seconds(dt.SLOT_IDLE_REMINT_SEC - 10), probs(frames(2), s3=ON))
    assert t.label_for(0, seconds(2)).speakers == ["S1"]
    later = seconds(dt.SLOT_IDLE_REMINT_SEC - 10)
    assert t.label_for(later, later + seconds(2)).speakers == ["S1"]


def test_idle_is_measured_from_the_slots_last_active_frame_not_the_gap_start():
    t = _long_tl()
    step = dt.SLOT_IDLE_REMINT_SEC * 0.6  # each gap alone is under the threshold
    for i in range(4):
        t.record(seconds(i * step), probs(frames(2), s3=ON))
    last = seconds(3 * step)
    assert t.label_for(last, last + seconds(2)).speakers == ["S1"]


def test_remint_numbers_are_unbounded_past_the_slot_count():
    t = _long_tl()
    gap = dt.SLOT_IDLE_REMINT_SEC + 60
    for i in range(10):  # one slot, ten incarnations: S1..S10 with only 8 slots
        t.record(seconds(i * gap), probs(frames(2), s0=ON))
    last = seconds(9 * gap)
    assert t.label_for(last, last + seconds(2)).speakers == ["S10"]


def test_remint_applies_per_slot_and_overlap_uses_each_slots_generation():
    t = _long_tl()
    gap = dt.SLOT_IDLE_REMINT_SEC + 60
    t.record(0, probs(frames(2), s0=ON, s1=ON))  # S1 and S2
    start = seconds(gap)
    t.record(start, probs(frames(2), s0=ON, s1=ON))  # both idle too long: S3, S4
    label = t.label_for(start, start + seconds(2))
    assert label.overlap is True
    assert sorted(label.speakers) == ["S3", "S4"]


def test_out_of_order_recording_does_not_remint():
    t = _long_tl()
    t.record(seconds(500), probs(frames(2), s0=ON))
    t.record(0, probs(frames(2), s0=ON))  # earlier audio recorded later
    assert t.label_for(seconds(500), seconds(502)).speakers == ["S1"]
    assert t.label_for(0, seconds(2)).speakers == ["S1"]


def test_generations_are_pruned_with_ring_eviction(monkeypatch):
    monkeypatch.setattr(dt, "IDLE_REMINT_SAMPLES", seconds(5))
    t = DiarizationTimeline(capacity_sec=20)
    t.set_slot_count(8)
    for i in range(60):  # a new generation every 10 s, far beyond the ring
        t.record(seconds(i * 10), probs(frames(1), s0=ON))
    assert len(t._gens[0]) < 10
    last = seconds(59 * 10)
    assert t.label_for(last, last + seconds(1)).speakers == ["S60"]


def test_activity_weighted_average_ignores_silent_frames(tl):
    arr = np.concatenate([probs(frames(3)), probs(frames(1), s2=ON)])
    tl.record(0, arr)
    label = tl.label_for(0, seconds(4))
    assert label is not None and label.speakers == ["S1"]


def test_slot_overflow_returns_none_with_single_warning(warnings_log):
    t = DiarizationTimeline()
    t.set_slot_count(2)
    t.record(0, probs(frames(1), s0=ON))
    t.record(seconds(1), probs(frames(1), s5=ON))  # beyond num_speakers
    assert t.label_for(seconds(1), seconds(2)) is None
    assert t.label_for(seconds(1), seconds(2)) is None
    t.record(seconds(2), probs(frames(1), s6=ON))
    assert t.label_for(seconds(2), seconds(3)) is None
    assert len(warnings_log) == 1


def test_overflow_once_flag_is_per_instance(warnings_log):
    for _ in range(2):
        t = DiarizationTimeline()
        t.set_slot_count(2)
        t.record(0, probs(frames(1), s5=ON))
        assert t.label_for(0, seconds(1)) is None
    assert len(warnings_log) == 2


# ---------------------------------------------------------------------------
# Coverage: zero / partial / GAP
# ---------------------------------------------------------------------------


def test_zero_coverage_outside_history(tl):
    tl.record(0, probs(frames(1), s0=ON))
    assert tl.label_for(seconds(5), seconds(6)) is None


def test_gap_only_window_is_none(tl):
    tl.record(0, probs(frames(1), s0=ON))
    tl.record_gap(seconds(1), seconds(3))
    assert tl.label_for(seconds(1), seconds(3)) is None


def test_partial_coverage_threshold(tl):
    window = 10
    good = min(1.0, dt.MIN_COVERAGE_FRACTION + 0.05)
    tl.record(0, probs(frames(window * good), s0=ON))
    assert tl.label_for(0, seconds(window)) is not None

    t2 = DiarizationTimeline()
    t2.set_slot_count(8)
    bad = dt.MIN_COVERAGE_FRACTION / 2
    t2.record(0, probs(frames(window * bad), s0=ON))
    assert t2.label_for(0, seconds(window)) is None


def test_partial_coverage_with_gap_part(tl):
    window = 10
    bad = dt.MIN_COVERAGE_FRACTION / 2
    rec_frames = frames(window * bad)
    tl.record(0, probs(rec_frames, s0=ON))
    tl.record_gap(rec_frames * HOP, seconds(window))
    assert tl.label_for(0, seconds(window)) is None


# ---------------------------------------------------------------------------
# coverage_high_water
# ---------------------------------------------------------------------------


def test_high_water_advances_on_record_and_gap(tl):
    assert tl.coverage_high_water() == 0
    tl.record(0, probs(frames(1), s0=ON))
    assert tl.coverage_high_water() == seconds(1)
    tl.record(seconds(2), probs(frames(1), s0=ON))  # hole at [1s, 2s)
    assert tl.coverage_high_water() == seconds(1)
    tl.record_gap(seconds(1), seconds(2))  # GAP resolves the hole
    assert tl.coverage_high_water() == seconds(3)


def test_high_water_never_regresses_on_eviction(tl):
    cap = int(dt.MAX_TURN_SEC * (1 + dt.DECODE_TIMEOUT_RATIO) + dt.HOLD_SEC)
    last = 0
    pos = 0
    chunk = 100
    while pos < seconds(cap + dt.RING_HEADROOM_SEC + 3 * chunk):
        tl.record(pos, probs(frames(chunk), s0=ON))
        pos += seconds(chunk)
        hw = tl.coverage_high_water()
        assert hw >= last
        last = hw
    assert last == pos


# ---------------------------------------------------------------------------
# Ring capacity
# ---------------------------------------------------------------------------


def _fill(tl, total_sec, chunk=100):
    pos = 0
    remaining = total_sec
    while remaining > 0:
        c = min(chunk, remaining)
        tl.record(pos, probs(frames(c), s0=ON))
        pos += seconds(c)
        remaining -= c
    return pos


def test_ring_capacity_invariant(tl):
    cap = dt.MAX_TURN_SEC * (1 + dt.DECODE_TIMEOUT_RATIO) + dt.HOLD_SEC
    cap = int(np.ceil(cap + dt.RING_HEADROOM_SEC))
    _fill(tl, cap)
    label = tl.label_for(0, dt.MAX_TURN_SEC * SR)
    assert label is not None and label.speakers == ["S1"]


def test_ring_evicts_past_capacity(tl):
    cap = dt.MAX_TURN_SEC * (1 + dt.DECODE_TIMEOUT_RATIO) + dt.HOLD_SEC
    cap = int(np.ceil(cap + dt.RING_HEADROOM_SEC))
    _fill(tl, cap + int(dt.RING_HEADROOM_SEC) + 1)
    # The oldest second is gone: zero coverage there.
    assert tl.label_for(0, seconds(1)) is None
    # Recent history is intact.
    recent_end = seconds(cap)
    assert tl.label_for(recent_end - seconds(5), recent_end) is not None


# ---------------------------------------------------------------------------
# Listener / degraded / flushed
# ---------------------------------------------------------------------------


def test_progress_listener_fires_on_coverage_advance(tl):
    calls = []
    tl.add_progress_listener(lambda *a: calls.append(a))
    tl.record(0, probs(frames(1), s0=ON))
    n = len(calls)
    assert n >= 1
    tl.record_gap(seconds(1), seconds(2))
    assert len(calls) > n


def test_mark_degraded_idempotent_single_warning(tl, warnings_log):
    assert tl.is_degraded is False
    tl.mark_degraded()
    tl.mark_degraded(reason="stalled")
    tl.mark_degraded()
    assert tl.is_degraded is True
    assert len(warnings_log) == 1


def test_mark_flushed(tl):
    assert tl.is_flushed is False
    tl.mark_flushed()
    assert tl.is_flushed is True


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_writer_and_readers(tl):
    n_chunks, chunk_sec = 200, 1
    barrier = threading.Barrier(3)
    errors = []
    done = threading.Event()

    def writer():
        try:
            barrier.wait(5)
            for i in range(n_chunks):
                tl.record(seconds(i * chunk_sec), probs(frames(chunk_sec), s0=ON))
        except Exception as e:  # pragma: no cover
            errors.append(e)
        finally:
            done.set()

    def reader():
        try:
            barrier.wait(5)
            while not done.is_set():
                hw = tl.coverage_high_water()
                if hw >= seconds(1):
                    # Fully recorded window must always label.
                    assert tl.label_for(0, seconds(1)) is not None
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(target=f, daemon=True) for f in (writer, reader, reader)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert tl.label_for(0, seconds(n_chunks)) is not None


def test_record_interleaved_into_in_progress_label_for(tl):
    """Deterministic: a `record` from another thread, issued while `label_for`
    is mid-iteration over the segment list, must not leak into that snapshot.

    The late write targets the queried window and would turn its label from
    S1 into an S1+S2 overlap; with the lock held across the snapshot the
    writer blocks until `label_for` has finished iterating. Impl-internal hook
    (`_segments`), so skipped if the timeline's storage changes shape."""
    if not isinstance(getattr(tl, "_segments", None), list):
        pytest.skip("timeline has no `_segments` list to interpose on")
    tl.record(0, probs(frames(2), s0=ON))  # [0, 2s): S1
    tl.record(seconds(4), probs(frames(1), s1=ON))  # [4s, 5s): maps slot 1 -> S2
    window = (0, seconds(4))
    before = tl.label_for(*window)
    assert before is not None and before.speakers == ["S1"] and not before.overlap

    mid_iter = threading.Event()
    write_done = threading.Event()

    class PausingList(list):
        """Index-based iteration that pauses after the first element so a
        concurrent insert (if the lock let one in) would be observed."""

        def __iter__(self):
            i = 0
            while i < len(self):
                yield self[i]
                if i == 0 and not mid_iter.is_set():
                    mid_iter.set()
                    # Give a write that is NOT blocked by the lock time to land.
                    write_done.wait(0.5)
                i += 1

    tl._segments = PausingList(tl._segments)

    def writer():
        mid_iter.wait(5)
        tl.record(seconds(2), probs(frames(2), s1=ON))  # [2s, 4s): S2
        write_done.set()

    result = {}
    wt = threading.Thread(target=writer, daemon=True)
    rt = threading.Thread(
        target=lambda: result.setdefault("l", tl.label_for(*window)), daemon=True
    )
    wt.start()
    rt.start()
    rt.join(5)
    wt.join(5)
    assert not rt.is_alive() and not wt.is_alive()
    assert mid_iter.is_set() and write_done.is_set()
    # The in-progress read saw the pre-write snapshot only...
    assert result["l"] == before
    # ...and the write really does change this window once it lands.
    after = tl.label_for(*window)
    assert after is not None and after.overlap and after.speakers == ["S1", "S2"]


# --- segmented_stt_orders_spans: the stamping-soundness probe ------------------


def test_segmented_stt_orders_spans_true_for_the_locked_pipecat():
    """The locked pipecat (1.3.0) awaits ``run_stt`` inline on VAD-stop."""
    assert dt.segmented_stt_orders_spans() is True


# The pipecat 1.9-1.12 shape, verbatim in the parts the probe inspects.
def _init_fifo(self, **kwargs):  # pragma: no cover - never run
    self._segment_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    self._segment_task: asyncio.Task | None = None


async def _start_one_task(self, frame):  # pragma: no cover - never run
    self._segment_task = self.create_task(self._segment_task_handler())


async def _ordered_task_handler(self):  # pragma: no cover - never run
    running = True
    while running:
        audio = await self._segment_queue.get()
        running = audio is not None
        if audio:
            try:
                await self.process_generator(self.run_stt(audio))
            except Exception as e:
                await self.push_error(f"{self}: transcription failed: {e}", exception=e)


async def _stop_puts_one_segment(self, frame):  # pragma: no cover - never run
    self._user_speaking = False
    if not self.is_usable:
        self._audio_buffer.clear()
        return
    audio = bytes(self._audio_buffer)
    self._audio_buffer.clear()
    await self._segment_queue.put(audio)


def _patch_queued_shape(
    monkeypatch,
    task_handler=_ordered_task_handler,
    *,
    init=_init_fifo,
    start=None,
    stop_handler=_stop_puts_one_segment,
):
    from pipecat.services.stt_service import SegmentedSTTService

    monkeypatch.setattr(
        SegmentedSTTService, "_handle_user_stopped_speaking", stop_handler
    )
    monkeypatch.setattr(
        SegmentedSTTService, "_segment_task_handler", task_handler, raising=False
    )
    monkeypatch.setattr(SegmentedSTTService, "__init__", init)
    monkeypatch.setattr(SegmentedSTTService, "start", start or _start_one_task)


def test_segmented_stt_orders_spans_true_for_pipecat_1_9_queue_shape(monkeypatch):
    """pipecat >= 1.9 enqueues the segment and ONE task takes segments off the
    queue and awaits ``run_stt`` for each in turn: still VAD-stop order."""
    _patch_queued_shape(monkeypatch)
    assert dt.segmented_stt_orders_spans() is True


def _init_lifo(self, **kwargs):  # pragma: no cover - never run
    self._segment_queue = asyncio.LifoQueue()


def _init_priority(self, **kwargs):  # pragma: no cover - never run
    self._segment_queue = asyncio.PriorityQueue()


def _init_unrecognised(self, **kwargs):  # pragma: no cover - never run
    self._segment_queue = self._make_segment_queue()


def _init_bounded(self, **kwargs):  # pragma: no cover - never run
    self._segment_queue = asyncio.Queue(maxsize=4)


def _init_twice(self, **kwargs):  # pragma: no cover - never run
    self._segment_queue = asyncio.Queue()
    self._segment_queue = asyncio.LifoQueue()


@pytest.mark.parametrize(
    "init",
    [_init_lifo, _init_priority, _init_unrecognised, _init_bounded, _init_twice],
    ids=["lifo", "priority", "unrecognised", "bounded-kwargs", "reassigned"],
)
def test_segmented_stt_orders_spans_false_unless_the_queue_is_a_plain_fifo(
    monkeypatch, init
):
    """Regression: the probe read only the put/get/run_stt method bodies, so a
    LIFO / priority queue behind the same ``put``/``get`` calls passed and a
    span could be popped by another segment's ``run_stt``. Anything but one
    plain ``asyncio.Queue()`` construction is unrecognised and fails closed."""
    _patch_queued_shape(monkeypatch, init=init)
    assert dt.segmented_stt_orders_spans() is False


async def _start_two_tasks(self, frame):  # pragma: no cover - never run
    self._segment_task = self.create_task(self._segment_task_handler())
    self._extra_task = self.create_task(self._segment_task_handler())


async def _start_in_a_loop(self, frame):  # pragma: no cover - never run
    for _ in range(2):
        self._segment_task = self.create_task(self._segment_task_handler())


async def _start_without_the_task(self, frame):  # pragma: no cover - never run
    pass


@pytest.mark.parametrize(
    "start",
    [_start_two_tasks, _start_in_a_loop, _start_without_the_task],
    ids=["two-tasks", "loop", "no-task"],
)
def test_segmented_stt_orders_spans_false_unless_start_creates_one_consumer(
    monkeypatch, start
):
    """Regression: two consumers draining one FIFO can still finish out of
    order; the probe never looked at how many handler tasks ``start`` makes."""
    _patch_queued_shape(monkeypatch, start=start)
    assert dt.segmented_stt_orders_spans() is False


def test_segmented_stt_orders_spans_false_with_a_second_queue_consumer(monkeypatch):
    """Another method also taking segments off the queue is a second consumer."""
    from pipecat.services.stt_service import SegmentedSTTService

    _patch_queued_shape(monkeypatch)

    async def _steal(self):  # pragma: no cover - never run
        await self._segment_queue.get()

    monkeypatch.setattr(SegmentedSTTService, "_steal", _steal, raising=False)
    assert dt.segmented_stt_orders_spans() is False


def test_segmented_stt_orders_spans_false_when_segments_run_concurrently(monkeypatch):
    """A queue drained by tasks spawned per segment can finish out of order, so
    a span could be popped by another segment's ``run_stt``."""

    async def _segment_task_handler(self):  # pragma: no cover - never run
        audio = await self._segment_queue.get()
        self.create_task(self.run_stt(audio))

    _patch_queued_shape(monkeypatch, _segment_task_handler)
    assert dt.segmented_stt_orders_spans() is False


async def _handler_one_shot(self):  # pragma: no cover - never run
    audio = await self._segment_queue.get()
    await self.process_generator(self.run_stt(audio))


async def _handler_run_stt_outside_loop(self):  # pragma: no cover - never run
    running = True
    audio = None
    while running:
        audio = await self._segment_queue.get()
        running = audio is not None
    await self.process_generator(self.run_stt(audio))


async def _handler_two_run_stt_sites(self):  # pragma: no cover - never run
    running = True
    while running:
        audio = await self._segment_queue.get()
        running = audio is not None
        if audio:
            await self.process_generator(self.run_stt(audio))
            await self.process_generator(self.run_stt(audio))


async def _handler_returns_inside_loop(self):  # pragma: no cover - never run
    while True:
        audio = await self._segment_queue.get()
        await self.process_generator(self.run_stt(audio))
        return


async def _handler_breaks_inside_loop(self):  # pragma: no cover - never run
    running = True
    while running:
        audio = await self._segment_queue.get()
        running = audio is not None
        if audio:
            await self.process_generator(self.run_stt(audio))
        break


async def _handler_stops_on_a_non_sentinel(self):  # pragma: no cover - never run
    running = True
    while running:
        audio = await self._segment_queue.get()
        running = len(self._segment_queue._queue) > 0
        if audio:
            await self.process_generator(self.run_stt(audio))


async def _handler_other_condition(self):  # pragma: no cover - never run
    running = True
    while running:
        audio = await self._segment_queue.get()
        running = audio is not None
        if audio and self._muted is False:
            await self.process_generator(self.run_stt(audio))


async def _handler_run_stt_not_awaited(self):  # pragma: no cover - never run
    running = True
    while running:
        audio = await self._segment_queue.get()
        running = audio is not None
        if audio:
            self.process_generator(self.run_stt(audio))


@pytest.mark.parametrize(
    "task_handler",
    [
        _handler_one_shot,
        _handler_run_stt_outside_loop,
        _handler_two_run_stt_sites,
        _handler_returns_inside_loop,
        _handler_breaks_inside_loop,
        _handler_stops_on_a_non_sentinel,
        _handler_other_condition,
        _handler_run_stt_not_awaited,
    ],
    ids=[
        "one-shot",
        "run_stt-outside-loop",
        "two-run_stt-sites",
        "return-in-loop",
        "break-in-loop",
        "loop-ends-without-sentinel",
        "extra-run_stt-condition",
        "run_stt-not-awaited",
    ],
)
def test_segmented_stt_orders_spans_false_unless_the_reader_drains_one_for_one(
    monkeypatch, task_handler
):
    """Regression: the probe checked for one ``get`` and one awaited
    ``run_stt`` but never for a loop, so a handler that transcribes ONE segment
    and returns (or skips/duplicates ``run_stt``) passed - later segments and
    their spans then stay queued or pop the wrong span. The reader must be one
    loop, ended only by the ``None`` sentinel, awaiting exactly one ``run_stt``
    per real segment."""
    _patch_queued_shape(monkeypatch, task_handler)
    assert dt.segmented_stt_orders_spans() is False


async def _stop_puts_twice(self, frame):  # pragma: no cover - never run
    await self._segment_queue.put(b"a")
    await self._segment_queue.put(b"b")


async def _stop_puts_conditionally(self, frame):  # pragma: no cover - never run
    if self._audio_buffer:
        await self._segment_queue.put(bytes(self._audio_buffer))


async def _stop_drops_on_another_condition(self, frame):  # pragma: no cover
    if len(self._audio_buffer) < 320:
        return
    await self._segment_queue.put(bytes(self._audio_buffer))


async def _stop_puts_none(self, frame):  # pragma: no cover - never run
    await self._segment_queue.put(None)


@pytest.mark.parametrize(
    "stop_handler",
    [
        _stop_puts_twice,
        _stop_puts_conditionally,
        _stop_drops_on_another_condition,
        _stop_puts_none,
    ],
    ids=["two-puts", "conditional-put", "unmirrored-drop", "puts-sentinel"],
)
def test_segmented_stt_orders_spans_false_unless_each_stop_puts_one_segment(
    monkeypatch, stop_handler
):
    """Regression: the STT appends one span per VAD-stop (skipping only when
    ``is_usable`` is false, mirroring pipecat), so the stop handler must put
    exactly one segment, unconditionally once past that guard; anything else
    strands a span or pairs it with another segment."""
    _patch_queued_shape(monkeypatch, stop_handler=stop_handler)
    assert dt.segmented_stt_orders_spans() is False


def test_segmented_stt_orders_spans_false_with_a_second_segment_producer(
    monkeypatch,
):
    """Only the VAD-stop handler may put a segment; any other put must be the
    ``None`` finish sentinel (pipecat's ``stop``)."""
    from pipecat.services.stt_service import SegmentedSTTService

    _patch_queued_shape(monkeypatch)

    async def _finish(self):  # pragma: no cover - never run
        await self._segment_queue.put(None)

    async def _flush_early(self):  # pragma: no cover - never run
        await self._segment_queue.put(bytes(self._audio_buffer))

    monkeypatch.setattr(SegmentedSTTService, "_finish", _finish, raising=False)
    assert dt.segmented_stt_orders_spans() is True
    monkeypatch.setattr(
        SegmentedSTTService, "_flush_early", _flush_early, raising=False
    )
    assert dt.segmented_stt_orders_spans() is False


def test_segmented_stt_orders_spans_false_with_two_inline_run_stt_sites(monkeypatch):
    """The inline (<= 1.8) shape must also await ``run_stt`` once per stop."""
    from pipecat.services.stt_service import SegmentedSTTService

    if "_segment_task_handler" in vars(SegmentedSTTService):
        pytest.skip("locked pipecat is not the inline shape")

    async def _handle_user_stopped_speaking(self, frame):  # pragma: no cover
        await self.process_generator(self.run_stt(b"a"))
        await self.process_generator(self.run_stt(b"b"))

    monkeypatch.setattr(
        SegmentedSTTService,
        "_handle_user_stopped_speaking",
        _handle_user_stopped_speaking,
    )
    assert dt.segmented_stt_orders_spans() is False


def test_segmented_stt_orders_spans_false_for_an_unrecognised_shape(monkeypatch):
    """Neither inline nor the queue-and-one-task shape (e.g. a handler that
    hands the segment to some other mechanism)."""
    from pipecat.services.stt_service import SegmentedSTTService

    async def _handle_user_stopped_speaking(self, frame):
        self.schedule_somewhere_else(frame)

    monkeypatch.setattr(
        SegmentedSTTService,
        "_handle_user_stopped_speaking",
        _handle_user_stopped_speaking,
    )
    assert dt.segmented_stt_orders_spans() is False


def test_segmented_stt_orders_spans_fails_closed_without_source(monkeypatch):
    import inspect

    def _no_source(obj):
        raise OSError("source not available")

    monkeypatch.setattr(inspect, "getsource", _no_source)
    assert dt.segmented_stt_orders_spans() is False
