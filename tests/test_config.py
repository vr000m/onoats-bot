"""Config loader: config.toml load + env-override precedence (env > file > default)."""

from __future__ import annotations

import textwrap

from onoats.config import OnoatsConfig, load_config, normalize_launchd_label

_CONFIG = textwrap.dedent(
    """
    [devices]
    mic = "MacBook Pro Microphone"
    system = "BlackHole 2ch"

    [stt]
    service = "deepgram"
    model = "nova-2"

    [speakers]
    me = "Varun"
    them = "Caller"

    [categories]
    set = ["work", "personal"]

    [tuning]
    silence_timeout_sec = 120
    segment_hint_threshold = 45
    """
)


def _write_config(tmp_path) -> object:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(_CONFIG)
    return load_config(config_path=cfg_path, secrets_path=tmp_path / "secrets.env")


def test_missing_config_yields_defaults(tmp_path, monkeypatch):
    for var in ("STT_SERVICE", "STT_MODEL", "SILENCE_TIMEOUT_SEC", "ONOATS_CATEGORIES"):
        monkeypatch.delenv(var, raising=False)
    cfg = load_config(
        config_path=tmp_path / "absent.toml", secrets_path=tmp_path / "absent.env"
    )
    assert isinstance(cfg, OnoatsConfig)
    assert cfg.stt_service == "whisper"  # built-in default
    assert cfg.silence_timeout_sec == 300.0
    assert cfg.category_set == {"uncategorized"}


def test_config_file_values_load(tmp_path, monkeypatch):
    for var in ("STT_SERVICE", "STT_MODEL", "SILENCE_TIMEOUT_SEC", "MIC_INPUT_DEVICE"):
        monkeypatch.delenv(var, raising=False)
    cfg = _write_config(tmp_path)
    assert cfg.stt_service == "deepgram"
    assert cfg.stt_model == "nova-2"
    assert cfg.mic_device == "MacBook Pro Microphone"
    assert cfg.system_device == "BlackHole 2ch"
    assert cfg.silence_timeout_sec == 120.0
    assert cfg.segment_hint_threshold == 45.0
    assert cfg.category_set == {"work", "personal", "uncategorized"}


