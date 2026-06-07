# vendored from koda shared/session_queue.py (filesystem helpers only — no SQLite)
"""Session-file work queue — the *filesystem* side only.

The upstream ``shared/session_queue.py`` owns both the filesystem rotation AND
the SQLite ``processing_jobs`` FSM (claim / mark_done / mark_failed /
reclaim_stale / enqueue_job …). onoats is a file-only recorder: it vendors
ONLY the directory helpers + the ``.active/ → pending/`` rotation. All
``processing_jobs`` DB code is dropped — the queue contract is files-on-disk;
a consumer (a queue worker) owns its own observability DB and back-fills a
rowless ``pending/`` file via ``claim()``.

Directory layout (all under the resolved data dir)::

    .active/   the recorder's live recording file (NOT under sessions/)
    sessions/pending/   session files waiting for a consumer to claim
    sessions/claimed/   (consumer-owned; onoats never writes here)
    sessions/done/      (converter output stage)
    sessions/failed/    (converter failure stage)

Ordering invariant — the rotation ``rename(2)`` is the only cross-process
concurrency primitive. ``rename(2)`` atomicity holds because all directories
share one filesystem.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger

from onoats._vendor.store import onoats_data_dir

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ACTIVE_DIR = ".active"
SESSIONS_DIR = "sessions"
QUEUE_SUBDIRS = ("pending", "claimed", "done", "failed")


# ---------------------------------------------------------------------------
# Path resolution + directory creation
# ---------------------------------------------------------------------------


def active_dir(data_dir: Path | None = None) -> Path:
    """``<data_dir>/.active/`` — the recorder's live recording directory."""
    base = data_dir if data_dir is not None else onoats_data_dir()
    return base / ACTIVE_DIR


def sessions_dir(data_dir: Path | None = None) -> Path:
    """``<data_dir>/sessions/`` — root of the work queue."""
    base = data_dir if data_dir is not None else onoats_data_dir()
    return base / SESSIONS_DIR


def queue_dir(name: str, data_dir: Path | None = None) -> Path:
    """Return one of the ``pending`` / ``claimed`` / ``done`` / ``failed``
    directories under ``sessions/``."""
    if name not in QUEUE_SUBDIRS:
        raise ValueError(
            f"Unknown queue directory {name!r}; expected one of {QUEUE_SUBDIRS}"
        )
    return sessions_dir(data_dir) / name


def ensure_queue_dirs(data_dir: Path | None = None) -> None:
    """Create all four queue directories idempotently (``mode=0o700``).

    The directories must exist *before* the first rotation — do not rely on
    lazy creation inside the rotation helpers.
    """
    for name in QUEUE_SUBDIRS:
        queue_dir(name, data_dir).mkdir(parents=True, exist_ok=True, mode=0o700)


def _session_id(path: Path) -> str:
    """Derive the session id (the file stem) from a session-file path."""
    return path.stem


# ---------------------------------------------------------------------------
# Fresh .active/ session creation (continuation flush)
# ---------------------------------------------------------------------------


def _new_active_session(data_dir: Path | None = None) -> tuple[Path, str]:
    """Mint a fresh, empty ``.active/`` session file and return its path + id.

    The file is created empty (0o600) so the new session id is reserved on
    disk before any utterance is appended.
    """
    adir = active_dir(data_dir)
    adir.mkdir(parents=True, exist_ok=True, mode=0o700)
    now = datetime.now()
    short_id = uuid.uuid4().hex[:8]
    session_id = f"session_{now.strftime('%Y%m%d_%H%M%S')}_{short_id}"
    path = adir / f"{session_id}.jsonl"
    # O_EXCL — a uuid collision within the same second must surface, not
    # silently reuse another session's file.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    return path, session_id


def new_active_session(data_dir: Path | None = None) -> tuple[Path, str]:
    """Public alias of :func:`_new_active_session` for pre-minting a fresh
    ``.active/`` session before a continuation flush.

    The continuation flush swaps the transcript buffer's session file
    atomically under the buffer's write lock; that requires knowing the next
    path *before* the flush. Callers pre-mint the path with this helper and
    pass it into ``TranscriptBuffer.flush(next_session_file=...)``.
    """
    return _new_active_session(data_dir)


# ---------------------------------------------------------------------------
# Rotation: .active/ -> pending/
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RotationResult:
    """Result of :func:`rotate_to_pending`.

    ``pending_path`` / ``session_id`` describe the file that was rotated into
    ``pending/``. When ``continue_session=True`` the rotation also opens a
    fresh ``.active/`` session — ``next_active_path`` / ``next_session_id``
    then carry that new file; both are ``None`` for a terminal flush.
    """

    pending_path: Path
    session_id: str
    next_active_path: Path | None
    next_session_id: str | None


def rotate_active_to_pending(
    active_path: Path,
    *,
    data_dir: Path | None = None,
) -> str:
    """Rotate a finalised ``.active/`` file into ``pending/`` — rename only.

    Use this when the caller has already pre-minted (or does not need) the
    fresh ``.active/`` session via :func:`new_active_session`.

    Returns the session id (file stem). Raises ``FileNotFoundError`` if
    ``active_path`` does not exist; ``OSError`` on other filesystem errors.
    """
    active_path = Path(active_path)
    session_id = _session_id(active_path)
    pending_path = queue_dir("pending", data_dir) / f"{session_id}.jsonl"
    os.rename(active_path, pending_path)
    logger.info(
        f"session_queue: rotated {active_path.name} → pending/{pending_path.name}"
    )
    return session_id


def rotate_to_pending(
    active_path: Path,
    *,
    continue_session: bool,
    data_dir: Path | None = None,
) -> RotationResult:
    """Rotate a finalised ``.active/`` session file into ``pending/``.

    This performs ONLY the filesystem rotation (rename first); onoats writes
    no DB row. A rowless ``pending/`` file is the normal state — a downstream
    consumer back-fills its own bookkeeping on claim.

    Terminal flush (``continue_session=False``):
        rename ``.active/X.jsonl`` → ``pending/X.jsonl`` and stop.

    Continuation flush (``continue_session=True``):
        rename into ``pending/`` FIRST, then open a fresh ``.active/`` session
        so the ongoing recording has somewhere to land. Crash-safety rule: the
        rotation happens first; the fresh open second. A crash in between
        leaves an empty ``.active/`` and the rotated file already safely in
        ``pending/`` — nothing is double-processed.

    Raises:
        FileNotFoundError: If ``active_path`` does not exist.
        OSError: On other filesystem errors during the rename.
    """
    active_path = Path(active_path)
    session_id = _session_id(active_path)
    pending_path = queue_dir("pending", data_dir) / f"{session_id}.jsonl"

    os.rename(active_path, pending_path)
    logger.info(
        f"session_queue: rotated {active_path.name} → pending/{pending_path.name}"
    )

    next_active_path: Path | None = None
    next_session_id: str | None = None
    if continue_session:
        next_active_path, next_session_id = _new_active_session(data_dir)
        logger.info(
            f"session_queue: opened fresh active session {next_active_path.name}"
        )

    return RotationResult(
        pending_path=pending_path,
        session_id=session_id,
        next_active_path=next_active_path,
        next_session_id=next_session_id,
    )
