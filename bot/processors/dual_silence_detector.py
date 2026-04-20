"""Dual-input silence detector that waits for both sources to go idle."""

from __future__ import annotations

import asyncio
import os
from typing import Callable, Optional

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

_SILENCE_TIMEOUT_DEFAULT = 300
_POLL_INTERVAL = 10.0
# If a VADStarted arrives without a matching VADStopped (STT crash, transport
# drop), treat the branch as idle after this many seconds of silence on that
# branch. Guards against wedging the flush coordinator open indefinitely.
_SPEAKING_STALENESS_SECS = 30.0


def _silence_timeout() -> float:
    try:
        return float(os.environ.get("SILENCE_TIMEOUT_SEC", _SILENCE_TIMEOUT_DEFAULT))
    except ValueError:
        return float(_SILENCE_TIMEOUT_DEFAULT)


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
        self._timeout = silence_timeout if silence_timeout is not None else _silence_timeout()
        self._poll_interval = poll_interval
        self._last_vad_activity: dict[str, float] = {}
        self._speaking: dict[str, bool] = {}
        self._speaking_since: dict[str, float] = {}
        self._fired = False
        self._monitor_task: Optional[asyncio.Task] = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        # Prefer branch-aware first-class `source`; fall back to legacy
        # dynamic attributes to stay compatible with older frame paths.
        source = str(
            getattr(frame, "source", None)
            or getattr(frame, "koda_source", "")
            or getattr(frame, "user_id", "")
            or ""
        ).strip()
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
        logger.info("DualSilenceDetector: monitoring task started")

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
        forever; after ``_SPEAKING_STALENESS_SECS`` with no new VAD activity,
        treat it as idle.
        """
        if not self._speaking_since:
            return any(self._speaking.values())
        now = asyncio.get_running_loop().time()
        for source, started_at in list(self._speaking_since.items()):
            if not self._speaking.get(source, False):
                continue
            last_activity = self._last_vad_activity.get(source, started_at)
            if now - max(started_at, last_activity) > _SPEAKING_STALENESS_SECS:
                logger.warning(
                    f"DualSilenceDetector: clearing stale speaking state for "
                    f"source={source!r} (no VAD stop for "
                    f"{now - max(started_at, last_activity):.1f}s)"
                )
                self._speaking[source] = False
                self._speaking_since.pop(source, None)
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