def test_env_overrides_file(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    # File says deepgram/nova-2; env must win.
    monkeypatch.setenv("STT_SERVICE", "whisper")
    monkeypatch.setenv("STT_MODEL", "large-v3")
    monkeypatch.setenv("SILENCE_TIMEOUT_SEC", "600")
    assert cfg.stt_service == "whisper"
    assert cfg.stt_model == "large-v3"
    assert cfg.silence_timeout_sec == 600.0


def test_secrets_env_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    (tmp_path / "secrets.env").write_text(
        "DEEPGRAM_API_KEY=from_file_xxxxxxxxxxxxxxxx\n"
    )
    cfg = load_config(
        config_path=tmp_path / "absent.toml", secrets_path=tmp_path / "secrets.env"
    )
    # File secret is read when env is absent.
    assert cfg.get_secret("DEEPGRAM_API_KEY") == "from_file_xxxxxxxxxxxxxxxx"
    # Process env wins when present.
    monkeypatch.setenv("DEEPGRAM_API_KEY", "from_env_yyyyyyyyyyyyyyyy")
    assert cfg.get_secret("DEEPGRAM_API_KEY") == "from_env_yyyyyyyyyyyyyyyy"


def test_normalize_launchd_label_shared_strip_empty_validate_sequence():
    """gauntlet round-4 finding 1: `OnoatsConfig.stt_launchd_label` and
    `onoats init`'s config-carry-over (`init.py`) both need the exact same
    strip -> empty-as-None -> allowlist-validate sequence for a *string*
    label; extracted to `normalize_launchd_label` so the two callers can't
    independently drift on "absent" vs "malformed" (they did, twice, in
    rounds 5 and 6)."""
    # Whitespace-padded but otherwise well-formed: strips, then validates.
    assert (
        normalize_launchd_label(" pipecat.stt-server.nemotron ")
        == "pipecat.stt-server.nemotron"
    )
    # Empty/whitespace-only normalizes to absent, not "malformed".
    assert normalize_launchd_label("") is None
    assert normalize_launchd_label("   ") is None
    assert normalize_launchd_label(None) is None
    # Well-formed but with an interior character the allowlist rejects.
    assert normalize_launchd_label("bad label;rm") is None


def test_blank_stt_launchd_label_env_var_disables_kickstart(tmp_path, monkeypatch):
    """Round-9 finding: `stt_launchd_label`'s docstring promises that an
    empty/whitespace-only value "from either source" normalizes to `None`, but
    `_env_or` reads a blank env var as *absent* and falls through to
    config.toml — so `STT_LAUNCHD_LABEL=` in a plist or shell wrapper silently
    kept kickstarting whatever label config.toml named, and there was no way
    to turn self-healing off from the environment at all. Presence, not
    truthiness, is the question an explicit override asks."""
    from onoats.config import OnoatsConfig

    cfg = OnoatsConfig(raw={"stt": {"launchd_label": "pipecat.stt-server"}})
    monkeypatch.delenv("STT_LAUNCHD_LABEL", raising=False)
    assert cfg.stt_launchd_label == "pipecat.stt-server"

    for blank in ("", "   ", "\t"):
        monkeypatch.setenv("STT_LAUNCHD_LABEL", blank)
        assert cfg.stt_launchd_label is None, blank

    # A real env value still wins over config.toml, unchanged.
    monkeypatch.setenv("STT_LAUNCHD_LABEL", "pipecat.stt-server.nemotron")
    assert cfg.stt_launchd_label == "pipecat.stt-server.nemotron"


def test_diarization_flag_resolution(monkeypatch):
    """env ONOATS_DIARIZATION > [stt].diarization > default False."""
    monkeypatch.delenv("ONOATS_DIARIZATION", raising=False)
    assert OnoatsConfig(raw={}).diarization is False
    assert OnoatsConfig(raw={"stt": {"diarization": True}}).diarization is True
    assert OnoatsConfig(raw={"stt": {"diarization": False}}).diarization is False
    assert OnoatsConfig(raw={"stt": {"diarization": "on"}}).diarization is True

    for truthy in ("1", "true", "TRUE", "yes", "On"):
        monkeypatch.setenv("ONOATS_DIARIZATION", truthy)
        assert OnoatsConfig(raw={}).diarization is True, truthy
    # env wins over the file in both directions
    monkeypatch.setenv("ONOATS_DIARIZATION", "0")
    assert OnoatsConfig(raw={"stt": {"diarization": True}}).diarization is False
    monkeypatch.setenv("ONOATS_DIARIZATION", "true")
    assert OnoatsConfig(raw={"stt": {"diarization": False}}).diarization is True
    # blank env falls through to the file
    monkeypatch.setenv("ONOATS_DIARIZATION", "  ")
    assert OnoatsConfig(raw={"stt": {"diarization": True}}).diarization is True


def test_diarization_flag_garbage_is_false_with_warning(monkeypatch):
    from loguru import logger

    monkeypatch.delenv("ONOATS_DIARIZATION", raising=False)
    records: list[str] = []
    sink = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        assert OnoatsConfig(raw={"stt": {"diarization": "maybe"}}).diarization is False
    finally:
        logger.remove(sink)
    assert len(records) == 1 and "diarization" in records[0]


# --- diarization flag: env ONOATS_DIARIZATION > [stt] diarization > False ---


def test_diarization_defaults_off(monkeypatch):
    monkeypatch.delenv("ONOATS_DIARIZATION", raising=False)
    assert OnoatsConfig(raw={}).diarization is False
    assert OnoatsConfig(raw={"stt": {"service": "websocket"}}).diarization is False


def test_diarization_from_toml(monkeypatch):
    monkeypatch.delenv("ONOATS_DIARIZATION", raising=False)
    assert OnoatsConfig(raw={"stt": {"diarization": True}}).diarization is True
    assert OnoatsConfig(raw={"stt": {"diarization": False}}).diarization is False


def test_diarization_env_on_beats_toml_off(monkeypatch):
    monkeypatch.setenv("ONOATS_DIARIZATION", "1")
    assert OnoatsConfig(raw={"stt": {"diarization": False}}).diarization is True


def test_diarization_env_off_beats_toml_on(monkeypatch):
    monkeypatch.setenv("ONOATS_DIARIZATION", "0")
    assert OnoatsConfig(raw={"stt": {"diarization": True}}).diarization is False


def test_diarization_env_only_no_toml(monkeypatch):
    monkeypatch.setenv("ONOATS_DIARIZATION", "true")
    assert OnoatsConfig(raw={}).diarization is True


def test_diarization_blank_env_falls_through_to_toml(monkeypatch):
    monkeypatch.setenv("ONOATS_DIARIZATION", "   ")
    assert OnoatsConfig(raw={"stt": {"diarization": True}}).diarization is True


def test_diarization_resolves_from_config_file(tmp_path, monkeypatch):
    monkeypatch.delenv("ONOATS_DIARIZATION", raising=False)
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            [stt]
            service = "websocket"
            diarization = true
            """
        )
    )
    cfg = load_config(config_path=cfg_path, secrets_path=tmp_path / "secrets.env")
    assert cfg.diarization is True
