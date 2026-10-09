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


# --- segmented_stt_runs_inline: the stamping-soundness probe -------------------


def test_segmented_stt_runs_inline_true_for_the_locked_pipecat():
    """The locked pipecat (1.3.0) awaits ``run_stt`` inline on VAD-stop."""
    assert dt.segmented_stt_runs_inline() is True


def test_segmented_stt_runs_inline_false_for_a_queued_segment_shape(monkeypatch):
    """pipecat >= 1.9 enqueues the segment and a task runs ``run_stt`` later:
    the single-slot span would then be matched to the wrong segment."""
    from pipecat.services.stt_service import SegmentedSTTService

    async def _handle_user_stopped_speaking(self, frame):
        await self._segment_queue.put(b"audio")

    monkeypatch.setattr(
        SegmentedSTTService,
        "_handle_user_stopped_speaking",
        _handle_user_stopped_speaking,
    )
    assert dt.segmented_stt_runs_inline() is False


def test_segmented_stt_runs_inline_false_when_a_segment_task_exists(monkeypatch):
    """The class-level ``_segment_task_handler`` alone is enough to refuse, even
    if the stop handler's source still mentions ``self.run_stt(`` inline."""
    from pipecat.services.stt_service import SegmentedSTTService

    async def _segment_task_handler(self):  # pragma: no cover - never run
        return None

    monkeypatch.setattr(
        SegmentedSTTService,
        "_segment_task_handler",
        _segment_task_handler,
        raising=False,
    )
    assert dt.segmented_stt_runs_inline() is False


def test_segmented_stt_runs_inline_fails_closed_without_source(monkeypatch):
    import inspect

    def _no_source(obj):
        raise OSError("source not available")

    monkeypatch.setattr(inspect, "getsource", _no_source)
    assert dt.segmented_stt_runs_inline() is False
