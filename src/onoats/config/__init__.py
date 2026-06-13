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

    [storage]   data_dir = "..."                       # recorder data root (else XDG)
    [devices]   mic = "...", system = "..."           # by stable device name
    [stt]       service = "...", model = "...", language = "en"|"auto"|<code>,
                ws_socket/ws_host/ws_port/ws_uri = "..."
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
    "audio": {"source": "portaudio"},
    "stt": {"service": "whisper", "model": "", "language": "en"},
    "speakers": {"me": "Me", "them": "Them"},
    "categories": {"set": ["uncategorized"]},
    "tuning": {
        "silence_timeout_sec": 300.0,
        "segment_hint_threshold": 120.0,
        "audio_heartbeat_sec": 0.0,
    },
}


def _env_or_with_source(env_name: str, file_value: Any) -> tuple[Any, str]:
    """Resolve a value and its provenance in one place.

    Encodes the precedence (env var > config.toml > default) exactly once so
    the resolved value and the "where did this come from" label can never
    drift apart. Returns ``(value, label)`` where ``label`` is one of
    "from env" / "from config" / "default".
    """
    env = os.environ.get(env_name, "")
    if env.strip():
        return env, "from env"
    if file_value not in (None, ""):
        return file_value, "from config"
    # file_value is None or "" here — caller falls through to a built-in default.
    return file_value, "default"


def _env_or(env_name: str, file_value: Any) -> Any:
    """Process env var wins; else the config.toml value; else None."""
    return _env_or_with_source(env_name, file_value)[0]


def _source_of(env_name: str, file_value: Any) -> str:
    """Provenance label for the value :func:`_env_or` would return."""
    return _env_or_with_source(env_name, file_value)[1]


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

    @property
    def mic_device_source(self) -> str:
        """Where ``mic_device`` resolved from: "from env" / "from config" / "default"."""
        return _source_of("MIC_INPUT_DEVICE", self.raw.get("devices", {}).get("mic"))

    @property
    def system_device_source(self) -> str:
        """Where ``system_device`` resolved from: "from env" / "from config" / "default"."""
        return _source_of(
            "SYSTEM_INPUT_DEVICE", self.raw.get("devices", {}).get("system")
        )

    # ---- audio source ----
    @property
    def audio_source(self) -> str:
        """Capture backend: ``portaudio`` (default) or ``socket``.

        env ``AUDIO_SOURCE`` > config.toml ``[audio].source`` > default
        ``portaudio``. ``socket`` reads framed PCM16 from the per-branch unix
        sockets (``mic_socket`` / ``system_socket``) instead of PortAudio
        devices; the default keeps today's PortAudio path unchanged.
        """
        return (
            str(
                _env_or("AUDIO_SOURCE", self.raw.get("audio", {}).get("source"))
                or _DEFAULTS["audio"]["source"]
            )
            .lower()
            .strip()
        )

    @property
    def mic_socket(self) -> str | None:
        """Unix socket path for the ``me`` / mic branch under ``AUDIO_SOURCE=socket``.

        env ``ONOATS_MIC_SOCKET`` > config.toml ``[audio].mic_socket``. ``None``
        when neither set — socket mode then refuses to start without a path.
        """
        return _env_or("ONOATS_MIC_SOCKET", self.raw.get("audio", {}).get("mic_socket"))

    @property
    def system_socket(self) -> str | None:
        """Unix socket path for the ``them`` / system branch under ``AUDIO_SOURCE=socket``.

        env ``ONOATS_SYSTEM_SOCKET`` > config.toml ``[audio].system_socket``.
        ``None`` when neither set — socket mode then refuses to start.
        """
        return _env_or(
            "ONOATS_SYSTEM_SOCKET", self.raw.get("audio", {}).get("system_socket")
        )

    @property
    def capturer_nonce(self) -> str | None:
        """Generation nonce the capturer must echo in its handshake (socket mode).

        env ``ONOATS_CAPTURER_NONCE`` (set by the Phase-3 supervisor per launch)
        > config.toml ``[audio].capturer_nonce``. ``None`` when unset — the
        transport then does not gate on the nonce (e.g. socket mode driven
        without the supervisor). When the supervisor exports it, a capturer that
        handshakes with a missing or stale nonce is rejected.
        """
        return _env_or(
            "ONOATS_CAPTURER_NONCE", self.raw.get("audio", {}).get("capturer_nonce")
        )

    # ---- storage ----
    @property
    def data_dir(self) -> str | None:
        """Recorder data root: env ``ONOATS_DATA_DIR`` > config.toml ``[storage].data_dir``.

        ``None`` falls through to the XDG default in ``_vendor/store.py``.
        Setting this lets onoats write its queue into another tree (e.g.
        ``~/koda-data``) so a downstream worker drains the same ``sessions/``.
        """
        return _env_or("ONOATS_DATA_DIR", self.raw.get("storage", {}).get("data_dir"))

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

    @property
    def stt_language(self) -> str:
        """Decode language for the whisper/websocket backends.

        env ``STT_LANGUAGE`` > env ``STT_WS_LANGUAGE`` (legacy alias) >
        config.toml ``[stt].language`` > ``"en"``. The bare name matches the
        other cross-backend vars (``STT_SERVICE`` / ``STT_MODEL``); the
        ``STT_WS_``-prefixed alias predates the key applying beyond the
        websocket backend and is kept for backward compatibility.
        ``"auto"`` (any case) means auto-detect — the runtime maps it to
        ``None`` for the backend, never the literal string (whisper rejects
        a literal "auto"). Not consumed by the Deepgram backend.
        """
        # strip-then-default: a whitespace-only file value must fall back to
        # "en", not reach the backend as language="" (env values are already
        # strip-guarded inside _env_or).
        val = str(
            _env_or(
                "STT_LANGUAGE",
                _env_or("STT_WS_LANGUAGE", self.raw.get("stt", {}).get("language")),
            )
            or ""
        ).strip()
        return val or _DEFAULTS["stt"]["language"]

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
    # Note: the shutdown timers (SHUTDOWN_DRAIN_TIMEOUT_SEC /
    # SHUTDOWN_CANCEL_TIMEOUT_SEC) are intentionally NOT exposed here — they are
    # env-only operator escape hatches read raw in ``onoats.runtime`` (which
    # never loads config), matching how ``__main__`` reads SILENCE_TIMEOUT_SEC.
    # Don't add ``_tuning_float`` accessors for them without also routing the
    # runtime reads through config, or you create a second config path.
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
