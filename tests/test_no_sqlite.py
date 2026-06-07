"""Zero-SQLite invariant: the recorder opens no database, creates no *.db.

The startup ``build_transcript_store_only()`` was removed; the recorder emits
files only. This test drives the file-only flush/crash-recovery paths and
asserts (a) no ``*.db`` file is created under the data dir, and (b)
``aiosqlite`` is never imported by the runtime.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path


def test_runtime_does_not_import_aiosqlite():
    code = (
        "import onoats.runtime, sys; "
        "assert 'aiosqlite' not in sys.modules, 'aiosqlite imported by runtime'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def test_crash_recovery_creates_no_db(tmp_path: Path, monkeypatch):
    """run_crash_recovery over a fixture .active/ file creates no *.db."""
    monkeypatch.setenv("ONOATS_DATA_DIR", str(tmp_path))
    from onoats.runtime import run_crash_recovery

    active = tmp_path / ".active"
    active.mkdir(parents=True)
    session = active / "session_20260101_000000_abcdef00.jsonl"
    session.write_text(
        '{"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": "hi"}\n'
    )

    asyncio.run(run_crash_recovery(data_dir=tmp_path))

    # The file rotated into pending/; NO database anywhere under the tree.
    dbs = list(tmp_path.rglob("*.db"))
    assert dbs == [], f"recorder created SQLite file(s): {dbs}"
    pending = list((tmp_path / "sessions" / "pending").glob("*.jsonl"))
    assert len(pending) == 1, "crash recovery should rotate the orphan into pending/"


def test_flush_and_rotate_creates_no_db(tmp_path: Path):
    """A full flush_and_rotate cycle writes files only — no *.db."""
    from onoats.processors.transcript_buffer import TranscriptBuffer
    from onoats.runtime import flush_and_rotate

    async def _run() -> None:
        buf = TranscriptBuffer(data_dir=tmp_path)
        # Materialise a session file via the buffer's write path.
        await buf._write_entry(
            {"time": "2026-01-01T00:00:00+00:00", "type": "utterance", "text": "hello"}
        )
        await flush_and_rotate(buf, "test", continue_session=False, data_dir=tmp_path)

    asyncio.run(_run())
    assert list(tmp_path.rglob("*.db")) == [], "flush path must not create SQLite"
