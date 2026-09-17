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

import pytest

from onoats import runtime
from onoats.config import OnoatsConfig, validate_launchd_label

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


def test_rss_probe_redacts_leaky_connect_exception_text(monkeypatch):
    """Round 8 finding 2 (the missed 5th call site): `log_stt_server_rss`
    builds its own `TranscriptionClient` from the same `STT_WS_URI` and
    calls `connect()` — the same `InvalidURI`-shaped userinfo-leak exposure
    as `_preflight_stt_ws`/`_kickstart_and_retry`/`_ensure_connected`, but
    its `except Exception as exc: logger.debug(f"...({exc})")` interpolated
    the raw exception text instead of routing it through `_safe_exc_text`.
    Pin that the probe's debug log no longer leaks the credential.
    """
    import asyncio

    import stt_server.client as stt_client

    monkeypatch.delenv("STT_WS_SOCKET", raising=False)
    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(raw={"stt": {"service": "websocket"}}),
    )

    leaky_uri = "ws://secretuser:hunter2@stt.example.internal/v1"

    class _InvalidURILikeError(Exception):
        def __init__(self, uri: str, msg: str):
            super().__init__(f"{uri} isn't a valid URI: {msg}")

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            raise _InvalidURILikeError(leaky_uri, "bad")

        async def close_session(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(stt_client, "TranscriptionClient", _FakeClient)

    logged: list[str] = []
    monkeypatch.setattr(runtime.logger, "debug", lambda msg: logged.append(msg))

    asyncio.run(runtime.log_stt_server_rss("startup"))

    assert logged, "expected the probe-failed debug log to fire"
    combined = "\n".join(logged)
    assert "secretuser" not in combined
    assert "hunter2" not in combined
    assert "isn't a valid URI" in combined


def test_rss_probe_teardown_is_deadline_bounded(monkeypatch):
    """Round-4 finding 5: `log_stt_server_rss`'s shutdown-probe teardown
    called `_closing.close_quietly(client)` with NO deadline, so its two
    fixed-`CLOSE_TIMEOUT_SEC` (5.0s each) closers could cost up to ~10s
    against an unreachable server — on top of the ~2s the probe's own
    `wait_for` already spent — despite this function's own docstring
    calling the probe "2s-bounded". A `close_session`/`close` pair that
    each hang past the bound must not push total elapsed anywhere near
    the old ~12s; the fix caps teardown to the same
    `_RSS_PROBE_TIMEOUT_SEC` window the probe itself gets.
    """
    import asyncio
    import time

    import stt_server.client as stt_client

    monkeypatch.delenv("STT_WS_SOCKET", raising=False)
    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(raw={"stt": {"service": "websocket"}}),
    )
    monkeypatch.setattr(runtime, "_RSS_PROBE_TIMEOUT_SEC", 0.05)

    class _HangingCloseClient:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            raise OSError("no server in test")  # probe fails fast

        async def close_session(self):
            await asyncio.sleep(10)

        async def close(self):
            await asyncio.sleep(10)

    monkeypatch.setattr(stt_client, "TranscriptionClient", _HangingCloseClient)

    started = time.monotonic()
    asyncio.run(runtime.log_stt_server_rss("startup"))
    elapsed = time.monotonic() - started

    # Old behaviour: unbounded teardown could burn ~10s (2 closers x
    # CLOSE_TIMEOUT_SEC=5.0) on top of the probe's own ~0.05s. Bounded to
    # the probe's own window, total elapsed must stay well under 1s.
    assert elapsed < 1.0


def test_rss_probe_events_generator_is_explicitly_closed(monkeypatch):
    """Round-4 finding 5: the probe's `async for event in client.events()`
    left the underlying async generator without an explicit `aclose()` when
    `wait_for` cancels the probe (timeout) — cancellation unwinds the
    generator's own suspended frame but does not mark it closed. Assert the
    generator instance the probe iterates is explicitly closed."""
    import asyncio

    import stt_server.client as stt_client

    monkeypatch.delenv("STT_WS_SOCKET", raising=False)
    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(raw={"stt": {"service": "websocket"}}),
    )

    closed = {"n": 0}

    class _TrackingEvents:
        def __aiter__(self):
            return self

        async def __anext__(self):
            # Never yields a `server.status` event — forces the probe to
            # fall through to "status reply missing" without ever
            # `return`-ing out of the `async for` early.
            raise StopAsyncIteration

        async def aclose(self):
            closed["n"] += 1

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            pass

        async def status(self):
            pass

        def events(self):
            return _TrackingEvents()

        async def close_session(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(stt_client, "TranscriptionClient", _FakeClient)

    asyncio.run(runtime.log_stt_server_rss("startup"))

    assert closed["n"] == 1


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


# ---------------------------------------------------------------------------
# Round-2 review-gauntlet fix (finding 16): label allowlist at resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "gui/501/some.other.job",
        "label; forged",
        "label: forged",
        "bad\x00label",
    ],
)
def test_malformed_launchd_label_resolves_to_none(monkeypatch, bad):
    """The label is env/config-sourced and lands in two delimiter-sensitive
    places: launchctl's `gui/<uid>/<label>` service target, and the status
    `warning` string whose merge splits on exactly `"; "` and `": "`.
    Validate once, here, at the single resolution point; a non-conforming
    value is treated as absent (self-healing simply off)."""
    monkeypatch.delenv("STT_LAUNCHD_LABEL", raising=False)
    cfg = OnoatsConfig(raw={"stt": {"launchd_label": bad}})
    assert cfg.stt_launchd_label is None


