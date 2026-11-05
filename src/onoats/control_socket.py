"""Recorder-owned, generation-authenticated local control channel.

This module is deliberately a stdlib-only leaf.  It knows only the wire
representation of a recorder identity; callers may pass any object exposing
``pid`` and ``start_epoch`` attributes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import os
import socket
import stat
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

CONTROL_SOCKET_FILENAME = "onoats-control.sock"
CONTROL_PROTOCOL_VERSION = 1
CONTROL_MAX_REQUEST_BYTES = 4096
CONTROL_MAX_RESPONSE_BYTES = 4096
CONTROL_REQUEST_TIMEOUT_SECONDS = 5.0
CONTROL_REQUEST_FIELDS = ("version", "command", "pid", "start_epoch")
CONTROL_RESPONSE_FIELDS = ("version", "ok", "code", "message")
CONTROL_COMMANDS = ("flush", "stop")

_SOL_LOCAL = 0
_LOCAL_PEERCRED = 1
_XUCRED_VERSION = 0
_XUCRED = struct.Struct("@IIh16I")
_DARWIN_SUN_PATH_BYTES = 104


class ControlError(RuntimeError):
    """The recorder rejected or could not receive a control request."""


@dataclass(frozen=True)
class ControlResponse:
    ok: bool
    code: str
    message: str


def control_socket_path(data_dir: Path) -> Path:
    return data_dir / ".active" / CONTROL_SOCKET_FILENAME


def _validate_socket_path(path: Path) -> None:
    """Reject paths that cannot be represented without process-global state."""
    if len(os.fsencode(path)) + 1 > _DARWIN_SUN_PATH_BYTES:
        raise ControlError(
            "control socket path exceeds Darwin's sockaddr_un limit; "
            "configure a shorter ONOATS_DATA_DIR"
        )


async def _start_owned_unix_server(
    callback: Callable[[asyncio.StreamReader, asyncio.StreamWriter], None],
    listener: socket.socket,
) -> asyncio.AbstractServer:
    """Start a server without delegating endpoint removal to asyncio.

    Python 3.13 added ``cleanup_socket`` with a default of ``True``. Python
    3.12 has no such parameter, so feature-detect the keyword while preserving
    this module's inode-checked cleanup as the sole removal authority.
    """
    kwargs: dict[str, Any] = {"sock": listener}
    if "cleanup_socket" in inspect.signature(asyncio.start_unix_server).parameters:
        kwargs["cleanup_socket"] = False
    return await asyncio.start_unix_server(callback, **kwargs)


def _record_generation(record: Any) -> tuple[int, float]:
    return int(record.pid), float(record.start_epoch)


def _quarantine_and_remove(path: Path, expected: tuple[int, int]) -> bool:
    """Move the public entry aside, verify it, then remove only that inode.

    If a different entry wins the race, restore it with a no-overwrite hard
    link. When a newer public entry already exists, retain the raced entry in
    quarantine rather than deleting either one.
    """
    try:
        quarantine_dir = Path(
            tempfile.mkdtemp(prefix=".onoats-control-quarantine-", dir=path.parent)
        )
    except OSError:
        return False
    quarantine_path = quarantine_dir / path.name
    try:
        os.rename(path, quarantine_path)
    except OSError:
        try:
            quarantine_dir.rmdir()
        except OSError:
            pass
        return False

    try:
        moved = quarantine_path.lstat()
        if (moved.st_dev, moved.st_ino) != expected:
            try:
                os.link(quarantine_path, path, follow_symlinks=False)
            except OSError:
                return False
            # Retain the quarantine link even after restoration. A successor
            # may replace the public name immediately, and cleanup must never
            # delete the raced foreign entry's final link.
            return False
        quarantine_path.unlink()
        return True
    except OSError:
        return False
    finally:
        try:
            quarantine_dir.rmdir()
        except OSError:
            pass


def _darwin_peer_uid(peer: socket.socket) -> int:
    """Return the Darwin LOCAL_PEERCRED uid, failing closed elsewhere."""
    if sys.platform != "darwin":
        raise ControlError("LOCAL_PEERCRED is unavailable on this platform")
    raw = peer.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED.size)
    if len(raw) != _XUCRED.size:
        raise ControlError("LOCAL_PEERCRED returned a malformed xucred")
    values = _XUCRED.unpack(raw)
    version, uid, group_count = values[:3]
    if version != _XUCRED_VERSION or not 0 <= group_count <= 16:
        raise ControlError("LOCAL_PEERCRED returned an unsupported xucred")
    return uid


def _encoded_response(ok: bool, code: str, message: str) -> bytes:
    payload = {
        "version": CONTROL_PROTOCOL_VERSION,
        "ok": ok,
        "code": code,
        "message": message,
    }
    encoded = (
        json.dumps(payload, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    if len(encoded) > CONTROL_MAX_RESPONSE_BYTES:
        raise ControlError("control response exceeded its byte limit")
    return encoded


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


class ControlServer:
    """One recorder generation's serialized Flush/Stop endpoint."""

    def __init__(
        self,
        data_dir: Path,
        record: Any,
        *,
        on_flush: Callable[[], Awaitable[None] | None],
        on_stop: Callable[[], Awaitable[None] | None],
        stopping_event: asyncio.Event | None = None,
        peer_uid_getter: Callable[[socket.socket], int] | None = None,
    ) -> None:
        self.path = control_socket_path(data_dir)
        self._pid, self._start_epoch = _record_generation(record)
        self._on_flush = on_flush
        self._on_stop = on_stop
        self._stopping_event = stopping_event
        self._peer_uid_getter = peer_uid_getter or _darwin_peer_uid
        self._server: asyncio.AbstractServer | None = None
        self._command_lock = asyncio.Lock()
        self._stopping = False
        self._closing = False
        self._handlers: set[asyncio.Task[Any]] = set()
        self._owned_identity: tuple[int, int] | None = None

    async def start(self) -> None:
        if self._server is not None:
            return
        _validate_socket_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        try:
            existing = self.path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(existing.st_mode):
                raise ControlError(
                    f"refusing to replace non-socket control path {self.path}"
                )
            if not _quarantine_and_remove(
                self.path, (existing.st_dev, existing.st_ino)
            ):
                raise ControlError(
                    f"control endpoint changed while removing stale socket {self.path}"
                )
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            listener.setblocking(False)
            listener.listen(socket.SOMAXCONN)
            self._server = await _start_owned_unix_server(self._accept, listener)
        except BaseException:
            listener.close()
            raise
        os.chmod(self.path, 0o600)
        opened = self.path.lstat()
        if not stat.S_ISSOCK(opened.st_mode):
            await self.close()
            raise ControlError("bound control endpoint is not a socket")
        self._owned_identity = (opened.st_dev, opened.st_ino)

        self._closing = False

    def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        handler = self._reject_closing if self._closing else self._serve_client
        task = asyncio.create_task(handler(reader, writer))
        self._handlers.add(task)
        task.add_done_callback(self._handlers.discard)

    async def _reject_closing(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        del reader
        try:
            writer.write(
                _encoded_response(False, "stopping", "control server is closing")
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            response = await self._handle(reader, writer)
            writer.write(
                _encoded_response(response.ok, response.code, response.message)
            )
            await writer.drain()
        except Exception as exc:
            try:
                writer.write(_encoded_response(False, "server-error", str(exc)))
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> ControlResponse:
        peer = writer.get_extra_info("socket")
        if peer is None:
            return ControlResponse(
                False, "unauthorized", "peer credentials unavailable"
            )
        try:
            uid = self._peer_uid_getter(peer)
        except Exception:
            return ControlResponse(
                False, "unauthorized", "peer credentials unavailable"
            )
        if uid != os.geteuid():
            return ControlResponse(
                False, "unauthorized", "peer uid does not own recorder"
            )

        try:
            raw = await asyncio.wait_for(
                reader.readexactly(CONTROL_MAX_REQUEST_BYTES + 1),
                timeout=CONTROL_REQUEST_TIMEOUT_SECONDS,
            )
        except asyncio.IncompleteReadError as exc:
            raw = exc.partial
        except TimeoutError:
            return ControlResponse(False, "malformed", "request did not reach EOF")
        if len(raw) > CONTROL_MAX_REQUEST_BYTES:
            return ControlResponse(False, "oversized", "request exceeds byte limit")
        if raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
            return ControlResponse(
                False, "malformed", "request must contain one JSON line"
            )
        try:
            request = json.loads(raw[:-1])
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ControlResponse(False, "malformed", "request is not valid JSON")
        if not isinstance(request, dict) or set(request) != set(CONTROL_REQUEST_FIELDS):
            return ControlResponse(False, "malformed", "request fields do not match v1")
        if (
            type(request["version"]) is not int
            or request["version"] != CONTROL_PROTOCOL_VERSION
        ):
            return ControlResponse(
                False, "unsupported-version", "unsupported protocol version"
            )
        command = request["command"]
        if command not in CONTROL_COMMANDS:
            return ControlResponse(False, "unsupported-command", "unsupported command")
        pid = request["pid"]
        epoch = request["start_epoch"]
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return ControlResponse(False, "malformed", "pid must be a positive integer")
        if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
            return ControlResponse(False, "malformed", "start_epoch must be numeric")
        if not math.isfinite(float(epoch)) or float(epoch) <= 0:
            return ControlResponse(False, "malformed", "start_epoch must be positive")
        if pid != self._pid or float(epoch) != self._start_epoch:
            return ControlResponse(
                False, "generation-mismatch", "recorder generation changed"
            )

        async with self._command_lock:
            if command == "flush":
                if self._stopping or (
                    self._stopping_event is not None and self._stopping_event.is_set()
                ):
                    return ControlResponse(False, "stopping", "recorder is stopping")
                await _maybe_await(self._on_flush())
                return ControlResponse(True, "accepted", "flush rotation completed")
            if self._stopping or (
                self._stopping_event is not None and self._stopping_event.is_set()
            ):
                return ControlResponse(True, "accepted", "stop already accepted")
            await _maybe_await(self._on_stop())
            self._stopping = True
            return ControlResponse(True, "accepted", "graceful stop requested")

    async def close(self) -> None:
        """Stop accepting, settle accepted commands, then remove our socket."""
        self._closing = True
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        current = asyncio.current_task()
        await asyncio.sleep(0)
        while True:
            pending = [task for task in self._handlers if task is not current]
            if not pending:
                break
            await asyncio.gather(*pending, return_exceptions=True)
            await asyncio.sleep(0)
        owned = self._owned_identity
        self._owned_identity = None
        if owned is None:
            return
        _quarantine_and_remove(self.path, owned)


def send_control_command(
    data_dir: Path,
    record: Any,
    command: str,
    *,
    timeout: float = 5.0,
) -> ControlResponse:
    """Send one complete-generation request and wait for its acknowledgement."""
    if command not in CONTROL_COMMANDS:
        raise ControlError(f"unsupported control command: {command}")
    pid, epoch = _record_generation(record)
    if pid <= 0 or not math.isfinite(epoch) or epoch <= 0:
        raise ControlError("recorder identity is incomplete or legacy")
    request = {
        "version": CONTROL_PROTOCOL_VERSION,
        "command": command,
        "pid": pid,
        "start_epoch": epoch,
    }
    encoded = (
        json.dumps(request, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    if len(encoded) > CONTROL_MAX_REQUEST_BYTES:
        raise ControlError("control request exceeded its byte limit")
    path = control_socket_path(data_dir)
    _validate_socket_path(path)
    peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    peer.settimeout(timeout)
    try:
        peer.connect(str(path))
        peer.sendall(encoded)
        peer.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = peer.recv(min(4096, CONTROL_MAX_RESPONSE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > CONTROL_MAX_RESPONSE_BYTES:
                raise ControlError("control response exceeded its byte limit")
    except (FileNotFoundError, ConnectionError, OSError) as exc:
        raise ControlError(
            "recorder control channel is unavailable; the running recorder may "
            "be incompatible and was not signalled"
        ) from exc
    finally:
        peer.close()
    raw = b"".join(chunks)
    if raw.count(b"\n") != 1 or not raw.endswith(b"\n"):
        raise ControlError("recorder returned a malformed acknowledgement")
    try:
        payload = json.loads(raw[:-1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlError("recorder returned a malformed acknowledgement") from exc
    if not isinstance(payload, dict) or set(payload) != set(CONTROL_RESPONSE_FIELDS):
        raise ControlError("recorder returned an incompatible acknowledgement")
    if (
        type(payload["version"]) is not int
        or payload["version"] != CONTROL_PROTOCOL_VERSION
    ):
        raise ControlError("recorder returned an incompatible acknowledgement")
    if not isinstance(payload["ok"], bool) or not all(
        isinstance(payload[key], str) for key in ("code", "message")
    ):
        raise ControlError("recorder returned a malformed acknowledgement")
    return ControlResponse(payload["ok"], payload["code"], payload["message"])
