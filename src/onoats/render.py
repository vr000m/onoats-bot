"""Self-contained markdown renderer for onoats transcripts.

This is onoats' OWN renderer — it deliberately does not reuse any upstream
classifier-coupled markdown renderer or semantic segmentation.
It renders ONE chronological transcript per session: a header plus
speaker-tagged lines, mapping the canonical ``me``/``them`` ``source`` enum to
the configured display labels (default ``Me`` / ``Them``).

Pure function: no network, no DB, no segmentation, no classification.
"""

from __future__ import annotations

from onoats.jsonl import Session, Utterance

_DEFAULT_LABELS = {"me": "Me", "them": "Them"}


def session_date(session: Session) -> str:
    """Best-effort YYYY-MM-DD from the first utterance, else the session id."""
    for utt in session.utterances:
        if utt.time and len(utt.time) >= 10:
            return utt.time[:10]
    # session id form: session_YYYYMMDD_HHMMSS_<short>
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
