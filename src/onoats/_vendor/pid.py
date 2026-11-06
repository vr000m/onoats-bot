# vendored from koda shared/koda_pid.py
"""Read-side helpers for the recorder's pid file.

The recorder writes ``<data_dir>/.active/onoats.pid`` with a four-line
format::

    <pid>
    onoats-bot
    <ps cmdline>
    <start_epoch>     # optional; absent in legacy pid files

The marker on line 2 (``onoats-bot``) lets readers distinguish an onoats
recorder pid file from some unrelated process that happens to have left an
``onoats.pid`` lying around. ``read_pid_file`` validates the marker before
returning the pid; callers that skip this validation risk acting on a
foreign pid.

The optional ``start_epoch`` (4th line) is the recorder's process start
time. Combined with ``pid`` it forms a generation token that survives pid
recycling: a new recorder reusing the same pid will have a different
``start_epoch``.

This module only owns the *read* side — the write/remove primitives live in
``onoats/runtime.py`` because they are recorder-process-only.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

PID_FILENAME = "onoats.pid"
PID_MARKER = "onoats-bot"
PID_MAX_BYTES = 65536


@dataclass(frozen=True)
class PidRecord:
    """Recorder identity tuple read from the pid file.

    ``cmdline`` is the ``ps -p <pid> -o command=`` fingerprint the recorder
    captured of *itself* at startup (3rd line); it is the empty string for
    legacy pid files written before the field existed. Callers that verify
    process identity (e.g. ``onoats flush`` before signalling) must refuse to
    act when it is empty — without a stored fingerprint there is nothing to
    compare the live process against.

    ``start_epoch`` is ``0.0`` for legacy pid files written before the field
    existed; callers that need pid-recycling protection should treat ``0.0``
    as "unknown" and degrade gracefully (still pid-valid, but no generation
    check).
    """

    pid: int
    cmdline: str = ""
    start_epoch: float = 0.0


def _parse_pid_text(text: str, *, source: Path) -> PidRecord | None:
    try:
        lines = text.strip().splitlines()
        if len(lines) < 2 or lines[1].strip() != PID_MARKER:
            logger.warning(f"PID file {source} missing identity marker — ignoring")
            return None
        pid = int(lines[0].strip())
        # Line 3 (index 2) is the cmdline fingerprint. ``.strip()`` on the
        # whole file content already trimmed a trailing newline; an absent
        # 3rd line (legacy 2-line file) leaves ``cmdline`` empty.
        cmdline = lines[2].strip() if len(lines) >= 3 else ""
        start_epoch = 0.0
        if len(lines) >= 4:
            try:
                start_epoch = float(lines[3].strip())
            except ValueError:
                start_epoch = 0.0
        return PidRecord(pid=pid, cmdline=cmdline, start_epoch=start_epoch)
    except (ValueError, OSError):
        return None


def _parse_pid_file(pid_path: Path) -> PidRecord | None:
    """Read one PID authority record through a bounded, no-follow descriptor.

    PID records authorize lifecycle control, so pathname convenience APIs are
    inappropriate here: they follow symlinks, block on FIFOs, and have no byte
    bound.  The opened inode must be a safe, caller-owned regular file and must
    still occupy the public pathname after the bounded read.
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(pid_path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != getattr(os, "geteuid", lambda: opened.st_uid)()
            or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or opened.st_size > PID_MAX_BYTES
        ):
            return None
        data = os.read(descriptor, PID_MAX_BYTES + 1)
        if len(data) > PID_MAX_BYTES:
            return None
        current = os.lstat(pid_path)
        if not stat.S_ISREG(current.st_mode) or (
            opened.st_dev,
            opened.st_ino,
        ) != (current.st_dev, current.st_ino):
            return None
        return _parse_pid_text(data.decode("utf-8"), source=pid_path)
    except (UnicodeError, OSError):
        return None
    finally:
        os.close(descriptor)


def read_pid_file(pid_path: Path) -> int | None:
    """Read and validate a PID file. Returns the PID if valid, None otherwise."""
    rec = _parse_pid_file(pid_path)
    return None if rec is None else rec.pid


def read_pid_record(pid_path: Path) -> PidRecord | None:
    """Read and validate a PID file. Returns ``PidRecord`` or None.

    Use this when you need pid-recycling protection.
    """
    return _parse_pid_file(pid_path)


def compare_and_unlink_pid_record(pid_path: Path, expected: PidRecord) -> bool:
    """Unlink only when the complete current record still equals ``expected``.

    This is the sole stale/teardown cleanup primitive.  Comparing only the PID
    would let an old generation delete a same-PID successor's authoritative
    record.  Unreadable, malformed, absent, foreign, or replaced records are
    preserved (and report ``False``).
    """
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(pid_path, flags)
    except OSError:
        return False
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != getattr(os, "geteuid", lambda: opened.st_uid)()
            or opened.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or opened.st_size > PID_MAX_BYTES
        ):
            return False
        data = os.read(descriptor, PID_MAX_BYTES + 1)
        if len(data) > PID_MAX_BYTES:
            return False
        try:
            record = _parse_pid_text(data.decode("utf-8"), source=pid_path)
        except UnicodeError:
            return False
        if record != expected:
            return False
        opened_identity = (opened.st_dev, opened.st_ino)
    finally:
        os.close(descriptor)

    try:
        quarantine_dir = Path(
            tempfile.mkdtemp(prefix=".onoats-pid-quarantine-", dir=pid_path.parent)
        )
    except OSError:
        return False
    quarantine_path = quarantine_dir / pid_path.name
    try:
        os.rename(pid_path, quarantine_path)
    except OSError:
        try:
            quarantine_dir.rmdir()
        except OSError:
            pass
        return False

    try:
        moved = quarantine_path.lstat()
        if (moved.st_dev, moved.st_ino) != opened_identity:
            # A successor won the open-to-rename race. Restore it atomically
            # without replacing any newer entry that reached the public name.
            try:
                os.link(quarantine_path, pid_path, follow_symlinks=False)
            except OSError:
                return False
            # Keep the quarantine link as preservation authority: another
            # actor may replace the just-restored public name immediately.
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


