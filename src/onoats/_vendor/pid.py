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

from dataclasses import dataclass
from pathlib import Path

from loguru import logger

PID_FILENAME = "onoats.pid"
PID_MARKER = "onoats-bot"


@dataclass(frozen=True)
class PidRecord:
    """Recorder identity tuple read from the pid file.

    ``start_epoch`` is ``0.0`` for legacy pid files written before the field
    existed; callers that need pid-recycling protection should treat ``0.0``
    as "unknown" and degrade gracefully (still pid-valid, but no generation
    check).
    """

    pid: int
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
        start_epoch = 0.0
        if len(lines) >= 4:
            try:
                start_epoch = float(lines[3].strip())
            except ValueError:
                start_epoch = 0.0
        return PidRecord(pid=pid, start_epoch=start_epoch)
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
