"""Ephemeral, display-only telemetry for the menu-bar application.

This module is deliberately a stdlib-only leaf.  Producers mutate
``UITelemetryState`` in memory; the single ``UITelemetryPublisher`` task owns
coalesced filesystem writes.  Nothing consuming this file may use it as a
liveness authority -- the PID and lifecycle status files retain that role.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import json
import logging
import math
import os
import stat
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

UI_TELEMETRY_SCHEMA_VERSION = 1
UI_TELEMETRY_FILENAME = "onoats.ui.json"
UI_TELEMETRY_MODE = 0o600
MAX_WRITE_HZ = 4.0
HEARTBEAT_SECONDS = 2.0
SNAPSHOT_EXPIRY_SECONDS = 5.0
AUDIO_OBSERVATION_EXPIRY_SECONDS = 1.0
AUDIO_OBSERVATION_REFRESH_SECONDS = 0.5
SEMINAR_CATEGORY = "seminars"
TRANSCRIPT_LIMIT = 240
DETAIL_LIMIT = 256
LEVEL_MIN = 0.0
LEVEL_MAX = 1.0
MAX_SNAPSHOT_BYTES = 64 * 1024
_AT_FDCWD = -2
_RENAME_EXCL = 0x00000004

STT_STATES = frozenset({"failed", "recovering", "starting", "unknown", "ready"})
STT_PRECEDENCE = ("failed", "recovering", "starting", "unknown", "ready")
BRANCHES = ("me", "them")

_LOG = logging.getLogger(__name__)


def aggregate_stt_state(me: str, them: str) -> str:
    """Return the fixed worst-state aggregate for the two required branches."""
    if me not in STT_STATES or them not in STT_STATES:
        raise ValueError("invalid STT state")
    for state in STT_PRECEDENCE:
        if state in (me, them):
            return state
    raise AssertionError("unreachable")


def _cap(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


class UITelemetryState:
    """Mutable in-memory state updated by synchronous recorder callbacks."""

    def __init__(
        self,
        pid: int,
        start_epoch: float,
        locked_category: str | None = None,
        *,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.pid = int(pid)
        self.start_epoch = float(start_epoch)
        self.locked_category = _cap(locked_category, DETAIL_LIMIT)
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._audio: dict[str, dict[str, float] | None] = {
            branch: None for branch in BRANCHES
        }
        self._audio_observed_monotonic: dict[str, float | None] = {
            branch: None for branch in BRANCHES
        }
        self._stt: dict[str, dict[str, str | None]] = {
            branch: {"state": "unknown", "detail": None} for branch in BRANCHES
        }
        self._transcript: dict[str, str] | None = None
        self._revision = 0
        self._notifier: Callable[[], None] | None = None

    @property
    def revision(self) -> int:
        return self._revision

    def set_notifier(self, notifier: Callable[[], None] | None) -> None:
        self._notifier = notifier

    def _changed(self) -> None:
        self._revision += 1
        if self._notifier is not None:
            self._notifier()

    def observe_level(self, branch: str, level: float) -> bool:
        """Record one valid level, rejecting rather than clamping bad values."""
        if branch not in BRANCHES:
            raise ValueError(f"unknown audio branch {branch!r}")
        try:
            value = float(level)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or not LEVEL_MIN <= value <= LEVEL_MAX:
            return False
        # JSON has no useful negative-zero distinction; publish canonical zero.
        if value == 0.0:
            value = 0.0
        now = self._clock()
        monotonic_now = self._monotonic_clock()
        previous = self._audio[branch]
        previous_monotonic = self._audio_observed_monotonic[branch]
        if (
            previous is not None
            and previous["level"] == value
            and previous_monotonic is not None
            and monotonic_now - previous_monotonic < AUDIO_OBSERVATION_REFRESH_SECONDS
        ):
            return True
        self._audio[branch] = {"level": value, "observed_at": now}
        self._audio_observed_monotonic[branch] = monotonic_now
        self._changed()
        return True

    def set_stt_state(self, branch: str, state: str, detail: str | None = None) -> None:
        if branch not in BRANCHES:
            raise ValueError(f"unknown STT branch {branch!r}")
        if state not in STT_STATES:
            raise ValueError(f"unknown STT state {state!r}")
        value = {"state": state, "detail": _cap(detail, DETAIL_LIMIT)}
        if value != self._stt[branch]:
            self._stt[branch] = value
            self._changed()

    def set_transcript(
        self,
        text: str,
        source: str | None = None,
        speaker_label: str | None = None,
    ) -> None:
        text = str(text).strip()
        if not text:
            return
        value = {"text": text[:TRANSCRIPT_LIMIT]}
        if source:
            value["source"] = str(source)
        if speaker_label:
            value["speaker_label"] = str(speaker_label)[:DETAIL_LIMIT]
        if value != self._transcript:
            self._transcript = value
            self._changed()

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        now = self._clock() if now is None else float(now)
        me_state = str(self._stt["me"]["state"])
        them_state = str(self._stt["them"]["state"])
        record: dict[str, Any] = {
            "schema": UI_TELEMETRY_SCHEMA_VERSION,
            "pid": self.pid,
            "start_epoch": self.start_epoch,
            "updated_at": now,
            "audio": {
                branch: (dict(value) if value is not None else None)
                for branch, value in self._audio.items()
            },
            "stt": {
                "me": dict(self._stt["me"]),
                "them": dict(self._stt["them"]),
                "aggregate": aggregate_stt_state(me_state, them_state),
            },
        }
        if self._transcript is not None:
            record["transcript"] = dict(self._transcript)
        if self.locked_category:
            record["locked_category"] = self.locked_category
        return record


class UITelemetryPublisher:
    """One bounded task that coalesces state changes and heartbeat writes."""

    def __init__(
        self,
        data_dir: Path,
        state: UITelemetryState,
        *,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.state = state
        self.path = self.data_dir / ".active" / UI_TELEMETRY_FILENAME
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._sleep = sleep
        self._wake = asyncio.Event()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._last_write_monotonic = float("-inf")
        self._written_revision = -1
        self._write_failed = False
        self._cleanup_failed = False

    def _notify(self) -> None:
        self._wake.set()

    def start(self) -> asyncio.Task[None]:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="ui_telemetry_publisher")
        return self._task

    def request_stop(self) -> asyncio.Task[None] | None:
        """Synchronously request shutdown and return the one publisher task."""
        self._stopping = True
        self._wake.set()
        return self._task

    async def run(self) -> None:
        self.state.set_notifier(self._notify)
        self._wake.set()
        try:
            while not self._stopping:
                monotonic_now = self._monotonic_clock()
                changed = self.state.revision != self._written_revision
                elapsed = monotonic_now - self._last_write_monotonic
                heartbeat_due = elapsed >= HEARTBEAT_SECONDS
                rate_ready = elapsed >= 1.0 / MAX_WRITE_HZ
                if rate_ready and (changed or heartbeat_due):
                    self.write_once(self._clock(), monotonic_now=monotonic_now)
                    continue

                wait_for = max(0.0, 1.0 / MAX_WRITE_HZ - elapsed)
                if not changed:
                    wait_for = max(0.0, HEARTBEAT_SECONDS - elapsed)
                self._wake.clear()
                wake_task = asyncio.create_task(self._wake.wait())
                sleep_task = asyncio.create_task(self._sleep(wait_for))
                try:
                    await asyncio.wait(
                        {wake_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    for task in (wake_task, sleep_task):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(wake_task, sleep_task, return_exceptions=True)
        finally:
            self.state.set_notifier(None)

    def write_once(
        self,
        now: float | None = None,
        *,
        monotonic_now: float | None = None,
    ) -> bool:
        """Attempt one atomic write, returning success without raising."""
        now = self._clock() if now is None else float(now)
        monotonic_now = (
            self._monotonic_clock() if monotonic_now is None else float(monotonic_now)
        )
        tmp: str | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                self.state.snapshot(now),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            fd, tmp = tempfile.mkstemp(
                dir=self.path.parent, prefix=".onoats-ui-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), UI_TELEMETRY_MODE)
                handle.write(payload)
            os.replace(tmp, self.path)
            tmp = None
            if self._write_failed:
                _LOG.warning("UI telemetry writes recovered")
                self._write_failed = False
            self._last_write_monotonic = monotonic_now
            self._written_revision = self.state.revision
            return True
        except Exception as exc:
            if not self._write_failed:
                _LOG.warning("Could not write UI telemetry: %s", exc)
                self._write_failed = True
            self._last_write_monotonic = monotonic_now
            return False
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    async def stop(self, timeout: float = 0.5) -> None:
        task = self.request_stop()
        if task is None or task is asyncio.current_task():
            return
        try:
            await asyncio.wait_for(task, timeout=timeout)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def cleanup(self) -> bool:
        try:
            removed = _cleanup_owned_snapshot(
                self.path, pid=self.state.pid, start_epoch=self.state.start_epoch
            )
            if not removed:
                return False
            if self._cleanup_failed:
                _LOG.warning("UI telemetry cleanup recovered")
                self._cleanup_failed = False
            return True
        except FileNotFoundError:
            return False
        except Exception as exc:
            if not self._cleanup_failed:
                _LOG.warning("Could not clean up UI telemetry: %s", exc)
                self._cleanup_failed = True
            return False


def read_snapshot(
    path: Path,
    *,
    pid: int,
    start_epoch: float,
    now: float | None = None,
) -> dict[str, Any] | None:
    """Read only a fresh schema-v1 snapshot belonging to the requested run."""
    try:
        record = json.loads(
            Path(path).read_text(encoding="utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON number: {value}")
            ),
        )
        current = time.time() if now is None else float(now)
        if (
            not isinstance(record, dict)
            or record.get("schema") != UI_TELEMETRY_SCHEMA_VERSION
            or record.get("pid") != pid
            or record.get("start_epoch") != start_epoch
            or not isinstance(record.get("updated_at"), (int, float))
            or not math.isfinite(float(record["updated_at"]))
            or current - float(record["updated_at"]) > SNAPSHOT_EXPIRY_SECONDS
            or float(record["updated_at"]) > current + SNAPSHOT_EXPIRY_SECONDS
        ):
            return None
        stt = record.get("stt")
        if not isinstance(stt, dict):
            return None
        branch_states: list[str] = []
        for branch in BRANCHES:
            value = stt.get(branch)
            if not isinstance(value, dict) or value.get("state") not in STT_STATES:
                return None
            detail = value.get("detail")
            if detail is not None and (
                not isinstance(detail, str) or len(detail) > DETAIL_LIMIT
            ):
                return None
            branch_states.append(value["state"])
        if stt.get("aggregate") != aggregate_stt_state(*branch_states):
            return None
        audio = record.get("audio")
        if not isinstance(audio, dict):
            return None
        for branch in BRANCHES:
            observation = audio.get(branch)
            if observation is None:
                continue
            if not isinstance(observation, dict):
                return None
            level = observation.get("level")
            observed_at = observation.get("observed_at")
            if (
                not isinstance(level, (int, float))
                or not math.isfinite(float(level))
                or not LEVEL_MIN <= float(level) <= LEVEL_MAX
                or not isinstance(observed_at, (int, float))
                or not math.isfinite(float(observed_at))
            ):
                return None
        return record
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def cleanup_snapshot(data_dir: Path, *, pid: int, start_epoch: float) -> bool:
    """Unlink the ephemeral file only when it still belongs to this run."""
    path = Path(data_dir) / ".active" / UI_TELEMETRY_FILENAME
    try:
        return _cleanup_owned_snapshot(path, pid=pid, start_epoch=start_epoch)
    except FileNotFoundError:
        return False
    except Exception as exc:
        _LOG.warning("Could not clean up UI telemetry: %s", exc)
        return False


def _cleanup_owned_snapshot(path: Path, *, pid: int, start_epoch: float) -> bool:
    """Quarantine and remove only the descriptor-owned ephemeral snapshot.

    Reads are non-blocking, no-follow, and bounded.  A private sibling
    directory gives the rename an absent destination, so publication-path
    replacement can only move either the opened inode or a foreign replacement
    into quarantine; it can never be overwritten.  A foreign moved entry is
    restored without overwriting a newer public-path replacement.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    quarantine_dir: Path | None = None
    quarantined: Path | None = None
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return False
        if not _descriptor_has_owner(
            descriptor, opened, pid=pid, start_epoch=start_epoch
        ):
            return False
        if not _rename_noreplace_available():
            return False

        # This is advisory only: the subsequent rename and quarantine inode
        # check, not this pathname observation, decide what may be removed.
        os.lstat(path)

        quarantine_dir = Path(
            tempfile.mkdtemp(prefix=".onoats-ui-cleanup-", dir=path.parent)
        )
        os.chmod(quarantine_dir, 0o700)
        quarantined = quarantine_dir / "snapshot"
        os.rename(path, quarantined)

        moved = os.lstat(quarantined)
        if (opened.st_dev, opened.st_ino) != (moved.st_dev, moved.st_ino):
            _restore_quarantined(quarantined, path)
            return False

        # Ownership may have changed while the public name was live.  Re-read
        # the same opened inode only after it has been isolated in quarantine.
        if not _descriptor_has_owner(
            descriptor, os.fstat(descriptor), pid=pid, start_epoch=start_epoch
        ):
            _restore_quarantined(quarantined, path)
            return False

        try:
            os.unlink(quarantined)
        except Exception:
            _restore_quarantined(quarantined, path)
            raise
        quarantined = None
        return True
    finally:
        os.close(descriptor)
        if quarantine_dir is not None and (
            quarantined is None or not os.path.lexists(quarantined)
        ):
            try:
                quarantine_dir.rmdir()
            except OSError:
                pass


