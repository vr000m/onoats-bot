"""Whole-state-space contract for ``onoats._event_loop.run_bounded_event_loop``.

Pending-task outcome x async-generator shutdown x executor shutdown. Contract:
both loop finalizers are attempted whatever pending-task cancellation did; any
failed stage runs ``on_cleanup_failure`` + ``hard_exit(1)`` and returns 1; a
clean run returns the awaitable's result and never calls ``hard_exit``.
"""

from __future__ import annotations

import asyncio
import itertools

import pytest

from onoats import _event_loop

CANCEL_TIMEOUT = 0.05
FINALIZER_TIMEOUT = 0.2

PENDING = ("none", "settles", "survives", "wait_raises", "zero_timeout")
FINALIZER_MODES = ("ok", "fails", "times_out")


@pytest.mark.parametrize(
    ("pending", "asyncgens", "executor"),
    list(itertools.product(PENDING, FINALIZER_MODES, FINALIZER_MODES)),
)
def test_finalizers_are_attempted_regardless_of_pending_task_outcome(
    monkeypatch, pending: str, asyncgens: str, executor: str
) -> None:
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", FINALIZER_TIMEOUT)
    ran: list[str] = []
    generator_finalized: list[bool] = []
    exits: list[int] = []
    failures: list[bool] = []

    original_asyncgens = asyncio.BaseEventLoop.shutdown_asyncgens
    original_executor = asyncio.BaseEventLoop.shutdown_default_executor

    def finalizer(name: str, mode: str, original):
        async def run(self, *args, **kwargs):
            ran.append(name)
            if mode == "fails":
                raise RuntimeError(f"{name} failed")
            if mode == "times_out":
                await asyncio.sleep(FINALIZER_TIMEOUT * 10)
            return await original(self, *args, **kwargs)

        return run

    monkeypatch.setattr(
        asyncio.BaseEventLoop,
        "shutdown_asyncgens",
        finalizer("asyncgens", asyncgens, original_asyncgens),
    )
    monkeypatch.setattr(
        asyncio.BaseEventLoop,
        "shutdown_default_executor",
        finalizer("executor", executor, original_executor),
    )

    if pending == "wait_raises":
        real_wait = asyncio.wait
        calls = {"n": 0}

        def wait(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("pending-task wait failed")
            return real_wait(*args, **kwargs)

        monkeypatch.setattr(_event_loop.asyncio, "wait", wait)

    retained = []

    async def generator():
        try:
            yield None
        finally:
            generator_finalized.append(True)

    async def settles() -> None:
        await asyncio.Event().wait()

    async def resistant() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(FINALIZER_TIMEOUT * 10)

    async def run() -> int:
        stream = generator()
        retained.append(stream)
        await anext(stream)
        if pending in ("settles", "wait_raises", "zero_timeout"):
            asyncio.create_task(settles())
        elif pending == "survives":
            asyncio.create_task(resistant())
        await asyncio.sleep(0)
        return 7

    result = _event_loop.run_bounded_event_loop(
        run(),
        cancel_timeout=0.0 if pending == "zero_timeout" else CANCEL_TIMEOUT,
        hard_exit=exits.append,
        on_cleanup_failure=lambda: failures.append(True),
    )

    # Async-generator shutdown is always attempted. The executor shares the
    # finalizer deadline, so it is skipped (and counted as failed) only when
    # the async-generator shutdown has already consumed that whole budget.
    expected_ran = (
        ["asyncgens"] if asyncgens == "times_out" else ["asyncgens", "executor"]
    )
    assert ran == expected_ran
    if asyncgens == "ok":
        assert generator_finalized == [True], "open async generator not finalized"

    stage_failed = (
        pending in ("survives", "wait_raises", "zero_timeout")
        or asyncgens != "ok"
        or executor != "ok"
    )
    if stage_failed:
        assert (result, exits, failures) == (1, [1], [True])
    else:
        assert (result, exits, failures) == (7, [], [])


def test_reported_repro_resistant_task_still_finalizes_open_async_generator(
    monkeypatch,
) -> None:
    """Round-5 repro: before the fix, ``finalized`` stayed empty."""
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", FINALIZER_TIMEOUT)
    finalized: list[bool] = []
    exits: list[int] = []
    retained = []

    async def generator():
        try:
            yield None
        finally:
            finalized.append(True)

    async def resistant() -> None:
        while True:
            try:
                await asyncio.sleep(FINALIZER_TIMEOUT * 10)
            except asyncio.CancelledError:
                continue

    async def run() -> int:
        stream = generator()
        retained.append(stream)
        await anext(stream)
        asyncio.create_task(resistant())
        await asyncio.sleep(0)
        return 0

    result = _event_loop.run_bounded_event_loop(
        run(),
        cancel_timeout=CANCEL_TIMEOUT,
        hard_exit=exits.append,
        on_cleanup_failure=lambda: None,
    )
    assert {"result": result, "exits": exits, "finalized": finalized} == {
        "result": 1,
        "exits": [1],
        "finalized": [True],
    }
