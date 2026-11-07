"""Invariant tests for the recorder-owned lifecycle authority.

The controller is intentionally exercised without either recorder entrypoint.
Those entrypoints, the control socket, and signal handlers are adapters; this
suite pins the one state machine that they must all delegate to.
"""

from __future__ import annotations

import asyncio
import ast
import functools
import inspect
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from onoats.recorder_lifecycle import (
    LifecycleRotationError,
    LifecycleStoppingError,
    RecorderLifecycleController,
)


ROOT = Path(__file__).resolve().parents[1]


def _async_test(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class RotationSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.started = asyncio.Event()
        self.completed = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, reason: str, continue_session: bool) -> bool:
        self.calls.append((reason, continue_session))
        self.started.set()
        await self.release.wait()
        self.completed.set()
        return True


def _controller(
    rotation: RotationSpy,
) -> tuple[RecorderLifecycleController, asyncio.Event, asyncio.Event, list[str]]:
    graceful_marks: list[str] = []
    controller = RecorderLifecycleController(
        rotation,
        mark_graceful=lambda: graceful_marks.append("graceful"),
    )
    return (
        controller,
        controller.shutdown_event,
        controller.force_exit_event,
        graceful_marks,
    )


@_async_test
async def test_flush_first_completes_before_stop_and_terminal_rotation() -> None:
    rotation = RotationSpy()
    rotation.release.clear()
    controller, shutdown, _force, marks = _controller(rotation)

    flush = asyncio.create_task(controller.flush("control flush"))
    await rotation.started.wait()
    stop = asyncio.create_task(controller.stop("control stop"))
    await asyncio.sleep(0)

    assert not shutdown.is_set()
    assert not stop.done(), "Stop bypassed the controller lock held by Flush"

    rotation.release.set()
    assert await flush is True
    assert await stop is True
    assert shutdown.is_set()
    assert marks == ["graceful"]
    assert await controller.terminal_rotation("shutdown") is True
    assert controller.state == "stopped"
    assert rotation.calls == [
        ("control flush", True),
        ("shutdown", False),
    ]


@_async_test
async def test_stop_first_rejects_every_later_continuation() -> None:
    rotation = RotationSpy()
    controller, shutdown, _force, marks = _controller(rotation)

    assert await controller.stop("signal SIGTERM") is True
    assert controller.state == "stopping"
    assert shutdown.is_set()
    assert marks == ["graceful"]

    with pytest.raises(LifecycleStoppingError):
        await controller.flush("late control flush")
    with pytest.raises(LifecycleStoppingError):
        await controller.flush("late SIGUSR1")
    assert rotation.calls == []

    assert await controller.terminal_rotation("pipeline terminal") is True
    assert rotation.calls == [("pipeline terminal", False)]


@_async_test
async def test_terminal_rotation_wins_over_waiting_flush() -> None:
    rotation = RotationSpy()
    rotation.release.clear()
    controller, shutdown, _force, _marks = _controller(rotation)

    terminal = asyncio.create_task(controller.terminal_rotation("terminal error"))
    await rotation.started.wait()
    flush = asyncio.create_task(controller.flush("late silence timeout"))
    await asyncio.sleep(0)

    assert controller.state == "stopping"
    assert shutdown.is_set()
    assert not flush.done()

    rotation.release.set()
    assert await terminal is True
    with pytest.raises(LifecycleStoppingError):
        await flush
    assert controller.state == "stopped"
    assert rotation.calls == [("terminal error", False)]


@_async_test
async def test_duplicate_stop_and_finish_are_idempotent() -> None:
    rotation = RotationSpy()
    controller, shutdown, _force, marks = _controller(rotation)

    first, duplicate = await asyncio.gather(
        controller.stop("control stop"),
        controller.stop("duplicate control stop"),
    )
    assert first is True
    assert duplicate is False
    assert shutdown.is_set()
    assert marks == ["graceful"]

    first_finish, duplicate_finish = await asyncio.gather(
        controller.terminal_rotation("shutdown"),
        controller.terminal_rotation("finally"),
    )
    assert sorted((first_finish, duplicate_finish)) == [False, True]
    assert rotation.calls == [("shutdown", False)]
    assert controller.state == "stopped"


