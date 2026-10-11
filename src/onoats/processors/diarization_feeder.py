"""Them-channel diarization: pass-through feeder + single worker thread.

``DiarizationFeeder`` sits in ``system_arm`` before ``system_stt``. It is a pure
observer: every frame is forwarded unchanged and undelayed - ``EndFrame`` and
``CancelFrame`` included, which only signal the worker to flush/abort; the one
bounded wait, the worker join, happens later in ``cleanup()``, off the frame
path. It counts 16 kHz PCM16 samples
with the shared :class:`SampleCounter` (the same clock ``WebSocketSTTService``
counts independently) and enqueues ``(start_sample, pcm_bytes)`` onto a bounded
drop-oldest queue. A drop is simply a missing ``start_sample`` range; the
feeder never writes timeline coverage (its only timeline call is the
idempotent ``mark_degraded`` when the worker fails to start or to join).

``DiarizerWorker`` owns **one daemon thread** that performs the model load
(``diarizer_factory()``), ``set_streaming_config("low")`` before the first
``feed()``, ``timeline.set_slot_count(model.config.num_speakers)``, every
``feed()``, the final flush, and every timeline coverage write. ``mlx_audio`` is only
imported inside :func:`load_mlx_diarizer` (the production factory), never at
module top level.

Failure policy: any runtime failure (loader raises, ``feed()`` raises, or
``BUDGET_OVERRUN_CONSECUTIVE`` consecutive over-budget windows) degrades -
``timeline.mark_degraded`` (exactly one WARNING), the feeder keeps passing frames
and stops enqueueing. Never a fatal ``ErrorFrame``.
"""

from __future__ import annotations

import asyncio
import bisect
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np
from loguru import logger
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from onoats.config.audio_devices import PIPELINE_SAMPLE_RATE
from onoats.stt import diarization_model
from onoats.stt.diarization_timeline import (
    BYTES_PER_SAMPLE,
    FRAME_SAMPLES,
    SAMPLE_RATE_HZ,
    DiarizationTimeline,
    SampleCounter,
)

STREAMING_PRESET = "low"

#: Processing time must stay under ``BUDGET_RATIO`` x audio duration, measured
#: over accumulation windows of at least ``BUDGET_WINDOW_SEC`` of fed audio
#: (the model buffers most calls cheaply and computes in bursts, so a per-call
#: check is meaningless). ``BUDGET_OVERRUN_CONSECUTIVE`` consecutive
#: over-budget windows degrade (a single slow warm-up window must not).
BUDGET_RATIO = 0.8
BUDGET_WINDOW_SEC = 1.0
BUDGET_OVERRUN_CONSECUTIVE = 5

#: Drop-oldest queue bound, in chunks (~30 s at 10 ms chunks).
DEFAULT_QUEUE_MAX = 3000
#: Bounded wait for the worker join in ``cleanup()``.
DEFAULT_JOIN_TIMEOUT_SEC = 5.0

DiarizerFactory = Callable[[], Any]


def load_mlx_diarizer() -> Any:
    """Production ``diarizer_factory``; the only place ``mlx_audio`` is imported.

    Loads from the cached snapshot directory (``diarization_model``, the same
    owner and file check as the startup preflight); a miss is a fail-loud
    worker error.
    """
    snapshot = diarization_model.cached_snapshot_dir()  # cheap; fail first
    from mlx_audio.vad import load as load_diarization

    return load_diarization(snapshot, strict=True)


def _int_repr(value: object) -> str:
    """A model-config value for the guard's WARNING: the repr of an exact
    ``int``, else only its type name (never an arbitrary object's repr)."""
    return repr(value) if type(value) is int else f"<{type(value).__name__}>"


