"""Whole-state-space contract for `CancellationLedger.run_to_completion`.

`onoats._closing.CancellationLedger` is the one owner of the "attempt every
step, remember a cancellation, re-raise it once" invariant. Its shielded step
runner is what the socket supervisor's authoritative teardown ``finally`` uses.
The contract, cell by cell (step outcome x caller cancellation):

* the step always runs to completion — a caller cancellation never abandons it;
* the step's own result / exception reaches the caller, including when the
  caller's cancellation lands in the same event-loop turn the step finishes;
* only a cancellation of the step itself propagates as ``CancelledError``;
* any caller cancellation is remembered and re-raised by ``raise_if_cancelled``.
"""

from __future__ import annotations

import asyncio

import pytest

from onoats._closing import CancellationLedger


class StepError(Exception):
    pass


@pytest.mark.parametrize("outcome", ["returns", "raises", "cancelled-itself"])
@pytest.mark.parametrize("caller_cancel", ["none", "while-pending", "same-turn"])
def test_run_to_completion_whole_state_space(outcome, caller_cancel):
    ledger = CancellationLedger()
    trace: list[str] = []

    async def scenario():
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = asyncio.Event()
        caller_task: asyncio.Task | None = None

        async def step():
            trace.append("step-start")
            started.set()
            await release.wait()
            if caller_cancel == "same-turn":
                # Queued ahead of the step task's done callbacks, so the caller
                # is cancelled in the same loop turn the step finishes — before
                # the shield wakes the caller with the step's outcome.
                loop.call_soon(caller_task.cancel)
            trace.append("step-done")
            if outcome == "raises":
                raise StepError("step failed")
            if outcome == "cancelled-itself":
                raise asyncio.CancelledError
            return "result"

        async def caller():
            try:
                return ("returned", await ledger.run_to_completion(step()))
            except asyncio.CancelledError:
                return ("raised", "CancelledError")
            except StepError:
                return ("raised", "StepError")

        caller_task = asyncio.create_task(caller())
        await started.wait()
        if caller_cancel == "while-pending":
            caller_task.cancel()
            await asyncio.sleep(0)
        release.set()
        return await asyncio.wait_for(caller_task, timeout=2)

    result = asyncio.run(scenario())

    assert trace == ["step-start", "step-done"], "the step was abandoned"
    if outcome == "cancelled-itself":
        assert result == ("raised", "CancelledError")
        return
    expected = (
        ("returned", "result") if outcome == "returns" else ("raised", "StepError")
    )
    assert result == expected
    if caller_cancel == "none":
        ledger.raise_if_cancelled()
    else:
        with pytest.raises(asyncio.CancelledError):
            ledger.raise_if_cancelled()
