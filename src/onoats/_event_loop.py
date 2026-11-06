"""One bounded event-loop owner for recorder process boundaries."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

# Each loop-owned finalizer (``shutdown_asyncgens``, then
# ``shutdown_default_executor``) gets this budget of its own. It is separate
# from the pending-task cancellation deadline: zero grace stays immediate for
# abandoned recorder work, while a clean loop can still close async generators
# and an unused executor without being converted into a failed process exit.
FINALIZER_TIMEOUT_SECONDS = 1.0


def run_bounded_event_loop(
    awaitable: Awaitable[T],
    *,
    cancel_timeout: float,
    hard_exit: Callable[[int], object],
    on_cleanup_failure: Callable[[], None],
) -> T | int:
    """Run *awaitable*, then tear the loop down within fixed deadlines.

    Teardown is a sequence of stages. The first stage that fails ends teardown:
    no later stage runs and no task gets another loop turn, because both
    callers' ``hard_exit`` is ``os._exit`` (nothing left unfinalized can outlive
    the process).

    1. **Pending tasks.** Every task still scheduled is cancelled and awaited
       for at most ``cancel_timeout`` seconds in total; tasks created by a
       cancellation handler inside that window are cancelled in turn within the
       same deadline. The stage fails when a task is still pending at the
       deadline, when the wait itself raises, or when tasks are pending and
       ``cancel_timeout <= 0`` (zero grace: no task gets another turn).
    2. **``shutdown_asyncgens``**, with its own ``FINALIZER_TIMEOUT_SECONDS``.
    3. **``shutdown_default_executor``**, with its own
       ``FINALIZER_TIMEOUT_SECONDS``. A slow but successful async-generator
       shutdown cannot consume this budget.

    A finalizer fails when it raises, when running it raises, or when it is
    still pending at its deadline (it is then cancelled, never given another
    turn).

    =================================  ==================  ==================
    Stage 1 / 2 / 3 outcome            turns after fail     result
    =================================  ==================  ==================
    no pending, ok, ok                 n/a                  awaitable result
    all settle in grace, ok, ok        n/a                  awaitable result
    survivor / wait raises / zero      none; 2-3 skipped    fail
      grace with pending, *, *
    clean, fails or times out, *       none; 3 skipped      fail
    clean, ok, fails or times out      none                 fail
    =================================  ==================  ==================

    "fail" means ``on_cleanup_failure()``, then ``hard_exit(1)``, then return
    1; the awaitable's own result or exception is superseded. On success the
    awaitable's exception is re-raised or its result returned, so a clean run
    is never converted into rc=1. Worst-case teardown time before
    ``hard_exit``: ``cancel_timeout + 2 * FINALIZER_TIMEOUT_SECONDS`` (plus the
    duration of any single blocking, non-awaiting step already running).
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result: T | None = None
    failure: tuple[BaseException, object] | None = None
    cleanup_failed = False
    try:
        result = loop.run_until_complete(awaitable)
    except BaseException as exc:
        failure = (exc, exc.__traceback__)
    finally:
        try:
            cleanup_failed = not (
                _cancel_pending_tasks(loop, max(0.0, cancel_timeout))
                and _run_finalizer(loop, loop.shutdown_asyncgens())
                and _run_finalizer(loop, loop.shutdown_default_executor())
            )
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    if cleanup_failed:
        on_cleanup_failure()
        hard_exit(1)
        return 1
    if failure is not None:
        exc, traceback = failure
        raise exc.with_traceback(traceback)  # type: ignore[arg-type]
    return result  # type: ignore[return-value]


def _cancel_pending_tasks(loop: asyncio.AbstractEventLoop, grace: float) -> bool:
    """Cancel every task within *grace* seconds; False when any is abandoned."""
    pending = asyncio.all_tasks(loop)
    if not pending:
        return True
    for task in pending:
        task.cancel()
    if grace <= 0:
        return False
    deadline = loop.time() + grace
    while pending:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        try:
            _done, survivors = loop.run_until_complete(
                asyncio.wait(pending, timeout=remaining)
            )
        except BaseException:
            return False
        if survivors:
            return False
        # A cancellation handler may have scheduled new work; it shares the
        # same deadline rather than receiving turns during finalization.
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
    return True


def _run_finalizer(
    loop: asyncio.AbstractEventLoop, finalizer: Awaitable[object]
) -> bool:
    """Run one loop finalizer within its own budget; False on any failure."""
    budget = FINALIZER_TIMEOUT_SECONDS
    if budget <= 0:
        if inspect.iscoroutine(finalizer):
            finalizer.close()
        return False
    task = loop.create_task(finalizer)
    try:
        done, _pending = loop.run_until_complete(asyncio.wait({task}, timeout=budget))
    except BaseException:
        return False
    if task not in done:
        task.cancel()
        return False
    try:
        task.result()
    except BaseException:
        return False
    return True
