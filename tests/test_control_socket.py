"""Recorder-owned control-channel protocol and lifecycle regressions."""

from __future__ import annotations

import asyncio
import functools
import json
import os
import shutil
import socket
import uuid
from pathlib import Path

import pytest

from onoats._vendor.pid import PidRecord
from onoats.control_socket import (
    CONTROL_MAX_REQUEST_BYTES,
    CONTROL_PROTOCOL_VERSION,
    CONTROL_REQUEST_FIELDS,
    CONTROL_SOCKET_FILENAME,
    ControlServer,
    ControlError,
    send_control_command,
)


RECORD = PidRecord(pid=4100, cmdline="onoats bot", start_epoch=1234.5)


def _async_test(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


@pytest.fixture
def control_data_dir():
    """Keep the AF_UNIX path below macOS's short sockaddr_un limit."""
    path = Path("/private/tmp") / f"oc{uuid.uuid4().hex[:8]}"
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _request(
    command: str = "flush", *, record: PidRecord = RECORD, **changes: object
) -> dict[str, object]:
    value: dict[str, object] = {
        "version": CONTROL_PROTOCOL_VERSION,
        "command": command,
        "pid": record.pid,
        "start_epoch": record.start_epoch,
    }
    value.update(changes)
    return value


async def _raw_exchange(path: Path, payload: bytes) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(payload)
    await writer.drain()
    writer.write_eof()
    line = await asyncio.wait_for(reader.readline(), timeout=2)
    writer.close()
    await writer.wait_closed()
    assert line.endswith(b"\n"), line
    return json.loads(line)


async def _send(path: Path, value: dict[str, object]) -> dict[str, object]:
    return await _raw_exchange(path, json.dumps(value).encode() + b"\n")


@pytest.fixture
def callbacks():
    actions: list[str] = []

    async def flush() -> None:
        actions.append("flush")

    async def stop() -> None:
        actions.append("stop")

    return actions, flush, stop


def _bind_unix_socket(path: Path) -> socket.socket:
    endpoint = socket.socket(socket.AF_UNIX)
    endpoint.bind(str(path))
    return endpoint


@_async_test
@pytest.mark.parametrize("cleanup_socket_supported", [False, True])
async def test_asyncio_socket_cleanup_is_never_endpoint_owner(
    monkeypatch, cleanup_socket_supported: bool
) -> None:
    """Python 3.12/3.13 both leave inode cleanup to ControlServer.close."""
    import onoats.control_socket as control

    listener = object()
    server = object()
    calls: list[tuple[object, object, object]] = []

    if cleanup_socket_supported:

        async def start(callback, *, sock, cleanup_socket=True):
            calls.append((callback, sock, cleanup_socket))
            return server

    else:

        async def start(callback, *, sock):
            calls.append((callback, sock, "unsupported"))
            return server

    monkeypatch.setattr(control.asyncio, "start_unix_server", start)

    def callback(_reader, _writer):
        return None

    assert await control._start_owned_unix_server(callback, listener) is server
    assert calls == [
        (
            callback,
            listener,
            False if cleanup_socket_supported else "unsupported",
        )
    ]


@pytest.mark.parametrize("operation", ["bind", "connect"])
def test_overlong_control_paths_fail_without_changing_process_cwd(
    tmp_path: Path, operation: str
) -> None:
    data_dir = tmp_path / ("x" * 110)
    cwd_before = Path.cwd()

    with pytest.raises(ControlError, match="sockaddr_un|shorter ONOATS_DATA_DIR"):
        if operation == "bind":
            server = ControlServer(
                data_dir,
                RECORD,
                on_flush=lambda: None,
                on_stop=lambda: None,
                peer_uid_getter=lambda _sock: os.geteuid(),
            )
            asyncio.run(server.start())
        else:
            send_control_command(data_dir, RECORD, "flush")

    assert Path.cwd() == cwd_before
    assert not (data_dir / ".active").exists()


@_async_test
async def test_server_uses_owner_only_endpoint_and_command_specific_handlers(
    control_data_dir: Path, callbacks
) -> None:
    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    try:
        assert path.is_socket()
        assert path.parent.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600
        flush_response = await _send(path, _request("flush"))
        stop_response = await _send(path, _request("stop"))
    finally:
        await server.close()

    assert flush_response["ok"] is True
    assert stop_response["ok"] is True
    assert actions == ["flush", "stop"], "handler swapping must fail differentially"
    assert not path.exists()


@_async_test
@pytest.mark.parametrize(
    "changes",
    [
        {"pid": None},
        {"start_epoch": None},
        {"pid": RECORD.pid + 1},
        {"start_epoch": RECORD.start_epoch + 1},
        {"version": CONTROL_PROTOCOL_VERSION + 1},
        {"command": "rotate"},
        {"extra": "field"},
    ],
)
async def test_generation_and_exact_request_shape_fail_closed(
    control_data_dir: Path, callbacks, changes: dict[str, object]
) -> None:
    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    try:
        response = await _send(
            control_data_dir / ".active" / CONTROL_SOCKET_FILENAME,
            _request(**changes),
        )
    finally:
        await server.close()
    assert response["ok"] is False
    assert actions == []


@_async_test
@pytest.mark.parametrize("missing", CONTROL_REQUEST_FIELDS)
async def test_every_required_field_is_rejected_when_absent(
    control_data_dir: Path, callbacks, missing: str
) -> None:
    actions, flush, stop = callbacks
    value = _request()
    del value[missing]
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    try:
        response = await _send(
            control_data_dir / ".active" / CONTROL_SOCKET_FILENAME, value
        )
    finally:
        await server.close()
    assert response["ok"] is False
    assert actions == []


@_async_test
@pytest.mark.parametrize(
    "payload",
    [
        b"not-json\n",
        b"[]\n",
        json.dumps(_request()).encode()
        + b"\n"
        + json.dumps(_request()).encode()
        + b"\n",
        b"{" + b" " * CONTROL_MAX_REQUEST_BYTES + b"}\n",
    ],
)
async def test_malformed_oversized_and_multi_message_input_is_rejected(
    control_data_dir: Path, callbacks, payload: bytes
) -> None:
    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    try:
        response = await _raw_exchange(
            control_data_dir / ".active" / CONTROL_SOCKET_FILENAME, payload
        )
    finally:
        await server.close()
    assert response["ok"] is False
    assert actions == []


@_async_test
async def test_request_byte_limit_is_inclusive(
    control_data_dir: Path, callbacks
) -> None:
    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    compact = json.dumps(_request(), separators=(",", ":")).encode()
    payload = compact + b" " * (CONTROL_MAX_REQUEST_BYTES - len(compact) - 1) + b"\n"
    assert len(payload) == CONTROL_MAX_REQUEST_BYTES
    try:
        response = await _raw_exchange(path, payload)
    finally:
        await server.close()
    assert response["ok"] is True
    assert actions == ["flush"]


@_async_test
@pytest.mark.parametrize(
    "peer_uid_getter",
    [lambda _sock: None, lambda _sock: os.geteuid() + 1],
    ids=["credentials-absent", "wrong-uid"],
)
async def test_peer_credentials_fail_closed(
    control_data_dir: Path, callbacks, peer_uid_getter
) -> None:
    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=peer_uid_getter,
    )
    await server.start()
    try:
        response = await _send(
            control_data_dir / ".active" / CONTROL_SOCKET_FILENAME, _request()
        )
    finally:
        await server.close()
    assert response["ok"] is False
    assert actions == []


