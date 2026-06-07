"""Filtered live terminal output for the experimental dual-input bot."""

from __future__ import annotations

from datetime import datetime, timezone

from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from bot.frames import resolve_frame_source


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
                print(f"[{stamp}] {_display_label(source)}: {text}", flush=True)

        await self.push_frame(frame, direction)
