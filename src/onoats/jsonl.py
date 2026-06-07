"""Minimal reader for the onoats session-queue JSONL contract.

A session file is a type-discriminated JSONL stream:

    {"type": "session_meta", "category": "<cat>"}        # optional FIRST line
    {"type": "utterance", "time": "...", "text": "...", "source": "me"|"them"}
    {"type": "silence_gap", "time": "...", "duration_seconds": N}

This is onoats' own minimal reader — no upstream classifier/segmenter/renderer
imports. It tolerates a missing ``session_meta`` line (the category then
defaults to ``uncategorized``) and skips malformed lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

UNCATEGORIZED = "uncategorized"


@dataclass
class Utterance:
    """One spoken line. ``source`` is the canonical ``me``/``them`` enum."""

    time: str
    text: str
    source: str | None = None


@dataclass
class SilenceGap:
    """A recorded silence hint between utterances."""

    time: str
    duration_seconds: float | None = None


@dataclass
class Session:
    """A parsed session: its id, category, and ordered entries."""

    session_id: str
    category: str = UNCATEGORIZED
    utterances: list[Utterance] = field(default_factory=list)
    entries: list[Utterance | SilenceGap] = field(default_factory=list)


def read_session_file(path: str | Path) -> Session:
    """Parse a session JSONL file into a :class:`Session`.

    Dispatches on the ``type`` field. A missing/blank ``session_meta`` line
    leaves ``category`` as ``uncategorized``. Malformed JSON lines are skipped.
    """
    path = Path(path)
    session = Session(session_id=path.stem)
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            etype = entry.get("type")
            if etype == "session_meta":
                category = entry.get("category")
                if isinstance(category, str) and category.strip():
                    session.category = category.strip()
            elif etype == "utterance":
                utt = Utterance(
                    time=str(entry.get("time", "")),
                    text=str(entry.get("text", "")),
                    source=entry.get("source"),
                )
                session.utterances.append(utt)
                session.entries.append(utt)
            elif etype == "silence_gap":
                session.entries.append(
                    SilenceGap(
                        time=str(entry.get("time", "")),
                        duration_seconds=entry.get("duration_seconds"),
                    )
                )
    return session
