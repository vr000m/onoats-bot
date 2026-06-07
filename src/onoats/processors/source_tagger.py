"""Attach a stable source label to frames produced by one capture branch."""

from __future__ import annotations

from dataclasses import fields

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from bot.frames import (
    BranchVADUserStartedSpeakingFrame,
    BranchVADUserStoppedSpeakingFrame,
)


def _rewrap_vad(
    frame: VADUserStartedSpeakingFrame | VADUserStoppedSpeakingFrame,
    source: str,
    source_order: int,
) -> Frame:
    """Return a branch-tagged replacement for a Pipecat VAD frame.

    Preserves existing dataclass field values (start_secs/stop_secs, timestamp)
    by copying field-by-field, then adds ``source`` / ``source_order``.
    """
    cls = (
        BranchVADUserStartedSpeakingFrame
        if isinstance(frame, VADUserStartedSpeakingFrame)
        else BranchVADUserStoppedSpeakingFrame
    )
    # Frame.id / broadcast_sibling_id are init=False; skip them or the
    # subclass constructor rejects them as unexpected kwargs.
    kwargs = {
        f.name: getattr(frame, f.name) for f in fields(frame) if f.init and hasattr(frame, f.name)
    }
    kwargs["source"] = source
    kwargs["source_order"] = source_order
    try:
        return cls(**kwargs)
    except TypeError as exc:
        # Surface the incompatibility so a future Pipecat frame-shape change
        # doesn't silently degrade branch tagging. Fall back to attribute-level
        # tagging so the downstream resolver still sees a source.
        logger.warning(
            f"SourceTagger: could not rewrap {type(frame).__name__} as "
            f"{cls.__name__} ({exc}); falling back to attribute tagging"
        )
        setattr(frame, "source", source)
        setattr(frame, "source_order", source_order)
        return frame


class SourceTagger(FrameProcessor):
    """Tag downstream frames with a coarse speaker/source identity.

    ``TranscriptionFrame.user_id`` is overwritten so downstream processors and
    persisted buffer entries can treat the source as authoritative instead of
    relying on backend-specific STT behaviour. VAD frames are rewrapped as
    branch-aware subclasses so the dual-idle coordinator can read source from
    a first-class field.
    """

    def __init__(self, source: str, source_order: int, **kwargs):
        super().__init__(**kwargs)
        self._source = source
        self._source_order = source_order
        self._branch_sequence = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TranscriptionFrame):
            frame.user_id = self._source
            setattr(frame, "koda_source", self._source)
            setattr(frame, "koda_source_order", self._source_order)
            setattr(frame, "koda_branch_sequence", self._branch_sequence)
            self._branch_sequence += 1
        elif isinstance(frame, (VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)):
            frame = _rewrap_vad(frame, self._source, self._source_order)

        await self.push_frame(frame, direction)
