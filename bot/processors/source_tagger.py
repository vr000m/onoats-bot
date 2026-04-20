"""Attach a stable source label to frames produced by one capture branch."""

from __future__ import annotations

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class SourceTagger(FrameProcessor):
    """Tag downstream frames with a coarse speaker/source identity.

    ``TranscriptionFrame.user_id`` is overwritten so downstream processors and
    persisted buffer entries can treat the source as authoritative instead of
    relying on backend-specific STT behaviour.
    """

    def __init__(self, source: str, source_order: int, **kwargs):
        super().__init__(**kwargs)
        self._source = source
        self._source_order = source_order
        self._branch_sequence = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        setattr(frame, "koda_source", self._source)
        setattr(frame, "koda_source_order", self._source_order)

        if isinstance(frame, TranscriptionFrame):
            frame.user_id = self._source
            setattr(frame, "koda_branch_sequence", self._branch_sequence)
            self._branch_sequence += 1
        elif isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            setattr(frame, "user_id", self._source)

        await self.push_frame(frame, direction)
