"""Self-contained markdown renderer for onoats transcripts.

This is onoats' OWN renderer — it deliberately does not reuse any upstream
classifier-coupled markdown renderer or semantic segmentation.
It renders ONE chronological transcript per session: a header plus
speaker-tagged lines, mapping the canonical ``me``/``them`` ``source`` enum to
the configured display labels (default ``Me`` / ``Them``).

Pure function: no network, no DB, no segmentation, no classification.
"""

from __future__ import annotations

from datetime import datetime

from onoats.jsonl import Session, Utterance

_DEFAULT_LABELS = {"me": "Me", "them": "Them"}


def session_date(session: Session) -> str:
    """Best-effort ``YYYY-MM-DD`` (LOCAL date) from the first utterance.

    Utterance timestamps are stored in UTC (``...+00:00``); transcripts are
    grouped by **local** calendar date so an evening recording files under the
    day it happened, not the next UTC day (e.g. 19:21 PDT = 02:21 UTC tomorrow).
    This follows the project-wide convention: UTC storage, local grouping.

    A timezone-aware timestamp is converted to the local zone; a naive one is
    taken as-is. Falls back to the session-id stamp, then ``unknown-date``.
    """
    for utt in session.utterances:
        if not utt.time:
            continue
        try:
            dt = datetime.fromisoformat(utt.time)
        except ValueError:
            # Unparseable but plausibly date-prefixed (``YYYY-MM-DD...``).
            return (
                utt.time[:10] if len(utt.time) >= 10 else _date_from_session_id(session)
            )
        if dt.tzinfo is not None:
            dt = dt.astimezone()  # aware UTC -> local system zone
        return dt.strftime("%Y-%m-%d")
    return _date_from_session_id(session)


def _date_from_session_id(session: Session) -> str:
    # session id form: session_YYYYMMDD_HHMMSS_<short> (the stamp is local).
    parts = session.session_id.split("_")
    if len(parts) >= 2 and len(parts[1]) == 8 and parts[1].isdigit():
        d = parts[1]
        return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    return "unknown-date"


def _label_for(source: str | None, labels: dict[str, str]) -> str:
    """Display label for a canonical source enum, tolerant of unknowns."""
    if source in labels:
        return labels[source]
    if source:
        return source
    return "Unknown"


def render_session(
    session: Session,
    *,
    speaker_labels: dict[str, str] | None = None,
) -> str:
    """Render a session to a chronological markdown transcript string.

    ``speaker_labels`` maps the canonical ``me``/``them`` enum to display
    labels (from ``OnoatsConfig.speaker_labels()``). Silence gaps are omitted
    to keep the transcript clean.
    """
    labels = dict(_DEFAULT_LABELS)
    if speaker_labels:
        labels.update(speaker_labels)

    date = session_date(session)
    lines: list[str] = [
        f"# {session.session_id}",
        "",
        f"- Date: {date}",
        f"- Category: {session.category}",
        "",
    ]

    for entry in session.entries:
        if isinstance(entry, Utterance):
            text = entry.text.strip()
            if not text:
                continue
            label = _label_for(entry.source, labels)
            lines.append(f"**{label}:** {text}")
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"
