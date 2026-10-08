"""Phase 5 - `onoats.processors.diarization_labeler.DiarizationLabeler`.

Frame attributes only (no ``TranscriptBuffer``). The timeline is the real
``DiarizationTimeline`` driven directly (``set_slot_count`` / ``record`` /
``record_gap`` / ``mark_flushed``), never via the feeder.

Time: the labeler takes an injectable ``clock``. Its release task also waits in
real time (``asyncio.wait_for``), so expiry tests use a tiny ``hold_sec`` plus a
manually advanced fake clock and a short real sleep to let the task observe it;
"still held" assertions leave the fake clock untouched.
"""

from __future__ import annotations

import asyncio
import threading
import time

import numpy as np
import pytest
from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from onoats.config.audio_devices import PIPELINE_SAMPLE_RATE
from onoats.processors import diarization_labeler as dl
from onoats.stt.diarization_timeline import FRAME_SAMPLES, DiarizationTimeline

SLOTS = 2
HOLD = 0.05  # real-time hold used with the fake clock
SETTLE = 0.2  # real time to let the release task run


# ------------------------------------------------------------------- helpers


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


def rows(n, slot, p=0.9):
    """``n`` diarizer rows (10 ms each) with ``slot`` active."""
    arr = np.zeros((n, SLOTS), dtype=np.float32)
    arr[:, slot] = p
    return arr


def make_timeline(**kw):
    tl = DiarizationTimeline(**kw)
    tl.set_slot_count(SLOTS)
    return tl


def tframe(text, start=None, end=None):
    f = TranscriptionFrame(text=text, user_id="u", timestamp="t")
    if start is not None:
        f.onoats_segment_start_sample = start
        f.onoats_segment_end_sample = end
    return f


def label_of(frame):
    return (
        getattr(frame, "onoats_speakers", None),
        getattr(frame, "onoats_overlap", None),
    )


@pytest.fixture
def warnings_seen():
    msgs = []
    hid = logger.add(lambda m: msgs.append(m.record["message"]), level="WARNING")
    yield msgs
    logger.remove(hid)


class Harness:
    """Runs a labeler inside one event loop, capturing pushed frames."""

    def __init__(self, labeler, monkeypatch):
        self.labeler = labeler
        self.out = []
        self.times = []

        async def _base(self_, frame, direction):
            return None

        async def _push(frame, direction=FrameDirection.DOWNSTREAM):
            if isinstance(frame, StartFrame):
                return
            self.out.append(frame)
            self.times.append(time.monotonic())

        monkeypatch.setattr(dl.FrameProcessor, "process_frame", _base)
        labeler.push_frame = _push  # type: ignore[method-assign]

    async def send(self, frame, direction=FrameDirection.DOWNSTREAM):
        await self.labeler.process_frame(frame, direction)

    async def start(self):
        await self.send(StartFrame(audio_in_sample_rate=PIPELINE_SAMPLE_RATE))

    async def settle(self, t=SETTLE):
        await asyncio.sleep(t)

    async def close(self):
        await self.labeler.cleanup()

    def texts(self):
        return [getattr(f, "text", type(f).__name__) for f in self.out]


def run(coro):
    asyncio.run(coro)


def make(monkeypatch, tl, *, hold_sec=HOLD, clock=None, **kw):
    clock = clock or FakeClock()
    lab = dl.DiarizationLabeler(tl, hold_sec=hold_sec, clock=clock, **kw)
    return Harness(lab, monkeypatch), clock


# ------------------------------------------------------- pass-through / basics


def test_unstamped_transcription_passes_through_untouched(monkeypatch):
    tl = make_timeline()
    tl.record(0, rows(20, 0))
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    f = tframe("hello")  # no stamped range

    async def go():
        await h.start()
        t0 = time.monotonic()
        await h.send(f)
        assert h.out[-1] is f  # immediate, in-line
        assert time.monotonic() - t0 < 1.0
        assert label_of(f) == (None, None)
        await h.close()

    run(go())


def test_non_transcription_frames_pass_through(monkeypatch):
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    vad = VADUserStartedSpeakingFrame(start_secs=0.2)

    async def go():
        await h.start()
        await h.send(vad)
        assert vad in h.out
        await h.close()

    run(go())


