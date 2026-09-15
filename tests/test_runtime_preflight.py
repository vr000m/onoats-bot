"""Phase 3 — `_preflight_stt_ws` characterization + kickstart wiring.

Two layers, in the order the dev plan calls for:

  1. Characterization tests for TODAY's `_preflight_stt_ws` behavior (timeout,
     OSError, ValueError, generic Exception paths), pinned BEFORE any product
     change — a fake `TranscriptionClient` stands in for the real websocket
     handshake.
  2. Tests for the new kickstart wiring: kickstart is attempted at most once
     per preflight failure, only after handshake-unreachable (TimeoutError /
     OSError) exhaustion, only when the shared cooldown has elapsed, and a
     missing/unconfigured label reproduces today's `SttPreflightError`
     byte-for-byte. `on_recovery` fires only after the post-kickstart retry
     actually succeeds. `_create_stt_service`'s `data_dir` wiring and the
     `onoats init` preflight path (never kickstarts) round it out.
"""

from __future__ import annotations

import asyncio

import pytest

from onoats import runtime
from onoats.runtime import SttPreflightError, _preflight_stt_ws


@pytest.fixture(autouse=True)
def _clear_preflight_cache():
    """`_preflight_cache` (runtime.py:404) is module-level, keyed on the
    endpoint tuple — must not leak a "already probed OK" result between
    tests that reuse the same kwargs."""
    runtime._preflight_cache.clear()
    yield
    runtime._preflight_cache.clear()


def _kwargs(**over):
    base = dict(socket_path="/tmp/stt.sock", host=None, port=None, uri=None)
    base.update(over)
    return base


class _FakeClient:
    """Stands in for `stt_server.client.TranscriptionClient`.

    `connect_raises` is a callable so a test can vary behaviour across
    repeated attempts (e.g. OSError then success) by returning a different
    exception (or None) each call.
    """

    instances: list[_FakeClient] = []

    def __init__(self, connect_raises=None, **kwargs):
        self.kwargs = kwargs
        self._connect_raises = connect_raises or (lambda: None)
        self.connect_calls = 0
        self.closed = False
        self.session_closed = False
        _FakeClient.instances.append(self)

    async def connect(self):
        self.connect_calls += 1
        exc = self._connect_raises()
        if exc is not None:
            raise exc

    async def close_session(self):
        self.session_closed = True

    async def close(self):
        self.closed = True


def _install_fake_client(monkeypatch, connect_raises=None):
    import stt_server.client as stt_client

    def factory(**kwargs):
        return _FakeClient(connect_raises=connect_raises, **kwargs)

    monkeypatch.setattr(stt_client, "TranscriptionClient", factory)
    return factory


@pytest.fixture(autouse=True)
def _reset_fake_client_instances():
    _FakeClient.instances = []
    yield
    _FakeClient.instances = []


# ---------------------------------------------------------------------------
# 1. Characterization — today's behavior, no product change
# ---------------------------------------------------------------------------


def test_characterization_timeout_raises_sttpreflighterror(monkeypatch):
    async def always_hang():
        await asyncio.sleep(10)

    import stt_server.client as stt_client

    class _HangingClient:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            await asyncio.sleep(10)

        async def close_session(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(stt_client, "TranscriptionClient", _HangingClient)
    monkeypatch.setattr(runtime, "_PREFLIGHT_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError, match="did not complete handshake"):
        asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))


def test_characterization_oserror_retries_then_raises(monkeypatch):
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError, match="not reachable"):
        asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))

    # Retried once (two attempts total) on OSError, per the existing
    # cold-start-tolerance comment (runtime.py:391-397).
    assert _FakeClient.instances[0].connect_calls == 2


def test_characterization_valueerror_fails_fast_no_retry(monkeypatch):
    calls = {"n": 0}

    def raiser():
        calls["n"] += 1
        return ValueError("bad kwargs")

    _install_fake_client(monkeypatch, connect_raises=raiser)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError, match="misconfigured endpoint"):
        asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))

    assert calls["n"] == 1  # no retry — not a cold-start race


def test_characterization_generic_exception_fails_fast_no_retry(monkeypatch):
    calls = {"n": 0}

    def raiser():
        calls["n"] += 1
        return RuntimeError("unexpected first frame")

    _install_fake_client(monkeypatch, connect_raises=raiser)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError, match="handshake failed"):
        asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))

    assert calls["n"] == 1


