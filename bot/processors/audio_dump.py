"""Raw PCM dump for offline SmartTurn A/B replay.

Gated by ``KODA_AUDIO_DUMP=1``. Writes append-only 16-bit signed
little-endian PCM, one file per branch per call, opened with
``O_NOFOLLOW`` and mode ``0o600`` so the directory cannot be tricked
into appending through a symlink and the audio is not world-readable.
A sidecar JSON stamps the format so the offline replay tool needs no
out-of-band knowledge.

Lossless is required for the A/B premise — feeding decoded AAC/Opus
back through the analyser would attribute encoder artifacts to algorithm
differences. The handle is opened with ``buffering=0`` so a hard kill
preserves every byte already passed to ``write(2)``.

Per the spike plan in
``docs/dev_plans/20260420-design-whisper-websocket-server.md`` this
infrastructure is intended to outlive shadow-mode. Once the offline
replay is in place we can flip the SmartTurn commit gate with evidence
rather than guesswork.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from loguru import logger

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

# Defaults sized for two-call validation, not unattended capture. Operator
# can raise via env if a longer corpus is needed.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB / branch / call
MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024  # refuse to start dumping below 5 GB free


def audio_dump_enabled() -> bool:
    """Whether the raw PCM dump processor should be wired into the pipeline."""
    return os.environ.get("KODA_AUDIO_DUMP", "").lower() in {"1", "true", "yes"}


def resolve_dump_dir() -> Path:
    """Resolve the dump root, creating it with restrictive perms."""
    from shared.store import shadow_data_dir

    raw = shadow_data_dir() / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    try:
        raw.chmod(0o700)
    except OSError:
        pass
    return raw


def _max_bytes() -> int:
    raw = os.environ.get("KODA_AUDIO_DUMP_MAX_BYTES", "").strip()
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_BYTES


def _open_append_secure(path: Path):
    """Open ``path`` for binary append with O_NOFOLLOW + 0o600 + unbuffered.

    Refuses to follow a symlink at the path. Mode 0o600 only matters on
    creation; existing files keep their mode (defence-in-depth — the
    parent directory is also chmod 0o700).
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    fd = os.open(str(path), flags, 0o600)
    return os.fdopen(fd, "ab", buffering=0)


class RawAudioDumpProcessor(FrameProcessor):
    """Append every InputAudioRawFrame from this branch to a raw PCM file.

    One instance per branch. Writes to ``<dump_dir>/<call_id>_<source>.pcm``.
    Opens with ``O_NOFOLLOW``, mode ``0o600``, and ``buffering=0`` so
    bytes are durable on hard crash and the path cannot be redirected
    via a symlink. Stops writing past ``KODA_AUDIO_DUMP_MAX_BYTES``
    (default 2 GiB / branch) and refuses to start when the disk has
    less than 5 GiB free.
    """

    def __init__(
        self,
        *,
        source: str,
        call_id: str,
        dump_dir: Path,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._source = source
        self._call_id = call_id
        dump_dir.mkdir(parents=True, exist_ok=True)
        try:
            dump_dir.chmod(0o700)
        except OSError:
            pass
        self._pcm_path = dump_dir / f"{call_id}_{source}.pcm"
        self._meta_path = dump_dir / f"{call_id}_meta.json"
        self._sample_rate: int | None = None
        self._dump_dir = dump_dir
        self._bytes_written = 0
        self._max_bytes = _max_bytes()
        self._capped = False
        self._fh = None

        free = shutil.disk_usage(dump_dir).free
        if free < MIN_FREE_BYTES:
            logger.warning(
                f"RawAudioDump[{source}]: disabled — only {free // (1024**3)} GiB free "
                f"on {dump_dir} (need {MIN_FREE_BYTES // (1024**3)} GiB)"
            )
            return

        try:
            self._fh = _open_append_secure(self._pcm_path)
        except OSError as exc:
            logger.warning(f"RawAudioDump[{source}]: cannot open {self._pcm_path}: {exc}")
            return

        logger.info(
            f"RawAudioDump[{source}]: writing to {self._pcm_path} "
            f"(cap {self._max_bytes // (1024**2)} MiB)"
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        try:
            if isinstance(frame, StartFrame):
                self._sample_rate = getattr(frame, "audio_in_sample_rate", None)
                self._write_meta()
            elif isinstance(frame, InputAudioRawFrame):
                if self._fh is None or self._capped:
                    pass
                else:
                    n = self._fh.write(frame.audio)
                    self._bytes_written += n
                    if self._bytes_written >= self._max_bytes:
                        self._capped = True
                        logger.warning(
                            f"RawAudioDump[{self._source}]: hit cap "
                            f"({self._bytes_written} bytes); stopping further writes"
                        )
                        self._close()
            elif isinstance(frame, (EndFrame, CancelFrame)):
                self._close()
        except Exception as exc:
            logger.warning(f"RawAudioDump[{self._source}]: write error: {exc}")

        await self.push_frame(frame, direction)

    def _close(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.flush()
            self._fh.close()
        except Exception as exc:
            logger.warning(f"RawAudioDump[{self._source}]: close error: {exc}")
        finally:
            self._fh = None
        secs = self._bytes_written / max(self._sample_rate or 16000, 1) / 2
        logger.info(
            f"RawAudioDump[{self._source}]: closed — {self._bytes_written} bytes "
            f"({secs:.1f}s) -> {self._pcm_path}"
        )

    def _write_meta(self) -> None:
        meta = {
            "call_id": self._call_id,
            "sample_rate": self._sample_rate,
            "channels": 1,
            "sample_format": "s16le",
            "branches": ["me", "them"],
            "max_bytes": self._max_bytes,
            "note": "append-only PCM; read bytes until EOF",
        }
        try:
            payload = json.dumps(meta, indent=2).encode("utf-8")
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow:
                flags |= nofollow
            fd = os.open(str(self._meta_path), flags, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
        except OSError as exc:
            logger.warning(f"RawAudioDump[{self._source}]: meta write failed: {exc}")