def test_covered_frame_labelled_immediately(monkeypatch):
    tl = make_timeline()
    tl.record(0, rows(20, 0))
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    f = tframe("hi", 0, 20 * FRAME_SAMPLES)

    async def go():
        await h.start()
        await h.send(f)
        assert h.out[-1] is f
        assert label_of(f) == (["S1"], False)
        await h.close()

    run(go())


# ------------------------------------------------------------------ hold/release


def test_held_until_worker_progress_callback_releases(monkeypatch):
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=30.0)  # clock never expires it
    f = tframe("hold", 0, 20 * FRAME_SAMPLES)

    async def go():
        await h.start()
        await h.send(f)
        await h.settle()
        assert f not in h.out  # still held, coverage 0

        # Worker thread records coverage; no further frame traffic at all.
        th = threading.Thread(target=lambda: tl.record(0, rows(20, 0)))
        th.start()
        th.join()
        await h.settle()
        assert h.out == [f]
        assert label_of(f) == (["S1"], False)
        await h.close()

    run(go())


def test_partial_coverage_keeps_holding_until_end_reached(monkeypatch):
    """N chunks behind: released only when coverage reaches the segment end."""
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    f = tframe("slow", 0, 30 * FRAME_SAMPLES)

    async def go():
        await h.start()
        await h.send(f)
        for i in range(2):  # two chunks of 10 rows: still short of 30
            tl.record(i * 10 * FRAME_SAMPLES, rows(10, 0))
            await h.settle(0.1)
            assert f not in h.out
        tl.record(20 * FRAME_SAMPLES, rows(10, 0))
        await h.settle()
        assert h.out == [f]
        assert label_of(f) == (["S1"], False)
        await h.close()

    run(go())


def test_released_at_hold_sec_with_no_further_traffic(monkeypatch):
    tl = make_timeline()
    h, clock = make(monkeypatch, tl)
    f = tframe("late", 0, 20 * FRAME_SAMPLES)

    async def go():
        await h.start()
        await h.send(f)
        await h.settle()
        assert f not in h.out  # fake clock not advanced -> still held
        clock.advance(HOLD + 0.01)
        await h.settle()
        assert h.out == [f]  # released by the deadline alone
        assert label_of(f) == (None, None)  # no coverage -> no attributes
        await h.close()

    run(go())


def test_expiry_with_partial_coverage_labels_what_exists(monkeypatch):
    tl = make_timeline()
    tl.record(0, rows(10, 1))  # first half covered; second half pending
    h, clock = make(monkeypatch, tl)
    f = tframe("half", 0, 20 * FRAME_SAMPLES)

    async def go():
        await h.start()
        await h.send(f)
        clock.advance(HOLD + 0.01)
        await h.settle()
        assert h.out == [f]
        assert label_of(f) == (["S1"], False)  # slot 1 is the first mapped -> S1
        await h.close()

    run(go())


def test_evicted_from_ring_released_unlabelled(monkeypatch):
    # Tiny ring: early audio is evicted before its frame arrives.
    tl = make_timeline(capacity_sec=0.5)
    for i in range(10):  # 10 x 100 ms = 1 s of audio, ring keeps ~0.5 s
        tl.record(i * 10 * FRAME_SAMPLES, rows(10, 0))
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    f = tframe("old", 0, 10 * FRAME_SAMPLES)

    async def go():
        await h.start()
        await h.send(f)
        await h.settle()
        assert h.out == [f]  # coverage already past it: released, no hold
        assert label_of(f) == (None, None)  # evicted -> no label
        await h.close()

    run(go())


def test_vad_frame_reaches_downstream_before_held_transcription(monkeypatch):
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    f = tframe("held", 0, 20 * FRAME_SAMPLES)
    vad = VADUserStartedSpeakingFrame(start_secs=0.2)

    async def go():
        await h.start()
        await h.send(f)
        await h.send(vad)
        assert h.out[-1] is vad and f not in h.out
        tl.record(0, rows(20, 0))
        await h.settle()
        assert h.out[-2:] == [vad, f]
        await h.close()

    run(go())


