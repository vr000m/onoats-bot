"""SilenceDetector — triggers a callback after a configurable period of VAD inactivity.

Observes VADUserStartedSpeakingFrame and VADUserStoppedSpeakingFrame. When no VAD
activity occurs for SILENCE_TIMEOUT seconds, calls the provided on_silence_timeout
callback. The timer is reset on any new VAD activity.

All frames are passed downstream unchanged (transparent processor).
"""

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

# ---------------------------------------------------------------------------
# Config (from environment with sensible defaults)
# ---------------------------------------------------------------------------

# How long (seconds) of VAD inactivity triggers the silence callback.
# Default: 300s (5 minutes), as per the dev plan.
_SILENCE_TIMEOUT_DEFAULT = 300

# How often (seconds) the monitoring task polls for timeout.
_POLL_INTERVAL = 10.0


def _silence_timeout() -> float:
    try:
        return float(os.environ.get("SILENCE_TIMEOUT_SEC", _SILENCE_TIMEOUT_DEFAULT))
    except ValueError:
        return float(_SILENCE_TIMEOUT_DEFAULT)


# ---------------------------------------------------------------------------
# SilenceDetector
# ---------------------------------------------------------------------------


class SilenceDetector(FrameProcessor):
    """Transparent processor that calls a callback on prolonged VAD inactivity.

    The detector starts its background monitoring task when the pipeline starts
    (call ``start_monitoring()`` manually if using outside a pipeline, or
    override ``process_frame`` to call it on ``StartFrame``).

    VAD activity (start *or* stop speaking) resets the inactivity timer.
    When ``SILENCE_TIMEOUT`` seconds pass with no VAD activity, ``on_silence_timeout``
    is called from within the monitoring asyncio task.

    All frames are forwarded downstream unchanged.

    Args:
        on_silence_timeout: Async or sync callable invoked when the timeout fires.
            Signature: ``() -> None`` (or ``async () -> None``).
        silence_timeout: Override timeout in seconds. Reads ``SILENCE_TIMEOUT``
            env var if not provided; defaults to 300s.
        poll_interval: How often (seconds) the background task checks the timer.
            Defaults to 10s.
    """

    def __init__(
        self,
        on_silence_timeout: Callable,
        silence_timeout: Optional[float] = None,
        poll_interval: float = _POLL_INTERVAL,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self._on_silence_timeout = on_silence_timeout
        self._timeout = (
            silence_timeout if silence_timeout is not None else _silence_timeout()
        )
        self._poll_interval = poll_interval

        # Monotonic timestamp of last VAD activity (set on start/stop speaking frames).
        # None means we haven't seen any VAD activity yet; the timer starts on first activity.
        self._last_vad_activity: Optional[float] = None

        # Whether the timeout has already fired (prevents repeated callbacks
        # until the timer is explicitly reset by new activity).
        self._fired = False

        # Whether the user is currently speaking. Timeout is suppressed while True.
        self._speaking = False

        # Background monitoring task handle
        self._monitor_task: Optional[asyncio.Task] = None

        logger.debug(
            f"SilenceDetector initialized (timeout={self._timeout}s, poll={self._poll_interval}s)"
        )

    # ------------------------------------------------------------------
    # FrameProcessor interface
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._on_speech_start()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._on_speech_stop()

        await self.push_frame(frame, direction)

    # ------------------------------------------------------------------
    # Monitoring task lifecycle
    # ------------------------------------------------------------------

    async def start_monitoring(self) -> None:
        """Start the background task that checks for inactivity timeouts.

        Safe to call multiple times — a task already running is left untouched.
        """
        if self._monitor_task is not None and not self._monitor_task.done():
            logger.debug("SilenceDetector: monitoring task already running")
            return

        self._monitor_task = asyncio.create_task(
            self._monitoring_loop(),
            name="silence_detector_monitor",
        )
        logger.info("SilenceDetector: monitoring task started")

    async def stop_monitoring(self) -> None:
        """Cancel the background monitoring task gracefully."""
        if self._monitor_task is None:
            return

        if not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        self._monitor_task = None
        logger.info("SilenceDetector: monitoring task stopped")

    # ------------------------------------------------------------------
    # Timer management
    # ------------------------------------------------------------------

    def _on_speech_start(self) -> None:
        """Mark that the user is speaking — suppress timeout while active."""
        self._speaking = True
        self._fired = False
        logger.debug("SilenceDetector: speech started")

    def _on_speech_stop(self) -> None:
        """Mark that the user stopped speaking — arm the inactivity timer."""
        self._speaking = False
        self._last_vad_activity = asyncio.get_running_loop().time()
        self._fired = False
        logger.debug("SilenceDetector: speech stopped, timer armed")

    def reset_timer(self) -> None:
        """Reset the inactivity timer without firing the callback.

        Used after a manual flush (e.g. SIGUSR1) so the silence timeout
        restarts from scratch for the new transcript.
        """
        self._last_vad_activity = None
        self._fired = False
        logger.info("SilenceDetector: timer reset")

    # ------------------------------------------------------------------
    # Background monitoring loop
    # ------------------------------------------------------------------

    async def _monitoring_loop(self) -> None:
        """Periodically check whether the inactivity timeout has elapsed."""
        logger.debug("SilenceDetector: monitoring loop running")
        try:
            while True:
                await asyncio.sleep(self._poll_interval)
                await self._check_timeout()
        except asyncio.CancelledError:
            logger.debug("SilenceDetector: monitoring loop cancelled")
            raise

    async def _check_timeout(self) -> None:
        """Fire the callback if silence has exceeded the configured timeout."""
        if self._fired:
            return

        if self._speaking:
            # User is actively speaking — do not fire timeout mid-speech.
            return

        if self._last_vad_activity is None:
            # No VAD activity observed yet — timer hasn't started.
            return

        now = asyncio.get_running_loop().time()
        elapsed = now - self._last_vad_activity

        if elapsed >= self._timeout:
            logger.info(
                f"SilenceDetector: timeout fired after {elapsed:.1f}s of inactivity "
                f"(threshold={self._timeout}s)"
            )
            self._fired = True
            await self._invoke_callback()

    async def _invoke_callback(self) -> None:
        """Call on_silence_timeout, supporting both sync and async callables."""
        try:
            result = self._on_silence_timeout()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.error(f"SilenceDetector: on_silence_timeout callback raised: {exc}")
