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
