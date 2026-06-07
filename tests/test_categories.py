"""Category config: default set, --category validation, session_meta encoding."""

from __future__ import annotations

import json

import pytest

from onoats.categories import (
    DEFAULT_CATEGORY,
    InvalidCategoryError,
    category_set,
    session_meta_line,
    validate_category,
)
from onoats.config import OnoatsConfig


def _cfg(cats=None) -> OnoatsConfig:
    raw = {"categories": {"set": cats}} if cats is not None else {}
    return OnoatsConfig(raw=raw, secrets={})


def test_default_category_set_is_uncategorized(monkeypatch):
    monkeypatch.delenv("ONOATS_CATEGORIES", raising=False)
    assert category_set(_cfg()) == {"uncategorized"}


def test_configured_set_always_includes_uncategorized(monkeypatch):
    monkeypatch.delenv("ONOATS_CATEGORIES", raising=False)
    assert category_set(_cfg(["work", "personal"])) == {
        "work",
        "personal",
        "uncategorized",
    }


def test_validate_category_accepts_configured(monkeypatch):
    monkeypatch.delenv("ONOATS_CATEGORIES", raising=False)
    assert validate_category("Work", config=_cfg(["work", "personal"])) == "work"


def test_validate_category_rejects_unconfigured(monkeypatch):
    monkeypatch.delenv("ONOATS_CATEGORIES", raising=False)
    with pytest.raises(InvalidCategoryError):
        validate_category("seminars", config=_cfg(["work"]))


def test_validate_category_rejects_uncategorized(monkeypatch):
    monkeypatch.delenv("ONOATS_CATEGORIES", raising=False)
    with pytest.raises(InvalidCategoryError):
        validate_category("uncategorized", config=_cfg(["work"]))


def test_validate_none_passes_through(monkeypatch):
    monkeypatch.delenv("ONOATS_CATEGORIES", raising=False)
    assert validate_category(None, config=_cfg(["work"])) is None


def test_session_meta_line_encodes_category():
    line = session_meta_line("work")
    parsed = json.loads(line)
    assert parsed == {"type": "session_meta", "category": "work"}
    assert "\n" not in line  # writer appends the newline


def test_session_meta_line_defaults_to_uncategorized():
    parsed = json.loads(session_meta_line(None))
    assert parsed["category"] == DEFAULT_CATEGORY


def test_session_meta_is_first_line_in_session_file(tmp_path):
    """The recorder writes the session_meta line FIRST when a category is locked."""
    import asyncio

    from onoats.processors.transcript_buffer import TranscriptBuffer

    async def _run() -> None:
        buf = TranscriptBuffer(data_dir=tmp_path, locked_category="work")
        await buf._write_entry(
            {"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": "hello"}
        )

    asyncio.run(_run())
    session_files = list((tmp_path / ".active").glob("*.jsonl"))
    assert len(session_files) == 1
    lines = session_files[0].read_text().splitlines()
    first = json.loads(lines[0])
    assert first == {"type": "session_meta", "category": "work"}
    # The category is NOT encoded in the filename — the session_id stem stays
    # load-bearing for a consumer's back-fill keying.
    assert "work" not in session_files[0].stem
