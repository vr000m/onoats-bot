"""Koda-local frame types that extend Pipecat's frame hierarchy."""

from bot.frames.branch_vad import (
    BranchVADUserStartedSpeakingFrame,
    BranchVADUserStoppedSpeakingFrame,
)

__all__ = [
    "BranchVADUserStartedSpeakingFrame",
    "BranchVADUserStoppedSpeakingFrame",
    "KNOWN_BRANCH_SOURCES",
    "resolve_frame_source",
]

#: Branch-tag values that ``SourceTagger`` produces. A diarizing STT may emit
#: arbitrary ``user_id`` values (e.g. ``speaker_1``); those are ignored by the
#: dual-branch coordinator because they do not identify a capture branch.
KNOWN_BRANCH_SOURCES: frozenset[str] = frozenset({"me", "them"})


def resolve_frame_source(frame) -> str | None:
    """Return the normalized branch identity for *frame*, or None.

    Canonical lookup order — SourceTagger sets ``koda_source`` on every
    branch-tagged frame; ``source`` is the dataclass field on the
    BranchVAD subclasses; ``user_id`` is Pipecat's native attribute for
    STTs that diarize. Only values in :data:`KNOWN_BRANCH_SOURCES` are
    returned — a diarizing STT that emits ``speaker_1`` on ``user_id``
    will not leak through as a phantom branch.
    """
    for attr in ("koda_source", "source", "user_id"):
        raw = getattr(frame, attr, None)
        if not raw:
            continue
        normalized = str(raw).strip().lower()
        if normalized in KNOWN_BRANCH_SOURCES:
            return normalized
    return None