@_async_test
async def test_malformed_peer_credential_probe_fails_closed(
    control_data_dir: Path, callbacks
) -> None:
    actions, flush, stop = callbacks

    def malformed(_sock):
        raise ValueError("bad xucred version")

    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=malformed,
    )
    await server.start()
    try:
        response = await _send(
            control_data_dir / ".active" / CONTROL_SOCKET_FILENAME, _request()
        )
    finally:
        await server.close()
    assert response["ok"] is False
    assert actions == []


@_async_test
async def test_duplicate_stop_is_idempotent_and_flush_after_stop_is_rejected(
    control_data_dir: Path, callbacks
) -> None:
    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    try:
        first = await _send(path, _request("stop"))
        duplicate = await _send(path, _request("stop"))
        late_flush = await _send(path, _request("flush"))
    finally:
        await server.close()
    assert first["ok"] is duplicate["ok"] is True
    assert late_flush["ok"] is False
    assert actions == ["stop"]


@_async_test
@pytest.mark.parametrize(
    ("signal_stop_requested", "command", "accepted", "expected_actions"),
    [
        (False, "flush", True, ["flush"]),
        (False, "stop", True, ["stop"]),
        (True, "flush", False, []),
        (True, "stop", True, []),
    ],
)
async def test_signal_and_socket_stop_share_the_complete_command_state_space(
    control_data_dir: Path,
    callbacks,
    signal_stop_requested: bool,
    command: str,
    accepted: bool,
    expected_actions: list[str],
) -> None:
    actions, flush, stop = callbacks
    stopping_event = asyncio.Event()
    if signal_stop_requested:
        stopping_event.set()
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        stopping_event=stopping_event,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    try:
        response = await _send(
            control_data_dir / ".active" / CONTROL_SOCKET_FILENAME,
            _request(command),
        )
    finally:
        await server.close()
    assert response["ok"] is accepted
    assert actions == expected_actions