def _descriptor_has_owner(
    descriptor: int,
    info: os.stat_result,
    *,
    pid: int,
    start_epoch: float,
) -> bool:
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SNAPSHOT_BYTES:
        return False
    os.lseek(descriptor, 0, os.SEEK_SET)
    payload = os.read(descriptor, MAX_SNAPSHOT_BYTES + 1)
    if len(payload) > MAX_SNAPSHOT_BYTES:
        return False
    record = json.loads(payload.decode("utf-8"))
    return (
        isinstance(record, dict)
        and record.get("pid") == pid
        and record.get("start_epoch") == start_epoch
    )


def _restore_quarantined(quarantined: Path, public_path: Path) -> None:
    """Restore a moved replacement without overwriting a newer publication."""
    try:
        _rename_noreplace(quarantined, public_path)
    except FileExistsError:
        # A newer public-path replacement wins; preserve the moved entry in its
        # private quarantine rather than deleting either replacement.
        return
    except (NotImplementedError, OSError):
        # Cleanup is fail-open if the platform cannot provide an atomic
        # no-replace rename.  Never emulate it with a racy exists+rename pair.
        return


def _rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename any entry type without replacing ``destination``.

    macOS exposes this contract as ``renameatx_np(RENAME_EXCL)``.  Keeping the
    operation in one syscall is essential: a pathname check followed by plain
    rename could overwrite a publisher that wins the intervening race.
    """
    renameatx_np = _rename_noreplace_symbol()
    if renameatx_np is None:
        raise NotImplementedError("atomic no-replace rename is unavailable")
    renameatx_np.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx_np.restype = ctypes.c_int
    result = renameatx_np(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(destination),
        _RENAME_EXCL,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    raise OSError(error, os.strerror(error), source)


def _rename_noreplace_symbol() -> Any | None:
    return getattr(ctypes.CDLL(None, use_errno=True), "renameatx_np", None)


def _rename_noreplace_available() -> bool:
    return _rename_noreplace_symbol() is not None
