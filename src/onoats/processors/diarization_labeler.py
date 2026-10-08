"""Them-channel diarization: downstream labeler (``system_arm``, after ``SourceTagger``).

A stamped ``TranscriptionFrame`` (``onoats_segment_start_sample`` /
``onoats_segment_end_sample`` set by ``WebSocketSTTService``) is parked in an
ordered pending deque - ``process_frame`` never awaits on it, so VAD / audio
frames behind it keep flowing. A **labeler-owned asyncio task** releases the
deque head-first once ``timeline.coverage_high_water()`` reaches the head's
segment end, or ``hold_sec`` (``HOLD_SEC``) has elapsed. The task is woken by
the worker's progress listener (fires on the **worker thread**, hence
``loop.call_soon_threadsafe``) and by the per-head deadline, so a held frame
releases even when no further frame ever arrives.

Frame-class rule: only ``TranscriptionFrame``s wait. A frame without a stamped
range (the fail-loud case) and every other frame pass through immediately.
``EndFrame`` waits up to ``hold_sec`` for ``timeline.is_flushed`` /
``is_degraded``, releases every pending frame in FIFO order (labelled with the
coverage available then), and only then is forwarded. ``CancelFrame`` releases
immediately without waiting. A teardown with neither (``cleanup()`` after a
pipeline exception) stops the release task, then releases the in-flight frame
and every pending frame the same way, FIFO, logging (never raising) a push
failure. Once ``timeline.is_degraded``, the labeler forwards transcriptions
immediately with no hold and no attributes.

Stall guard: a hung ``feed()`` never trips the worker's own degrade triggers,
so the labeler counts consecutive ``hold_sec`` **windows** whose expiries saw
no ``coverage_high_water()`` advance and, after ``STALL_CONSECUTIVE_TIMEOUTS``,
calls ``timeline.mark_degraded("stalled")``. Expiries while the worker is still
loading the model (``not timeline.is_ready``, within ``MODEL_LOAD_GRACE_SEC`` of
the labeler starting) are expected and never count: a cold load is not a hung
``feed()``. Several frames expiring within one
window count once (a no-progress expiry counts only if it lands >= ``hold_sec``
after the previously counted one), and every coverage-driven release - in-line
or from the task - resets the streak, as does coverage that advanced since the
last counted expiry with no held frame observing it, so only back-to-back
stalled windows degrade.

Fail-open: an unexpected exception in the release task logs one ERROR,
delivers the in-flight frame and every pending frame unlabelled (FIFO, never
twice), and degrades the timeline so later frames are not held.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from onoats._redact import safe_exc_text
from onoats.stt.diarization_timeline import (
    HOLD_SEC,
    MODEL_LOAD_GRACE_SEC,
    DiarizationTimeline,
)

#: Consecutive ``HOLD_SEC`` expiries with no ``coverage_high_water()`` progress
#: after which the labeler declares the diarizer stalled (a Metal hang rather
#: than an error) and degrades the timeline. A single slow window must not
#: degrade; two in a row with zero progress means the worker is not producing.
STALL_CONSECUTIVE_TIMEOUTS = 2


@dataclass
class _Pending:
    frame: TranscriptionFrame
    direction: FrameDirection
    start: int
    end: int
    deadline: float
    hw_at_park: int


class DiarizationLabeler(FrameProcessor):
    """Attach ``onoats_speakers`` (list[str]) / ``onoats_overlap`` (bool)."""

    def __init__(
        self,
        timeline: DiarizationTimeline,
        *,
        hold_sec: float = HOLD_SEC,
        clock: Callable[[], float] = time.monotonic,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._timeline = timeline
        self._hold_sec = float(hold_sec)
        # NOT `_clock`: pipecat's FrameProcessor.setup() overwrites self._clock
        # with its SystemClock object.
        self._now = clock
        self._pending: deque[_Pending] = deque()
        self._release_lock: asyncio.Lock | None = None
        self._task: asyncio.Task | None = None
        self._wake_event: asyncio.Event | None = None
        self._progress_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener_registered = False
        self._closed = False
        self._stall_streak = 0
        #: Clock time of the last expiry counted toward ``_stall_streak``.
        self._last_stall_at: float | None = None
        #: ``coverage_high_water()`` at the last counted expiry.
        self._hw_at_last_stall: int | None = None
        #: Clock time of the first ``_ensure_started`` (model-load grace anchor).
        self._started_at: float | None = None
        #: Frame popped by the release task but not yet successfully pushed.
        self._in_flight: _Pending | None = None

    # ------------------------------------------------------------- frame entry

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            self._ensure_started()
            await self.push_frame(frame, direction)
        elif isinstance(frame, EndFrame):
            await self._on_end()
            await self.push_frame(frame, direction)
        elif isinstance(frame, CancelFrame):
            await self._release_all()
            await self._stop_task()
            await self.push_frame(frame, direction)
        elif (
            isinstance(frame, TranscriptionFrame)
            and direction == FrameDirection.DOWNSTREAM
        ):
            await self._on_transcription(frame, direction)
        else:
            await self.push_frame(frame, direction)

    async def cleanup(self) -> None:
        # Stop the release task first: it can no longer push concurrently, and
        # a push it was cancelled mid-way through leaves its frame in
        # ``_in_flight`` for recovery below. ``super().cleanup()`` cancels our
        # process task, so nothing still holds ``_release_lock`` after it.
        await self._stop_task()
        await super().cleanup()
        await self._release_on_cleanup()

    # ------------------------------------------------------------ transcription

    async def _on_transcription(
        self, frame: TranscriptionFrame, direction: FrameDirection
    ) -> None:
        start = getattr(frame, "onoats_segment_start_sample", None)
        end = getattr(frame, "onoats_segment_end_sample", None)
        if start is None or end is None or self._timeline.is_degraded:
            # Unstamped (fail-loud upstream) or degraded: no hold, no attributes.
            # Serialise behind the release task (it pops the head and pushes it
            # while holding the lock, so ``_pending`` can be empty mid-push) and
            # drain anything parked first, so FIFO order is preserved.
            if self._release_lock is None:
                await self.push_frame(frame, direction)
                return
            async with self._release_lock:
                await self._drain_all()
                await self.push_frame(frame, direction)
            return

        self._ensure_started()
        hw = self._timeline.coverage_high_water()
        entry = _Pending(
            frame, direction, int(start), int(end), self._now() + self._hold_sec, hw
        )
        async with self._release_lock:
            if self._timeline.is_degraded:
                # Degraded while we waited for the lock (e.g. release-task
                # fail-open): never park, forward unlabelled in line.
                await self.push_frame(frame, direction)
                return
            if not self._pending and hw >= entry.end:
                # Already covered and nothing ahead of it: release in line.
                # Coverage-driven release => the worker is producing.
                self._reset_stall()
                await self._push_labelled(entry)
                return
            self._pending.append(entry)
        self._wake()

    # ------------------------------------------------------------------ task

    def _ensure_started(self) -> None:
        if self._closed:
            return
        if self._release_lock is None:
            self._started_at = self._now()
            self._release_lock = asyncio.Lock()
            self._wake_event = asyncio.Event()
            self._progress_event = asyncio.Event()
            self._loop = asyncio.get_running_loop()
        if not self._listener_registered:
            self._listener_registered = True
            self._timeline.add_progress_listener(self._on_worker_progress)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._run(), name="diarization_labeler_release"
            )

    def _on_worker_progress(self) -> None:
        """Progress listener: runs on the WORKER thread."""
        loop = self._loop
        if loop is None or self._closed:
            return
        try:
            loop.call_soon_threadsafe(self._wake)
        except RuntimeError:  # loop already closed
            pass

    def _wake(self) -> None:
        if self._wake_event is not None:
            self._wake_event.set()
        if self._progress_event is not None:
            self._progress_event.set()

    async def _stop_task(self) -> None:
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        assert self._wake_event is not None
        while True:
            self._wake_event.clear()
            try:
                await self._drain_ready()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._fail_open(exc)
                return
            timeout = None
            if self._pending:
                timeout = max(0.0, self._pending[0].deadline - self._now())
            try:
                await asyncio.wait_for(self._wake_event.wait(), timeout)
            except TimeoutError:
                pass

    def _model_loading(self, now: float) -> bool:
        """True while the worker is still loading the model (within the grace)."""
        return (
            not self._timeline.is_ready
            and self._started_at is not None
            and now - self._started_at < MODEL_LOAD_GRACE_SEC
        )

    def _reset_stall(self) -> None:
        self._stall_streak = 0
        self._last_stall_at = None
        self._hw_at_last_stall = None

    async def _drain_ready(self) -> None:
        """Release head-first while the head is covered or past its deadline."""
        assert self._release_lock is not None
        async with self._release_lock:
            while self._pending:
                head = self._pending[0]
                if self._timeline.is_degraded:
                    await self._pop_and_push()
                    continue
                hw = self._timeline.coverage_high_water()
                if hw >= head.end:
                    self._reset_stall()
                    await self._pop_and_push()
                    continue
                now = self._now()
                if now < head.deadline:
                    return
                # Expiry. It counts toward the stall streak only if coverage did
                # not advance at all while this frame was held AND it starts a
                # new hold window (>= hold_sec after the last counted expiry):
                # several frames expiring in one slow window count once. The
                # head stays in ``_pending`` until ``_pop_and_push`` (no await
                # in between, so nothing can observe the difference).
                if hw <= head.hw_at_park:
                    if (
                        self._hw_at_last_stall is not None
                        and hw > self._hw_at_last_stall
                    ):
                        # Coverage advanced since the last counted stall with
                        # no held frame observing it: not back-to-back.
                        self._reset_stall()
                    if not self._model_loading(now) and (
                        self._last_stall_at is None
                        or now - self._last_stall_at >= self._hold_sec
                    ):
                        self._stall_streak += 1
                        self._last_stall_at = now
                        self._hw_at_last_stall = hw
                else:
                    self._reset_stall()
                await self._pop_and_push()
                if (
                    self._stall_streak >= STALL_CONSECUTIVE_TIMEOUTS
                    and not self._timeline.is_degraded
                ):
                    # mark_degraded logs the single WARNING; carry the detail in it.
                    self._timeline.mark_degraded(
                        reason=(
                            "stalled: no coverage progress over "
                            f"{self._stall_streak} consecutive hold windows"
                        )
                    )

    async def _fail_open(self, exc: Exception) -> None:
        """Release task hit an unexpected error: never lose a transcript.

        One ERROR, degrade the timeline (so nothing is held from now on), then
        deliver the in-flight frame (popped, not yet pushed) and every pending
        frame unlabelled in FIFO order. Each frame is pushed at most once.
        """
        logger.error(
            "DiarizationLabeler: release task failed "
            f"({type(exc).__name__}: {safe_exc_text(exc)}); "
            "releasing pending transcripts unlabelled and disabling labels"
        )
        self._timeline.mark_degraded(
            reason=f"labeler release task failed: {type(exc).__name__}"
        )
        assert self._release_lock is not None
        async with self._release_lock:
            await self._deliver_held(self._take_held(), context="fail-open")

    # ----------------------------------------------------------------- release

    async def _push_labelled(self, entry: _Pending) -> None:
        """Label ``entry.frame`` with current coverage and push it. Lock held.

        The one owner of "degraded means no label attributes": once the
        timeline is degraded the frame is pushed with any label attributes
        stripped (an in-flight frame retried after a failed push may carry
        them from that attempt) instead of labelled.
        """
        if self._timeline.is_degraded:
            _strip_labels(entry.frame)
        else:
            try:
                label = self._timeline.label_for(entry.start, entry.end)
            except Exception as exc:  # never lose a transcript over a label
                logger.warning(f"DiarizationLabeler: label_for failed: {exc!r}")
                label = None
            if label is not None:
                entry.frame.onoats_speakers = list(label.speakers)
                entry.frame.onoats_overlap = bool(label.overlap)
        await self.push_frame(entry.frame, entry.direction)

    async def _pop_and_push(self) -> None:
        """Pop the head of ``_pending`` and push it. Lock held.

        The one way a single frame leaves ``_pending``: it sits in
        ``_in_flight`` across the push (left set if the push is cancelled or
        raises), so ``_take_held`` hands it to ``_release_on_cleanup`` /
        ``_fail_open`` instead of losing it.
        """
        entry = self._in_flight = self._pending.popleft()
        await self._push_labelled(entry)
        self._in_flight = None

    async def _drain_all(self) -> None:
        """Release every pending frame, FIFO, labelled. Lock held."""
        while self._pending:
            await self._pop_and_push()

    def _take_held(self) -> list[_Pending]:
        """Take the in-flight frame, then every pending one (FIFO). Lock held."""
        entries: list[_Pending] = []
        if self._in_flight is not None:
            entries.append(self._in_flight)
            self._in_flight = None
        entries.extend(self._pending)
        self._pending.clear()
        return entries

    async def _deliver_held(self, entries: list[_Pending], *, context: str) -> None:
        """Push each taken frame once; log, never raise, a push failure. Lock held."""
        for entry in entries:
            try:
                await self._push_labelled(entry)
            except Exception as exc:
                logger.error(
                    "DiarizationLabeler: dropped a transcript during "
                    f"{context} ({type(exc).__name__}: {safe_exc_text(exc)})"
                )

    async def _release_all(self) -> None:
        if self._release_lock is None:
            return
        async with self._release_lock:
            await self._drain_all()

    async def _release_on_cleanup(self) -> None:
        """Teardown without EndFrame/CancelFrame: never lose a transcript.

        Runs after the release task and process task are stopped. Delivers the
        in-flight frame (popped, push interrupted) and every pending frame in
        FIFO order, labelled with the coverage available now. A push failure is
        logged and never raised out of cleanup. After End/CancelFrame both are
        already empty, so this is a no-op on the normal paths.
        """
        if self._release_lock is None:
            return
        async with self._release_lock:
            entries = self._take_held()
            if entries:
                logger.debug(
                    f"DiarizationLabeler: releasing {len(entries)} held "
                    "transcript(s) at cleanup (no EndFrame/CancelFrame)"
                )
            await self._deliver_held(entries, context="cleanup")

    async def _on_end(self) -> None:
        """Graceful shutdown: wait <= hold_sec for the final flush, then drain."""
        if self._pending and self._progress_event is not None:
            deadline = self._now() + self._hold_sec
            while not (self._timeline.is_flushed or self._timeline.is_degraded):
                remaining = deadline - self._now()
                if remaining <= 0:
                    break
                self._progress_event.clear()
                if self._timeline.is_flushed or self._timeline.is_degraded:
                    break
                try:
                    await asyncio.wait_for(self._progress_event.wait(), remaining)
                except TimeoutError:
                    break
        await self._release_all()
        await self._stop_task()


def _strip_labels(frame: TranscriptionFrame) -> None:
    """Remove any label attributes so the frame is delivered unlabelled."""
    for attr in ("onoats_speakers", "onoats_overlap"):
        try:
            delattr(frame, attr)
        except AttributeError:
            pass
