"""Koda-local frame types that extend Pipecat's frame hierarchy."""

from bot.frames.branch_vad import (
    BranchVADUserStartedSpeakingFrame,
    BranchVADUserStoppedSpeakingFrame,
)

__all__ = [
    "BranchVADUserStartedSpeakingFrame",
    "BranchVADUserStoppedSpeakingFrame",
    "resolve_frame_source",
]


def resolve_frame_source(frame) -> str | None:
    """Return the normalized branch identity for *frame*, or None.

    Canonical lookup order — SourceTagger sets ``koda_source`` on every
    branch-tagged frame; ``source`` is the dataclass field on the
    BranchVAD subclasses; ``user_id`` is Pipecat's native attribute for
    STTs that diarize. Trim + lowercase so callers can use it as a set
    or dict key without surprises.
    """
    for attr in ("koda_source", "source", "user_id"):
        raw = getattr(frame, attr, None)
        if not raw:
            continue
        normalized = str(raw).strip().lower()
        if normalized:
            return normalized
    return None