def test_two_held_frames_release_fifo(monkeypatch):
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    a = tframe("a", 0, 10 * FRAME_SAMPLES)
    b = tframe("b", 10 * FRAME_SAMPLES, 20 * FRAME_SAMPLES)

    async def go():
        await h.start()
        await h.send(a)
        await h.send(b)
        # Cover b's range first via a second span; a's range is still missing,
        # so head-of-line a must block b (coverage is contiguous anyway).
        tl.record(10 * FRAME_SAMPLES, rows(10, 1))
        await h.settle(0.1)
        assert a not in h.out and b not in h.out
        tl.record(0, rows(10, 0))
        await h.settle()
        assert h.out == [a, b]
        await h.close()

    run(go())


def test_continuation_s1_then_s2_clock_monotonic(monkeypatch):
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    s1 = tframe("one", 0, 20 * FRAME_SAMPLES)
    s2 = tframe("two", 20 * FRAME_SAMPLES, 40 * FRAME_SAMPLES)

    async def go():
        await h.start()
        await h.send(s1)
        await h.send(s2)
        tl.record(0, rows(20, 0))
        tl.record(20 * FRAME_SAMPLES, rows(20, 1))
        await h.settle()
        assert h.out == [s1, s2]
        assert label_of(s1) == (["S1"], False)
        assert label_of(s2) == (["S2"], False)
        assert tl.coverage_high_water() == 40 * FRAME_SAMPLES
        await h.close()

    run(go())


# ------------------------------------------------------------- Cancel / End


def test_cancel_frame_releases_immediately(monkeypatch):
    tl = make_timeline()
    tl.record(0, rows(10, 0))  # partial coverage at cancel time
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    f = tframe("c", 0, 20 * FRAME_SAMPLES)
    cancel = CancelFrame()

    async def go():
        await h.start()
        await h.send(f)
        await h.settle(0.05)
        assert f not in h.out
        t0 = time.monotonic()
        await h.send(cancel)
        assert time.monotonic() - t0 < 2.0  # nowhere near hold_sec=30
        assert h.out[-2:] == [f, cancel]
        await h.close()

    run(go())


def test_endframe_waits_for_flush_then_releases_before_endframe(monkeypatch):
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=10.0, clock=time.monotonic)
    f = tframe("end", 0, 20 * FRAME_SAMPLES)
    end = EndFrame()

    def worker_flush():
        time.sleep(0.2)
        tl.record(0, rows(20, 1))  # flush-completed coverage
        tl.mark_flushed()

    async def go():
        await h.start()
        await h.send(f)
        th = threading.Thread(target=worker_flush)
        th.start()
        t0 = time.monotonic()
        await h.send(end)
        elapsed = time.monotonic() - t0
        th.join()
        assert 0.15 <= elapsed < 5.0  # waited for the flush, returned early
        assert h.out == [f, end]  # held frame strictly before EndFrame
        assert label_of(f) == (["S1"], False)  # labelled with flushed coverage
        await h.close()

    run(go())


def test_endframe_without_flush_waits_at_most_hold_sec(monkeypatch):
    tl = make_timeline()
    hold = 0.3
    h, _ = make(monkeypatch, tl, hold_sec=hold, clock=time.monotonic)
    f = tframe("end", 0, 20 * FRAME_SAMPLES)
    end = EndFrame()

    async def go():
        await h.start()
        await h.send(f)
        t0 = time.monotonic()
        await h.send(end)
        elapsed = time.monotonic() - t0
        assert elapsed < hold + 1.0  # bounded by HOLD_SEC (plus slack)
        assert h.out == [f, end]
        assert label_of(f) == (None, None)  # never covered -> unlabelled
        await h.close()

    run(go())


def test_endframe_with_nothing_pending_forwards_promptly(monkeypatch):
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=30.0, clock=time.monotonic)
    end = EndFrame()

    async def go():
        await h.start()
        t0 = time.monotonic()
        await h.send(end)
        assert time.monotonic() - t0 < 2.0
        assert h.out == [end]
        await h.close()

    run(go())


def test_endframe_releases_multiple_held_fifo(monkeypatch):
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=10.0, clock=time.monotonic)
    a = tframe("a", 0, 10 * FRAME_SAMPLES)
    b = tframe("b", 10 * FRAME_SAMPLES, 20 * FRAME_SAMPLES)
    end = EndFrame()

    async def go():
        await h.start()
        await h.send(a)
        await h.send(b)
        tl.record(0, rows(20, 0))
        tl.mark_flushed()
        await h.send(end)
        assert h.out == [a, b, end]
        await h.close()

    run(go())