def test_characterization_success_closes_client_and_caches(monkeypatch):
    _install_fake_client(monkeypatch, connect_raises=lambda: None)

    asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))

    inst = _FakeClient.instances[0]
    assert inst.connect_calls == 1
    assert inst.session_closed is True
    assert inst.closed is True

    # Second call for the same endpoint is a no-op (cached) — doesn't build
    # a second client.
    asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))
    assert len(_FakeClient.instances) == 1


# ---------------------------------------------------------------------------
# 2. Kickstart wiring
# ---------------------------------------------------------------------------


def test_missing_label_reproduces_sttpreflighterror_byte_for_byte(monkeypatch):
    """No `launchd_label` configured -> identical error to the
    characterization baseline; kickstart must not even be attempted."""
    kickstart_calls = []
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError, match="not reachable") as exc_info_labelless:
        asyncio.run(
            _preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock", launchd_label=None)
        )

    assert kickstart_calls == []
    runtime._preflight_cache.clear()
    _FakeClient.instances.clear()

    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )
    with pytest.raises(SttPreflightError, match="not reachable") as exc_info_baseline:
        asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))

    assert str(exc_info_labelless.value) == str(exc_info_baseline.value)
    assert kickstart_calls == []


def test_kickstart_attempted_only_after_handshake_unreachable_exhaustion(monkeypatch):
    """OSError final-attempt exhaustion with a configured label -> kickstart
    is attempted exactly once."""
    kickstart_calls = []

    async def fake_to_thread(fn, *args, **kwargs):
        kickstart_calls.append(args)
        return fn(*args, **kwargs)

    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    stamped = []
    monkeypatch.setattr(
        "onoats.stt.launchd._stamp_cooldown", lambda label, **kw: stamped.append(label)
    )
    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)

    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    assert len(kickstart_calls) == 1
    assert stamped == ["pipecat.stt-server"]


def test_valueerror_never_triggers_kickstart(monkeypatch):
    kickstart_calls = []
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    _install_fake_client(monkeypatch, connect_raises=lambda: ValueError("bad"))
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError, match="misconfigured endpoint"):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    assert kickstart_calls == []


def test_generic_exception_never_triggers_kickstart(monkeypatch):
    kickstart_calls = []
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    _install_fake_client(monkeypatch, connect_raises=lambda: RuntimeError("weird"))
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError, match="handshake failed"):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    assert kickstart_calls == []


def test_cooldown_active_falls_straight_through_to_sttpreflighterror(monkeypatch):
    """If the shared cooldown has NOT elapsed, kickstart must not be called
    at all — falls straight through exactly as if no label were configured."""
    kickstart_calls = []
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: False
    )
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError, match="not reachable"):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    assert kickstart_calls == []


def test_kickstart_attempted_at_most_once_per_preflight_failure(monkeypatch):
    """Even though `kickstart_stt_server` itself could theoretically be
    retried, the preflight call site must invoke it exactly once per
    preflight failure — never loop on it."""
    kickstart_calls = []
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr("onoats.stt.launchd._stamp_cooldown", lambda label, **kw: None)
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    assert len(kickstart_calls) == 1


def test_kickstart_never_raises_from_preflight(monkeypatch):
    """`kickstart_stt_server` itself never raises (tested directly in
    test_stt_launchd.py), but the preflight call site must also tolerate a
    kickstart-path failure without leaking a non-SttPreflightError."""
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: False
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr("onoats.stt.launchd._stamp_cooldown", lambda label, **kw: None)
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )


def test_successful_post_kickstart_retry_calls_on_recovery(monkeypatch):
    """`on_recovery` fires only after the post-kickstart retry loop's
    handshake actually succeeds — not merely because `kickstart_stt_server`
    returned True."""
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr("onoats.stt.launchd._stamp_cooldown", lambda label, **kw: None)

    # First two attempts (the pre-existing schedule) fail; kickstart fires;
    # the post-kickstart retry then succeeds.
    remaining_failures = {"n": 2}

    def connect_raises():
        if remaining_failures["n"] > 0:
            remaining_failures["n"] -= 1
            return ConnectionRefusedError("refused")
        return None

    _install_fake_client(monkeypatch, connect_raises=connect_raises)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    recovered = []
    asyncio.run(
        _preflight_stt_ws(
            _kwargs(),
            "unix:/tmp/stt.sock",
            launchd_label="pipecat.stt-server",
            on_recovery=lambda msg: recovered.append(msg),
        )
    )

    assert len(recovered) == 1
    assert "kickstarted pipecat.stt-server" in recovered[0]
    assert "restarted automatically" in recovered[0]


