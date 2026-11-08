"""Whole-state-space contract for ``onoats._event_loop.run_bounded_event_loop``.

Pending-task outcome x async-generator shutdown x executor shutdown. Contract
(see the function docstring): teardown stages run in order and the first failed
stage ends teardown -- no later finalizer runs and no abandoned task gets
another loop turn; any failure runs ``on_cleanup_failure`` + ``hard_exit(1)``
and returns 1; each finalizer has its own budget; a clean run returns the
awaitable's result and never calls ``hard_exit``.
"""

from __future__ import annotations

import asyncio
import itertools
import time

import pytest

from onoats import _event_loop

CANCEL_TIMEOUT = 0.05
FINALIZER_TIMEOUT = 0.2
# Scheduling slack for wall-clock bounds on a loaded CI machine.
SLACK = 0.15

PENDING = ("none", "settles", "survives", "wait_raises", "zero_timeout")
FINALIZER_MODES = ("ok", "fails", "times_out")
STAGE1_FAILS = ("survives", "wait_raises", "zero_timeout")


def _patch_finalizers(monkeypatch, ran: list[str], modes: dict[str, object]) -> None:
    """Wrap both loop finalizers; a float mode is a delay before succeeding."""
    originals = {
        "asyncgens": asyncio.BaseEventLoop.shutdown_asyncgens,
        "executor": asyncio.BaseEventLoop.shutdown_default_executor,
    }
    attrs = {
        "asyncgens": "shutdown_asyncgens",
        "executor": "shutdown_default_executor",
    }
    for name, attr in attrs.items():
        mode = modes[name]
        original = originals[name]

        def make(name=name, mode=mode, original=original):
            async def run(self, *args, **kwargs):
                ran.append(name)
                if mode == "fails":
                    raise RuntimeError(f"{name} failed")
                if mode == "times_out":
                    await asyncio.sleep(FINALIZER_TIMEOUT * 10)
                elif isinstance(mode, float):
                    await asyncio.sleep(mode)
                return await original(self, *args, **kwargs)

            return run

        monkeypatch.setattr(asyncio.BaseEventLoop, attr, make())


@pytest.mark.parametrize(
    ("pending", "asyncgens", "executor"),
    list(itertools.product(PENDING, FINALIZER_MODES, FINALIZER_MODES)),
)
def test_teardown_stops_at_the_first_failed_stage(
    monkeypatch, pending: str, asyncgens: str, executor: str
) -> None:
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", FINALIZER_TIMEOUT)
    ran: list[str] = []
    _patch_finalizers(monkeypatch, ran, {"asyncgens": asyncgens, "executor": executor})
    generator_finalized: list[bool] = []
    exits: list[float] = []
    failures: list[bool] = []
    handler_turns: list[float] = []
    clock: dict[str, float] = {}

    if pending == "wait_raises":

        def wait(*args, **kwargs):
            raise RuntimeError("pending-task wait failed")

        monkeypatch.setattr(_event_loop.asyncio, "wait", wait)

    retained = []

    async def generator():
        try:
            yield None
        finally:
            generator_finalized.append(True)

    async def settles() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_turns.append(time.monotonic())
            raise

    async def resistant() -> None:
        # Cancellation-resistant: keeps taking turns until the loop stops
        # giving it any. Each turn is recorded.
        while True:
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                pass
            handler_turns.append(time.monotonic())

    async def run() -> int:
        stream = generator()
        retained.append(stream)
        await anext(stream)
        if pending in ("settles", "wait_raises", "zero_timeout"):
            asyncio.create_task(settles())
        elif pending == "survives":
            asyncio.create_task(resistant())
        await asyncio.sleep(0)
        clock["teardown_start"] = time.monotonic()
        return 7

    result = _event_loop.run_bounded_event_loop(
        run(),
        cancel_timeout=0.0 if pending == "zero_timeout" else CANCEL_TIMEOUT,
        hard_exit=lambda rc: exits.append(time.monotonic()) or rc,
        on_cleanup_failure=lambda: failures.append(True),
    )
    start = clock["teardown_start"]

    if pending in STAGE1_FAILS:
        # Stage 1 failed: no finalizer runs and abandoned tasks get no turns.
        assert ran == []
        assert generator_finalized == []
        if pending in ("wait_raises", "zero_timeout"):
            assert handler_turns == [], "abandoned task got a loop turn"
        else:
            assert handler_turns, "resistant task never got its grace"
            assert max(handler_turns) - start < CANCEL_TIMEOUT + SLACK
        assert (result, len(exits), failures) == (1, 1, [True])
        assert exits[0] - start < CANCEL_TIMEOUT + SLACK
        return

    if pending == "settles":
        assert len(handler_turns) == 1, "cancellation grace was not granted"
    expected_ran = ["asyncgens"] if asyncgens != "ok" else ["asyncgens", "executor"]
    assert ran == expected_ran
    if asyncgens == "ok":
        assert generator_finalized == [True], "open async generator not finalized"

    if asyncgens == "ok" and executor == "ok":
        assert (result, exits, failures) == (7, [], [])
    else:
        assert (result, len(exits), failures) == (1, 1, [True])
        bound = CANCEL_TIMEOUT + 2 * FINALIZER_TIMEOUT + SLACK
        assert exits[0] - start < bound