@_async_test
async def test_admitted_flush_survives_adapter_cancellation() -> None:
    """Losing a control client cannot cancel a rotation already admitted."""
    rotation = RotationSpy()
    rotation.release.clear()
    controller, shutdown, _force, _marks = _controller(rotation)

    flush = asyncio.create_task(controller.flush("disconnected client"))
    await rotation.started.wait()
    flush.cancel()
    stop = asyncio.create_task(controller.stop("signal after disconnect"))
    await asyncio.sleep(0)
    assert not stop.done()

    rotation.release.set()
    with pytest.raises(asyncio.CancelledError):
        await flush
    assert rotation.completed.is_set(), (
        "admitted rotation was cancelled with its adapter"
    )
    assert await stop is True
    assert shutdown.is_set()
    assert rotation.calls == [("disconnected client", True)]


@_async_test
async def test_force_exit_cancels_stuck_rotation_after_adapter_cancellation() -> None:
    """Force exit bounds an admitted rotation without weakening normal shielding."""
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck(_reason: str, _continue_session: bool) -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    controller = RecorderLifecycleController(stuck)
    flush = asyncio.create_task(controller.flush("stuck control flush"))
    await started.wait()
    flush.cancel()
    await asyncio.sleep(0)
    assert not flush.done(), "ordinary adapter cancellation must preserve admission"

    controller.force_exit_event.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(flush, timeout=0.5)
    assert cancelled.is_set(), "force exit must cancel the abandoned rotation"


@pytest.mark.parametrize("operation", ["flush", "terminal"])
@_async_test
async def test_force_exit_does_not_await_cancellation_resistant_rotation(
    operation: str,
) -> None:
    """Both lifecycle rotation modes leave resistant work after the force bound."""
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def resistant(_reason: str, _continue_session: bool) -> bool:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
        return True

    controller = RecorderLifecycleController(resistant)
    if operation == "flush":
        action = asyncio.create_task(controller.flush("resistant continuation"))
        await started.wait()
        action.cancel()
    else:
        action = asyncio.create_task(controller.terminal_rotation("resistant terminal"))
        await started.wait()

    controller.force_exit_event.set()
    done, _pending = await asyncio.wait({action}, timeout=0.5)
    bounded = action in done
    release.set()
    await asyncio.gather(action, return_exceptions=True)

    assert bounded, f"forced {operation} waited past its cancellation deadline"
    assert cancelled.is_set()
    if operation == "terminal":
        assert controller.state == "stopped"


def test_direct_recorder_process_exit_is_bounded_after_resistant_rotation() -> None:
    """The PortAudio runner must not let asyncio shutdown re-await abandoned work."""
    script = textwrap.dedent(
        """
        import asyncio
        from onoats import recorder_lifecycle, runtime

        recorder_lifecycle.LIFECYCLE_FORCE_SETTLE_TIMEOUT_SECONDS = 0.01
        runtime.RECORDER_LOOP_CLEANUP_TIMEOUT_SECONDS = 0.01

        async def resistant(_reason, _continue_session):
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue

        async def run():
            controller = recorder_lifecycle.RecorderLifecycleController(resistant)
            terminal = asyncio.create_task(controller.terminal_rotation("shutdown"))
            await asyncio.sleep(0)
            controller.force_exit_event.set()
            await terminal
            return 0

        raise SystemExit(runtime.run_recorder_event_loop(run()))
        """
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=2,
    )

    assert result.returncode == 1, result.stderr
    assert "forcing process exit" in result.stderr


def test_direct_recorder_runner_preserves_normal_result_and_exception() -> None:
    from onoats import runtime

    assert runtime.run_recorder_event_loop(asyncio.sleep(0, result=7)) == 7

    async def fail() -> None:
        raise RuntimeError("recorder failed")

    with pytest.raises(RuntimeError, match="recorder failed"):
        runtime.run_recorder_event_loop(fail())