# ---------------------------------------------------------------------------
# Flush-target verification (identity check before signalling)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlushTarget:
    """Outcome of resolving a flushable recorder pid.

    ``pid`` is set (and ``reason`` empty) only when the live process at that
    pid has been positively identified as the onoats recorder and is safe to
    signal. Otherwise ``pid`` is ``None`` and ``reason`` explains why; ``stale``
    is ``True`` when the caller should remove the now-untrustworthy pid file.
    """

    pid: int | None
    reason: str = ""
    stale: bool = False
    record: PidRecord | None = None


def _live_ps_cmdline(pid: int) -> str | None:
    """Return ``ps -p <pid> -o command=`` of the *live* process, or ``None``.

    ``None`` means the process is gone (or ps could not be run). This must
    produce a byte-identical string to ``runtime._own_ps_cmdline`` (same
    ``ps -p <pid> -o command=`` invocation) so the stored fingerprint and the
    live readback compare equal for a genuine recorder.
    """
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout.strip()
    return out or None


def resolve_flush_target(
    pid_path: Path,
    *,
    expected_pid: int | None = None,
    expected_start_epoch: float | None = None,
) -> FlushTarget:
    """Resolve the complete recorder generation used by the control client.

    Performs, in order: marker-validated parse, cmdline-fingerprint presence,
    liveness (``os.kill(pid, 0)``), and a live-vs-stored cmdline comparison
    that defends against pid recycling — a crashed recorder can leave a stale
    pid file whose pid the kernel later reassigns to an unrelated program.
    """
    rec = _parse_pid_file(pid_path)
    if rec is None:
        return FlushTarget(
            None, "no running recorder found (no valid pid file)", stale=False
        )
    if expected_pid is not None and (
        rec.pid != expected_pid or rec.start_epoch != expected_start_epoch
    ):
        return FlushTarget(
            None,
            "requested recorder identity is no longer active",
            stale=False,
            record=rec,
        )
    if not rec.cmdline:
        return FlushTarget(
            None,
            "pid file has no cmdline fingerprint (legacy/incomplete) — refusing to signal",
            stale=False,
            record=rec,
        )
    try:
        os.kill(rec.pid, 0)
    except ProcessLookupError:
        return FlushTarget(
            None,
            f"recorder pid {rec.pid} is not running (stale pid file)",
            stale=True,
            record=rec,
        )
    except PermissionError:
        # Alive but owned by another user — fall through to the cmdline check,
        # which still distinguishes the recorder from a recycled foreign pid.
        pass
    except OSError as exc:
        return FlushTarget(
            None, f"could not probe pid {rec.pid}: {exc}", stale=False, record=rec
        )

    live = _live_ps_cmdline(rec.pid)
    if live is None:
        # Liveness was positively established above (``os.kill(pid, 0)`` did not
        # raise ``ProcessLookupError``), so a missing readback here is NOT proof
        # the recorder is gone — it means the identity probe itself failed:
        # ``ps`` unavailable, non-zero, or timed out (or, rarely, the process
        # exited in the window since the liveness check). This is indeterminate.
        # Refuse to signal, but do NOT mark stale: deleting a live recorder's
        # pid file on a transient ``ps`` hiccup would orphan it from ``status``
        # and future ``flush``, and could let a later startup miss the active
        # instance. A genuinely-dead pid is caught by the ``os.kill`` check on
        # the next invocation, which re-probes and unlinks then.
        return FlushTarget(
            None,
            f"could not verify recorder identity for pid {rec.pid} "
            f"(ps probe failed) — refusing to signal",
            stale=False,
            record=rec,
        )
    if live != rec.cmdline:
        return FlushTarget(
            None,
            f"pid {rec.pid} identity mismatch (likely PID reuse): live process is "
            f"not the onoats recorder — refusing to signal",
            stale=True,
            record=rec,
        )
    return FlushTarget(rec.pid, record=rec)


def fingerprint_matches(rec: PidRecord) -> bool:
    """Pid-recycling check for *read-only* liveness verdicts (status / menu).

    ``os.kill(pid, 0)`` alone reports a *recycled* pid as alive — a crashed
    recorder's stale pid file plus kernel pid reuse would render some unrelated
    program as RUNNING. Callers AND this check with their liveness probe: when
    the record carries a cmdline fingerprint, the live process must match it
    (same comparison ``resolve_flush_target`` performs before signalling).

    Read-only verdicts err on the side of "alive": legacy fingerprint-less
    records and an indeterminate ``ps`` probe (unavailable / transient failure)
    return True rather than flapping a live recorder to stopped — only a
    *positive* mismatch returns False. (``resolve_flush_target`` makes the
    opposite call for the same situations because it gates a signal, where
    acting on uncertainty is the danger.)
    """
    if not rec.cmdline:
        return True
    live = _live_ps_cmdline(rec.pid)
    if live is None:
        return True
    return live == rec.cmdline
