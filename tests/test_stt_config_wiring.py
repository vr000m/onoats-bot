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
