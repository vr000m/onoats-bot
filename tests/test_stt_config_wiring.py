"""The recorder must consume config.toml, not just env vars.

Regression tests for the soak bugs found on the first live `onoats bot` run:
  A. STT service/ws_socket from config.toml were ignored (runtime read only
     env), so a configured `service = "websocket"` silently fell back to
     whisper.
  B. The whisper-cpu fallback passed `device` into `WhisperSTTService.Settings`,
     which rejects it -> TypeError crash (`device` is a constructor kwarg).
  C. `[devices] mic/system` from config.toml were ignored, so the bot
     re-prompted on every launch despite `onoats init`.
"""

from __future__ import annotations

import os

from onoats import runtime
from onoats.config import OnoatsConfig

# --- A. STT selection + ws endpoint come from config.toml -------------------


def test_stt_service_and_ws_socket_from_config_toml():
    cfg = OnoatsConfig(
        raw={"stt": {"service": "websocket", "ws_socket": "~/x/nemotron.sock"}}
    )
    assert cfg.stt_service == "websocket"
    assert cfg.stt_ws_socket == "~/x/nemotron.sock"


def test_ws_env_expands_socket_from_config(monkeypatch):
    monkeypatch.delenv("STT_WS_SOCKET", raising=False)
    cfg = OnoatsConfig(raw={"stt": {"ws_socket": "~/x/nemotron.sock"}})
    env = runtime._ws_env(cfg)
    assert env["STT_WS_SOCKET"] == os.path.expanduser("~/x/nemotron.sock")


def test_env_socket_overrides_config(monkeypatch):
    monkeypatch.setenv("STT_WS_SOCKET", "/run/explicit.sock")
    cfg = OnoatsConfig(raw={"stt": {"ws_socket": "~/x/nemotron.sock"}})
    # cfg.stt_ws_socket resolves env-over-file; _ws_env then expanduser-es it
    # (a no-op for an absolute path).
    assert runtime._ws_env(cfg)["STT_WS_SOCKET"] == "/run/explicit.sock"


def test_stt_service_defaults_to_whisper_when_unset(monkeypatch):
    monkeypatch.delenv("STT_SERVICE", raising=False)
    assert OnoatsConfig(raw={}).stt_service == "whisper"


# --- A1. STT language comes from config.toml (env wins), auto -> None -------


def test_stt_language_defaults_to_en(monkeypatch):
    monkeypatch.delenv("STT_LANGUAGE", raising=False)
    monkeypatch.delenv("STT_WS_LANGUAGE", raising=False)
    assert OnoatsConfig(raw={}).stt_language == "en"


def test_stt_language_from_config_toml(monkeypatch):
    monkeypatch.delenv("STT_LANGUAGE", raising=False)
    monkeypatch.delenv("STT_WS_LANGUAGE", raising=False)
    cfg = OnoatsConfig(raw={"stt": {"language": "sv"}})
    assert cfg.stt_language == "sv"


def test_env_language_overrides_config(monkeypatch):
    monkeypatch.delenv("STT_LANGUAGE", raising=False)
    monkeypatch.setenv("STT_WS_LANGUAGE", "de")
    cfg = OnoatsConfig(raw={"stt": {"language": "sv"}})
    assert cfg.stt_language == "de"


def test_stt_language_env_beats_legacy_alias_and_config(monkeypatch):
    """STT_LANGUAGE is the canonical env var (cross-backend, like STT_SERVICE /
    STT_MODEL); STT_WS_LANGUAGE survives as a legacy alias below it.
    """
    monkeypatch.setenv("STT_LANGUAGE", "fi")
    monkeypatch.setenv("STT_WS_LANGUAGE", "de")
    assert OnoatsConfig(raw={"stt": {"language": "sv"}}).stt_language == "fi"
    monkeypatch.delenv("STT_LANGUAGE")
    assert OnoatsConfig(raw={"stt": {"language": "sv"}}).stt_language == "de"


def test_whitespace_only_language_falls_back_to_en(monkeypatch):
    """A whitespace-only file value must resolve to "en", never reach the
    backend as language="" (the pre-config inline code had this guard too).
    """
    monkeypatch.delenv("STT_LANGUAGE", raising=False)
    monkeypatch.delenv("STT_WS_LANGUAGE", raising=False)
    assert OnoatsConfig(raw={"stt": {"language": "   "}}).stt_language == "en"
    monkeypatch.setenv("STT_WS_LANGUAGE", "   ")
    assert OnoatsConfig(raw={}).stt_language == "en"


def test_resolve_stt_language_maps_auto_to_none(monkeypatch):
    """``auto`` must reach the backends as None, never the literal string.

    whisper/mlx raises on a literal "auto"; None means auto-detect uniformly
    (mlx built-in detection / nemotron's own auto language-ID).
    """
    monkeypatch.delenv("STT_LANGUAGE", raising=False)
    monkeypatch.delenv("STT_WS_LANGUAGE", raising=False)
    assert runtime._resolve_stt_language(OnoatsConfig(raw={})) == "en"
    assert (
        runtime._resolve_stt_language(OnoatsConfig(raw={"stt": {"language": "Auto"}}))
        is None
    )
    monkeypatch.setenv("STT_WS_LANGUAGE", "auto")
    assert runtime._resolve_stt_language(OnoatsConfig(raw={})) is None