# ------------------------------------------------------- degraded / stall guard


def test_degraded_forwards_without_hold_and_without_attributes(
    monkeypatch, warnings_seen
):
    tl = make_timeline()
    tl.record(0, rows(20, 0))  # coverage exists but must NOT be applied
    tl.mark_degraded("test")
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    f = tframe("deg", 0, 40 * FRAME_SAMPLES)  # uncovered AND degraded

    async def go():
        await h.start()
        t0 = time.monotonic()
        await h.send(f)
        assert time.monotonic() - t0 < 2.0  # far below hold_sec
        assert h.out == [f]
        assert label_of(f) == (None, None)
        await h.close()

    run(go())


def test_degrade_while_held_releases_without_attributes(monkeypatch):
    tl = make_timeline()
    tl.record(0, rows(10, 0))
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    f = tframe("x", 0, 40 * FRAME_SAMPLES)

    async def go():
        await h.start()
        await h.send(f)
        await h.settle(0.05)
        assert f not in h.out
        tl.mark_degraded("worker")
        await h.settle()
        assert h.out == [f]
        assert label_of(f) == (None, None)
        await h.close()

    run(go())


def test_stall_guard_marks_degraded_once_then_stops_holding(monkeypatch, warnings_seen):
    tl = make_timeline()
    h, clock = make(monkeypatch, tl)
    never = 10**9  # coverage never reaches this
    frames = [tframe(f"s{i}", 0, never) for i in range(dl.STALL_CONSECUTIVE_TIMEOUTS)]

    async def go():
        await h.start()
        for f in frames:
            await h.send(f)
            assert not tl.is_degraded or f is frames[-1]
            clock.advance(HOLD + 0.01)
            await h.settle()
        # Every expiry released its frame (unlabelled), then the guard tripped.
        assert h.out == frames
        assert tl.is_degraded
        # Later frames are not held: forwarded in-line, no attributes.
        later = tframe("later", 0, never)
        await h.send(later)
        assert h.out[-1] is later
        assert label_of(later) == (None, None)
        await h.close()

    run(go())
    degrade = [
        m for m in warnings_seen if "stall" in m.lower() or "degrad" in m.lower()
    ]
    assert len(warnings_seen) == 1, warnings_seen
    assert len(degrade) == 1


def test_single_expiry_below_threshold_does_not_degrade(monkeypatch):
    tl = make_timeline()
    h, clock = make(monkeypatch, tl)
    f = tframe("once", 0, 10**9)

    async def go():
        await h.start()
        await h.send(f)
        clock.advance(HOLD + 0.01)
        await h.settle()
        assert h.out == [f]
        assert not tl.is_degraded
        await h.close()

    run(go())


def test_expiry_with_coverage_progress_does_not_count_as_stall(monkeypatch):
    tl = make_timeline()
    h, clock = make(monkeypatch, tl)

    async def go():
        await h.start()
        for i in range(dl.STALL_CONSECUTIVE_TIMEOUTS + 2):
            f = tframe(f"p{i}", 0, 10**9)
            await h.send(f)
            # Worker makes progress between parking and expiry.
            tl.record(i * FRAME_SAMPLES, rows(1, 0))
            await h.settle(0.05)
            clock.advance(HOLD + 0.01)
            await h.settle()
            assert f in h.out
        assert not tl.is_degraded
        await h.close()

    run(go())


def test_constants_sane():
    assert dl.STALL_CONSECUTIVE_TIMEOUTS >= 2


# ------------------------------------------- stall-guard / fail-open regressions


def test_inline_covered_release_resets_stall_streak(monkeypatch, warnings_seen):
    """D1: expiry, then in-line covered releases, then one isolated expiry.

    The in-line release proves the worker is producing, so it must reset the
    streak; otherwise the two non-consecutive expiries accumulate and degrade.
    """
    tl = make_timeline()
    h, clock = make(monkeypatch, tl)
    never = 10**9

    async def go():
        await h.start()
        first = tframe("first", 0, never)
        await h.send(first)
        clock.advance(HOLD + 0.01)
        await h.settle()
        assert h.out == [first]  # expired, no progress -> streak 1

        tl.record(0, rows(20, 0))
        covered = [
            tframe(f"c{i}", i * 5 * FRAME_SAMPLES, (i + 1) * 5 * FRAME_SAMPLES)
            for i in range(4)
        ]
        for c in covered:
            await h.send(c)
            assert h.out[-1] is c  # released in line
        await h.settle(0.05)

        clock.advance(100 * HOLD)  # much later, an isolated slow window
        lone = tframe("lone", 0, never)
        await h.send(lone)
        clock.advance(HOLD + 0.01)
        await h.settle()
        assert h.out[-1] is lone
        assert not tl.is_degraded
        await h.close()

    run(go())
    assert warnings_seen == []


