"""Recorder-owned, generation-authenticated local control channel.

This module is deliberately a stdlib-only leaf.  It knows only the wire
representation of a recorder identity; callers may pass any object exposing
``pid`` and ``start_epoch`` attributes.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
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
_AT_FDCWD = -100
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001


class ControlError(RuntimeError):
    """The recorder rejected or could not receive a control request."""


@dataclass(frozen=True)
class ControlResponse:
    ok: bool
    code: str
    message: str


@dataclass
class _EndpointIdentity:
    """A socket-node identity, pinned by an ``O_PATH`` fd when available."""

    device: int
    inode: int
    descriptor: int | None = None

    def matches(self, candidate: os.stat_result) -> bool:
        if self.descriptor is None:
            expected = (self.device, self.inode)
        else:
            try:
                held = os.fstat(self.descriptor)
            except OSError:
                return False
            expected = (held.st_dev, held.st_ino)
        return (candidate.st_dev, candidate.st_ino) == expected

    def close(self) -> None:
        descriptor, self.descriptor = self.descriptor, None
        if descriptor is not None:
            os.close(descriptor)


def control_socket_path(data_dir: Path) -> Path:
    return data_dir / ".active" / CONTROL_SOCKET_FILENAME


def _validate_socket_path(path: Path) -> None:
    """Reject paths that cannot be represented without process-global state."""
    if len(os.fsencode(path)) + 1 > _DARWIN_SUN_PATH_BYTES:
        raise ControlError(
            "control socket path exceeds Darwin's sockaddr_un limit; "
            "configure a shorter ONOATS_DATA_DIR"
        )


def _capture_endpoint_identity(path: Path, opened: os.stat_result) -> _EndpointIdentity:
    """Capture a socket node without allowing Linux to recycle its inode.

    Linux may immediately reuse a socket node's inode after unlink. Holding an
    ``O_PATH`` descriptor pins that inode until cleanup has quarantined and
    verified the public entry. Darwin cannot open a socket node this way, so it
    retains the existing device/inode comparison.
    """
    o_path = getattr(os, "O_PATH", None)
    if o_path is None:
        return _EndpointIdentity(opened.st_dev, opened.st_ino)

    flags = o_path | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    identity = _EndpointIdentity(opened.st_dev, opened.st_ino, descriptor)
    try:
        held = os.fstat(descriptor)
        current = path.lstat()
        expected = (opened.st_dev, opened.st_ino)
        if (held.st_dev, held.st_ino) != expected or (
            current.st_dev,
            current.st_ino,
        ) != expected:
            raise ControlError(f"control endpoint changed while capturing {path}")
    except BaseException:
        identity.close()
        raise
    return identity


async def _start_owned_unix_server(
    callback: Callable[[asyncio.StreamReader, asyncio.StreamWriter], None],
    listener: socket.socket,
) -> asyncio.AbstractServer:
    """Start a server without delegating endpoint removal to asyncio.

    Python 3.13 added ``cleanup_socket`` with a default of ``True``. Python
    3.12 has no such parameter. Select by the runtime contract rather than
    signature introspection: some 3.13 builds expose the keyword only through
    ``**kwds`` even though omitting it still enables automatic unlinking.
    """
    kwargs: dict[str, Any] = {"sock": listener}
    if sys.version_info >= (3, 13):
        kwargs["cleanup_socket"] = False
    return await asyncio.start_unix_server(callback, **kwargs)


def _record_generation(record: Any) -> tuple[int, float]:
    return int(record.pid), float(record.start_epoch)


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically publish one filesystem entry without replacing another."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise OSError(errno.ENOSYS, "exclusive rename is unavailable")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(os.fsencode(source), os.fsencode(destination), _RENAME_EXCL)
    else:
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOSYS, "exclusive rename is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD,
            os.fsencode(source),
            _AT_FDCWD,
            os.fsencode(destination),
            _RENAME_NOREPLACE,
        )
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


def _quarantine_and_remove(path: Path, expected: _EndpointIdentity) -> bool:
    """Move the public entry aside, verify it, then remove only that inode.

    If a different entry wins the race, restore it with an exclusive rename.
    When a newer public entry already exists, retain the raced entry in
    quarantine rather than deleting either one. Unix sockets cannot be hard
    linked on Darwin, so restoration must use the same no-replace primitive as
    endpoint publication.
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
        if not expected.matches(moved):
            try:
                _rename_noreplace(quarantine_path, path)
            except OSError:
                return False
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


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class ControlServer:
    """One recorder generation's serialized Flush/Stop endpoint."""

    def __init__(
        self,
        data_dir: Path,
        record: Any,
        *,
        on_flush: Callable[[], Awaitable[None] | None],
        on_stop: Callable[[], Awaitable[None] | None],
        peer_uid_getter: Callable[[socket.socket], int] | None = None,
    ) -> None:
        self.path = control_socket_path(data_dir)
        self._pid, self._start_epoch = _record_generation(record)
        self._on_flush = on_flush
        self._on_stop = on_stop
        self._peer_uid_getter = peer_uid_getter or _darwin_peer_uid
        self._server: asyncio.AbstractServer | None = None
        self._closing = False
        self._handlers: set[asyncio.Task[Any]] = set()
        self._owned_identity: _EndpointIdentity | None = None

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
            try:
                stale_identity = _capture_endpoint_identity(self.path, existing)
            except (FileNotFoundError, OSError) as exc:
                raise ControlError(
                    f"control endpoint changed while removing stale socket {self.path}"
                ) from exc
            try:
                if not _quarantine_and_remove(self.path, stale_identity):
                    raise ControlError(
                        "control endpoint changed while removing stale socket "
                        f"{self.path}"
                    )
            finally:
                stale_identity.close()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        private_dir: Path | None = None
        private_path: Path | None = None
        captured: _EndpointIdentity | None = None
        try:
            private_dir = Path(
                tempfile.mkdtemp(prefix=".onoats-control-bind-", dir=self.path.parent)
            )
            os.chmod(private_dir, 0o700)
            private_path = private_dir / CONTROL_SOCKET_FILENAME
            listener.bind(str(private_path))
            os.chmod(private_path, 0o600)
            opened = private_path.lstat()
            if not stat.S_ISSOCK(opened.st_mode):
                raise ControlError("bound control endpoint is not a socket")
            captured = _capture_endpoint_identity(private_path, opened)
            listener.setblocking(False)
            listener.listen(socket.SOMAXCONN)
            try:
                _rename_noreplace(private_path, self.path)
            except FileExistsError as exc:
                raise ControlError(
                    f"control endpoint appeared while publishing {self.path}"
                ) from exc
            self._owned_identity = captured
            captured = None
            self._closing = False
            self._server = await _start_owned_unix_server(self._accept, listener)
        except BaseException:
            listener.close()
            owned = self._owned_identity
            self._owned_identity = None
            if owned is not None:
                try:
                    _quarantine_and_remove(self.path, owned)
                finally:
                    owned.close()
            raise
        finally:
            if captured is not None:
                captured.close()
            if private_path is not None:
                try:
                    private_path.unlink()
                except FileNotFoundError:
                    pass
            if private_dir is not None:
                try:
                    private_dir.rmdir()
                except OSError:
                    pass

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

        if command == "flush":
            accepted = await _maybe_await(self._on_flush())
            if accepted is False:
                return ControlResponse(False, "stopping", "recorder is stopping")
            return ControlResponse(True, "accepted", "flush rotation completed")
        await _maybe_await(self._on_stop())
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
        try:
            _quarantine_and_remove(self.path, owned)
        finally:
            owned.close()


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