def test_zero_cancel_timeout_preserves_clean_recorder_result(monkeypatch) -> None:
    """Zero is immediate pending-task cancellation, not a zero finalizer budget."""
    from onoats import runtime

    exits = []
    monkeypatch.setattr(runtime, "RECORDER_LOOP_CLEANUP_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(runtime, "_RECORDER_HARD_EXIT", exits.append)

    assert runtime.run_recorder_event_loop(asyncio.sleep(0, result=7)) == 7
    assert exits == []


def test_zero_cancel_timeout_immediately_rejects_abandoned_task(monkeypatch) -> None:
    from onoats import runtime

    exits = []
    monkeypatch.setattr(runtime, "RECORDER_LOOP_CLEANUP_TIMEOUT_SECONDS", 0.0)
    monkeypatch.setattr(runtime, "_RECORDER_HARD_EXIT", exits.append)

    async def run() -> int:
        asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        return 0

    assert runtime.run_recorder_event_loop(run()) == 1
    assert exits == [1]


def test_bounded_event_loop_cleanup_has_one_owner() -> None:
    """Mutation guard: call sites delegate and cannot drift their own teardown."""
    import inspect

    from onoats import _event_loop, cli, runtime

    owner = inspect.getsource(_event_loop.run_bounded_event_loop)
    assert "shutdown_asyncgens" in owner and "shutdown_default_executor" in owner
    for call_site in (runtime.run_recorder_event_loop, cli._run_socket_supervisor):
        source = inspect.getsource(call_site)
        assert "run_bounded_event_loop(" in source
        assert "shutdown_asyncgens" not in source
        assert "new_event_loop" not in source


def test_event_loop_finalizers_receive_a_budget_after_task_cancellation(
    monkeypatch,
) -> None:
    """Pending-task grace cannot consume the separate loop-finalizer budget."""
    from onoats import _event_loop

    finalized = []
    retained = []
    exits = []
    monkeypatch.setattr(_event_loop, "FINALIZER_TIMEOUT_SECONDS", 0.15)

    async def generator():
        try:
            yield None
        finally:
            await asyncio.sleep(0.08)
            finalized.append(True)

    async def resistant_task():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.12)

    async def run() -> int:
        stream = generator()
        retained.append(stream)
        await anext(stream)
        asyncio.create_task(resistant_task())
        await asyncio.sleep(0)
        return 0

    assert (
        _event_loop.run_bounded_event_loop(
            run(),
            cancel_timeout=0.15,
            hard_exit=exits.append,
            on_cleanup_failure=lambda: None,
        )
        == 0
    )
    assert finalized == [True]
    assert exits == []


@_async_test
async def test_lock_admission_order_prevents_flush_reopening_after_stop() -> None:
    rotation = RotationSpy()
    rotation.release.clear()
    controller, _shutdown, _force, _marks = _controller(rotation)

    first = asyncio.create_task(controller.flush("first flush"))
    await rotation.started.wait()
    stop = asyncio.create_task(controller.stop("queued stop"))
    late = asyncio.create_task(controller.flush("queued late flush"))
    await asyncio.sleep(0)
    rotation.release.set()

    assert await first is True
    assert await stop is True
    with pytest.raises(LifecycleStoppingError):
        await late
    assert rotation.calls == [("first flush", True)]


@_async_test
async def test_second_signal_sets_force_exit_without_second_state_owner() -> None:
    rotation = RotationSpy()
    controller, shutdown, force, marks = _controller(rotation)

    assert await controller.stop("first signal") is True
    loop = asyncio.get_running_loop()
    assert controller.signal(loop) is True
    await asyncio.sleep(0)
    assert shutdown.is_set()
    assert force.is_set()
    assert marks == ["graceful"]
    with pytest.raises(LifecycleStoppingError):
        await controller.flush("flush after force")

    assert controller.signal(loop) is True
    assert rotation.calls == []


@_async_test
async def test_back_to_back_signals_latch_first_synchronously_before_admission() -> (
    None
):
    rotation = RotationSpy()
    controller, shutdown, force, marks = _controller(rotation)
    loop = asyncio.get_running_loop()
    scheduled: list[asyncio.AbstractEventLoop] = []
    controller.schedule_stop = lambda target: scheduled.append(target)

    assert controller.signal(loop) is False
    assert scheduled == [loop]
    assert not shutdown.is_set(), "test seam must pause before async admission"
    assert controller.signal(loop) is True
    await asyncio.sleep(0)

    assert force.is_set()
    assert shutdown.is_set()
    assert marks == []
    assert rotation.calls == []