def test_zero_grace_never_runs_a_blocking_cancellation_handler(monkeypatch) -> None:
    """Round-6 probe: at 9dfc7ab the handler ran and hard_exit fired at ~2 s."""
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", FINALIZER_TIMEOUT)
    handler_ran: list[bool] = []
    exits: list[float] = []
    clock: dict[str, float] = {}

    async def blocks_on_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_ran.append(True)
            time.sleep(1.0)
            raise

    async def run() -> int:
        asyncio.create_task(blocks_on_cancel())
        await asyncio.sleep(0)
        clock["start"] = time.monotonic()
        return 0

    result = _event_loop.run_bounded_event_loop(
        run(),
        cancel_timeout=0.0,
        hard_exit=lambda rc: exits.append(time.monotonic()),
        on_cleanup_failure=lambda: None,
    )
    assert (result, handler_ran) == (1, [])
    assert len(exits) == 1 and exits[0] - clock["start"] < SLACK


def test_round5_repro_resistant_task_with_open_generator_hard_fails_promptly(
    monkeypatch,
) -> None:
    """Round-5 repro, new contract: no finalizer, no extra turns, prompt exit.

    The open generator is deliberately left unfinalized: the process is about to
    ``os._exit``, and finalizing it would need ``run_until_complete``, which
    would hand the abandoned task more turns.
    """
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", FINALIZER_TIMEOUT)
    finalized: list[bool] = []
    exits: list[float] = []
    turns: list[float] = []
    retained = []
    clock: dict[str, float] = {}

    async def generator():
        try:
            yield None
        finally:
            finalized.append(True)

    async def resistant() -> None:
        while True:
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                pass
            turns.append(time.monotonic())

    async def run() -> int:
        stream = generator()
        retained.append(stream)
        await anext(stream)
        asyncio.create_task(resistant())
        await asyncio.sleep(0)
        clock["start"] = time.monotonic()
        return 0

    result = _event_loop.run_bounded_event_loop(
        run(),
        cancel_timeout=CANCEL_TIMEOUT,
        hard_exit=lambda rc: exits.append(time.monotonic()),
        on_cleanup_failure=lambda: None,
    )
    start = clock["start"]
    assert (result, finalized, len(exits)) == (1, [], 1)
    assert max(turns) - start < CANCEL_TIMEOUT + SLACK
    assert exits[0] - start < CANCEL_TIMEOUT + SLACK


