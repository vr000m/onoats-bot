"""Persistence-neutral display-label composition shared by UI boundaries."""

from __future__ import annotations


def compose_speaker_label(
    display_label: str, speakers: list[str] | None, overlap: bool
) -> str:
    """Add a diarization suffix without changing canonical persisted values."""
    if not speakers:
        return display_label
    if overlap and len(speakers) > 1:
        return f"{display_label} ({' / '.join(speakers)}, overlapping)"
    return f"{display_label} ({' / '.join(speakers)})"