def test_whisper_settings_accept_language_none():
    """The auto-detect path builds Settings(language=None) — pin that the
    pinned pipecat accepts it (assert_given rejects only NOT_GIVEN, not None).
    """
    from pipecat.services.whisper.stt import WhisperSTTService

    settings = WhisperSTTService.Settings(model="base", language=None)
    assert settings.language is None


# --- A2. the RSS probe resolves the SAME endpoint as the data path ----------


def test_rss_probe_uses_config_socket_not_env_default(monkeypatch):
    """`log_stt_server_rss` must resolve from the config-layered env.

    Regression for the probe reporting the wrong stt_server: it resolved its
    endpoint from bare `os.environ`, missed the config.toml `[stt] ws_socket`,
    and fell back to the default socket (a stale/wrong server). The data path
    (`_create_stt_service`) and banner both go through `_ws_env(cfg)`; the
    probe must too. Pin it by capturing the socket the probe hands to the
    client and asserting it is the configured one, not the default.
    """
    import asyncio

    import stt_server.client as stt_client

    monkeypatch.delenv("STT_WS_SOCKET", raising=False)
    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(
            raw={"stt": {"service": "websocket", "ws_socket": "~/x/nemotron.sock"}}
        ),
    )

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def connect(self):
            raise OSError("no server in test")  # swallowed by the probe

        async def close_session(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(stt_client, "TranscriptionClient", _FakeClient)

    asyncio.run(runtime.log_stt_server_rss("startup"))

    assert captured["socket_path"] == os.path.expanduser("~/x/nemotron.sock")
    assert captured["socket_path"] != os.path.expanduser(runtime._DEFAULT_STT_WS_SOCKET)


# --- B. whisper-cpu Settings must not carry `device` ------------------------


def test_whisper_settings_reject_device_kwarg():
    """`device` is a WhisperSTTService constructor kwarg, NOT a Settings field.

    Pins the contract behind Bug B: building Settings with `device` must raise,
    so the runtime is forced to pass it to the service constructor instead.
    """
    from pipecat.services.whisper.stt import WhisperSTTService

    # the shape the runtime now uses — valid
    WhisperSTTService.Settings(model="base", language="en")

    try:
        WhisperSTTService.Settings(model="base", device="cpu", language="en")
    except TypeError:
        pass
    else:
        raise AssertionError(
            "WhisperSTTService.Settings unexpectedly accepted `device`; "
            "the runtime's constructor-kwarg fix may be unnecessary — re-check."
        )


# --- C. devices come from config.toml ---------------------------------------


def test_devices_from_config_toml():
    cfg = OnoatsConfig(
        raw={"devices": {"mic": "Scarlett Solo USB", "system": "BlackHole 2ch"}}
    )
    assert cfg.mic_device == "Scarlett Solo USB"
    assert cfg.system_device == "BlackHole 2ch"


def test_env_overrides_config_devices(monkeypatch):
    monkeypatch.setenv("MIC_INPUT_DEVICE", "Opal C1 Audio Mic")
    cfg = OnoatsConfig(raw={"devices": {"mic": "Scarlett Solo USB"}})
    assert cfg.mic_device == "Opal C1 Audio Mic"


# --- D. stt_launchd_label: fully optional, defaults to today's behavior -----
#
# Design intent (dev plan Phase 1): the new key is opt-in only. Absent means
# self-healing kickstart is skipped entirely and today's plain
# SttPreflightError / silent reconnect-failure behavior is preserved exactly
# -- no existing config.toml should observe any change from this property
# existing at all.


def test_stt_launchd_label_absent_is_none(monkeypatch):
    """No config, no env -> None (today's exact behavior: no kickstart)."""
    monkeypatch.delenv("STT_LAUNCHD_LABEL", raising=False)
    assert OnoatsConfig(raw={}).stt_launchd_label is None


def test_stt_launchd_label_from_config_toml(monkeypatch):
    monkeypatch.delenv("STT_LAUNCHD_LABEL", raising=False)
    cfg = OnoatsConfig(raw={"stt": {"launchd_label": "pipecat.stt-server.nemotron"}})
    assert cfg.stt_launchd_label == "pipecat.stt-server.nemotron"


def test_stt_launchd_label_from_env_only(monkeypatch):
    monkeypatch.setenv("STT_LAUNCHD_LABEL", "pipecat.stt-server")
    cfg = OnoatsConfig(raw={})
    assert cfg.stt_launchd_label == "pipecat.stt-server"


def test_stt_launchd_label_env_overrides_config(monkeypatch):
    monkeypatch.setenv("STT_LAUNCHD_LABEL", "pipecat.stt-server")
    cfg = OnoatsConfig(raw={"stt": {"launchd_label": "pipecat.stt-server.nemotron"}})
    assert cfg.stt_launchd_label == "pipecat.stt-server"


def test_stt_launchd_label_empty_string_config_is_absent(monkeypatch):
    """An empty-string file value must resolve to None, not "" -- kickstart
    call sites treat "no label configured" as the skip condition, so a
    stray `launchd_label = ""` in config.toml must not be mistaken for a
    configured (but empty) label.
    """
    monkeypatch.delenv("STT_LAUNCHD_LABEL", raising=False)
    assert OnoatsConfig(raw={"stt": {"launchd_label": ""}}).stt_launchd_label is None


def test_stt_launchd_label_whitespace_only_config_is_absent(monkeypatch):
    monkeypatch.delenv("STT_LAUNCHD_LABEL", raising=False)
    assert OnoatsConfig(raw={"stt": {"launchd_label": "   "}}).stt_launchd_label is None