def test_slow_asyncgen_shutdown_does_not_starve_the_executor(monkeypatch) -> None:
    """Each finalizer owns its budget: two slow-but-bounded finalizers pass."""
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", FINALIZER_TIMEOUT)
    ran: list[str] = []
    slow = FINALIZER_TIMEOUT * 0.7
    _patch_finalizers(monkeypatch, ran, {"asyncgens": slow, "executor": slow})
    exits: list[int] = []

    async def run() -> int:
        return 3

    result = _event_loop.run_bounded_event_loop(
        run(),
        cancel_timeout=CANCEL_TIMEOUT,
        hard_exit=exits.append,
        on_cleanup_failure=lambda: None,
    )
    assert (result, ran, exits) == (3, ["asyncgens", "executor"], [])


def test_work_spawned_by_a_cancellation_handler_gets_no_finalizer_turns(
    monkeypatch,
) -> None:
    """New tasks from a handler are cancelled within the same grace window.

    At 9dfc7ab the spawned task was never cancelled and kept ticking while the
    async-generator finalizer ran.
    """
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", FINALIZER_TIMEOUT)
    ticks: list[float] = []
    spawned_cancelled: list[bool] = []
    finalizer_started: list[float] = []
    exits: list[int] = []
    retained = []

    async def generator():
        try:
            yield None
        finally:
            finalizer_started.append(time.monotonic())
            await asyncio.sleep(0.05)

    async def spawned() -> None:
        try:
            while True:
                ticks.append(time.monotonic())
                await asyncio.sleep(0.005)
        except asyncio.CancelledError:
            spawned_cancelled.append(True)
            raise

    async def spawns_on_cancel() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            retained.append(asyncio.get_running_loop().create_task(spawned()))
            raise

    async def run() -> int:
        stream = generator()
        retained.append(stream)
        await anext(stream)
        asyncio.create_task(spawns_on_cancel())
        await asyncio.sleep(0)
        return 5

    result = _event_loop.run_bounded_event_loop(
        run(),
        cancel_timeout=CANCEL_TIMEOUT,
        hard_exit=exits.append,
        on_cleanup_failure=lambda: None,
    )
    assert (result, exits, spawned_cancelled) == (5, [], [True])
    assert len(finalizer_started) == 1
    assert all(tick < finalizer_started[0] for tick in ticks)


def test_task_created_by_an_asyncgen_finalizer_fails_teardown(monkeypatch) -> None:
    """Round 7: work spawned during finalization must not be silently dropped.

    Before the settled stage, a task created by an async generator's ``finally``
    stayed pending, the loop closed over it, and teardown reported success
    (the original result, ``hard_exit`` unused).
    """
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", FINALIZER_TIMEOUT)
    exits: list[int] = []
    failures: list[bool] = []
    retained: list[object] = []

    async def forever() -> None:
        await asyncio.Event().wait()

    async def generator():
        try:
            yield None
        finally:
            retained.append(asyncio.get_running_loop().create_task(forever()))

    async def run() -> int:
        stream = generator()
        retained.append(stream)
        await anext(stream)
        return 7

    result = _event_loop.run_bounded_event_loop(
        run(),
        cancel_timeout=CANCEL_TIMEOUT,
        hard_exit=exits.append,
        on_cleanup_failure=lambda: failures.append(True),
    )
    assert (result, exits, failures) == (1, [1], [True])


def test_task_created_and_finished_during_finalization_is_clean(monkeypatch) -> None:
    """A finalizer-spawned task that completes by itself is not a failure."""
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", FINALIZER_TIMEOUT)
    exits: list[int] = []
    retained: list[object] = []

    async def quick() -> None:
        return None

    async def generator():
        try:
            yield None
        finally:
            task = asyncio.get_running_loop().create_task(quick())
            retained.append(task)
            await task

    async def run() -> int:
        stream = generator()
        retained.append(stream)
        await anext(stream)
        return 7

    result = _event_loop.run_bounded_event_loop(
        run(),
        cancel_timeout=CANCEL_TIMEOUT,
        hard_exit=exits.append,
        on_cleanup_failure=lambda: None,
    )
    assert (result, exits) == (7, [])
