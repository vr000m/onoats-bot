"""Recorder-owned serialization for flush, stop, and terminal rotation.

This leaf module deliberately knows nothing about the recorder pipeline, status
files, or control protocol.  Entry points supply the rotation and graceful-stop
adapters; every source of lifecycle intent then enters this one state machine.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from enum import StrEnum


class LifecycleState(StrEnum):
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"


class LifecycleStoppingError(RuntimeError):
    """Raised when a continuation is requested after stopping won admission."""


class LifecycleRotationError(RuntimeError):
    """Raised when an admitted continuation could not complete its rotation."""


class RecorderLifecycleController:
    """The sole ordering authority for one recorder session.

    The one lock orders continuation rotations against stop/terminal admission.
    Once stopping is admitted no later continuation can reopen the session.
    Terminal rotation is idempotent so pipeline-finally and shutdown-watcher
    paths may safely converge here.
    """

    def __init__(
        self,
        rotate: Callable[[str, bool], Awaitable[bool]],
        *,
        mark_graceful: Callable[[], None] | None = None,
    ) -> None:
        self._rotate = rotate
        self._mark_graceful = mark_graceful
        self._lock = asyncio.Lock()
        self._state = LifecycleState.RUNNING
        self.shutdown_event = asyncio.Event()
        self.force_exit_event = asyncio.Event()
        # Signal handlers can run outside the recorder loop.  Latch the first
        # signal before scheduling async admission so two back-to-back signals
        # cannot both observe RUNNING while the first coroutine is still queued.
        self._signal_lock = threading.Lock()
        self._first_signal_latched = False

    @property
    def state(self) -> LifecycleState:
        return self._state

    @property
    def stopping(self) -> bool:
        return self._state is not LifecycleState.RUNNING

    async def flush(self, reason: str) -> bool:
        """Complete one continuation rotation, or reject after stop admission."""
        async with self._lock:
            if self._state is not LifecycleState.RUNNING:
                raise LifecycleStoppingError("recorder is stopping")
            rotation = asyncio.create_task(self._rotate(reason, True))
            try:
                completed = await asyncio.shield(rotation)
            except asyncio.CancelledError:
                await asyncio.shield(rotation)
                raise
            if not completed:
                raise LifecycleRotationError("continuation rotation failed")
            return True

    async def stop(self, reason: str = "graceful stop requested") -> bool:
        """Admit a graceful stop; return ``False`` for an already admitted stop."""
        del reason
        async with self._lock:
            if self._state is not LifecycleState.RUNNING:
                return False
            self._state = LifecycleState.STOPPING
            if self._mark_graceful is not None:
                self._mark_graceful()
            self.shutdown_event.set()
            return True

    async def terminal_rotation(self, reason: str) -> bool:
        """Perform the one terminal rotation and close lifecycle admission."""
        async with self._lock:
            if self._state is LifecycleState.STOPPED:
                return False
            self._state = LifecycleState.STOPPING
            self.shutdown_event.set()
            try:
                completed = await self._rotate(reason, False)
                if not completed:
                    raise LifecycleRotationError("terminal rotation failed")
            finally:
                self._state = LifecycleState.STOPPED
            return True

    def schedule_stop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Schedule first-signal stop admission on the recorder loop."""
        asyncio.run_coroutine_threadsafe(self.stop("signal"), loop)

    def signal(self, loop: asyncio.AbstractEventLoop) -> bool:
        """Route a termination signal; ``True`` means the force path won."""
        with self._signal_lock:
            force = (
                self._first_signal_latched
                or self.stopping
                or self.shutdown_event.is_set()
            )
            if not force:
                self._first_signal_latched = True
        if force:
            loop.call_soon_threadsafe(self.force_exit_event.set)
            return True
        self.schedule_stop(loop)
        return False
