"""Raw PCM dump for offline SmartTurn A/B replay.

Gated by ``KODA_AUDIO_DUMP=1``. Writes append-only 16-bit signed
little-endian PCM, one file per branch per call. A sidecar JSON stamps
the format so the offline replay tool needs no out-of-band knowledge.

Lossless is required for the A/B premise — feeding decoded AAC/Opus
back through the analyser would attribute encoder artifacts to algorithm
differences. WAV would also work but isn't crash-safe (header size
field). Raw PCM + sidecar is append-only and the file is valid up to
its last successful write even if the bot is killed.

Per the spike plan in
``docs/dev_plans/20260420-design-whisper-websocket-server.md`` this
infrastructure is intended to outlive shadow-mode. Once the offline
replay is in place we can flip the SmartTurn commit gate with evidence
rather than guesswork.
"""

from __future__ import annotations

import json
import os
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


def audio_dump_enabled() -> bool:
    """Whether the raw PCM dump processor should be wired into the pipeline."""
    return os.environ.get("KODA_AUDIO_DUMP", "").lower() in {"1", "true", "yes"}


def resolve_dump_dir() -> Path:
    """Resolve the dump root, honouring KODA_DATA_DIR like the rest of Koda."""
    from shared.store import DEFAULT_DATA_DIR

    base = Path(os.environ.get("KODA_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    return base / "shadow" / "raw"


class RawAudioDumpProcessor(FrameProcessor):
    """Append every InputAudioRawFrame from this branch to a raw PCM file.

    One instance per branch. Writes to ``<dump_dir>/<call_id>_<source>.pcm``.
    The sample rate / format is recorded in ``<call_id>_meta.json`` next to
    the PCM. The replay tool reads bytes until EOF and the meta for format.
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
        self._pcm_path = dump_dir / f"{call_id}_{source}.pcm"
        self._meta_path = dump_dir / f"{call_id}_meta.json"
        # Append-mode so partial writes from a previous crash are preserved
        # (defensive — call_id is timestamped so collisions shouldn't happen).
        self._fh = open(self._pcm_path, "ab")
        self._sample_rate: int | None = None
        self._dump_dir = dump_dir
        self._bytes_written = 0
        logger.info(f"RawAudioDump[{source}]: writing to {self._pcm_path}")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        try:
            if isinstance(frame, StartFrame):
                self._sample_rate = getattr(frame, "audio_in_sample_rate", None)
                self._write_meta()
            elif isinstance(frame, InputAudioRawFrame):
                n = self._fh.write(frame.audio)
                self._bytes_written += n
            elif isinstance(frame, (EndFrame, CancelFrame)):
                # Best-effort flush so the file is durable on clean shutdown.
                # Append-mode means crashes still leave a usable prefix.
                self._fh.flush()
                logger.info(
                    f"RawAudioDump[{self._source}]: {self._bytes_written} bytes "
                    f"({self._bytes_written / max(self._sample_rate or 16000, 1) / 2:.1f}s) -> {self._pcm_path}"
                )
        except Exception as exc:
            logger.warning(f"RawAudioDump[{self._source}]: write error: {exc}")

        await self.push_frame(frame, direction)

    def _write_meta(self) -> None:
        # Both branches write the same meta — last writer wins, the content
        # is identical (sample rate is shared across the pipeline).
        meta = {
            "call_id": self._call_id,
            "sample_rate": self._sample_rate,
            "channels": 1,
            "sample_format": "s16le",
            "branches": ["me", "them"],
            "note": "append-only PCM; read bytes until EOF",
        }
        try:
            self._meta_path.write_text(json.dumps(meta, indent=2))
        except Exception as exc:
            logger.warning(f"RawAudioDump[{self._source}]: meta write failed: {exc}")
