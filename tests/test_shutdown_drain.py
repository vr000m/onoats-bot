"""Unit coverage for the graceful shutdown helper.

``stop_pipeline_for_shutdown`` is the transcript-preserving half of shutdown:
it queues an EndFrame (``stop_when_done``) so an in-flight STT segment can land
before the terminal flush, and only hard-cancels if the drain stalls past its
timeout or a second Ctrl+C forces exit. These tests pin that control flow with a
fake task — they do NOT exercise a real pipecat pipeline, so they verify the
orchestration, not that an EndFrame actually drains the local-audio transport
(that needs a hardware run).
"""

from __future__ import annotations

import asyncio

from onoats.runtime import _env_float, stop_pipeline_for_shutdown


class FakeTask:
    """Stand-in for a pipecat PipelineTask exposing the methods the helper uses."""

    def __init__(self, *, finished: bool = False):
        self._finished = finished
        self.stop_called = False
        self.cancel_called = False

    def has_finished(self) -> bool:
        return self._finished

    async def stop_when_done(self) -> None:
        # Real stop_when_done only queues an EndFrame and returns immediately;
        # the pipeline finishes later. Mirror that — do NOT set _finished here.
        self.stop_called = True

    async def cancel(self) -> None:
        self.cancel_called = True
        self._finished = True

    def finish_now(self) -> None:
        self._finished = True


def test_already_finished_is_a_noop():
    task = FakeTask(finished=True)

    async def scenario():
        # force_exit_event is never touched on this path, so a bare Event is fine.
        await stop_pipeline_for_shutdown(task, asyncio.Event())

    asyncio.run(scenario())
    assert task.stop_called is False
    assert task.cancel_called is False


def test_drain_completes_without_cancel():
    """Pipeline finishes during the drain window -> no hard cancel."""
    task = FakeTask()

    async def scenario():
        force_exit = asyncio.Event()
        # Simulate the pipeline draining shortly after the EndFrame is queued.
        asyncio.get_running_loop().call_later(0.1, task.finish_now)
        await stop_pipeline_for_shutdown(task, force_exit, drain_timeout_sec=5.0)

    asyncio.run(scenario())
    assert task.stop_called is True
    assert task.cancel_called is False


def test_drain_timeout_falls_back_to_cancel():
    """Drain that never finishes hard-cancels once the timeout elapses."""
    task = FakeTask()

    async def scenario():
        await stop_pipeline_for_shutdown(task, asyncio.Event(), drain_timeout_sec=0.1)

    asyncio.run(scenario())
    assert task.stop_called is True
    assert task.cancel_called is True


def test_force_exit_during_drain_cancels_immediately():
    """A second Ctrl+C (force_exit_event) short-circuits the drain wait."""
    task = FakeTask()

    async def scenario():
        force_exit = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, force_exit.set)
        # Generous drain timeout so the test only passes if force_exit is what
        # wakes the wait, not the timeout.
        await asyncio.wait_for(
            stop_pipeline_for_shutdown(task, force_exit, drain_timeout_sec=30.0),
            timeout=5.0,
        )

    asyncio.run(scenario())
    assert task.stop_called is True
    assert task.cancel_called is True


def test_env_float_clamps_negative(monkeypatch):
    """A negative timeout would make asyncio.wait fire immediately and defeat
    the drain; _env_float clamps it up to the floor instead."""
    monkeypatch.setenv("ONOATS_TEST_FLOAT", "-1")
    assert _env_float("ONOATS_TEST_FLOAT", 8.0, min_value=0.0) == 0.0


def test_env_float_falls_back_on_garbage(monkeypatch):
    """A non-numeric value falls back to the default rather than crashing."""
    monkeypatch.setenv("ONOATS_TEST_FLOAT", "not-a-number")
    assert _env_float("ONOATS_TEST_FLOAT", 8.0, min_value=0.0) == 8.0


def test_env_float_passes_valid_value(monkeypatch):
    monkeypatch.setenv("ONOATS_TEST_FLOAT", "3.5")
    assert _env_float("ONOATS_TEST_FLOAT", 8.0, min_value=0.0) == 3.5
