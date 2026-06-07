"""Filesystem-only converter: render, idempotency, failure routing, no DB."""

from __future__ import annotations

import json
import os

import pytest

from onoats._vendor.session_queue import ensure_queue_dirs, queue_dir
from onoats.convert import convert_once


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Point data + config dirs at tmp so no real user files are read/written."""
    monkeypatch.setenv("ONOATS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("KODA_DATA_DIR", raising=False)
    for var in ("ONOATS_SPEAKER_ME", "ONOATS_SPEAKER_THEM"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path / "data"


def _write_pending(data_dir, session_id, category, utterances):
    ensure_queue_dirs(data_dir)
    path = queue_dir("pending", data_dir) / f"{session_id}.jsonl"
    lines = []
    if category is not None:
        lines.append({"type": "session_meta", "category": category})
    for time, text, source in utterances:
        lines.append(
            {"type": "utterance", "time": time, "text": text, "source": source}
        )
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    return path


def _no_db_anywhere(root):
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            assert not name.endswith(".db"), f"unexpected DB file: {dirpath}/{name}"


def _snapshot(directory):
    """Map filename -> (bytes, mtime_ns) for every file in a directory."""
    snap = {}
    for entry in sorted(directory.iterdir()):
        if entry.is_file():
            st = entry.stat()
            snap[entry.name] = (entry.read_bytes(), st.st_mtime_ns)
    return snap


def test_convert_renders_default_labels(_isolate_env):
    data_dir = _isolate_env
    sid = "session_20260606_120000_aaaa1111"
    _write_pending(
        data_dir,
        sid,
        "work",
        [
            ("2026-06-06T12:00:00", "hello there", "me"),
            ("2026-06-06T12:00:05", "hi back", "them"),
        ],
    )

    result = convert_once(data_dir)
    assert result == {"converted": 1, "failed": 0}

    md = data_dir / "transcripts" / "work" / "2026-06-06" / f"{sid}.md"
    assert md.exists()
    text = md.read_text(encoding="utf-8")
    assert "**Me:** hello there" in text
    assert "**Them:** hi back" in text
    assert "Category: work" in text

    # source moved to done/, none left in pending/
    assert (queue_dir("done", data_dir) / f"{sid}.jsonl").exists()
    assert list(queue_dir("pending", data_dir).glob("*.jsonl")) == []

    _no_db_anywhere(data_dir)


def test_convert_uses_configured_label(_isolate_env, monkeypatch):
    data_dir = _isolate_env
    monkeypatch.setenv("ONOATS_SPEAKER_ME", "Varun")
    sid = "session_20260606_130000_bbbb2222"
    _write_pending(
        data_dir,
        sid,
        "work",
        [("2026-06-06T13:00:00", "from varun", "me")],
    )

    convert_once(data_dir)

    md = data_dir / "transcripts" / "work" / "2026-06-06" / f"{sid}.md"
    text = md.read_text(encoding="utf-8")
    assert "**Varun:** from varun" in text
    assert "**Me:**" not in text


def test_idempotent_second_run_is_clean_noop(_isolate_env):
    data_dir = _isolate_env
    sid = "session_20260606_140000_cccc3333"
    _write_pending(
        data_dir,
        sid,
        "personal",
        [("2026-06-06T14:00:00", "only once", "me")],
    )

    convert_once(data_dir)
    done = queue_dir("done", data_dir)
    transcripts = data_dir / "transcripts" / "personal" / "2026-06-06"

    done_snap = _snapshot(done)
    md_snap = _snapshot(transcripts)

    # Second run: pending is empty, nothing should change.
    result = convert_once(data_dir)
    assert result == {"converted": 0, "failed": 0}

    assert _snapshot(done) == done_snap, "done/ not byte-and-mtime-identical"
    assert _snapshot(transcripts) == md_snap, "transcripts changed on no-op run"
    assert list(queue_dir("pending", data_dir).glob("*.jsonl")) == []


def test_malformed_session_routed_to_failed_batch_continues(_isolate_env):
    data_dir = _isolate_env
    ensure_queue_dirs(data_dir)

    # A good session.
    good = "session_20260606_150000_dddd4444"
    _write_pending(data_dir, good, "work", [("2026-06-06T15:00:00", "good", "me")])

    # A malformed file: a directory at the markdown output path would force a
    # write error. Simpler: make the source unreadable by writing a *directory*
    # where a file is expected is not possible in pending; instead inject a
    # session whose category contains a path separator so the output write
    # collides with an existing file.
    bad = "session_20260606_160000_eeee5555"
    bad_path = queue_dir("pending", data_dir) / f"{bad}.jsonl"
    # category "../escape" would still be a valid dir; instead force a render
    # error by making the transcripts/<cat> path a FILE, not a dir.
    bad_cat = "blocked"
    (data_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    # Create a FILE named after the category dir so mkdir(parents) fails.
    (data_dir / "transcripts" / bad_cat).write_text("i am a file", encoding="utf-8")
    bad_path.write_text(
        json.dumps({"type": "session_meta", "category": bad_cat})
        + "\n"
        + json.dumps(
            {
                "type": "utterance",
                "time": "2026-06-06T16:00:00",
                "text": "x",
                "source": "me",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = convert_once(data_dir)
    assert result["converted"] == 1
    assert result["failed"] == 1

    assert (queue_dir("done", data_dir) / f"{good}.jsonl").exists()
    assert (queue_dir("failed", data_dir) / f"{bad}.jsonl").exists()
    assert list(queue_dir("pending", data_dir).glob("*.jsonl")) == []


def test_missing_session_meta_defaults_uncategorized(_isolate_env):
    data_dir = _isolate_env
    sid = "session_20260606_170000_ffff6666"
    _write_pending(data_dir, sid, None, [("2026-06-06T17:00:00", "no meta", "them")])

    convert_once(data_dir)

    md = data_dir / "transcripts" / "uncategorized" / "2026-06-06" / f"{sid}.md"
    assert md.exists()
    assert "Category: uncategorized" in md.read_text(encoding="utf-8")
