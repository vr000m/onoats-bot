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


def test_session_meta_line_omits_category_without_a_lock():
    """No lock => no ``category`` key: a consumer treats any non-blank category as
    a hard lock that skips classification, so ``uncategorized`` must never be
    defaulted in (it would pin every transcript)."""
    for absent in (None, ""):
        parsed = json.loads(session_meta_line(absent, run_id="abc"))
        assert parsed == {"type": "session_meta", "run_id": "abc"}
    assert DEFAULT_CATEGORY not in session_meta_line(None, run_id="abc")


def test_session_meta_line_carries_run_id_and_diarization_provenance():
    line = session_meta_line(
        "work", run_id="abc", diarization={"model": "m/x", "version": "rev1"}
    )
    assert json.loads(line) == {
        "type": "session_meta",
        "category": "work",
        "run_id": "abc",
        "diarization": {"model": "m/x", "version": "rev1"},
    }
    assert "\n" not in line


def test_session_meta_line_omits_diarization_when_off():
    assert "diarization" not in json.loads(session_meta_line("work", run_id="abc"))


def test_session_meta_is_first_line_in_session_file(tmp_path):
    """The recorder writes the session_meta line FIRST when a category is locked."""
    import asyncio

    from onoats.processors.transcript_buffer import TranscriptBuffer

    async def _run() -> TranscriptBuffer:
        buf = TranscriptBuffer(data_dir=tmp_path, locked_category="work")
        await buf._write_entry(
            {"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": "hello"}
        )
        return buf

    buf = asyncio.run(_run())
    session_files = list((tmp_path / ".active").glob("*.jsonl"))
    assert len(session_files) == 1
    lines = session_files[0].read_text().splitlines()
    first = json.loads(lines[0])
    assert first == {
        "type": "session_meta",
        "category": "work",
        "run_id": buf.run_id,
    }
    # The category is NOT encoded in the filename — the session_id stem stays
    # load-bearing for a consumer's back-fill keying.
    assert "work" not in session_files[0].stem


def _first_lines(tmp_path):
    files = sorted((tmp_path / ".active").glob("*.jsonl"))
    return [json.loads(f.read_text().splitlines()[0]) for f in files]


def test_session_meta_is_written_without_a_category_lock(tmp_path):
    import asyncio

    from onoats.processors.transcript_buffer import TranscriptBuffer

    async def _run() -> TranscriptBuffer:
        buf = TranscriptBuffer(data_dir=tmp_path)
        await buf._write_entry(
            {"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": "hi"}
        )
        return buf

    buf = asyncio.run(_run())
    (first,) = _first_lines(tmp_path)
    assert first == {"type": "session_meta", "run_id": buf.run_id}
    assert len(buf.run_id) == 32 and set(buf.run_id) <= set("0123456789abcdef")


def test_run_id_is_stable_across_continuation_files_and_new_per_buffer(tmp_path):
    import asyncio

    from onoats.processors.transcript_buffer import TranscriptBuffer

    entry = {"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": "hi"}
    next_file = tmp_path / ".active" / "session_next.jsonl"

    async def _run() -> tuple[str, str]:
        one = TranscriptBuffer(data_dir=tmp_path / "a", locked_category="work")
        await one._write_entry(entry)
        await one.flush(next_session_file=tmp_path / "a" / ".active" / "next.jsonl")
        await one._write_entry(entry)
        two = TranscriptBuffer(data_dir=tmp_path / "b")
        return one.run_id, two.run_id

    run_a, run_b = asyncio.run(_run())
    assert run_a != run_b
    heads = _first_lines(tmp_path / "a")
    assert len(heads) == 2  # the original file and the continuation file
    assert {h["run_id"] for h in heads} == {run_a}
    assert all(h["category"] == "work" for h in heads)
    del next_file


def test_session_meta_carries_diarization_provenance_only_when_given(tmp_path):
    import asyncio

    from onoats.processors.transcript_buffer import TranscriptBuffer

    entry = {"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": "hi"}

    async def _run() -> None:
        on = TranscriptBuffer(
            data_dir=tmp_path / "on", diarization_meta={"model": "m/x", "version": "r"}
        )
        off = TranscriptBuffer(data_dir=tmp_path / "off")
        await on._write_entry(entry)
        await off._write_entry(entry)

    asyncio.run(_run())
    (on_head,) = _first_lines(tmp_path / "on")
    (off_head,) = _first_lines(tmp_path / "off")
    assert on_head["diarization"] == {"model": "m/x", "version": "r"}
    assert "diarization" not in off_head


def test_failed_header_write_never_repeats_the_header_mid_file(tmp_path, monkeypatch):
    """A header that never landed on line 1 is not re-appended before every entry.

    Both header attempts for the first entry (file creation, then the pre-write
    guard) fail; the entry lands first. Later writes must leave the file alone:
    the old first-line sniff saw a non-header first line each time and appended
    another ``session_meta`` ahead of every subsequent entry.
    """
    import asyncio

    from onoats.processors import transcript_buffer as tb

    real_append = tb._append_line
    calls = {"n": 0}

    def flaky_append(path, line):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError(5, "transient I/O error")
        return real_append(path, line)

    monkeypatch.setattr(tb, "_append_line", flaky_append)

    async def _run() -> None:
        buf = tb.TranscriptBuffer(data_dir=tmp_path)
        for i in range(4):
            await buf._write_entry(
                {"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": i}
            )

    asyncio.run(_run())
    (session_file,) = (tmp_path / ".active").glob("*.jsonl")
    types = [json.loads(line)["type"] for line in session_file.read_text().splitlines()]
    assert types == ["utterance"] * 4


def test_session_header_is_settled_once_per_file(tmp_path, monkeypatch):
    """The header guard stats the file on the first write only, not on every entry."""
    import asyncio
    from pathlib import Path

    from onoats.processors.transcript_buffer import TranscriptBuffer

    stats = {"n": 0}
    real_exists = Path.exists

    def counting_exists(self):
        if self.suffix == ".jsonl":
            stats["n"] += 1
        return real_exists(self)

    async def _run() -> None:
        buf = TranscriptBuffer(data_dir=tmp_path)
        entry = {"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": "x"}
        await buf._write_entry(entry)
        monkeypatch.setattr(Path, "exists", counting_exists)
        for _ in range(5):
            await buf._write_entry(entry)

    asyncio.run(_run())
    assert stats["n"] == 0
    (first,) = _first_lines(tmp_path)
    assert first["type"] == "session_meta"
