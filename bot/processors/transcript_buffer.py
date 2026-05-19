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

from bot.frames import resolve_frame_source

# Reserved source key used in dual mode when a VAD frame arrives without
# a branch tag. Treating it as a distinct source keeps it inside the
# gating set so ``_last_vad_stop`` isn't overwritten while a tagged
# branch is still mid-utterance.
_UNTAGGED_SOURCE = "_untagged"

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


def _utterance_entry(
    text: str,
    *,
    source: str | None = None,
    source_order: int | None = None,
    branch_sequence: int | None = None,
    timestamp: str | None = None,
) -> dict:
    # Prefer the STT-supplied timestamp (ISO8601) when available; branches
    # can finalize at different speeds, so wall-clock arrival time would
    # chronologically invert interleaved utterances before segmentation.
    ts = timestamp if timestamp else datetime.now(timezone.utc).isoformat()
    entry = {
        "time": ts,
        "type": "utterance",
        "text": text,
    }
    if source:
        entry["source"] = source
    if isinstance(source_order, int):
        entry["source_order"] = source_order
    if isinstance(branch_sequence, int):
        entry["branch_sequence"] = branch_sequence
    return entry


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
        {"time": "ISO8601", "type": "utterance", "text": "...", "source": "me"}
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
        *,
        track_vad_gaps: bool = True,
        use_frame_source: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)

        self._threshold = (
            segment_hint_threshold
            if segment_hint_threshold is not None
            else _segment_hint_threshold()
        )
        self._data_dir = Path(data_dir) if data_dir is not None else _data_dir()
        self._track_vad_gaps = track_vad_gaps
        self._use_frame_source = use_frame_source

        # In-memory buffer of JSONL entry dicts
        self._buffer: list[dict] = []

        # Wall-clock time of last VADUserStoppedSpeakingFrame (or None).
        # In source-aware mode, updated only when *all* known branches
        # have gone idle — otherwise a brief pause on one branch while
        # the other is mid-utterance would be mis-recorded as a gap.
        self._last_vad_stop: Optional[float] = None  # asyncio.get_event_loop().time()

        # Source-aware cross-branch tracking (dual-input only). Keeps the
        # set of branches currently speaking so ``_last_vad_stop`` only
        # advances when every known branch is idle.
        self._speaking_sources: set[str] = set()

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
        elif self._track_vad_gaps and isinstance(frame, VADUserStartedSpeakingFrame):
            await self._handle_vad_started(frame)
        elif self._track_vad_gaps and isinstance(frame, VADUserStoppedSpeakingFrame):
            await self._handle_vad_stopped(frame)

        await self.push_frame(frame, direction)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def flush(
        self,
        *,
        next_session_file: Optional[Path] = None,
    ) -> tuple[list[dict], Path | None]:
        """Return the full in-memory buffer and session path, then reset state.

        Before resetting, materializes any entries that failed to write to disk
        so the session file is complete for crash recovery. Acquires the write
        lock to ensure no in-flight disk writes are in progress.

        Args:
            next_session_file: If provided, ``_session_file`` is atomically
                swapped to this path under ``_write_lock`` (instead of reset
                to ``None``). The continuation-flush path uses this to close
                the race where an utterance arriving between flush and the
                caller's session-file reassignment would otherwise land in a
                stray ``.active/`` file. The caller must pre-mint this path
                (see :func:`shared.session_queue.new_active_session`).
                Default ``None`` is the terminal-flush behaviour.

        Returns:
            Tuple of (entries, session_path). session_path may be None if no
            entries were written to disk.
        """
        async with self._write_lock:
            # Materialize any entries that never made it to disk
            unpersisted = [
                (i, entry)
                for i, entry in enumerate(self._buffer)
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
            # Atomic swap under the lock — utterances arriving immediately
            # after we release the lock land in ``next_session_file`` (or
            # trigger lazy creation if None).
            self._session_file = next_session_file
            self._persisted_indices = set()
            self._speaking_sources = set()
        logger.info(f"TranscriptBuffer flushed ({len(contents)} entries)")
        return contents, session_path

    async def flush_to_disk(self) -> None:
        """Write any pending in-memory state to .active/ for crash recovery.

        Called during graceful shutdown (SIGINT/SIGTERM). Acquires write lock
        to wait for any in-flight writes, then persists remaining entries.
        """
        async with self._write_lock:
            unpersisted = [
                (i, entry)
                for i, entry in enumerate(self._buffer)
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

        source = None
        source_order = None
        branch_sequence = None
        if self._use_frame_source:
            source = resolve_frame_source(frame)
            raw_source_order = getattr(frame, "koda_source_order", None)
            if isinstance(raw_source_order, int):
                source_order = raw_source_order
            raw_branch_sequence = getattr(frame, "koda_branch_sequence", None)
            if isinstance(raw_branch_sequence, int):
                branch_sequence = raw_branch_sequence

        frame_timestamp = getattr(frame, "timestamp", None)
        frame_timestamp = str(frame_timestamp) if frame_timestamp else None

        async with self._write_lock:
            entry = _utterance_entry(
                text,
                source=source,
                source_order=source_order,
                branch_sequence=branch_sequence,
                timestamp=frame_timestamp,
            )
            self._buffer.append(entry)
            idx = len(self._buffer) - 1
            if await self._write_entry(entry):
                self._persisted_indices.add(idx)
        logger.debug(f"TranscriptBuffer: utterance recorded ({len(text)} chars)")

    async def _handle_vad_started(self, frame: VADUserStartedSpeakingFrame) -> None:
        """On speech start, measure the actual silence gap since last stop."""
        now = asyncio.get_running_loop().time()
        source = self._frame_source(frame)

        # Source-aware path (dual-input): only measure gap when no branch
        # is currently speaking. Otherwise this branch's start overlaps
        # with another that's still mid-utterance — no silence at all.
        if source is not None:
            all_idle = not self._speaking_sources
            self._speaking_sources.add(source)
            if not all_idle or self._last_vad_stop is None:
                return
        elif self._last_vad_stop is None:
            return

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
        now = asyncio.get_running_loop().time()
        source = self._frame_source(frame)

        if source is not None:
            self._speaking_sources.discard(source)
            if self._speaking_sources:
                return

        self._last_vad_stop = now

    def _frame_source(self, frame: Frame) -> str | None:
        if not self._use_frame_source:
            return None
        # In dual mode, every VAD frame must participate in the gating
        # set — untagged ones get a reserved key so they can't bypass
        # ``_speaking_sources`` and spuriously advance ``_last_vad_stop``
        # while a tagged branch is still mid-utterance.
        return resolve_frame_source(frame) or _UNTAGGED_SOURCE

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
            raise RuntimeError(f"Session file {self._session_file} escapes .active/ directory")
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
