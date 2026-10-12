"""Speaker labels are render-only; the JSONL ``source`` enum stays canonical.

The configurable display labels (default ``Me``/``Them``) live in config and are
consumed only at render time. The on-disk ``source`` value written by the
recorder MUST stay the canonical ``me``/``them`` enum — the frozen wire contract
a consumer (e.g. a classifier) keys on. A label must NEVER reach the wire.
"""

from __future__ import annotations

import textwrap

from onoats.config import OnoatsConfig, load_config


def test_default_display_labels():
    cfg = OnoatsConfig(raw={}, secrets={})
    assert cfg.speaker_labels() == {"me": "Me", "them": "Them"}


def test_configured_display_labels(tmp_path, monkeypatch):
    monkeypatch.delenv("ONOATS_SPEAKER_ME", raising=False)
    monkeypatch.delenv("ONOATS_SPEAKER_THEM", raising=False)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            [speakers]
            me = "Varun"
            them = "Caller"
            """
        )
    )
    cfg = load_config(config_path=cfg_path, secrets_path=tmp_path / "absent.env")
    assert cfg.speaker_labels() == {"me": "Varun", "them": "Caller"}
    assert cfg.speaker_label_me == "Varun"
    assert cfg.speaker_label_them == "Caller"


def test_env_overrides_label(tmp_path, monkeypatch):
    cfg = OnoatsConfig(raw={"speakers": {"me": "Varun"}}, secrets={})
    monkeypatch.setenv("ONOATS_SPEAKER_ME", "EnvName")
    assert cfg.speaker_label_me == "EnvName"


def test_source_tagger_writes_canonical_enum_not_label():
    """SourceTagger writes the canonical ``me``/``them`` enum into user_id —
    never a display label. This is the frozen wire contract."""
    import asyncio

    from pipecat.frames.frames import TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection

    from onoats.processors.source_tagger import SourceTagger

    captured: list[TranscriptionFrame] = []

    tagger = SourceTagger(source="me", source_order=0)

    async def _fake_push(frame, direction):
        captured.append(frame)

    async def _run() -> None:
        # Bypass FrameProcessor setup machinery: stub the push + base check.
        tagger.push_frame = _fake_push  # type: ignore[method-assign]

        async def _noop(frame, direction):
            return None

        tagger._SourceTagger__dict__ = {}  # no-op, keeps linters quiet
        # Patch the base process_frame so we don't need a running pipeline.
        import onoats.processors.source_tagger as st

        orig = st.FrameProcessor.process_frame

        async def _base(self, frame, direction):
            return None

        st.FrameProcessor.process_frame = _base  # type: ignore[assignment]
        try:
            frame = TranscriptionFrame(text="hello", user_id="speaker_1", timestamp="t")
            await tagger.process_frame(frame, FrameDirection.DOWNSTREAM)
        finally:
            st.FrameProcessor.process_frame = orig  # type: ignore[assignment]

    asyncio.run(_run())

    assert captured, "SourceTagger should push the frame downstream"
    out = captured[0]
    # The wire value is the canonical enum, NOT a display label like "Me".
    assert out.user_id == "me"
    assert getattr(out, "onoats_source") == "me"
    assert out.user_id != "Me"


def test_transcript_buffer_jsonl_source_is_canonical_enum(tmp_path):
    """End-to-end: the JSONL ``source`` field written to disk is ``me``/``them``."""
    import asyncio
    import json

    from pipecat.frames.frames import TranscriptionFrame

    from onoats.processors.transcript_buffer import TranscriptBuffer

    async def _run() -> None:
        buf = TranscriptBuffer(data_dir=tmp_path, use_frame_source=True)
        frame = TranscriptionFrame(text="hi there", user_id="them", timestamp="t")
        setattr(frame, "onoats_source", "them")
        setattr(frame, "onoats_source_order", 1)
        await buf._handle_transcription(frame)

    asyncio.run(_run())
    session_files = list((tmp_path / ".active").glob("*.jsonl"))
    assert len(session_files) == 1
    entries = [json.loads(line) for line in session_files[0].read_text().splitlines()]
    utterances = [e for e in entries if e.get("type") == "utterance"]
    assert utterances and utterances[0]["source"] == "them"
    # No display label ("Them") ever reaches the wire.
    assert utterances[0]["source"] != "Them"


# ---------------------------------------------------------------------------
# Phase 6 — diarization label persistence + render
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402
import json  # noqa: E402

import pytest  # noqa: E402

EXPECTED_KEYS = {
    "time",
    "type",
    "text",
    "source",
    "source_order",
    "branch_sequence",
    "speakers",
    "overlap",
}
CONFIGURED_LABEL = "ZzCallerLabelZz"


def _them_frame(
    text="hello", *, speakers=None, overlap=None, ts="2026-01-01T00:00:00+00:00"
):
    from pipecat.frames.frames import TranscriptionFrame

    frame = TranscriptionFrame(text=text, user_id="them", timestamp=ts)
    setattr(frame, "onoats_source", "them")
    setattr(frame, "onoats_source_order", 1)
    setattr(frame, "onoats_branch_sequence", 3)
    if speakers is not None:
        setattr(frame, "onoats_speakers", speakers)
    if overlap is not None:
        setattr(frame, "onoats_overlap", overlap)
    return frame


def _utterances(path):
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip() and json.loads(line).get("type") == "utterance"
    ]


def _write_frames(tmp_path, frames):
    from onoats.processors.transcript_buffer import TranscriptBuffer

    async def _run():
        buf = TranscriptBuffer(data_dir=tmp_path, use_frame_source=True)
        for f in frames:
            await buf._handle_transcription(f)

    asyncio.run(_run())
    files = list((tmp_path / ".active").glob("*.jsonl"))
    assert len(files) == 1
    return files[0]


def test_entry_omits_label_keys_when_none():
    from onoats.processors.transcript_buffer import _utterance_entry

    entry = _utterance_entry("hi", source="them", source_order=1, branch_sequence=2)
    assert "speakers" not in entry
    assert "overlap" not in entry


def test_labelled_entry_schema_and_no_display_string(tmp_path):
    path = _write_frames(tmp_path, [_them_frame(speakers=["S1", "S2"], overlap=True)])
    (entry,) = _utterances(path)
    assert set(entry) == EXPECTED_KEYS
    assert entry["source"] == "them"
    assert entry["speakers"] == ["S1", "S2"]
    assert entry["overlap"] is True
    blob = json.dumps(entry)
    # Display labels (default or configured) are render-only, never persisted.
    assert CONFIGURED_LABEL not in blob
    assert "Them" not in blob and "Me" not in blob


def test_unlabelled_frame_has_no_label_keys(tmp_path):
    path = _write_frames(tmp_path, [_them_frame()])
    (entry,) = _utterances(path)
    assert "speakers" not in entry and "overlap" not in entry


def test_me_frame_never_persists_label_keys(tmp_path):
    from pipecat.frames.frames import TranscriptionFrame

    frame = TranscriptionFrame(text="mine", user_id="me", timestamp="t")
    setattr(frame, "onoats_source", "me")
    setattr(frame, "onoats_source_order", 0)
    path = _write_frames(tmp_path, [frame])
    (entry,) = _utterances(path)
    assert entry["source"] == "me"
    assert "speakers" not in entry and "overlap" not in entry


def test_writer_reader_round_trip(tmp_path):
    from onoats.jsonl import read_session_file

    path = _write_frames(
        tmp_path,
        [
            _them_frame("one", speakers=["S1"], overlap=False),
            _them_frame("two", speakers=["S1", "S2"], overlap=True),
            _them_frame("three"),
        ],
    )
    session = read_session_file(path)
    one, two, three = session.utterances
    assert one.speakers == ["S1"] and one.overlap is False
    assert two.speakers == ["S1", "S2"] and two.overlap is True
    assert three.speakers is None and three.overlap is False
    assert [u.source for u in session.utterances] == ["them"] * 3


def test_label_survives_flush_rotation(tmp_path):
    """S1 then S2 across a continuation flush: each side keeps its own label."""
    from onoats.jsonl import read_session_file
    from onoats.processors.transcript_buffer import TranscriptBuffer

    active = tmp_path / ".active"
    next_file = active / "session_next.jsonl"

    async def _run():
        buf = TranscriptBuffer(data_dir=tmp_path, use_frame_source=True)
        await buf._handle_transcription(
            _them_frame("first", speakers=["S1"], overlap=False)
        )
        entries1, path1 = await buf.flush(next_session_file=next_file)
        await buf._handle_transcription(
            _them_frame("second", speakers=["S2"], overlap=False)
        )
        await buf._handle_transcription(
            _them_frame("third", speakers=["S2"], overlap=False)
        )
        entries2, path2 = await buf.flush()
        return entries1, path1, entries2, path2

    entries1, path1, entries2, path2 = asyncio.run(_run())
    assert entries1[0]["speakers"] == ["S1"]
    assert [e["speakers"] for e in entries2] == [["S2"], ["S2"]]
    assert path2 == next_file
    s1 = read_session_file(path1)
    s2 = read_session_file(path2)
    assert [u.speakers for u in s1.utterances] == [["S1"]]
    assert [u.speakers for u in s2.utterances] == [["S2"], ["S2"]]
    assert all(u.source == "them" for u in s1.utterances + s2.utterances)


def test_old_file_back_compat(tmp_path):
    from onoats.jsonl import read_session_file

    path = tmp_path / "session_old.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "category": "uncategorized"})
        + "\n"
        + json.dumps(
            {"type": "utterance", "time": "t", "text": "legacy", "source": "them"}
        )
        + "\n"
    )
    (utt,) = read_session_file(path).utterances
    assert utt.text == "legacy" and utt.source == "them"
    assert utt.speakers is None and utt.overlap is False


@pytest.mark.parametrize(
    ("speakers", "overlap", "expected"),
    [
        (None, False, "Them"),
        ([], False, "Them"),
        (["S1"], False, "Them (S1)"),
        (["S1", "S2"], True, "Them (S1 / S2, overlapping)"),
    ],
)
def test_compose_speaker_label(speakers, overlap, expected):
    from onoats.render import compose_speaker_label

    assert compose_speaker_label("Them", speakers, overlap) == expected
    # Configured label is substituted verbatim.
    assert compose_speaker_label("Caller", speakers, overlap) == expected.replace(
        "Them", "Caller", 1
    )


def _render(tmp_path, labels):
    from onoats.jsonl import read_session_file
    from onoats.render import render_session

    path = _write_frames(
        tmp_path,
        [
            _them_frame("alpha", speakers=["S1"], overlap=False),
            _them_frame("beta", speakers=["S1", "S2"], overlap=True),
            _them_frame("gamma"),
        ],
    )
    return render_session(read_session_file(path), speaker_labels=labels)


def test_render_default_labels(tmp_path):
    out = _render(tmp_path, None)
    assert "**Them (S1):** alpha" in out
    assert "**Them (S1 / S2, overlapping):** beta" in out
    assert "**Them:** gamma" in out


def test_render_configured_labels(tmp_path):
    out = _render(tmp_path, {"me": "Varun", "them": "Caller"})
    assert "**Caller (S1):** alpha" in out
    assert "**Caller (S1 / S2, overlapping):** beta" in out
    assert "**Caller:** gamma" in out
    assert "Them" not in out


def _live_line(frame, capsys):
    from onoats.processors import live_terminal as lt

    renderer = lt.LiveTerminalRenderer()
    pushed = []

    async def _push(f, d):
        pushed.append(f)

    renderer.push_frame = _push  # type: ignore[method-assign]

    async def _base(self, f, d):
        return None

    orig = lt.FrameProcessor.process_frame
    lt.FrameProcessor.process_frame = _base  # type: ignore[assignment]
    try:
        from pipecat.processors.frame_processor import FrameDirection

        asyncio.run(renderer.process_frame(frame, FrameDirection.DOWNSTREAM))
    finally:
        lt.FrameProcessor.process_frame = orig  # type: ignore[assignment]
    assert pushed
    return capsys.readouterr().out


@pytest.mark.parametrize(
    ("speakers", "overlap"),
    [(None, None), (["S1"], False), (["S1", "S2"], True)],
)
def test_live_terminal_suffix_parity_with_render(capsys, speakers, overlap):
    """Live terminal keeps its Me/Them base name but shares the exact suffix."""
    from onoats.render import compose_speaker_label

    frame = _them_frame("live", speakers=speakers, overlap=overlap)
    frame.finalized = True
    out = _live_line(frame, capsys)
    expected = compose_speaker_label("Them", speakers, bool(overlap))
    assert f"] {expected}: live" in out


@pytest.mark.parametrize(
    ("speakers", "overlap", "expected"),
    [
        ("S1", None, "Them"),  # a bare string is not a speaker list
        ([1, 2], None, "Them"),  # non-str entries
        (["S1", "S2"], "false", "Them (S1 / S2)"),  # non-bool overlap ignored
        (["S1", "S2"], 1, "Them (S1 / S2)"),
    ],
)
def test_live_terminal_ignores_malformed_label_attributes(
    capsys, speakers, overlap, expected
):
    """The live terminal validates `onoats_speakers` / `onoats_overlap` the
    same way TranscriptBuffer does (shared resolvers in `onoats.frames`)."""
    frame = _them_frame("live", speakers=speakers, overlap=overlap)
    frame.finalized = True
    out = _live_line(frame, capsys)
    assert f"] {expected}: live" in out


def test_transcript_buffer_ignores_malformed_label_attributes(tmp_path):
    path = _write_frames(
        tmp_path,
        [
            _them_frame("a", speakers="S1", overlap="yes"),
            _them_frame("b", speakers=["S1", 2], overlap=1),
            _them_frame("c", speakers=("S1",), overlap=False),
        ],
    )
    a, b, c = _utterances(path)
    assert "speakers" not in a and "overlap" not in a
    assert "speakers" not in b and "overlap" not in b
    assert c["speakers"] == ["S1"] and c["overlap"] is False


def test_frame_label_resolvers():
    from types import SimpleNamespace

    from onoats.frames import resolve_frame_overlap, resolve_frame_speakers

    assert resolve_frame_speakers(SimpleNamespace()) is None
    assert resolve_frame_speakers(SimpleNamespace(onoats_speakers="S1")) is None
    assert resolve_frame_speakers(SimpleNamespace(onoats_speakers=["S1", 2])) is None
    assert resolve_frame_speakers(SimpleNamespace(onoats_speakers=("S1",))) == ["S1"]
    assert resolve_frame_overlap(SimpleNamespace()) is None
    assert resolve_frame_overlap(SimpleNamespace(onoats_overlap=1)) is None
    assert resolve_frame_overlap(SimpleNamespace(onoats_overlap=True)) is True
