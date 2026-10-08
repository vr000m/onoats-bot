"""Them-channel diarization: pass-through feeder + single worker thread.

``DiarizationFeeder`` sits in ``system_arm`` before ``system_stt``. It is a pure
observer: every frame is forwarded unchanged and undelayed - ``EndFrame`` and
``CancelFrame`` included, which only signal the worker to flush/abort; the one
bounded wait, the worker join, happens later in ``cleanup()``, off the frame
path. It counts 16 kHz PCM16 samples
with the shared :class:`SampleCounter` (the same clock ``WebSocketSTTService``
counts independently) and enqueues ``(start_sample, pcm_bytes)`` onto a bounded
drop-oldest queue. A drop is simply a missing ``start_sample`` range; the
feeder never writes the timeline.

``DiarizerWorker`` owns **one daemon thread** that performs the model load
(``diarizer_factory()``), ``set_streaming_config("low")`` before the first
``feed()``, ``timeline.set_slot_count(model.config.num_speakers)``, every
``feed()``, the final flush, and every timeline write. ``mlx_audio`` is only
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

#: Alias of the single owner (``diarization_model.REPO_ID``).
DIARIZATION_MODEL_ID = diarization_model.REPO_ID

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

    @property
    def stopped(self) -> bool:
        return self._dead

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
        overruns = 0  # consecutive over-budget WINDOWS
        win_elapsed = 0.0
        win_samples = 0  # int: avoids float drift in the window threshold

        def add_breakpoint() -> None:
            # Align to the next row boundary; shift the anchor so that row
            # lands exactly on the true pipeline position of input ``fed``
            # (exact when chunks are 160-sample multiples, the 10 ms norm).
            off = -(-fed // FRAME_SAMPLES) * FRAME_SAMPLES
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
                k = bisect.bisect_right(bp_offsets, off) - 1
                if k + 1 < len(bp_offsets):
                    # Breakpoints are frame-aligned, so this split is exact.
                    run_end = min(n_rows, bp_offsets[k + 1] // FRAME_SAMPLES - idx)
                else:
                    run_end = n_rows
                tl.record(off + bp_anchors[k], arr[i:run_end])
                i = run_end
            return n_rows

        while True:
            item = self._next()
            if item is None:
                break
            start, pcm = item
            expected = anchor + fed
            if start > expected:
                tl.record_gap(expected, start)
                anchor += start - expected
                add_breakpoint()
            elif start < expected:
                # Non-monotonic tag (should not happen): resync without a gap.
                anchor -= expected - start
                add_breakpoint()
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
