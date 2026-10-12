"""onoats-local frame types that extend Pipecat's frame hierarchy."""

from onoats.frames.branch_vad import (
    BranchVADUserStartedSpeakingFrame,
    BranchVADUserStoppedSpeakingFrame,
)

__all__ = [
    "BranchVADUserStartedSpeakingFrame",
    "BranchVADUserStoppedSpeakingFrame",
    "KNOWN_BRANCH_SOURCES",
    "resolve_frame_overlap",
    "resolve_frame_source",
    "resolve_frame_speakers",
]

#: Branch-tag values that ``SourceTagger`` produces. A diarizing STT may emit
#: arbitrary ``user_id`` values (e.g. ``speaker_1``); those are ignored by the
#: dual-branch coordinator because they do not identify a capture branch.
KNOWN_BRANCH_SOURCES: frozenset[str] = frozenset({"me", "them"})


def resolve_frame_source(frame) -> str | None:
    """Return the normalized branch identity for *frame*, or None.

    Canonical lookup order — SourceTagger sets ``onoats_source`` on every
    branch-tagged frame; ``source`` is the dataclass field on the
    BranchVAD subclasses; ``user_id`` is Pipecat's native attribute for
    STTs that diarize. Only values in :data:`KNOWN_BRANCH_SOURCES` are
    returned — a diarizing STT that emits ``speaker_1`` on ``user_id``
    will not leak through as a phantom branch.
    """
    for attr in ("onoats_source", "source", "user_id"):
        raw = getattr(frame, attr, None)
        if not raw:
            continue
        normalized = str(raw).strip().lower()
        if normalized in KNOWN_BRANCH_SOURCES:
            return normalized
    return None


def resolve_frame_speakers(frame) -> list[str] | None:
    """Return the diarization ``onoats_speakers`` of *frame*, or None.

    Only a list/tuple whose entries are all ``str`` is accepted (copied to a
    list); anything else - absent, a bare string, non-str entries - is None.
    The single reader for both consumers (``TranscriptBuffer`` and the live
    terminal) so they cannot disagree on a malformed value.
    """
    raw = getattr(frame, "onoats_speakers", None)
    if isinstance(raw, (list, tuple)) and all(isinstance(s, str) for s in raw):
        return list(raw)
    return None


def resolve_frame_overlap(frame) -> bool | None:
    """Return the diarization ``onoats_overlap`` of *frame* if a real bool.

    None when absent or not a ``bool`` (``1`` / ``"false"`` are not accepted).
    """
    raw = getattr(frame, "onoats_overlap", None)
    return raw if isinstance(raw, bool) else None