def test_malformed_launchd_label_from_env_also_resolves_to_none(monkeypatch):
    monkeypatch.setenv("STT_LAUNCHD_LABEL", "gui/0/evil")
    cfg = OnoatsConfig(raw={"stt": {"launchd_label": "pipecat.stt-server"}})
    assert cfg.stt_launchd_label is None


def test_wellformed_launchd_label_still_resolves(monkeypatch):
    monkeypatch.delenv("STT_LAUNCHD_LABEL", raising=False)
    cfg = OnoatsConfig(raw={"stt": {"launchd_label": "pipecat.stt-server.nemotron"}})
    assert cfg.stt_launchd_label == "pipecat.stt-server.nemotron"


# ---------------------------------------------------------------------------
# Round-4 review-gauntlet fix (finding 1): reject non-string values before
# coercion, not after
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [True, False, 42, 3.14, ["a"], {"x": 1}])
def test_non_string_launchd_label_config_resolves_to_none(monkeypatch, bad):
    """A typed `config.toml` value (`launchd_label = true`, `= 42`, ...) must
    be rejected on TYPE before the old `str(val).strip()` coercion, which
    would otherwise turn e.g. `true` into the string "True" -- a value that
    passes `_LAUNCHD_LABEL_RE` and would silently enable kickstart against a
    value the user never intended as a launchd label."""
    monkeypatch.delenv("STT_LAUNCHD_LABEL", raising=False)
    cfg = OnoatsConfig(raw={"stt": {"launchd_label": bad}})
    assert cfg.stt_launchd_label is None


def test_non_string_launchd_label_config_is_ignored_even_when_stringlike(
    monkeypatch,
):
    """`true` coerced via `str()` becomes "True", which IS well-formed per
    `_LAUNCHD_LABEL_RE` -- pinning this specifically guards against a fix
    that only rejects labels that would otherwise fail the regex."""
    monkeypatch.delenv("STT_LAUNCHD_LABEL", raising=False)
    cfg = OnoatsConfig(raw={"stt": {"launchd_label": True}})
    assert cfg.stt_launchd_label is None
    assert validate_launchd_label("True") == "True"  # regex alone would accept it


# --- launchd label validation (moved here from tests/test_stt_launchd.py:
# the validator is config-value validation and now lives in onoats.config,
# so `config` no longer imports the STT subsystem it configures) ---------


@pytest.mark.parametrize(
    "label",
    [
        "pipecat.stt-server",
        "pipecat.stt-server.nemotron",
        "a",
        "A0_-.",
    ],
)
def test_validate_launchd_label_accepts_wellformed(label):
    assert validate_launchd_label(label) == label


@pytest.mark.parametrize(
    "label",
    [
        "gui/501/other.job",  # redirects the kickstart at a different job
        "evil; stt",  # forges a "; "-separated pseudo-branch in `warning`
        "evil: pwned",  # forges a ": "-keyed pseudo-branch in `warning`
        "bad\x00label",  # reaches subprocess.run as a ValueError
        ".leading-dot",  # must start alphanumeric
        "",
        "x" * 129,
    ],
)
def test_validate_launchd_label_rejects_malformed(label):
    """Finding 16: the label is interpolated into launchctl's
    `gui/<uid>/<label>` service target AND into a status warning message
    whose merge format splits on exactly `"; "` and `": "`. Validate once at
    resolution; a non-conforming value is treated as absent (self-healing
    off), never passed through."""
    assert validate_launchd_label(label) is None


def test_validate_launchd_label_passes_none_through():
    assert validate_launchd_label(None) is None


def test_validate_launchd_label_rejects_trailing_newline():
    """Round-2 finding 19: `re.match` with a `$` anchor also matches just
    before a trailing newline, so `"label\\n"` passed a validator that
    documents itself as a self-sufficient trust boundary. `fullmatch` closes
    it without depending on every caller stripping first."""
    assert validate_launchd_label("pipecat.stt-server\n") is None
    assert validate_launchd_label("pipecat.stt-server\n\n") is None
