"""One bounded event-loop owner for recorder process boundaries."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import TypeVar


T = TypeVar("T")

# Loop-owned finalizers still need one scheduling window when the operator
# deliberately configures a zero cancellation grace. This deadline is separate
# from the pending-task cancellation deadline: zero remains immediate for
# abandoned recorder work, while a clean loop can close async generators and an
# unused executor without being converted into a failed process exit.
FINALIZER_TIMEOUT_SECONDS = 1.0


def run_bounded_event_loop(
    awaitable: Awaitable[T],
    *,
    cancel_timeout: float,
    hard_exit: Callable[[int], object],
    on_cleanup_failure: Callable[[], None],
) -> T | int:
    """Run *awaitable* and fail hard only when loop-owned cleanup is unbounded.

    ``cancel_timeout=0`` cancels pending tasks without granting them another
    loop turn. Finalizers use their own small positive deadline, so a successful
    coroutine with no abandoned tasks still returns its successful result.

    Contract (stages run in order; each is always attempted):

    1. Pending-task cancellation, bounded by ``cancel_timeout``. It fails when a
       task survives the wait, the wait itself raises, or tasks are pending
       with ``cancel_timeout == 0``.
    2. ``shutdown_asyncgens`` then ``shutdown_default_executor``, sharing one
       ``FINALIZER_TIMEOUT_SECONDS`` deadline that starts after stage 1. Both
       are attempted even if stage 1 or the async-generator shutdown failed.

    If any stage failed, ``on_cleanup_failure()`` and ``hard_exit(1)`` run and
    1 is returned (the awaitable's own result or exception is superseded).
    Otherwise the awaitable's exception is re-raised or its result returned;
    a clean run is never converted into rc=1.
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
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()

        cancel_timeout = max(0.0, cancel_timeout)
        if pending and cancel_timeout == 0:
            cleanup_failed = True
        elif pending:
            try:
                _done, survivors = loop.run_until_complete(
                    asyncio.wait(pending, timeout=cancel_timeout)
                )
            except BaseException:
                cleanup_failed = True
            else:
                cleanup_failed = bool(survivors)

        # Finalization owns a separate positive budget. A pending task may use
        # its entire cancellation grace without starving async-generator or
        # executor shutdown.
        finalizer_deadline = loop.time() + FINALIZER_TIMEOUT_SECONDS

        def run_finalizer(finalizer: Awaitable[object]) -> bool:
            remaining = finalizer_deadline - loop.time()
            if remaining <= 0:
                if inspect.iscoroutine(finalizer):
                    finalizer.close()
                return False
            task = loop.create_task(finalizer)
            try:
                done, _pending = loop.run_until_complete(
                    asyncio.wait({task}, timeout=remaining)
                )
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

        # Every finalizer is attempted whatever the earlier stages did: a
        # surviving task or a failed async-generator shutdown must not leave the
        # executor (or open generators) unfinalized. Any failed stage still
        # fails the process hard below.
        asyncgens_ok = run_finalizer(loop.shutdown_asyncgens())
        executor_ok = run_finalizer(loop.shutdown_default_executor())
        cleanup_failed = cleanup_failed or not (asyncgens_ok and executor_ok)
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