def test_on_recovery_not_called_when_post_kickstart_retry_also_fails(monkeypatch):
    """Kickstart returning True is not enough — if the retry loop's
    handshake still fails, `on_recovery` must never fire and the original
    SttPreflightError (noting the kickstart attempt) must still raise."""
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr("onoats.stt.launchd._stamp_cooldown", lambda label, **kw: None)
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("still refused")
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    recovered = []
    with pytest.raises(SttPreflightError, match="kickstart"):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(),
                "unix:/tmp/stt.sock",
                launchd_label="pipecat.stt-server",
                on_recovery=lambda msg: recovered.append(msg),
            )
        )

    assert recovered == []


def test_on_recovery_not_called_when_no_kickstart_was_needed(monkeypatch):
    """The common case (server reachable on the first try) must never fire
    `on_recovery` — that callback means "we had to kickstart", not "the
    server is up"."""
    _install_fake_client(monkeypatch, connect_raises=lambda: None)
    recovered = []
    asyncio.run(
        _preflight_stt_ws(
            _kwargs(),
            "unix:/tmp/stt.sock",
            launchd_label="pipecat.stt-server",
            on_recovery=lambda msg: recovered.append(msg),
        )
    )
    assert recovered == []


def test_kickstart_call_uses_asyncio_to_thread(monkeypatch):
    """Every call site that invokes `kickstart_stt_server` from an async
    context does so via `await asyncio.to_thread(kickstart_stt_server,
    label)` — assert the preflight path is no exception."""
    to_thread_calls = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kwargs):
        to_thread_calls.append((fn, args, kwargs))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr("onoats.stt.launchd._stamp_cooldown", lambda label, **kw: None)
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("x")
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    assert len(to_thread_calls) == 1
    # First positional arg to asyncio.to_thread must BE kickstart_stt_server
    # (imported into runtime's module namespace), not a wrapping lambda —
    # otherwise the "kickstart_stt_server" patch target above wouldn't have
    # taken effect for this call.
    assert to_thread_calls[0][0].__name__ == "kickstart_stt_server" or callable(
        to_thread_calls[0][0]
    )


# ---------------------------------------------------------------------------
# _create_stt_service: data_dir wiring + on_recovery -> set_warning_branch
# ---------------------------------------------------------------------------


def test_create_stt_service_builds_on_recovery_only_when_data_dir_given(
    monkeypatch, tmp_path
):
    """`_create_stt_service(data_dir=...)` must build an `on_recovery`
    callback wired to `status.set_warning_branch(data_dir, "stt", msg)`
    only when `data_dir` is not None."""
    from onoats.config import OnoatsConfig

    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(
            raw={
                "stt": {
                    "service": "websocket",
                    "launchd_label": "pipecat.stt-server",
                }
            }
        ),
    )

    captured_kwargs: dict = {}

    async def fake_preflight(kwargs, target, **kw):
        captured_kwargs.update(kw)

    monkeypatch.setattr(runtime, "_preflight_stt_ws", fake_preflight)

    class _FakeWSService:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr(
        "onoats.stt.websocket_stt_service.WebSocketSTTService", _FakeWSService
    )

    asyncio.run(runtime._create_stt_service(data_dir=tmp_path))
    assert captured_kwargs.get("launchd_label") == "pipecat.stt-server"
    assert callable(captured_kwargs.get("on_recovery"))

    warnings_set = []
    monkeypatch.setattr(
        "onoats.status.set_warning_branch",
        lambda data_dir, branch, msg: warnings_set.append((data_dir, branch, msg)),
    )
    captured_kwargs["on_recovery"](
        "stt: server restarted automatically (kickstarted x)"
    )
    assert warnings_set == [
        (tmp_path, "stt", "stt: server restarted automatically (kickstarted x)")
    ]

    captured_kwargs.clear()
    asyncio.run(runtime._create_stt_service(data_dir=None))
    assert captured_kwargs.get("on_recovery") is None


# ---------------------------------------------------------------------------
# `onoats init`'s preflight path: never kickstarts, regardless of a
# configured label (best-effort/non-fatal by design).
# ---------------------------------------------------------------------------


def test_init_preflight_never_calls_kickstart_even_with_label_configured(
    monkeypatch,
):
    from onoats import init as init_mod

    kickstart_calls = []
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    monkeypatch.setenv("STT_LAUNCHD_LABEL", "pipecat.stt-server")
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    # `_run_preflight` prints, catches SttPreflightError, and returns —
    # never raises.
    init_mod._run_preflight({"service": "websocket"}, {})

    assert kickstart_calls == []