class DiarizerWorker:
    """Owns the diarizer model, its thread, and all timeline writes."""

    def __init__(
        self,
        timeline: DiarizationTimeline,
        diarizer_factory: DiarizerFactory | None = None,
        *,
        queue_max: int = DEFAULT_QUEUE_MAX,
        budget_ratio: float = BUDGET_RATIO,
        overrun_consecutive: int = BUDGET_OVERRUN_CONSECUTIVE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeline = timeline
        self._factory = diarizer_factory or load_mlx_diarizer
        self._queue_max = max(1, int(queue_max))
        self._budget_ratio = float(budget_ratio)
        self._overrun_limit = max(1, int(overrun_consecutive))
        self._clock = clock
        self._queue: deque[tuple[int, bytes]] = deque()
        self._cond = threading.Condition()
        self._finish = False
        self._abort = False
        self._dead = False  # worker stopped (degraded / exited): stop enqueueing
        self._drop_warned = False
        self._thread: threading.Thread | None = None

    # ---- loop-side API -------------------------------------------------

    @property
    def thread(self) -> threading.Thread | None:
        return self._thread

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="diar-worker", daemon=True
        )
        self._thread.start()

    def enqueue(self, start_sample: int, pcm: bytes) -> bool:
        """Non-blocking; drops the OLDEST chunk when full. False if not accepted."""
        with self._cond:
            if self._dead or self._finish or self._abort:
                return False
            if len(self._queue) >= self._queue_max:
                self._queue.popleft()
                if not self._drop_warned:
                    self._drop_warned = True
                    logger.debug("diarizer queue full; dropping oldest chunk(s)")
            self._queue.append((int(start_sample), pcm))
            self._cond.notify()
        return True

    def finish(self) -> None:
        """Drain queued chunks, run the final flush, mark flushed, exit."""
        with self._cond:
            self._finish = True
            self._cond.notify_all()

    def abort(self) -> None:
        """Discard the queue and exit promptly (between model calls)."""
        with self._cond:
            self._abort = True
            self._queue.clear()
            self._cond.notify_all()

    @property
    def shutdown_requested(self) -> bool:
        """``finish()`` or ``abort()`` has been called."""
        return self._finish or self._abort

    def join(self, timeout: float | None = None) -> bool:
        """Bounded join. True if the thread is gone (or never started)."""
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()

    # ---- worker thread -------------------------------------------------

    def _next(self) -> tuple[int, bytes] | None:
        """Block for the next chunk; None => time to finish or abort."""
        with self._cond:
            while True:
                if self._abort:
                    return None
                if self._queue:
                    return self._queue.popleft()
                if self._finish:
                    return None
                self._cond.wait()

    def _run(self) -> None:
        tl = self._timeline
        try:
            try:
                self._run_inner()
            except Exception as exc:
                from onoats._redact import safe_exc_text

                tl.mark_degraded(
                    f"worker failure: {type(exc).__name__}: {safe_exc_text(exc)}"
                )
        finally:
            with self._cond:
                self._dead = True
                self._queue.clear()

    def _run_inner(self) -> None:
        tl = self._timeline
        model = self._factory()  # load happens on THIS thread
        if self._abort:
            return
        model.set_streaming_config(STREAMING_PRESET)
        model_rate = getattr(model, "sample_rate", PIPELINE_SAMPLE_RATE)
        if int(model_rate) != PIPELINE_SAMPLE_RATE or SAMPLE_RATE_HZ != int(model_rate):
            raise RuntimeError(
                f"diarizer sample rate {model_rate} != pipeline {PIPELINE_SAMPLE_RATE}"
            )
        # The whole timeline assumes ONE output row per 10 ms (160-sample) input
        # frame; a model revision with a coarser output hop (a subsampling
        # factor != 1 OR a processor hop != 160) would put every label at the
        # wrong time with no error. The weights are not revision-pinned, so
        # check the live model rather than trust the release-time contract
        # test (which skips when the cache is cold). Fails closed on anything
        # but the exact ints 1 and 160; the release-time contract test checks
        # the same predicate, so the two cannot disagree.
        if not diarization_model.one_row_per_frame(model.config):
            subsampling = getattr(model.config, "output_subsampling_factor", None)
            hop = diarization_model.row_hop_length(model.config)
            raise RuntimeError(
                f"diarizer output_subsampling_factor {_int_repr(subsampling)} "
                f"(need 1), hop_length {_int_repr(hop)} (need {FRAME_SAMPLES}): "
                f"rows are not {FRAME_SAMPLES}-sample frames"
            )
        tl.set_slot_count(int(model.config.num_speakers))
        init = getattr(model, "init_streaming_state", None)
        state = init() if init is not None else None

        anchor = 0  # current pipeline position minus model-input position
        fed = 0  # samples handed to the model
        rows_emitted = 0  # fallback frame index if state lacks frames_processed
        # Output row ``r`` describes MODEL-INPUT samples ``[r*160, r*160+160)``.
        # With lookahead, rows for pre-gap audio arrive after a re-anchor, so
        # each row maps through the anchor in effect when that input sample
        # was fed: monotonic breakpoints (frame-aligned model offset, anchor).
        bp_offsets: list[int] = [0]
        bp_anchors: list[int] = [0]
        # Model offsets of rows whose input straddles a drop (ascending; popped
        # as ``record`` passes them). Such a row mixes pre- and post-gap audio
        # and has no single pipeline position, so it is never recorded.
        skip_rows: deque[int] = deque()
        overruns = 0  # consecutive over-budget WINDOWS
        win_elapsed = 0.0
        win_samples = 0  # int: avoids float drift in the window threshold

        def reanchor(start: int) -> None:
            # Input ``fed`` now sits at pipeline ``start``. Break at the next
            # row boundary ``off``, shifting its anchor so that row lands
            # exactly on ``start`` (exact when chunks are 160-sample
            # multiples, the 10 ms norm). Rows below ``off`` keep the anchor
            # they were fed under. The GAP starts exactly where the mapped row
            # stream continues: at row ``r = floor(fed)`` (the first row not
            # wholly fed) under the anchor ``record`` would map it with - the
            # same ``bisect_right(bp_offsets, r)`` lookup. Aligned (``r ==
            # off``): that is up to a frame BEFORE the true ``expected`` after
            # an earlier unaligned re-anchor, and when that re-anchor's
            # breakpoint already sits at ``off`` (fed then grew exactly onto
            # it) it is THAT breakpoint's anchor, not the pre-breakpoint one.
            # Unaligned (``r == off - 160``): row ``r`` straddles the drop, so
            # it is skipped; recording it at the pre-gap anchor would lay it
            # over dropped audio and count that audio as speaker coverage.
            # Either way every position between the last recorded row before
            # the breakpoint and the first row after it is resolved by
            # construction, by a GAP only.
            nonlocal anchor
            expected = anchor + fed
            off = -(-fed // FRAME_SAMPLES) * FRAME_SAMPLES
            r = fed // FRAME_SAMPLES * FRAME_SAMPLES
            k = bisect.bisect_right(bp_offsets, r) - 1
            gap_lo = min(expected, r + bp_anchors[k])
            if off > fed and (not skip_rows or skip_rows[-1] != r):
                skip_rows.append(r)
            tl.record_gap(gap_lo, start)
            anchor += start - expected
            a = anchor - (off - fed)
            if bp_offsets[-1] == off:
                bp_anchors[-1] = a
            else:
                bp_offsets.append(off)
                bp_anchors.append(a)

        def frame_index() -> int:
            fp = getattr(state, "frames_processed", None)
            return int(fp) if fp is not None else rows_emitted

        def record(out: Any, idx: int) -> int:
            probs = getattr(out, "speaker_probs", None)
            if probs is None:
                return 0
            arr = np.array(probs, dtype=np.float32)  # mx.array -> numpy
            if arr.ndim != 2 or arr.shape[0] == 0:
                return 0
            n_rows = int(arr.shape[0])
            i = 0
            while i < n_rows:
                off = (idx + i) * FRAME_SAMPLES
                while skip_rows and skip_rows[0] < off:
                    skip_rows.popleft()
                if skip_rows and skip_rows[0] == off:
                    skip_rows.popleft()  # straddles a drop: its GAP covers it
                    i += 1
                    continue
                k = bisect.bisect_right(bp_offsets, off) - 1
                if k + 1 < len(bp_offsets):
                    # Breakpoints are frame-aligned, so this split is exact.
                    run_end = min(n_rows, bp_offsets[k + 1] // FRAME_SAMPLES - idx)
                else:
                    run_end = n_rows
                if skip_rows:
                    run_end = min(run_end, skip_rows[0] // FRAME_SAMPLES - idx)
                tl.record(off + bp_anchors[k], arr[i:run_end])
                i = run_end
            return n_rows

        while True:
            item = self._next()
            if item is None:
                break
            start, pcm = item
            if start != anchor + fed:
                # A drop (start ahead) or a non-monotonic tag (should not
                # happen; start behind) - re-anchor and resolve any hole.
                reanchor(start)
            n = len(pcm) // BYTES_PER_SAMPLE
            if n == 0:
                continue
            audio = (
                np.frombuffer(pcm, dtype="<i2", count=n).astype(np.float32) / 32768.0
            )
            idx = frame_index()
            t0 = self._clock()
            out, state = model.feed(audio, state, sample_rate=PIPELINE_SAMPLE_RATE)
            elapsed = self._clock() - t0
            fed += n
            rows_emitted += record(out, idx)
            # Budget over an accumulation window: per-call checks would let the
            # model's cheap buffering calls mask its periodic compute calls.
            win_elapsed += elapsed
            win_samples += n
            if win_samples >= BUDGET_WINDOW_SEC * PIPELINE_SAMPLE_RATE:
                win_audio = win_samples / PIPELINE_SAMPLE_RATE
                if win_elapsed > self._budget_ratio * win_audio:
                    overruns += 1
                    if overruns >= self._overrun_limit:
                        tl.mark_degraded(
                            f"processing over budget for {overruns} consecutive "
                            f"{BUDGET_WINDOW_SEC:g}s windows"
                        )
                        return
                else:
                    overruns = 0
                win_elapsed = 0.0
                win_samples = 0

        if self._abort or self._finish is False:
            return
        # Final flush (EndFrame): drain the model's lookahead, then mark flushed.
        idx = frame_index()
        out, state = model.feed(
            np.zeros(0, dtype=np.float32),
            state,
            sample_rate=PIPELINE_SAMPLE_RATE,
            final=True,
        )
        record(out, idx)
        tl.mark_flushed()


class DiarizationFeeder(FrameProcessor):
    """Pure pass-through observer that tags audio chunks with ``start_sample``."""

    def __init__(
        self,
        timeline: DiarizationTimeline,
        diarizer_factory: DiarizerFactory | None = None,
        *,
        worker: DiarizerWorker | None = None,
        queue_max: int = DEFAULT_QUEUE_MAX,
        join_timeout_sec: float = DEFAULT_JOIN_TIMEOUT_SEC,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._timeline = timeline
        self.worker = worker or DiarizerWorker(
            timeline, diarizer_factory, queue_max=queue_max
        )
        self._join_timeout = float(join_timeout_sec)
        self._counter = SampleCounter()

    @property
    def counter(self) -> SampleCounter:
        return self._counter

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            self._counter.reset()
            try:
                self.worker.start()
            except Exception as exc:  # never break the pipeline
                self._timeline.mark_degraded(f"worker start failed: {exc!r}")
        elif isinstance(frame, AudioRawFrame):
            # Always count (the STT service counts the same frames); only the
            # enqueue stops once the worker is gone or the timeline degraded.
            start = self._counter.add_pcm16_bytes(len(frame.audio))
            if not self._timeline.is_degraded:
                self.worker.enqueue(start, frame.audio)
        elif isinstance(frame, EndFrame):
            # Post the finish sentinel, then forward at once: the STT's own
            # drain of its final segment must not queue behind the worker, and
            # the labeler already bounds its wait on ``is_flushed`` by HOLD_SEC.
            # The thread is reaped (bounded) in ``cleanup()``.
            self.worker.finish()
        elif isinstance(frame, CancelFrame):
            self.worker.abort()

        await self.push_frame(frame, direction)

    async def cleanup(self) -> None:
        """Backstop: reap the worker however the pipeline ended.

        After a terminal frame the worker is already finishing/aborting, so
        this only joins (bounded). With no terminal frame (runner exception,
        teardown without Cancel/End) it aborts first, so the daemon thread
        cannot sit parked in ``_cond.wait()`` holding the model.
        """
        await super().cleanup()
        if not self.worker.shutdown_requested:
            self.worker.abort()
        await self._bounded_join()

    async def _bounded_join(self) -> None:
        try:
            done = await asyncio.to_thread(self.worker.join, self._join_timeout)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"diarizer join failed: {exc!r}")
            return
        if not done:
            # Daemon thread is left behind; labels must not wait on it.
            self.worker.abort()
            self._timeline.mark_degraded("worker did not stop within join timeout")
