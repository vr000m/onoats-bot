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
from onoats.stt.diarization_timeline import DiarizationTimeline

from onoats.stt import diarization_timeline as dt

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


def test_decode_timeout_ratio_parity_with_websocket_stt_service():
    src = Path(dt.__file__).with_name("websocket_stt_service.py").read_text()
    line = next(ln for ln in src.splitlines() if "decode_timeout = max(" in ln)
    assert f"{dt.DECODE_TIMEOUT_RATIO} * audio_seconds" in line


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
    """Deterministic: a `record` lands while `label_for` holds the lock.

    Needs the timeline's lock at `_lock` and re-entrant; skipped otherwise
    (impl-internal hook, flagged in the phase report)."""
    real = getattr(tl, "_lock", None)
    if real is None or not hasattr(real, "_is_owned"):
        pytest.skip("timeline has no re-entrant `_lock` to interpose on")
    tl.record(0, probs(frames(2), s0=ON))

    fired = []

    class Proxy:
        def __enter__(self_inner):
            real.acquire()
            if not fired:
                fired.append(True)
                tl.record(seconds(2), probs(frames(2), s0=ON))
            return self_inner

        def __exit__(self_inner, *exc):
            real.release()

        def acquire(self_inner, *a, **k):
            return real.acquire(*a, **k)

        def release(self_inner):
            return real.release()

    tl._lock = Proxy()
    result = {}
    th = threading.Thread(
        target=lambda: result.setdefault("l", tl.label_for(0, seconds(2))),
        daemon=True,
    )
    th.start()
    th.join(5)
    assert not th.is_alive()
    assert result["l"] is not None and result["l"].speakers == ["S1"]
    assert fired
