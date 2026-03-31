"""TranscriptBuffer — accumulates utterances and records silence gap hints.

Accumulates TranscriptionFrames with timestamps. Observes VADUserStoppedSpeakingFrame
to track inactivity and record silence_gap hints when the gap exceeds SEGMENT_HINT_THRESHOLD.
Appends entries incrementally to .active/session_*.jsonl for crash recovery.

All frames are passed downstream unchanged (transparent processor).
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# ---------------------------------------------------------------------------
# Config (from environment with sensible defaults)
# ---------------------------------------------------------------------------

# Gap (seconds) between VAD stop events that triggers a silence_gap hint.
# Default: 120s (2 minutes), as per the dev plan.
_SEGMENT_HINT_THRESHOLD_DEFAULT = 120

# Root data directory. Default: ~/koda-data
_KODA_DATA_DIR_DEFAULT = Path.home() / "koda-data"


def _data_dir() -> Path:
    raw = os.environ.get("KODA_DATA_DIR", "")
    return Path(raw).expanduser() if raw else _KODA_DATA_DIR_DEFAULT


def _segment_hint_threshold() -> float:
    try:
        return float(os.environ.get("SEGMENT_HINT_THRESHOLD", _SEGMENT_HINT_THRESHOLD_DEFAULT))
    except ValueError:
        return float(_SEGMENT_HINT_THRESHOLD_DEFAULT)


# ---------------------------------------------------------------------------
# Entry helpers
# ---------------------------------------------------------------------------


def _utterance_entry(text: str) -> dict:
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "type": "utterance",
        "text": text,
    }


def _silence_gap_entry(duration_seconds: float) -> dict:
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "type": "silence_gap",
        "duration_seconds": round(duration_seconds, 1),
    }


# ---------------------------------------------------------------------------
# TranscriptBuffer
# ---------------------------------------------------------------------------


class TranscriptBuffer(FrameProcessor):
    """Accumulates utterances + timestamps and marks silence gap hints.

    Transparent: all frames are passed downstream unchanged.

    Session JSONL layout (one entry per line)::

        {"time": "ISO8601", "type": "utterance", "text": "..."}
        {"time": "ISO8601", "type": "silence_gap", "duration_seconds": N}

    The session file is created on first write and lives in
    ``<KODA_DATA_DIR>/.active/session_<date>_<time>.jsonl``.

    Construction:
        buf = TranscriptBuffer()

    Methods:
        flush()          — return buffer contents as a list of dicts and reset
        flush_to_disk()  — write any in-memory state not yet on disk (graceful shutdown)
    """

    def __init__(
        self,
        segment_hint_threshold: Optional[float] = None,
        data_dir: Optional[Path] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self._threshold = segment_hint_threshold if segment_hint_threshold is not None else _segment_hint_threshold()
        self._data_dir = Path(data_dir) if data_dir is not None else _data_dir()

        # In-memory buffer of JSONL entry dicts
        self._buffer: list[dict] = []

        # Wall-clock time of last VADUserStoppedSpeakingFrame (or None)
        self._last_vad_stop: Optional[float] = None  # asyncio.get_event_loop().time()

        # Path of the current session JSONL file (created lazily on first write)
        self._session_file: Optional[Path] = None

        # Set of buffer indices that have been successfully persisted to disk.
        # flush_to_disk() writes only entries whose index is NOT in this set.
        self._persisted_indices: set[int] = set()

        # Lock to serialize flush/flush_to_disk with in-flight writes
        self._write_lock = asyncio.Lock()

        logger.debug(
            f"TranscriptBuffer initialized "
            f"(threshold={self._threshold}s, data_dir={self._data_dir})"
        )

    # ------------------------------------------------------------------
    # FrameProcessor interface
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            await self._handle_transcription(frame)
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            await self._handle_vad_started(frame)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._handle_vad_stopped(frame)

        await self.push_frame(frame, direction)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def flush(self) -> tuple[list[dict], Path | None]:
        """Return the full in-memory buffer and session path, then reset state.

        Before resetting, materializes any entries that failed to write to disk
        so the session file is complete for crash recovery. Acquires the write
        lock to ensure no in-flight disk writes are in progress.

        Returns:
            Tuple of (entries, session_path). session_path may be None if no
            entries were written to disk.
        """
        async with self._write_lock:
            # Materialize any entries that never made it to disk
            unpersisted = [
                (i, entry) for i, entry in enumerate(self._buffer)
                if i not in self._persisted_indices
            ]
            for i, entry in unpersisted:
                if await self._write_entry(entry):
                    self._persisted_indices.add(i)
                else:
                    logger.warning(
                        "TranscriptBuffer: entry materialization failed — "
                        "session file may be incomplete for crash recovery"
                    )

            contents = list(self._buffer)
            session_path = self._session_file
            self._buffer = []
            self._last_vad_stop = None
            self._session_file = None
            self._persisted_indices = set()
        logger.info(f"TranscriptBuffer flushed ({len(contents)} entries)")
        return contents, session_path

    async def flush_to_disk(self) -> None:
        """Write any pending in-memory state to .active/ for crash recovery.

        Called during graceful shutdown (SIGINT/SIGTERM). Acquires write lock
        to wait for any in-flight writes, then persists remaining entries.
        """
        async with self._write_lock:
            unpersisted = [
                (i, entry) for i, entry in enumerate(self._buffer)
                if i not in self._persisted_indices
            ]
            if unpersisted:
                logger.info(
                    f"TranscriptBuffer: flushing {len(unpersisted)} unpersisted entries to disk"
                )
                for i, entry in unpersisted:
                    await self._write_entry(entry)
            else:
                logger.debug("TranscriptBuffer.flush_to_disk: nothing to write")

    # ------------------------------------------------------------------
    # Internal handlers
    # ------------------------------------------------------------------

    async def _handle_transcription(self, frame: TranscriptionFrame) -> None:
        text = (frame.text or "").strip()
        if not text:
            return

        async with self._write_lock:
            entry = _utterance_entry(text)
            self._buffer.append(entry)
            idx = len(self._buffer) - 1
            if await self._write_entry(entry):
                self._persisted_indices.add(idx)
        logger.debug(f"TranscriptBuffer: utterance recorded ({len(text)} chars)")

    async def _handle_vad_started(self, frame: VADUserStartedSpeakingFrame) -> None:
        """On speech start, measure the actual silence gap since last stop."""
        now = asyncio.get_running_loop().time()

        if self._last_vad_stop is not None:
            silence_duration = now - self._last_vad_stop
            if silence_duration >= self._threshold:
                async with self._write_lock:
                    entry = _silence_gap_entry(silence_duration)
                    self._buffer.append(entry)
                    idx = len(self._buffer) - 1
                    if await self._write_entry(entry):
                        self._persisted_indices.add(idx)
                logger.info(
                    f"TranscriptBuffer: silence_gap recorded ({silence_duration:.1f}s >= {self._threshold}s threshold)"
                )

    async def _handle_vad_stopped(self, frame: VADUserStoppedSpeakingFrame) -> None:
        """On speech stop, record the timestamp for silence gap measurement."""
        self._last_vad_stop = asyncio.get_running_loop().time()

    # ------------------------------------------------------------------
    # Disk I/O
    # ------------------------------------------------------------------

    def _ensure_session_file(self) -> Path:
        """Create the session file path (and directory) on first use.

        Filenames include a UUID suffix to prevent collisions when two
        flushes happen within the same second.
        """
        if self._session_file is not None:
            return self._session_file

        import uuid

        active_dir = self._data_dir / ".active"

        # Reject symlinks in data_dir or .active before creating/writing
        for component in (self._data_dir, active_dir):
            if component.exists() and component.is_symlink():
                raise RuntimeError(f"Symlink detected in working storage path: {component}")

        active_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        now = datetime.now()
        short_id = uuid.uuid4().hex[:8]
        filename = f"session_{now.strftime('%Y%m%d_%H%M%S')}_{short_id}.jsonl"
        self._session_file = active_dir / filename

        # Verify session path resolves within .active/
        if not self._session_file.resolve().parent == active_dir.resolve():
            raise RuntimeError(
                f"Session file {self._session_file} escapes .active/ directory"
            )
        logger.info(f"TranscriptBuffer: session file created at {self._session_file}")
        return self._session_file

    async def _write_entry(self, entry: dict) -> bool:
        """Append a single JSONL entry to the session file. Returns True on success."""
        try:
            session_file = self._ensure_session_file()
            line = json.dumps(entry, ensure_ascii=False)
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _append_line, session_file, line)
            return True
        except Exception as exc:
            logger.error(f"TranscriptBuffer: failed to write entry to disk: {exc}")
            return False


def _append_line(path: Path, line: str) -> None:
    """Synchronous helper: append a JSONL line to *path* with 0o600 permissions."""
    import os as _os

    # Open with O_APPEND | O_CREAT and 0o600 — no race window for permissions
    fd = _os.open(str(path), _os.O_WRONLY | _os.O_APPEND | _os.O_CREAT, 0o600)
    try:
        _os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        _os.close(fd)