@pytest.mark.parametrize("outcome", ["done", "stuck", "fails"])
@pytest.mark.parametrize("force_before_stop_task", [False, True])
@_async_test
async def test_flush_stop_force_whole_state_space(
    outcome: str, force_before_stop_task: bool
) -> None:
    """A second signal bounds exit without changing first-Stop flush ordering."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def rotate(_reason: str, _continue_session: bool) -> bool:
        started.set()
        await release.wait()
        if outcome == "fails":
            raise OSError("flush failed")
        return True

    controller = RecorderLifecycleController(rotate)
    flush = asyncio.create_task(controller.flush("admitted"))
    await started.wait()
    loop = asyncio.get_running_loop()
    assert controller.signal(loop) is False
    if force_before_stop_task:
        assert controller.signal(loop) is True
    else:
        await asyncio.sleep(0)
        assert controller.signal(loop) is True
    await asyncio.sleep(0)

    assert controller.force_exit_event.is_set()
    assert controller.shutdown_event.is_set()
    assert not flush.done()
    release.set()
    if outcome == "fails":
        with pytest.raises(OSError, match="flush failed"):
            await flush
    else:
        assert await flush is True
    # The first signal's queued Stop retains admission order and completes
    # after the already-admitted continuation releases the lifecycle lock.
    await asyncio.sleep(0)
    assert controller.stopping


@_async_test
async def test_failed_terminal_rotation_never_reopens_continuations() -> None:
    calls: list[tuple[str, bool]] = []

    async def fail_rotation(reason: str, continue_session: bool) -> None:
        calls.append((reason, continue_session))
        raise OSError("terminal rotation failed")

    controller = RecorderLifecycleController(
        fail_rotation,
        mark_graceful=lambda: None,
    )

    with pytest.raises(OSError, match="terminal rotation failed"):
        await controller.terminal_rotation("terminal error")

    assert controller.state == "stopped"
    with pytest.raises(LifecycleStoppingError):
        await controller.flush("must stay closed")
    assert await controller.terminal_rotation("duplicate finally") is False
    assert calls == [("terminal error", False)]


@pytest.mark.parametrize("outcome", [True, False, "raise"])
@pytest.mark.parametrize("admit_stop_first", [False, True])
@_async_test
async def test_terminal_rotation_outcome_whole_state_space(
    outcome: bool | str, admit_stop_first: bool
) -> None:
    """Callback outcome controls success without reopening terminal admission."""
    calls: list[tuple[str, bool]] = []

    async def rotate(reason: str, continue_session: bool) -> bool:
        calls.append((reason, continue_session))
        if outcome == "raise":
            raise OSError("rotation callback raised")
        return outcome

    controller = RecorderLifecycleController(rotate)
    if admit_stop_first:
        assert await controller.stop("stop") is True

    if outcome is True:
        assert await controller.terminal_rotation("terminal") is True
    elif outcome is False:
        with pytest.raises(LifecycleRotationError, match="terminal rotation failed"):
            await controller.terminal_rotation("terminal")
    else:
        with pytest.raises(OSError, match="rotation callback raised"):
            await controller.terminal_rotation("terminal")

    assert controller.state == "stopped"
    assert controller.shutdown_event.is_set()
    assert calls == [("terminal", False)]
    assert await controller.terminal_rotation("duplicate") is False
    with pytest.raises(LifecycleStoppingError):
        await controller.flush("late continuation")


@_async_test
async def test_failed_continuation_rotation_is_reported_without_stopping_session() -> (
    None
):
    async def rejected(_reason: str, _continue_session: bool) -> bool:
        return False

    controller = RecorderLifecycleController(rejected)
    with pytest.raises(LifecycleRotationError, match="rotation failed"):
        await controller.flush("control flush")
    assert controller.state == "running"
    assert not controller.shutdown_event.is_set()


def test_lifecycle_controller_is_a_stdlib_only_leaf() -> None:
    source = (ROOT / "src/onoats/recorder_lifecycle.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        forbidden.extend(
            name for name in names if name == "onoats" or name.startswith("onoats.")
        )
    assert forbidden == []


@pytest.mark.parametrize("entrypoint", ["src/onoats/__main__.py", "src/onoats/dual.py"])
def test_recorder_entrypoints_mark_terminal_rotation_failure(entrypoint: str) -> None:
    """Both recorder return-code owners convert rotation failure to rc=1 state."""
    tree = ast.parse((ROOT / entrypoint).read_text(encoding="utf-8"))
    shutdown = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_shutdown"
    )
    terminal_try = next(
        node
        for node in ast.walk(shutdown)
        if isinstance(node, ast.Try)
        and any(
            isinstance(child, ast.Attribute) and child.attr == "terminal_rotation"
            for child in ast.walk(node)
        )
    )
    assert any(
        isinstance(child, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "ended_by_error"
            for target in child.targets
        )
        and isinstance(child.value, ast.Constant)
        and child.value.value is True
        for handler in terminal_try.handlers
        for child in ast.walk(handler)
    )


def test_control_server_has_no_second_lifecycle_lock_or_stopping_event_owner() -> None:
    import onoats.control_socket as control_socket

    source = inspect.getsource(control_socket.ControlServer)
    assert "_command_lock" not in source
    assert "stopping_event" not in source
    assert "self._stopping" not in source


@pytest.mark.parametrize(
    "relative_path", ["src/onoats/dual.py", "src/onoats/__main__.py"]
)
def test_both_entrypoints_delegate_lifecycle_serialization(relative_path: str) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    assert "RecorderLifecycleController(" in source
    assert "asyncio.Lock()" not in source
    control_flush = source.split("async def _control_flush() -> bool:", 1)[1].split(
        "async def _control_stop()", 1
    )[0]
    assert "return await lifecycle.flush" in control_flush
    assert "except LifecycleStoppingError" in control_flush


@pytest.mark.parametrize(
    "relative_path", ["src/onoats/dual.py", "src/onoats/__main__.py"]
)
def test_both_direct_entrypoints_use_bounded_event_loop_runner(
    relative_path: str,
) -> None:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    main = source.split("def main(", 1)[1]
    assert "run_recorder_event_loop(" in main
    assert "asyncio.run(" not in main


@_async_test
async def test_force_exit_bounds_terminal_rotation_queued_behind_stuck_flush() -> None:
    """Round 7: a stuck admitted Flush must not strand shutdown past force exit.

    The terminal rotation waits on the lifecycle lock the Flush holds; before
    the Flush raced force exit itself, nothing cancelled it (no adapter
    cancellation had happened) and a second Ctrl+C was ignored.
    """
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stuck(_reason: str, continue_session: bool) -> bool:
        if not continue_session:
            return True
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    controller = RecorderLifecycleController(stuck)
    flush = asyncio.create_task(controller.flush("stuck control flush"))
    await started.wait()
    terminal = asyncio.create_task(controller.terminal_rotation("pipeline ended"))
    await asyncio.sleep(0)
    assert not terminal.done(), "terminal rotation must queue behind the Flush"

    controller.force_exit_event.set()
    await asyncio.wait_for(terminal, timeout=1.0)
    assert cancelled.is_set(), "force exit must cancel the stuck continuation"
    with pytest.raises(LifecycleRotationError):
        await asyncio.wait_for(flush, timeout=0.5)


@pytest.mark.parametrize(
    "intent", ["first_signal_latched", "second_signal_force", "force_event_only"]
)
@_async_test
async def test_flush_rejected_once_termination_intent_precedes_stop_admission(
    intent: str,
) -> None:
    """Round 7 re-run: rows 2-4 of the admission table.

    A second signal (or the first signal's latch) can precede its Stop reaching
    the lock, leaving ``_state`` RUNNING.  A Flush arriving in that window must
    be rejected, never start a continuation rotation.
    """
    rotation = RotationSpy()
    controller, _shutdown, force, _marks = _controller(rotation)
    loop = asyncio.get_running_loop()
    scheduled: list[asyncio.AbstractEventLoop] = []
    controller.schedule_stop = lambda target: scheduled.append(target)

    if intent == "force_event_only":
        force.set()
    else:
        assert controller.signal(loop) is False
        if intent == "second_signal_force":
            assert controller.signal(loop) is True
            await asyncio.sleep(0)
            assert force.is_set()
    assert controller.state == "running", "test must exercise the pre-Stop window"

    with pytest.raises(LifecycleStoppingError):
        await controller.flush("late flush after termination intent")
    assert rotation.calls == []
    # Stop and terminal admission remain open in this window.
    assert await controller.stop("queued first-signal stop") is True
    assert await controller.terminal_rotation("shutdown") is True
    assert rotation.calls == [("shutdown", False)]
