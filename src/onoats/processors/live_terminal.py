"""Filtered live terminal output for the dual-input recorder."""

from __future__ import annotations

from datetime import datetime, timezone

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from onoats.frames import (
    resolve_frame_overlap,
    resolve_frame_source,
    resolve_frame_speakers,
)
from onoats.render import compose_speaker_label


def _display_label(source: str) -> str:
    return "Me" if source == "me" else "Them" if source == "them" else source


class LiveTerminalRenderer(FrameProcessor):
    """Print finalized speaker-labeled transcript lines to stdout."""

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            text = (frame.text or "").strip()
            finalized = getattr(frame, "finalized", True)
            source = resolve_frame_source(frame)
            if finalized and text and source:
                stamp = datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S")
                label = compose_speaker_label(
                    _display_label(source),
                    resolve_frame_speakers(frame),
                    bool(resolve_frame_overlap(frame)),
                )
                print(f"[{stamp}] {label}: {text}", flush=True)

        await self.push_frame(frame, direction)
