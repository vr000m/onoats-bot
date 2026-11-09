"""A flush must not report success when buffered entries never reached disk."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from onoats.processors import transcript_buffer as tb
from onoats.processors.transcript_buffer import TranscriptBuffer
from onoats.runtime import flush_and_rotate


def _entry(text: str) -> dict:
    return {"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": text}


def _pending(data_dir: Path) -> list[Path]:
    return sorted((data_dir / "sessions" / "pending").glob("*.jsonl"))


async def _record(buf: TranscriptBuffer, text: str) -> None:
    """Add an entry the way the frame handler does: buffer it, then try to write it."""
    entry = _entry(text)
    buf._buffer.append(entry)
    if await buf._write_entry(entry):
        buf._persisted_indices.add(len(buf._buffer) - 1)


def test_complete_session_flushes_true(tmp_path: Path):
    async def run() -> bool:
        buf = TranscriptBuffer(data_dir=tmp_path)
        await _record(buf, "one")
        await _record(buf, "two")
        return await flush_and_rotate(
            buf, "test", continue_session=False, data_dir=tmp_path
        )

    assert asyncio.run(run()) is True
    assert len(_pending(tmp_path)) == 1


def test_entries_with_no_session_file_fail_the_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Storage unwritable from the first entry: not "buffer was empty"."""

    def boom() -> Path:
        raise OSError("disk unavailable")

    async def run() -> bool:
        buf = TranscriptBuffer(data_dir=tmp_path)
        monkeypatch.setattr(buf, "_ensure_session_file", boom)
        await _record(buf, "lost")
        return await flush_and_rotate(
            buf, "test", continue_session=False, data_dir=tmp_path
        )

    from loguru import logger

    messages: list[str] = []
    sink = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        assert asyncio.run(run()) is False
    finally:
        logger.remove(sink)
    joined = "\n".join(messages)
    assert "could not be written to disk" in joined
    assert "buffer was empty" not in joined
    assert _pending(tmp_path) == []


def test_partial_session_is_queued_but_fails_the_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    real_append = tb._append_line

    def flaky(path: Path, line: str) -> None:
        if '"bad"' in line:
            raise OSError("no space left on device")
        real_append(path, line)

    async def run() -> tuple[bool, int, TranscriptBuffer]:
        buf = TranscriptBuffer(data_dir=tmp_path)
        monkeypatch.setattr(tb, "_append_line", flaky)
        await _record(buf, "good")
        await _record(buf, "bad")
        ok = await flush_and_rotate(
            buf, "test", continue_session=False, data_dir=tmp_path
        )
        return ok, buf.unpersisted_on_last_flush, buf

    ok, lost, _ = asyncio.run(run())
    assert ok is False
    assert lost == 1
    files = _pending(tmp_path)
    assert len(files) == 1
    text = files[0].read_text()
    assert '"good"' in text and '"bad"' not in text


def test_counter_resets_on_the_next_clean_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    real_append = tb._append_line

    def flaky(path: Path, line: str) -> None:
        if '"bad"' in line:
            raise OSError("no space left on device")
        real_append(path, line)

    async def run() -> tuple[int, int]:
        buf = TranscriptBuffer(data_dir=tmp_path)
        monkeypatch.setattr(tb, "_append_line", flaky)
        await _record(buf, "bad")
        await buf.flush()
        first = buf.unpersisted_on_last_flush
        await _record(buf, "fine")
        await buf.flush()
        return first, buf.unpersisted_on_last_flush

    assert asyncio.run(run()) == (1, 0)
