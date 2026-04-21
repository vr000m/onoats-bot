"""Dual-input silence detector that waits for both sources to go idle."""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from bot.frames import resolve_frame_source

_SILENCE_TIMEOUT_DEFAULT = 300.0
_POLL_INTERVAL = 10.0
# Minimum floor for the "speaking staleness" heuristic. A branch that emits
# VADStarted without a matching VADStopped (STT crash, transport drop) is
# cleared after this many seconds of silence — but never below this floor,
# and never smaller than the configured silence timeout, so active speakers
# aren't flushed mid-utterance.
_SPEAKING_STALENESS_FLOOR = 60.0
_SPEAKING_STALENESS_MULTIPLIER = 1.5


class DualSilenceDetector(FrameProcessor):
    """Fire only when the microphone and loopback branches are both idle."""

    def __init__(
        self,
        on_silence_timeout: Callable,
        silence_timeout: Optional[float] = None,
        poll_interval: float = _POLL_INTERVAL,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._on_silence_timeout = on_silence_timeout
        self._timeout = silence_timeout if silence_timeout is not None else _SILENCE_TIMEOUT_DEFAULT
        # Staleness must be >= the silence timeout, otherwise we'd clear an
        # actively speaking branch before the idle threshold is even reached.
        self._speaking_staleness = max(
            _SPEAKING_STALENESS_FLOOR,
            self._timeout * _SPEAKING_STALENESS_MULTIPLIER,
        )
        self._poll_interval = poll_interval
        self._last_vad_activity: dict[str, float] = {}
        self._speaking: dict[str, bool] = {}
        self._speaking_since: dict[str, float] = {}
        self._fired = False
        self._monitor_task: Optional[asyncio.Task] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        source = resolve_frame_source(frame)
        if source:
            if isinstance(frame, VADUserStartedSpeakingFrame):
                self._on_speech_start(source)
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._on_speech_stop(source)

        await self.push_frame(frame, direction)

    async def start_monitoring(self) -> None:
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(
            self._monitoring_loop(),
            name="dual_silence_detector_monitor",
        )
        logger.info(
            f"DualSilenceDetector: monitoring task started "
            f"(timeout={self._timeout}s, speaking_staleness={self._speaking_staleness}s)"
        )

    async def stop_monitoring(self) -> None:
        if self._monitor_task is None:
            return
        if not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._monitor_task = None
        logger.info("DualSilenceDetector: monitoring task stopped")

    def _on_speech_start(self, source: str) -> None:
        now = asyncio.get_running_loop().time()
        self._speaking[source] = True
        self._speaking_since[source] = now
        self._last_vad_activity[source] = now
        self._fired = False

    def _on_speech_stop(self, source: str) -> None:
        self._speaking[source] = False
        self._speaking_since.pop(source, None)
        self._last_vad_activity[source] = asyncio.get_running_loop().time()
        self._fired = False

    def reset_timer(self) -> None:
        self._last_vad_activity = {}
        self._speaking = {}
        self._speaking_since = {}
        self._fired = False
        logger.info("DualSilenceDetector: timer reset")

    def _effective_speaking(self) -> bool:
        """True if any branch is speaking AND has been speaking recently.

        A branch that emitted VADStarted without a matching VADStopped (STT
        crash, transport drop) would otherwise wedge the coordinator open
        forever; after ``self._speaking_staleness`` seconds with no new VAD
        activity, treat it as idle. The threshold scales with the configured
        silence timeout so long monologues and low-timeout configs don't
        flush active speech mid-utterance.
        """
        if not self._speaking_since:
            return any(self._speaking.values())
        now = asyncio.get_running_loop().time()
        for source, started_at in list(self._speaking_since.items()):
            if not self._speaking.get(source, False):
                continue
            last_activity = self._last_vad_activity.get(source, started_at)
            if now - max(started_at, last_activity) > self._speaking_staleness:
                logger.warning(
                    f"DualSilenceDetector: clearing stale speaking state for "
                    f"source={source!r} (no VAD stop for "
                    f"{now - max(started_at, last_activity):.1f}s)"
                )
                self._speaking[source] = False
                self._speaking_since.pop(source, None)
                # Reset the activity clock to *now* so _check_timeout does not
                # compare against a stale VADStarted timestamp from minutes
                # ago. Without this, a long uninterrupted utterance that
                # trips the staleness floor would immediately appear as
                # ``elapsed > self._timeout`` and flush mid-utterance. After
                # the clear, the branch gets a fresh silence window before
                # any timeout can fire.
                self._last_vad_activity[source] = now
        return any(self._speaking.values())

    async def _monitoring_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                await self._check_timeout()
        except asyncio.CancelledError:
            raise

    async def _check_timeout(self) -> None:
        if self._fired:
            return
        if self._effective_speaking():
            return
        if not self._last_vad_activity:
            return

        elapsed = asyncio.get_running_loop().time() - max(self._last_vad_activity.values())
        if elapsed >= self._timeout:
            logger.info(
                f"DualSilenceDetector: timeout fired after {elapsed:.1f}s of inactivity "
                f"(threshold={self._timeout}s)"
            )
            self._fired = True
            await self._invoke_callback()

    async def _invoke_callback(self) -> None:
        try:
            result = self._on_silence_timeout()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.error(f"DualSilenceDetector: on_silence_timeout callback raised: {exc}")
