"""Them-channel diarization timeline: correlation clock, coverage ring, label policy.

Leaf module: no ``onoats`` imports and no ``mlx_audio`` import (a third-party
import of ``stt_server.protocol`` is allowed and optional). Both the upstream
feeder/worker and the STT service import from here without either importing
the other, and ``import onoats.dual`` stays native-free.

It is the **single owner** of:

* **The correlation clock.** The clock is the count of 16 kHz mono PCM16
  samples seen since ``StartFrame`` (:class:`SampleCounter`). The feeder and
  ``WebSocketSTTService`` each count independently with this one helper; that
  is valid only because the feeder is a pure pass-through (no drop, no
  re-chunk between them). The clock is monotonic for the session and is NOT
  reset by ``onoats flush``.
* **:class:`DiarizationTimeline`** - a session-scoped, thread-safe,
  ring-bounded record of per-frame speaker probabilities. The **worker is
  the sole coverage writer** (``record``, ``record_gap``, ``set_slot_count``,
  ``mark_flushed``); the event loop otherwise only reads (``label_for``,
  ``coverage_high_water``, ``is_ready``, ``is_degraded``, ``is_flushed``,
  ``add_progress_listener``). ``mark_degraded`` is the one idempotent,
  thread-safe flag either thread may set: the worker on its own failures, the
  loop from the labeler (stall guard, release-task fail-open) and the feeder
  (worker start failure, worker join timeout).
* **The label policy** (:meth:`DiarizationTimeline.label_for`) and its named
  constants.

Slot -> ``S{n}`` mapping happens in ``record`` (worker thread), in the order
each model slot first crosses ``ACTIVITY_THRESHOLD`` in the session, so the
loop-side ``label_for`` never mutates mapping state.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
from loguru import logger

# --- Clock contract ---------------------------------------------------------

SAMPLE_RATE_HZ = 16_000
BYTES_PER_SAMPLE = 2  # PCM16 mono
#: Model output hop: one probability row per 10 ms (160 samples at 16 kHz).
FRAME_SAMPLES = 160

# --- Policy constants -------------------------------------------------------

#: Per-frame probability at/above which a slot counts as active.
ACTIVITY_THRESHOLD = 0.5
#: When no slot is active for >= OVERLAP_MIN_FRACTION of the window (sparse,
#: scattered activity), the top slot must lead the runner-up's activity-weighted
#: score by at least this much or the window is left unlabelled.
DOMINANT_MARGIN = 0.15
#: Two+ slots each active for >= this fraction of the covered window => overlap.
OVERLAP_MIN_FRACTION = 0.2
#: Minimum fraction of the window that must be recorded (not GAP/evicted/pending).
MIN_COVERAGE_FRACTION = 0.5
#: Max time the labeler holds a TranscriptionFrame waiting for coverage.
#: `low` preset lag (~1.04 s) + margin. The dev plan's ``LATENCY_BUDGET_SEC``
#: (2.0 s) is this + margin - a plan acceptance threshold, not a code constant.
HOLD_SEC = 1.5
#: Queue / segment-serialisation slack added to the ring capacity.
RING_HEADROOM_SEC = 10.0
#: How long after the labeler starts the worker may still be loading the model
#: (no coverage is possible yet) before a no-progress expiry counts toward the
#: labeler's stall guard. A cold model load is not a hung ``feed()``.
MODEL_LOAD_GRACE_SEC = 60.0
#: Decode-timeout factor; ``websocket_stt_service`` imports it for
#: ``max(_DECODE_TIMEOUT_SECONDS, DECODE_TIMEOUT_RATIO * audio_seconds)``.
DECODE_TIMEOUT_RATIO = 1.5

try:  # the STT server's own cap on one uncommitted turn
    from stt_server.protocol import MAX_UNCOMMITTED_SECONDS as _SERVER_MAX_TURN_SEC

    MAX_TURN_SEC: float = float(_SERVER_MAX_TURN_SEC)
except Exception:  # pragma: no cover - documented literal fallback
    MAX_TURN_SEC = 300.0

#: Ring capacity in seconds: a maximum-length turn (plus its decode time and the
#: hold window) cannot be evicted before its frame arrives.
RING_CAPACITY_SEC: float = (
    MAX_TURN_SEC * (1 + DECODE_TIMEOUT_RATIO) + HOLD_SEC + RING_HEADROOM_SEC
)

Listener = Callable[[], None]


class SampleCounter:
    """The one counter used by the feeder and the STT service.

    ``advance`` (samples) / ``add_pcm16_bytes`` return the pipeline sample position of the *start* of
    the chunk just counted. ``reset`` is for ``StartFrame`` only (never flush).
    """

    __slots__ = ("_samples",)

    def __init__(self) -> None:
        self._samples = 0

    @property
    def samples(self) -> int:
        return self._samples

    def advance(self, n_samples: int) -> int:
        start = self._samples
        self._samples += int(n_samples)
        return start

    def add_pcm16_bytes(self, nbytes: int) -> int:
        return self.advance(nbytes // BYTES_PER_SAMPLE)

    def reset(self) -> None:
        self._samples = 0


@dataclass(frozen=True)
class Label:
    speakers: list[str]
    overlap: bool = False


@dataclass
class _Segment:
    start: int
    probs: np.ndarray | None  # (n, slots) float32; None => GAP
    bad: np.ndarray | None = None  # (n,) bool, frames with unmappable activity
    length: int = 0  # samples covered

    @property
    def end(self) -> int:
        return self.start + self.length


class DiarizationTimeline:
    """Session-scoped speaker-probability timeline. See module docstring."""

    def __init__(self, capacity_sec: float | None = None) -> None:
        cap = RING_CAPACITY_SEC if capacity_sec is None else float(capacity_sec)
        self._capacity_samples = int(cap * SAMPLE_RATE_HZ)
        self._lock = threading.RLock()
        self._segments: list[_Segment] = []  # sorted by start
        self._high_water = 0
        self._newest_end = 0
        self._slot_count: int | None = None
        self._slot_map: dict[int, int] = {}  # model slot -> 1-based S{n}
        self._overflow_warned = False
        self._degraded = False
        self._flushed = False
        self._listeners: list[Listener] = []

    # ------------------------------------------------------------------ writes

    def set_slot_count(self, n: int) -> None:
        with self._lock:
            self._slot_count = int(n)
        self._notify()

    def record(self, start_sample: int, probs) -> None:
        """Worker: record ``probs`` (n, slots) whose row 0 sits at ``start_sample``."""
        arr = np.asarray(probs, dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] == 0:
            return
        n = arr.shape[0]
        warn = False
        with self._lock:
            slot_count = self._slot_count
            bad = np.zeros(n, dtype=bool)
            active = arr >= ACTIVITY_THRESHOLD
            # Map slots in order of first activity (earliest frame first).
            for row in np.flatnonzero(active.any(axis=1)):
                for slot in np.flatnonzero(active[row]):
                    slot = int(slot)
                    if slot_count is not None and slot >= slot_count:
                        bad[row] = True
                        continue
                    if slot not in self._slot_map:
                        self._slot_map[slot] = len(self._slot_map) + 1
            if bad.any() and not self._overflow_warned:
                self._overflow_warned = True
                warn = True
            self._insert(_Segment(int(start_sample), arr, bad, n * FRAME_SAMPLES))
        if warn:
            logger.warning(
                "DiarizationTimeline: speaker slot beyond model slot count "
                f"({slot_count}); affected windows are left unlabelled"
            )
        self._notify()

    def record_gap(self, start: int, end: int) -> None:
        """Worker: mark ``[start, end)`` resolved-but-unlabelled (dropped audio)."""
        if end <= start:
            return
        with self._lock:
            self._insert(_Segment(int(start), None, None, int(end - start)))
        self._notify()

    def mark_flushed(self) -> None:
        with self._lock:
            self._flushed = True
        self._notify()

    def mark_degraded(self, reason: str = "worker") -> None:
        """Idempotent; exactly one WARNING per timeline."""
        with self._lock:
            if self._degraded:
                return
            self._degraded = True
        logger.warning(f"DiarizationTimeline: degraded ({reason}); labels disabled")
        self._notify()

    def add_progress_listener(self, cb: Listener) -> None:
        with self._lock:
            self._listeners.append(cb)

    # ------------------------------------------------------------------- reads

    @property
    def is_ready(self) -> bool:
        """True once the worker has loaded the model and published its slot count."""
        with self._lock:
            return self._slot_count is not None

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    @property
    def is_flushed(self) -> bool:
        return self._flushed

    def coverage_high_water(self) -> int:
        """Highest ``p`` with every position ``< p`` resolved (recorded or GAP)."""
        with self._lock:
            return self._high_water

    def label_for(self, start_sample: int, end_sample: int) -> Label | None:
        end_sample = int(end_sample)
        start_sample = int(start_sample)
        window = end_sample - start_sample
        if window <= 0:
            return None
        # Snapshot under the lock; compute outside it.
        with self._lock:
            if self._slot_count is None:
                return None
            slot_map = dict(self._slot_map)
            parts: list[tuple[np.ndarray, np.ndarray]] = []
            covered = 0
            for seg in self._segments:
                if seg.end <= start_sample:
                    continue
                if seg.start >= end_sample:
                    break
                lo = max(seg.start, start_sample)
                hi = min(seg.end, end_sample)
                if seg.probs is None:
                    continue
                covered += hi - lo
                i0 = max(0, (lo - seg.start) // FRAME_SAMPLES)
                i1 = min(
                    seg.probs.shape[0], -(-(hi - seg.start) // FRAME_SAMPLES)
                )  # ceil
                parts.append((seg.probs[i0:i1], seg.bad[i0:i1]))
        if covered <= 0 or covered / window < MIN_COVERAGE_FRACTION:
            return None
        if any(bad.any() for _, bad in parts):
            return None
        rows = np.concatenate([p for p, _ in parts], axis=0)
        if rows.shape[0] == 0:
            return None
        active = rows >= ACTIVITY_THRESHOLD  # (n, slots)
        frame_active = active.any(axis=1)
        n_active_frames = int(frame_active.sum())
        if n_active_frames == 0:
            return None
        # Activity-weighted: only frames with some active speaker contribute.
        scores = rows[frame_active].mean(axis=0)
        fractions = active.sum(axis=0) / rows.shape[0]
        candidates = [
            s
            for s in range(rows.shape[1])
            if fractions[s] >= OVERLAP_MIN_FRACTION and s in slot_map
        ]

        def key(s: int) -> tuple[float, int]:
            return (-float(scores[s]), slot_map[s])

        if len(candidates) >= 2:
            top2 = sorted(candidates, key=key)[:2]
            return Label([f"S{slot_map[s]}" for s in top2], True)
        mapped = [
            s for s in range(rows.shape[1]) if s in slot_map and active[:, s].any()
        ]
        if not mapped:
            return None
        ranked = sorted(mapped, key=key)
        top = ranked[0]
        if not candidates and len(ranked) > 1:
            if float(scores[top]) - float(scores[ranked[1]]) < DOMINANT_MARGIN:
                return None
        return Label([f"S{slot_map[top]}"], False)

    # ---------------------------------------------------------------- internal

    def _insert(self, seg: _Segment) -> None:
        """Caller holds ``_lock``. Keep sorted, evict, advance high-water."""
        i = len(self._segments)
        while i > 0 and self._segments[i - 1].start > seg.start:
            i -= 1
        self._segments.insert(i, seg)
        self._newest_end = max(self._newest_end, seg.end)
        self._evict()
        self._advance()

    def _evict(self) -> None:
        cutoff = self._newest_end - self._capacity_samples
        if cutoff <= 0:
            return
        keep: list[_Segment] = []
        for seg in self._segments:
            if seg.end <= cutoff:
                continue
            if seg.start < cutoff:
                drop = -(-(cutoff - seg.start) // FRAME_SAMPLES)  # ceil
                if seg.probs is None:
                    seg = _Segment(cutoff, None, None, seg.end - cutoff)
                else:
                    probs = seg.probs[drop:]
                    if probs.shape[0] == 0:
                        continue
                    seg = _Segment(
                        seg.start + drop * FRAME_SAMPLES,
                        probs,
                        seg.bad[drop:],
                        probs.shape[0] * FRAME_SAMPLES,
                    )
            keep.append(seg)
        self._segments = keep
        # Evicted positions are no longer resolvable; never regress, never stall.
        self._high_water = max(self._high_water, cutoff)

    def _advance(self) -> None:
        moved = True
        while moved:
            moved = False
            for seg in self._segments:
                if seg.start <= self._high_water < seg.end:
                    self._high_water = seg.end
                    moved = True

    def _notify(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb()
            except Exception as exc:  # a listener must never break the worker
                logger.warning(
                    f"DiarizationTimeline: progress listener failed: {exc!r}"
                )