@_async_test
async def test_flush_first_finishes_but_signal_stop_rejects_the_next_flush(
    control_data_dir: Path,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    stopping_event = asyncio.Event()
    actions: list[str] = []

    async def flush() -> None:
        actions.append("flush")
        entered.set()
        await release.wait()

    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=lambda: None,
        stopping_event=stopping_event,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    try:
        first_task = asyncio.create_task(_send(path, _request("flush")))
        await asyncio.wait_for(entered.wait(), timeout=2)
        stopping_event.set()
        second_task = asyncio.create_task(_send(path, _request("flush")))
        release.set()
        first, second = await asyncio.gather(first_task, second_task)
    finally:
        await server.close()
    assert first["ok"] is True
    assert second == {
        "version": CONTROL_PROTOCOL_VERSION,
        "ok": False,
        "code": "stopping",
        "message": "recorder is stopping",
    }
    assert actions == ["flush"]


@_async_test
@pytest.mark.parametrize("first", ["flush", "stop"])
async def test_simultaneous_commands_follow_lock_acquisition_order(
    control_data_dir: Path, first: str
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    actions: list[str] = []

    async def flush() -> None:
        actions.append("flush")
        if first == "flush":
            entered.set()
            await release.wait()

    async def stop() -> None:
        actions.append("stop")
        if first == "stop":
            entered.set()
            await release.wait()

    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    second = "stop" if first == "flush" else "flush"
    try:
        first_task = asyncio.create_task(_send(path, _request(first)))
        await asyncio.wait_for(entered.wait(), timeout=2)
        second_task = asyncio.create_task(_send(path, _request(second)))
        await asyncio.sleep(0)
        release.set()
        first_response, second_response = await asyncio.gather(first_task, second_task)
    finally:
        await server.close()

    assert first_response["ok"] is True
    if first == "flush":
        assert second_response["ok"] is True
        assert actions == ["flush", "stop"]
    else:
        assert second_response["ok"] is False
        assert actions == ["stop"]


@_async_test
async def test_action_survives_client_acknowledgement_loss(
    control_data_dir: Path, callbacks
) -> None:
    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    try:
        _reader, writer = await asyncio.open_unix_connection(path)
        writer.write(json.dumps(_request("flush")).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if actions:
                break
            await asyncio.sleep(0.01)
    finally:
        await server.close()
    assert actions == ["flush"]


@_async_test
@pytest.mark.parametrize("command", ["flush", "stop"])
async def test_close_settles_handler_paused_before_first_coroutine_instruction(
    control_data_dir: Path, command: str
) -> None:
    """An accepted callback is registered before its coroutine can run."""
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    actions: list[tuple[str, bool]] = []

    async def flush() -> None:
        actions.append(("flush", path.exists()))

    async def stop() -> None:
        actions.append(("stop", path.exists()))

    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    loop = asyncio.get_running_loop()
    prior_factory = loop.get_task_factory()
    accepted = asyncio.Event()
    release = asyncio.Event()
    intercepted = False

    def pause_control_handler(loop, coroutine, context=None):
        nonlocal intercepted
        code = getattr(coroutine, "cr_code", None)
        qualname = "" if code is None else code.co_qualname
        if not intercepted and qualname.endswith(
            ("ControlServer._accept", "ControlServer._serve_client")
        ):
            intercepted = True
            accepted.set()
            target = coroutine

            async def paused():
                await release.wait()
                return await target

            coroutine = paused()
        return asyncio.tasks.Task(coroutine, loop=loop, context=context)

    loop.set_task_factory(pause_control_handler)
    reader = None
    writer = None
    close_task = None
    exchange_task = None
    try:
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write(json.dumps(_request(command)).encode() + b"\n")
        await writer.drain()
        writer.write_eof()
        await asyncio.wait_for(accepted.wait(), timeout=2)
        await asyncio.sleep(0)
        loop.set_task_factory(prior_factory)

        async def receive_response():
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            return json.loads(line)

        exchange_task = asyncio.create_task(receive_response())
        close_task = asyncio.create_task(server.close())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        closed_before_release = close_task.done()
        release.set()
        response, _ = await asyncio.gather(exchange_task, close_task)
    finally:
        loop.set_task_factory(prior_factory)
        release.set()
        if close_task is None:
            await server.close()
        elif not close_task.done():
            await close_task
        if writer is not None:
            writer.close()
            await writer.wait_closed()

    assert intercepted
    assert not closed_before_release, "close returned with an accepted handler paused"
    assert response["ok"] is True
    assert actions == [(command, True)], (
        "Flush/Stop ran only while endpoint teardown authority was still published"
    )
    assert not path.exists()


@_async_test
async def test_stale_startup_socket_replacement_at_final_boundary_survives(
    control_data_dir: Path, callbacks, monkeypatch
) -> None:
    import onoats.control_socket as control

    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    path.parent.mkdir(parents=True)
    stale = _bind_unix_socket(path)
    successor = None
    successor_inode = None
    real_rename = control.os.rename

    def replace_after_ownership_check(source, destination):
        nonlocal successor, successor_inode
        path.unlink()
        successor = _bind_unix_socket(path)
        successor_inode = path.stat().st_ino
        return real_rename(source, destination)

    monkeypatch.setattr(control.os, "rename", replace_after_ownership_check)
    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    try:
        with pytest.raises(control.ControlError, match="changed"):
            await server.start()
        assert successor is not None
        assert path.stat().st_ino == successor_inode
    finally:
        stale.close()
        if successor is not None:
            successor.close()
        path.unlink(missing_ok=True)


@_async_test
async def test_owned_shutdown_socket_replacement_at_final_boundary_survives(
    control_data_dir: Path, callbacks, monkeypatch
) -> None:
    import onoats.control_socket as control

    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    successor = None
    successor_inode = None
    real_rename = control.os.rename

    def replace_after_ownership_check(source, destination):
        nonlocal successor, successor_inode
        path.unlink()
        successor = _bind_unix_socket(path)
        successor_inode = path.stat().st_ino
        return real_rename(source, destination)

    monkeypatch.setattr(control.os, "rename", replace_after_ownership_check)
    try:
        await server.close()
        assert successor is not None
        assert path.stat().st_ino == successor_inode
    finally:
        if successor is not None:
            successor.close()
        path.unlink(missing_ok=True)


@_async_test
async def test_socket_quarantine_collision_preserves_foreign_and_newer_endpoints(
    control_data_dir: Path, callbacks, monkeypatch
) -> None:
    import onoats.control_socket as control

    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    raced = None
    newer = None
    raced_inode = None
    newer_inode = None
    real_rename = control.os.rename

    def collide_with_restore(source, destination):
        nonlocal raced, newer, raced_inode, newer_inode
        path.unlink()
        raced = _bind_unix_socket(path)
        raced_inode = path.stat().st_ino
        result = real_rename(source, destination)
        newer = _bind_unix_socket(path)
        newer_inode = path.stat().st_ino
        return result

    monkeypatch.setattr(control.os, "rename", collide_with_restore)
    try:
        await server.close()
        assert newer is not None
        assert path.stat().st_ino == newer_inode
        quarantined = list(
            path.parent.glob(f".onoats-control-quarantine-*/{CONTROL_SOCKET_FILENAME}")
        )
        assert len(quarantined) == 1
        assert raced is not None
        assert quarantined[0].stat().st_ino == raced_inode
    finally:
        if raced is not None:
            raced.close()
        if newer is not None:
            newer.close()
        path.unlink(missing_ok=True)
        for quarantine in path.parent.glob(".onoats-control-quarantine-*"):
            shutil.rmtree(quarantine, ignore_errors=True)


@_async_test
async def test_old_server_close_preserves_successor_endpoint(
    control_data_dir: Path, callbacks
) -> None:
    actions, flush, stop = callbacks
    old = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await old.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    path.unlink()
    successor_socket = socket.socket(socket.AF_UNIX)
    successor_socket.bind(str(path))
    successor_inode = path.stat().st_ino
    try:
        await old.close()
        assert path.stat().st_ino == successor_inode
    finally:
        successor_socket.close()
        path.unlink(missing_ok=True)


@_async_test
async def test_successor_rejects_old_expected_generation(
    control_data_dir: Path, callbacks
) -> None:
    actions, flush, stop = callbacks
    successor = PidRecord(RECORD.pid, RECORD.cmdline, RECORD.start_epoch + 1)
    server = ControlServer(
        control_data_dir,
        successor,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    try:
        old = await _send(path, _request(record=RECORD))
        current = await _send(path, _request(record=successor))
    finally:
        await server.close()
    assert old["ok"] is False
    assert current["ok"] is True
    assert actions == ["flush"]


def test_sync_client_fails_closed_for_missing_or_legacy_endpoint(
    control_data_dir: Path,
) -> None:
    from onoats.control_socket import ControlError

    with pytest.raises(ControlError, match="unavailable|incompatible"):
        send_control_command(control_data_dir, RECORD, "flush")
    path = control_data_dir / ".active" / CONTROL_SOCKET_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("legacy", encoding="utf-8")
    with pytest.raises(ControlError, match="unavailable|incompatible"):
        send_control_command(control_data_dir, RECORD, "stop")


def test_darwin_local_peercred_validates_layout_version_and_uid(monkeypatch) -> None:
    import onoats.control_socket as control

    monkeypatch.setattr(control.sys, "platform", "darwin")

    class Peer:
        raw = b""

        def getsockopt(self, level, option, size):
            assert (level, option, size) == (
                control._SOL_LOCAL,
                control._LOCAL_PEERCRED,
                control._XUCRED.size,
            )
            return self.raw

    peer = Peer()
    peer.raw = control._XUCRED.pack(
        control._XUCRED_VERSION, os.geteuid(), 0, *([0] * 16)
    )
    assert control._darwin_peer_uid(peer) == os.geteuid()

    peer.raw = peer.raw[:-1]
    with pytest.raises(control.ControlError, match="malformed"):
        control._darwin_peer_uid(peer)

    peer.raw = control._XUCRED.pack(
        control._XUCRED_VERSION + 1, os.geteuid(), 0, *([0] * 16)
    )
    with pytest.raises(control.ControlError, match="unsupported"):
        control._darwin_peer_uid(peer)


@pytest.mark.parametrize("start_epoch", [0.0, -1.0, float("nan"), float("inf")])
def test_client_rejects_incomplete_generation_before_socket_io(
    control_data_dir: Path, start_epoch: float
) -> None:
    from onoats.control_socket import ControlError

    legacy = PidRecord(RECORD.pid, RECORD.cmdline, start_epoch)
    with pytest.raises(ControlError, match="identity|legacy|generation|start_epoch"):
        send_control_command(control_data_dir, legacy, "flush")


@_async_test
@pytest.mark.parametrize(
    ("version", "accepted"),
    [(True, False), (1.0, False), ("1", False), (None, False), (1, True)],
)
async def test_request_version_requires_json_integer_one(
    control_data_dir: Path, callbacks, version: object, accepted: bool
) -> None:
    actions, flush, stop = callbacks
    server = ControlServer(
        control_data_dir,
        RECORD,
        on_flush=flush,
        on_stop=stop,
        peer_uid_getter=lambda _sock: os.geteuid(),
    )
    await server.start()
    try:
        response = await _send(
            control_data_dir / ".active" / CONTROL_SOCKET_FILENAME,
            _request(version=version),
        )
    finally:
        await server.close()
    assert response["ok"] is accepted
    assert actions == (["flush"] if accepted else [])


@pytest.mark.parametrize(
    ("version", "accepted"),
    [(True, False), (1.0, False), ("1", False), (None, False), (1, True)],
)
def test_acknowledgement_version_requires_json_integer_one(
    control_data_dir: Path, monkeypatch, version: object, accepted: bool
) -> None:
    import onoats.control_socket as control

    response = (
        json.dumps(
            {
                "version": version,
                "ok": True,
                "code": "accepted",
                "message": "flush rotation completed",
            },
            separators=(",", ":"),
        )
        + "\n"
    ).encode()

    class FakeSocket:
        def __init__(self, *_args, **_kwargs):
            self.chunks = [response, b""]

        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            pass

        def sendall(self, _payload):
            pass

        def shutdown(self, _direction):
            pass

        def recv(self, _size):
            return self.chunks.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(control.socket, "socket", FakeSocket)
    if accepted:
        parsed = send_control_command(control_data_dir, RECORD, "flush")
        assert parsed.ok is True
    else:
        with pytest.raises(control.ControlError, match="incompatible"):
            send_control_command(control_data_dir, RECORD, "flush")