def test_frames_expiring_within_one_window_count_once(monkeypatch, warnings_seen):
    """D2: two finals parked a fraction of hold_sec apart, one slow window."""
    tl = make_timeline()
    h, clock = make(monkeypatch, tl)
    never = 10**9
    a = tframe("a", 0, never)
    b = tframe("b", 0, never)

    async def go():
        await h.start()
        await h.send(a)
        clock.advance(0.3 * HOLD)
        await h.send(b)
        clock.advance(0.7 * HOLD + 0.001)
        await h.settle()
        assert h.out == [a]  # a expired (no progress), b still held
        clock.advance(0.3 * HOLD)
        await h.settle()
        assert h.out == [a, b]  # b expired 0.3*hold later, same window
        assert not tl.is_degraded
        await h.close()

    run(go())
    assert warnings_seen == []


def test_distinct_stalled_windows_degrade_once(monkeypatch, warnings_seen):
    """D2: N distinct windows without progress still degrade, one WARNING."""
    tl = make_timeline()
    h, clock = make(monkeypatch, tl)
    never = 10**9

    async def go():
        await h.start()
        for i in range(dl.STALL_CONSECUTIVE_TIMEOUTS):
            assert not tl.is_degraded
            # Two frames per window: the extra one must not count on its own.
            await h.send(tframe(f"w{i}a", 0, never))
            clock.advance(0.2 * HOLD)
            await h.send(tframe(f"w{i}b", 0, never))
            clock.advance(HOLD + 0.01)
            await h.settle()
        assert tl.is_degraded
        assert len(h.out) == 2 * dl.STALL_CONSECUTIVE_TIMEOUTS
        await h.close()

    run(go())
    assert len(warnings_seen) == 1, warnings_seen
    assert "stall" in warnings_seen[0].lower()


@pytest.fixture
def errors_seen():
    msgs = []
    hid = logger.add(lambda m: msgs.append(m.record["message"]), level="ERROR")
    yield msgs
    logger.remove(hid)


def test_release_task_fails_open_on_push_error(monkeypatch, warnings_seen, errors_seen):
    """D3: push_frame raising in the release task must not lose frames."""
    tl = make_timeline()
    h, _ = make(monkeypatch, tl, hold_sec=30.0)
    a = tframe("a", 0, 10 * FRAME_SAMPLES)
    b = tframe("b", 10 * FRAME_SAMPLES, 20 * FRAME_SAMPLES)
    c = tframe("c", 20 * FRAME_SAMPLES, 10**9)
    real_push = h.labeler.push_frame
    raised = []

    async def flaky_push(frame, direction=FrameDirection.DOWNSTREAM):
        if frame is a and not raised:
            raised.append(frame)
            raise RuntimeError("boom")
        await real_push(frame, direction)

    h.labeler.push_frame = flaky_push  # type: ignore[method-assign]

    async def go():
        await h.start()
        for f in (a, b, c):
            await h.send(f)
        await h.settle(0.05)
        assert h.out == []
        task = h.labeler._task
        tl.record(0, rows(20, 0))  # a and b now covered; c never is
        await h.settle()
        assert raised == [a]
        assert h.out == [a, b, c]  # each exactly once, FIFO
        for f in (a, b, c):
            assert label_of(f) == (None, None)
        assert tl.is_degraded
        assert task is not None and task.done()
        assert not task.cancelled() and task.exception() is None
        later = tframe("later", 0, 10**9)
        await h.send(later)
        assert h.out[-1] is later  # not held after fail-open
        await h.close()

    run(go())
    assert len(errors_seen) == 1, errors_seen
    assert "boom" in errors_seen[0]
    assert len(warnings_seen) == 2  # the ERROR + mark_degraded's one WARNING
    assert sum("degraded" in m for m in warnings_seen) == 1
