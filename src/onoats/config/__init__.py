"""Consolidated configuration loader for the onoats recorder.

Replaces the scattered ``.env`` + ``.last_dual_audio_devices`` reads with a
single ``$XDG_CONFIG_HOME/onoats/config.toml`` plus a ``0600 secrets.env`` for
STT secrets only (Deepgram key / websocket token — NO LLM keys).

Precedence (highest first):
    process env var  >  config.toml / secrets.env  >  built-in default

So an automation / driver mode that env-injects ``ONOATS_DATA_DIR``,
``STT_SERVICE`` etc. overrides the file without edits, and CI can override
anything. A missing ``config.toml`` is not an error — sensible defaults apply.

``config.toml`` sections::

    [devices]   mic = "...", system = "..."           # by stable device name
    [stt]       service = "...", model = "...", ws_socket/ws_host/ws_port/ws_uri = "..."
    [speakers]  me = "Me", them = "Them"               # RENDER-ONLY display labels
    [categories] set = ["uncategorized", ...]
    [tuning]    silence_timeout_sec / segment_hint_threshold / audio_heartbeat_sec

The ``[speakers]`` labels are consumed ONLY at render time by the converter
(Phase 3); the JSONL ``source`` field stays the canonical ``me``/``them`` enum
(the frozen wire contract). See ``processors/source_tagger.py``.

``audio_devices`` (the device picker) is a submodule of this package:
``from onoats.config.audio_devices import select_input_device``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from dotenv import dotenv_values
from loguru import logger

# ---------------------------------------------------------------------------
# Secret validators / helpers (vendored from koda shared/config.py)
# ---------------------------------------------------------------------------


def looks_like_bearer_token(v: str) -> bool:
    """Validator: value looks like an API key (20+ chars)."""
    # vendored from koda shared/config.py
    return len(v.strip()) >= 20


def _config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".config"
    return base / "onoats"


def config_toml_path() -> Path:
    return _config_home() / "config.toml"


def secrets_env_path() -> Path:
    return _config_home() / "secrets.env"


# ---------------------------------------------------------------------------
# Built-in defaults
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, dict[str, Any]] = {
    "stt": {"service": "whisper", "model": ""},
    "speakers": {"me": "Me", "them": "Them"},
    "categories": {"set": ["uncategorized"]},
    "tuning": {
        "silence_timeout_sec": 300.0,
        "segment_hint_threshold": 120.0,
        "audio_heartbeat_sec": 0.0,
    },
}


def _env_or(env_name: str, file_value: Any) -> Any:
    """Process env var wins; else the config.toml value; else None."""
    env = os.environ.get(env_name, "")
    if env.strip():
        return env
    return file_value


@dataclass(frozen=True)
class OnoatsConfig:
    """Resolved configuration, env-overridden over config.toml over defaults."""

    raw: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    # ---- devices ----
    @property
    def mic_device(self) -> str | None:
        return _env_or("MIC_INPUT_DEVICE", self.raw.get("devices", {}).get("mic"))

    @property
    def system_device(self) -> str | None:
        return _env_or("SYSTEM_INPUT_DEVICE", self.raw.get("devices", {}).get("system"))

    # ---- stt ----
    @property
    def stt_service(self) -> str:
        return (
            str(
                _env_or("STT_SERVICE", self.raw.get("stt", {}).get("service"))
                or _DEFAULTS["stt"]["service"]
            )
            .lower()
            .strip()
        )

    @property
    def stt_model(self) -> str:
        return str(
            _env_or("STT_MODEL", self.raw.get("stt", {}).get("model"))
            or _DEFAULTS["stt"]["model"]
        ).strip()

    # websocket endpoint (env wins; else config.toml [stt].ws_*). None when
    # neither set — the runtime then falls back to the built-in default socket.
    @property
    def stt_ws_socket(self) -> str | None:
        return _env_or("STT_WS_SOCKET", self.raw.get("stt", {}).get("ws_socket"))

    @property
    def stt_ws_host(self) -> str | None:
        return _env_or("STT_WS_HOST", self.raw.get("stt", {}).get("ws_host"))

    @property
    def stt_ws_port(self) -> str | None:
        return _env_or("STT_WS_PORT", self.raw.get("stt", {}).get("ws_port"))

    @property
    def stt_ws_uri(self) -> str | None:
        return _env_or("STT_WS_URI", self.raw.get("stt", {}).get("ws_uri"))

    # ---- speakers (render-only display labels) ----
    @property
    def speaker_label_me(self) -> str:
        return str(
            _env_or("ONOATS_SPEAKER_ME", self.raw.get("speakers", {}).get("me"))
            or _DEFAULTS["speakers"]["me"]
        )

    @property
    def speaker_label_them(self) -> str:
        return str(
            _env_or("ONOATS_SPEAKER_THEM", self.raw.get("speakers", {}).get("them"))
            or _DEFAULTS["speakers"]["them"]
        )

    def speaker_labels(self) -> dict[str, str]:
        """Map the canonical ``me``/``them`` source enum to display labels.

        RENDER-ONLY — never written into the queue. The converter (Phase 3)
        consumes this; the JSONL ``source`` field stays ``me``/``them``.
        """
        return {"me": self.speaker_label_me, "them": self.speaker_label_them}

    # ---- categories ----
    @property
    def category_set(self) -> set[str]:
        env = os.environ.get("ONOATS_CATEGORIES", "").strip()
        if env:
            cats = [c.strip().lower() for c in env.split(",") if c.strip()]
        else:
            raw = self.raw.get("categories", {}).get("set")
            cats = (
                [str(c).strip().lower() for c in raw if str(c).strip()]
                if isinstance(raw, list)
                else []
            )
        result = set(cats) if cats else set(_DEFAULTS["categories"]["set"])
        result.add("uncategorized")
        return result

    # ---- tuning ----
    def _tuning_float(self, key: str, env_name: str) -> float:
        val = _env_or(env_name, self.raw.get("tuning", {}).get(key))
        if val is None:
            return float(_DEFAULTS["tuning"][key])
        try:
            return float(val)
        except (TypeError, ValueError):
            return float(_DEFAULTS["tuning"][key])

    @property
    def silence_timeout_sec(self) -> float:
        return self._tuning_float("silence_timeout_sec", "SILENCE_TIMEOUT_SEC")

    @property
    def segment_hint_threshold(self) -> float:
        return self._tuning_float("segment_hint_threshold", "SEGMENT_HINT_THRESHOLD")

    @property
    def audio_heartbeat_sec(self) -> float:
        return self._tuning_float("audio_heartbeat_sec", "ONOATS_AUDIO_HEARTBEAT_SEC")

    # ---- secrets ----
    def get_secret(self, name: str, default: str | None = None) -> str | None:
        """Return a secret: process env wins, then secrets.env, else default."""
        env_val = os.getenv(name, "")
        if env_val.strip():
            return env_val
        file_val = (self.secrets.get(name) or "").strip()
        return file_val or default

    def require_secret(
        self,
        name: str,
        *,
        validate: Callable[[str], bool] | None = None,
        hint: str | None = None,
    ) -> str:
        """Return a secret or raise RuntimeError if missing/invalid.

        Resolves through the env > secrets.env precedence so onoats reads STT
        secrets from the consolidated ``secrets.env``.
        """
        val = (self.get_secret(name) or "").strip()
        if not val:
            msg = f"Missing required secret: {name}"
            if hint:
                msg += f" — {hint}"
            raise RuntimeError(msg)
        if validate and not validate(val):
            raise RuntimeError(
                f"Secret {name} is set but looks invalid (failed validation)"
            )
        return val


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        logger.warning(f"config: could not parse {path}: {exc} — using defaults")
        return {}


def _load_secrets(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return {k: v for k, v in dotenv_values(path).items() if v is not None}
    except OSError as exc:
        logger.warning(f"config: could not read {path}: {exc}")
        return {}


def load_config(
    *,
    config_path: Path | None = None,
    secrets_path: Path | None = None,
) -> OnoatsConfig:
    """Load ``config.toml`` + ``secrets.env`` into an :class:`OnoatsConfig`.

    A missing config.toml yields defaults (no crash). Env vars override the
    file at access time via the property accessors.
    """
    cfg = _load_toml(config_path or config_toml_path())
    secrets = _load_secrets(secrets_path or secrets_env_path())
    return OnoatsConfig(raw=cfg, secrets=secrets)
