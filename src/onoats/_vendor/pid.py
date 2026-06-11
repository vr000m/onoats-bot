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
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

PID_FILENAME = "onoats.pid"
PID_MARKER = "onoats-bot"


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


def _parse_pid_file(pid_path: Path) -> PidRecord | None:
    if not pid_path.exists():
        return None
    try:
        lines = pid_path.read_text(encoding="utf-8").strip().splitlines()
        if len(lines) < 2 or lines[1].strip() != PID_MARKER:
            logger.warning(f"PID file {pid_path} missing identity marker — ignoring")
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


def read_pid_file(pid_path: Path) -> int | None:
    """Read and validate a PID file. Returns the PID if valid, None otherwise."""
    rec = _parse_pid_file(pid_path)
    return None if rec is None else rec.pid


def read_pid_record(pid_path: Path) -> PidRecord | None:
    """Read and validate a PID file. Returns ``PidRecord`` or None.

    Use this when you need pid-recycling protection.
    """
    return _parse_pid_file(pid_path)


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


def resolve_flush_target(pid_path: Path) -> FlushTarget:
    """Resolve a pid that is safe to signal for ``onoats flush``.

    Performs, in order: marker-validated parse, cmdline-fingerprint presence,
    liveness (``os.kill(pid, 0)``), and a live-vs-stored cmdline comparison
    that defends against pid recycling — a crashed recorder can leave a stale
    pid file whose pid the kernel later reassigns to an unrelated program, and
    SIGUSR1's default disposition would terminate it.
    """
    rec = _parse_pid_file(pid_path)
    if rec is None:
        return FlushTarget(
            None, "no running recorder found (no valid pid file)", stale=False
        )
    if not rec.cmdline:
        return FlushTarget(
            None,
            "pid file has no cmdline fingerprint (legacy/incomplete) — refusing to signal",
            stale=False,
        )
    try:
        os.kill(rec.pid, 0)
    except ProcessLookupError:
        return FlushTarget(
            None, f"recorder pid {rec.pid} is not running (stale pid file)", stale=True
        )
    except PermissionError:
        # Alive but owned by another user — fall through to the cmdline check,
        # which still distinguishes the recorder from a recycled foreign pid.
        pass
    except OSError as exc:
        return FlushTarget(None, f"could not probe pid {rec.pid}: {exc}", stale=False)

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
        )
    if live != rec.cmdline:
        return FlushTarget(
            None,
            f"pid {rec.pid} identity mismatch (likely PID reuse): live process is "
            f"not the onoats recorder — refusing to signal",
            stale=True,
        )
    return FlushTarget(rec.pid)


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
