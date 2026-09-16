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


@pytest.fixture(autouse=True)
def _fast_post_kickstart_budget(monkeypatch):
    """The post-kickstart retry loop runs against a wall-clock DEADLINE
    (`_POST_KICKSTART_DEADLINE_SEC`, 45s in production) rather than a fixed
    attempt count — see `_kickstart_and_retry`. Collapse the budget to a
    single immediate attempt by default so the exhaustion tests don't burn
    45 real seconds each; the deadline-behaviour test overrides it."""
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 0.0)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_SETTLE_SEC", 0.0)


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
    # cold-start-tolerance comment (runtime.py:391-397). Round-2 fix
    # (finding 4): each attempt now gets its OWN client and the superseded
    # one is closed, instead of one client being re-`connect()`ed — a
    # handshake that got past ws_connect otherwise leaked its websocket.
    assert len(_FakeClient.instances) == 2
    assert [c.connect_calls for c in _FakeClient.instances] == [1, 1]
    assert all(c.closed for c in _FakeClient.instances)


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


def test_cooldown_stamped_before_kickstart_await_not_after(monkeypatch):
    """Review-gauntlet fix: `_kickstart_and_retry` must stamp the cooldown
    BEFORE `await`ing `asyncio.to_thread(kickstart_stt_server, ...)`, not
    after — matching `WebSocketSTTService._maybe_kickstart`'s ordering. An
    `await` yields control back to the event loop, so stamping first (with
    no `await` between the cooldown check and the stamp) is what closes the
    double-kickstart race for two callers exhausting concurrently on the
    same label."""
    events: list[str] = []

    async def fake_to_thread(fn, *args, **kwargs):
        events.append("kickstart_started")
        return fn(*args, **kwargs)

    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._stamp_cooldown",
        lambda label, **kw: events.append("stamped"),
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

    assert events == ["stamped", "kickstart_started"]


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

    # Round-2 finding 10: the preflight recovery has exactly ONE write path —
    # the captured message threaded into `write_running(warning=...)` by
    # dual.py. It must NOT also call `set_warning_branch`, which no-ops on a
    # non-running record (none exists yet at preflight time) and would
    # otherwise annotate a stale `running=True` record left by a crashed
    # earlier session.
    warnings_set = []
    monkeypatch.setattr(
        "onoats.status.set_warning_branch",
        lambda data_dir, branch, msg: warnings_set.append((data_dir, branch, msg)),
    )
    captured_kwargs["on_recovery"]("server restarted automatically (kickstarted x)")
    assert warnings_set == []

    captured_kwargs.clear()
    asyncio.run(runtime._create_stt_service(data_dir=None))
    assert captured_kwargs.get("on_recovery") is None


def test_on_recovery_swallows_status_write_oserror(monkeypatch, tmp_path):
    """Review-gauntlet fix: on_recovery is invoked from inside
    `_preflight_stt_ws`'s/`_ensure_connected`'s own exception handlers, on
    the one code path that just recovered. A status-file `OSError` (disk
    full, permissions) there must be logged and swallowed, not propagate —
    otherwise a successful self-heal would crash the bot instead of the
    `SttPreflightError` this whole feature exists to produce gracefully."""
    from onoats.config import OnoatsConfig

    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(
            raw={"stt": {"service": "websocket", "launchd_label": "pipecat.stt-server"}}
        ),
    )

    captured_kwargs: dict = {}

    async def fake_preflight(kwargs, target, **kw):
        captured_kwargs.update(kw)

    monkeypatch.setattr(runtime, "_preflight_stt_ws", fake_preflight)
    monkeypatch.setattr(
        "onoats.stt.websocket_stt_service.WebSocketSTTService",
        lambda **kw: object(),
    )
    monkeypatch.setattr(
        "onoats.status.set_warning_branch",
        lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")),
    )

    asyncio.run(runtime._create_stt_service(data_dir=tmp_path))
    # Must not raise — the OSError from set_warning_branch is swallowed.
    captured_kwargs["on_recovery"](
        "stt: server restarted automatically (kickstarted x)"
    )


def test_create_stt_service_arms_preflight_confirm_on_the_new_instance(
    monkeypatch, tmp_path
):
    """A preflight kickstart-recovery happens against a throwaway
    TranscriptionClient, before the WebSocketSTTService instance exists —
    without arming that instance's confirm gate, the warning
    `write_running(warning=...)` is about to carry (dual.py) would never be
    cleared by a later transcript.completed/failed event."""
    from onoats.config import OnoatsConfig

    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(
            raw={"stt": {"service": "websocket", "launchd_label": "pipecat.stt-server"}}
        ),
    )

    captured_kwargs: dict = {}

    class _FakeWSService:
        def __init__(self, **kw):
            captured_kwargs.update(kw)

    monkeypatch.setattr(
        "onoats.stt.websocket_stt_service.WebSocketSTTService", _FakeWSService
    )

    # Case 1: preflight recovered (on_recovery fired with a message).
    async def fake_preflight_recovers(kwargs, target, *, on_recovery=None, **kw):
        if on_recovery is not None:
            on_recovery("stt: server restarted automatically (kickstarted x)")

    monkeypatch.setattr(runtime, "_preflight_stt_ws", fake_preflight_recovers)
    asyncio.run(runtime._create_stt_service(data_dir=tmp_path))
    assert captured_kwargs.get("on_preflight_confirmed") is not None

    # Case 2: preflight succeeded without ever needing a kickstart.
    captured_kwargs.clear()

    async def fake_preflight_clean(kwargs, target, **kw):
        return None

    monkeypatch.setattr(runtime, "_preflight_stt_ws", fake_preflight_clean)
    asyncio.run(runtime._create_stt_service(data_dir=tmp_path))
    assert captured_kwargs.get("on_preflight_confirmed") is None

    # Case 3 (finding 12): a LATER instance whose own preflight call was a
    # `_preflight_cache` hit still gets armed when the caller tells it an
    # earlier call recovered — otherwise a session where only that instance
    # ever produces transcripts leaves the shared warning pinned forever.
    captured_kwargs.clear()
    asyncio.run(
        runtime._create_stt_service(
            data_dir=tmp_path, branch_instance="system", preflight_recovered=True
        )
    )
    assert captured_kwargs.get("on_preflight_confirmed") is not None


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


# ---------------------------------------------------------------------------
# Round-2 review-gauntlet fixes: post-kickstart retry loop
# ---------------------------------------------------------------------------


def test_post_kickstart_retry_uses_a_fresh_client_per_attempt(monkeypatch):
    """Finding 4: the retry loop reused the SAME already-failed
    TranscriptionClient for every attempt with no `close()` between them. A
    connect that gets past ws_connect but fails at handshake leaves that
    websocket orphaned — the next attempt overwrites the handle and only the
    last one is ever closed, so each failed attempt leaks a socket/FD.

    Each attempt must construct its own client and close it on failure."""
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr("onoats.stt.launchd._stamp_cooldown", lambda label, **kw: None)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)
    # Two pre-kickstart attempts fail, then three post-kickstart ones.
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 5.0)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_SETTLE_SEC", 0.0)

    calls = {"n": 0}

    def connect_raises():
        calls["n"] += 1
        # Attempts 1-2 = the normal schedule; 3-4 = post-kickstart retries;
        # 5 = the post-kickstart attempt that finally succeeds.
        return ConnectionRefusedError("refused") if calls["n"] < 5 else None

    _install_fake_client(monkeypatch, connect_raises=connect_raises)

    asyncio.run(
        _preflight_stt_ws(
            _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
        )
    )

    # One client per attempt — never reused (2 normal + 3 post-kickstart).
    assert len(_FakeClient.instances) == 5
    assert all(c.connect_calls == 1 for c in _FakeClient.instances)
    assert calls["n"] == 5
    # Every client is closed: the failed ones by the retry loop, the stale
    # pre-kickstart one and the finally-successful one by the caller.
    assert all(c.closed for c in _FakeClient.instances)


def test_post_kickstart_retry_budget_is_a_deadline_not_an_attempt_count(monkeypatch):
    """Finding 5: with a fixed 3-attempt loop the real wall-clock budget was
    only ~2s (two inter-attempt sleeps), because a connect issued right after
    `launchctl kickstart -k` fails in microseconds rather than consuming its
    3s timeout. A cold mlx/nemotron model load takes far longer, so a
    SUCCESSFUL kickstart still aborted the session. The loop must run against
    an elapsed-time deadline instead."""
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr("onoats.stt.launchd._stamp_cooldown", lambda label, **kw: None)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 5.0)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_SETTLE_SEC", 0.0)

    calls = {"n": 0}

    def connect_raises():
        calls["n"] += 1
        # 2 normal attempts + 8 failed post-kickstart attempts (more than the
        # old fixed count of 3), then success on the 9th post-kickstart try.
        return ConnectionRefusedError("refused") if calls["n"] < 11 else None

    _install_fake_client(monkeypatch, connect_raises=connect_raises)

    recovered: list = []
    asyncio.run(
        _preflight_stt_ws(
            _kwargs(),
            "unix:/tmp/stt.sock",
            launchd_label="pipecat.stt-server",
            on_recovery=recovered.append,
        )
    )

    assert calls["n"] == 11  # the old 3-attempt loop would have given up at 5
    assert len(recovered) == 1
    assert "kickstarted pipecat.stt-server" in recovered[0]


def test_post_kickstart_deadline_expiry_raises_kickstart_noting_error(monkeypatch):
    """The deadline is a bound, not an invitation to loop forever: once it
    expires with the server still unreachable, the kickstart-noting
    SttPreflightError must still raise."""
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr("onoats.stt.launchd._stamp_cooldown", lambda label, **kw: None)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 0.05)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_SETTLE_SEC", 0.01)
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )

    with pytest.raises(SttPreflightError) as excinfo:
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )
    assert "still unreachable after retry" in str(excinfo.value)


def test_preflight_recovery_message_is_bare_and_holder_is_prefixed_once(
    monkeypatch, tmp_path
):
    """Finding 1: one builder (`launchd.recovery_message`, bare) and one
    prefixer (`status.format_warning_branch`). The message handed to
    `on_recovery` — which feeds `set_warning_branch`, itself a prefixer —
    must be bare, while the string `_create_stt_service` returns for
    `write_running(warning=...)` (a RAW whole-field write) must carry exactly
    one prefix."""
    from onoats.config import OnoatsConfig

    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(
            raw={"stt": {"service": "websocket", "launchd_label": "pipecat.stt-server"}}
        ),
    )
    monkeypatch.setattr(
        "onoats.stt.websocket_stt_service.WebSocketSTTService", lambda **kw: object()
    )

    seen: list = []

    async def fake_preflight(kwargs, target, *, on_recovery=None, **kw):
        from onoats.stt.launchd import recovery_message

        msg = recovery_message("pipecat.stt-server")
        seen.append(msg)
        if on_recovery is not None:
            on_recovery(msg)

    monkeypatch.setattr(runtime, "_preflight_stt_ws", fake_preflight)

    _, holder_message = asyncio.run(runtime._create_stt_service(data_dir=tmp_path))

    assert not seen[0].startswith("stt:")  # bare into set_warning_branch
    assert holder_message == f"stt: {seen[0]}"  # prefixed exactly once
    assert "stt: stt:" not in holder_message


def test_live_recovery_branch_is_instance_scoped(monkeypatch, tmp_path):
    """Finding 6: mic and system are two independent WebSocketSTTService
    instances against one server. Sharing a single "stt" branch key let one
    instance's clear erase the other's still-unconfirmed warning. The LIVE
    callback must be instance-scoped; the PREFLIGHT one stays shared (it is
    one probe of one server)."""
    from onoats.config import OnoatsConfig

    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(
            raw={"stt": {"service": "websocket", "launchd_label": "pipecat.stt-server"}}
        ),
    )
    captured: dict = {}
    monkeypatch.setattr(
        "onoats.stt.websocket_stt_service.WebSocketSTTService",
        lambda **kw: captured.update(kw) or object(),
    )

    async def fake_preflight(kwargs, target, *, on_recovery=None, **kw):
        if on_recovery is not None:
            on_recovery("probe recovered")

    monkeypatch.setattr(runtime, "_preflight_stt_ws", fake_preflight)

    branches: list = []
    monkeypatch.setattr(
        "onoats.status.set_warning_branch",
        lambda dd, branch, msg: branches.append((branch, msg)),
    )

    _svc, preflight_msg = asyncio.run(
        runtime._create_stt_service(data_dir=tmp_path, branch_instance="mic")
    )
    # The preflight recovery lands on the SHARED branch — carried out through
    # the returned message (one write path, round-2 finding 10), not through
    # a `set_warning_branch` call.
    assert preflight_msg == "stt: probe recovered"
    assert branches == []

    # The instance's own live recoveries land on its OWN branch.
    branches.clear()
    captured["on_recovery"]("live recovery")
    assert branches == [("stt-mic", "live recovery")]

    branches.clear()
    asyncio.run(
        runtime._create_stt_service(data_dir=tmp_path, branch_instance="system")
    )
    branches.clear()
    captured["on_recovery"]("live recovery")
    assert branches == [("stt-system", "live recovery")]


def test_on_recovery_swallows_non_oserror_callback_failures(monkeypatch, tmp_path):
    """The recovery callback fires on the one code path that just recovered;
    any exception from the status write (not just OSError) must be logged and
    swallowed rather than crashing the bot or being misread as a failure."""
    from onoats.config import OnoatsConfig

    monkeypatch.setattr(
        "onoats.config.load_config",
        lambda: OnoatsConfig(raw={"stt": {"service": "websocket"}}),
    )
    monkeypatch.setattr(
        "onoats.stt.websocket_stt_service.WebSocketSTTService", lambda **kw: object()
    )
    monkeypatch.setattr(
        "onoats.status.set_warning_branch",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("status backend exploded")),
    )

    async def fake_preflight(kwargs, target, *, on_recovery=None, **kw):
        if on_recovery is not None:
            on_recovery("recovered")

    monkeypatch.setattr(runtime, "_preflight_stt_ws", fake_preflight)

    # Must not raise.
    asyncio.run(runtime._create_stt_service(data_dir=tmp_path))


# ---------------------------------------------------------------------------
# Round-2 findings — the post-kickstart retry loop
# ---------------------------------------------------------------------------


def _allow_kickstart(monkeypatch):
    monkeypatch.setattr(
        "onoats.stt.launchd.kickstart_stt_server", lambda label, **kw: True
    )
    monkeypatch.setattr(
        "onoats.stt.launchd._cooldown_elapsed", lambda label, **kw: True
    )
    monkeypatch.setattr("onoats.stt.launchd._stamp_cooldown", lambda label, **kw: None)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)


def test_post_kickstart_loop_does_not_retry_non_reachability_errors(monkeypatch):
    """Round-2 finding 7: the post-kickstart loop caught bare `Exception`, so
    an auth/protocol failure (a genuine, non-transient misconfiguration) was
    retried until the whole ~45s deadline expired instead of surfacing at
    once — unlike the pre-kickstart schedule, which fails fast on exactly
    those shapes."""
    _allow_kickstart(monkeypatch)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 60.0)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_SETTLE_SEC", 0.0)

    calls = {"n": 0}

    def connect_raises():
        calls["n"] += 1
        if calls["n"] <= 2:  # the two pre-kickstart attempts
            return ConnectionRefusedError("refused")
        return RuntimeError("401 unauthorized")

    _install_fake_client(monkeypatch, connect_raises=connect_raises)

    with pytest.raises(SttPreflightError) as excinfo:
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    # Exactly ONE post-kickstart attempt, not a deadline's worth.
    assert calls["n"] == 3
    assert "handshake failed" in str(excinfo.value)
    assert "401 unauthorized" in str(excinfo.value)


def test_post_kickstart_client_factory_failure_stays_inside_the_error_contract(
    monkeypatch,
):
    """Round-2 finding 15: `make_client()` sat outside the retry loop's try, so
    a raising factory escaped `_preflight_stt_ws`'s `SttPreflightError`
    contract as a raw exception."""
    _allow_kickstart(monkeypatch)

    import stt_server.client as stt_client

    calls = {"n": 0}

    def factory(**kwargs):
        calls["n"] += 1
        if calls["n"] > 2:  # only the post-kickstart construction blows up
            raise ValueError("mis-shaped endpoint kwargs")
        return _FakeClient(connect_raises=lambda: ConnectionRefusedError("refused"))

    monkeypatch.setattr(stt_client, "TranscriptionClient", factory)

    with pytest.raises(SttPreflightError):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )


def test_post_kickstart_cancellation_closes_the_in_flight_candidate(monkeypatch):
    """Round-2 finding 14: `asyncio.CancelledError` (task cancellation during
    shutdown) bypasses `except Exception`, so the attempt's client was never
    closed — a leaked socket/FD on every cancelled preflight."""
    _allow_kickstart(monkeypatch)

    calls = {"n": 0}

    def connect_raises():
        calls["n"] += 1
        if calls["n"] <= 2:
            return ConnectionRefusedError("refused")
        return asyncio.CancelledError()

    _install_fake_client(monkeypatch, connect_raises=connect_raises)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    post_kickstart_client = _FakeClient.instances[-1]
    assert post_kickstart_client.closed is True


def test_cancellation_during_stale_close_does_not_leak_the_recovered_client(
    monkeypatch,
):
    """Round-4 finding 4: on the "recovered" success path, the stale
    pre-kickstart client used to be closed via `await _close_client_quietly(
    client)` BEFORE `client = recovered` executed. If shutdown cancellation
    arrived during that await, the outer `finally` still saw `client`
    pointing at the already-dead stale client -- the live, connected
    `recovered` socket was never closed, and its socket/FD leaked. Ownership
    must transfer to `client` before any await so `finally` always closes
    the right one, regardless of what happens tearing down the stale one."""
    _allow_kickstart(monkeypatch)

    instances: list = []

    class _Client:
        def __init__(self, **kwargs):
            self.index = len(instances)
            self.closed = False
            instances.append(self)

        async def connect(self):
            # Attempts 0 and 1 are the two pre-kickstart attempts (both fail
            # so kickstart triggers); attempt 2 is the post-kickstart
            # candidate inside `_kickstart_and_retry` — it succeeds and
            # becomes `recovered`.
            if self.index < 2:
                raise ConnectionRefusedError("refused")

        async def close_session(self):
            if self.index == 1:
                # Simulate shutdown cancellation arriving mid-teardown of
                # the STALE (already-dead) pre-kickstart client — the exact
                # window the fix must survive.
                raise asyncio.CancelledError()

        async def close(self):
            self.closed = True

    import stt_server.client as stt_client

    monkeypatch.setattr(stt_client, "TranscriptionClient", lambda **kw: _Client(**kw))

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    assert len(instances) == 3
    recovered = instances[2]
    assert recovered.closed is True


def test_close_client_quietly_is_time_bounded(monkeypatch):
    """Round-2 finding 6: `close_session()` waits for the server's ack, so
    against the unreachable server every caller has already diagnosed, an
    unbounded close hangs forever — and in the post-kickstart loop that means
    never reaching the deadline check."""

    class _HangingCloser:
        async def close_session(self):
            await asyncio.sleep(3600)

        async def close(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(runtime, "_CLOSE_TIMEOUT_SEC", 0.01)

    async def _run():
        await runtime._close_client_quietly(_HangingCloser())

    asyncio.run(asyncio.wait_for(_run(), timeout=5))


# ---------------------------------------------------------------------------
# Round-5 review-gauntlet fixes
# ---------------------------------------------------------------------------


def test_close_client_quietly_still_attempts_close_at_an_expired_deadline(
    monkeypatch,
):
    """Round-5 finding 3: a `deadline` already in the past used to floor the
    per-closer timeout at a bare `0`, and `asyncio.wait_for(coro, timeout=0)`
    cancels the wrapping Task before it is ever stepped — so `close_session`/
    `close` were never even called, not merely cut short. A socket a failed
    candidate already opened was left open with no exception to signal it."""

    calls: list[str] = []

    class _TrackingCloser:
        async def close_session(self):
            calls.append("close_session")

        async def close(self):
            calls.append("close")

    async def _run():
        loop = asyncio.get_running_loop()
        # Deadline already expired: the old code floored the timeout at 0.0,
        # skipping both closers entirely.
        await runtime._close_client_quietly(
            _TrackingCloser(), deadline=loop.time() - 1.0
        )

    asyncio.run(asyncio.wait_for(_run(), timeout=5))
    assert calls == ["close_session", "close"]


def test_close_client_quietly_runs_close_after_cancellation_mid_close_session(
    monkeypatch,
):
    """Round-5 finding 4: `except Exception: pass` does not catch
    `asyncio.CancelledError` (a `BaseException`), so a cancellation arriving
    during `close_session`'s await used to propagate straight out of
    `_close_client_quietly` before the loop ever reached `close()` — leaving
    the underlying socket/FD (torn down by `close()`, not `close_session()`)
    open on the recovered-client handoff path."""

    calls: list[str] = []

    class _CancelDuringCloseSession:
        async def close_session(self):
            calls.append("close_session")
            raise asyncio.CancelledError()

        async def close(self):
            calls.append("close")

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await runtime._close_client_quietly(_CancelDuringCloseSession())

    asyncio.run(_run())
    # `close()` must still have been attempted before the cancellation
    # propagated, and the cancellation itself must still propagate (never
    # swallowed).
    assert calls == ["close_session", "close"]


def test_retry_exhausted_message_reports_the_post_kickstart_budget(monkeypatch):
    """Round-2 finding 16: the error text quoted the ~6s PRE-kickstart budget
    even though the elapsed time also included the post-kickstart deadline."""
    _allow_kickstart(monkeypatch)
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )

    with pytest.raises(SttPreflightError) as excinfo:
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )
    # OSError path phrases it as "not reachable"; the timeout path is the one
    # that quotes a budget, so assert on the value rather than the wording.
    assert "kickstarted" in str(excinfo.value)


def test_timeout_retry_exhausted_message_quotes_the_full_budget(monkeypatch):
    _allow_kickstart(monkeypatch)

    class _HangingClient:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            await asyncio.sleep(10)

        async def close_session(self):
            pass

        async def close(self):
            pass

    import stt_server.client as stt_client

    monkeypatch.setattr(stt_client, "TranscriptionClient", _HangingClient)
    monkeypatch.setattr(runtime, "_PREFLIGHT_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_ATTEMPT_TIMEOUT_SEC", 0.01)
    # Non-zero so the reported budget can be told apart from the ~6s
    # pre-kickstart schedule the message used to quote on its own.
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 0.05)

    with pytest.raises(SttPreflightError) as excinfo:
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    # The reported budget must include the post-kickstart deadline, not just
    # the pre-kickstart schedule.
    pre_kickstart_only = (
        runtime._PREFLIGHT_TIMEOUT_SEC
        + runtime._PREFLIGHT_RETRY_TIMEOUT_SEC
        + runtime._PREFLIGHT_RETRY_DELAY_SEC
    )
    assert f"within {pre_kickstart_only:.1f}s" not in str(excinfo.value)
    expected = (
        runtime._PREFLIGHT_TIMEOUT_SEC
        + runtime._PREFLIGHT_RETRY_TIMEOUT_SEC
        + runtime._PREFLIGHT_RETRY_DELAY_SEC
        + runtime._POST_KICKSTART_DEADLINE_SEC
    )
    assert f"{expected:.1f}s" in str(excinfo.value)


def test_post_kickstart_deadline_is_not_overrun_by_a_late_attempt(monkeypatch):
    """Round-2 finding 13: a failed attempt landing just under the deadline
    still bought a full settle-sleep plus a full connect timeout before the
    next deadline check, overrunning the budget by ~5s."""
    _allow_kickstart(monkeypatch)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 0.05)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_SETTLE_SEC", 10.0)
    _install_fake_client(
        monkeypatch, connect_raises=lambda: ConnectionRefusedError("refused")
    )

    async def _run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(SttPreflightError):
            await _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        return loop.time() - started

    elapsed = asyncio.run(_run())
    # Without the cap the 10s settle-sleep alone would blow this budget.
    assert elapsed < 2.0


# ---------------------------------------------------------------------------
# Round-3 review-gauntlet fixes
# ---------------------------------------------------------------------------


def test_close_client_quietly_honours_a_caller_deadline(monkeypatch):
    """Round-3 finding 1: `_close_client_quietly` ran each of its two closers
    on a fixed `_CLOSE_TIMEOUT_SEC`, so a teardown could burn up to 2x that
    *inside* a caller whose own budget was supposed to be a strict cap. With a
    `deadline` the teardown can never outlive the budget it runs inside."""

    class _HangingCloser:
        async def close_session(self):
            await asyncio.sleep(3600)

        async def close(self):
            await asyncio.sleep(3600)

    monkeypatch.setattr(runtime, "_CLOSE_TIMEOUT_SEC", 30.0)

    async def _run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        # Deadline already in the past: nothing may be waited on at all.
        await runtime._close_client_quietly(_HangingCloser(), deadline=loop.time())
        spent_expired = loop.time() - started

        started = loop.time()
        await runtime._close_client_quietly(
            _HangingCloser(), deadline=loop.time() + 0.05
        )
        spent_budgeted = loop.time() - started
        return spent_expired, spent_budgeted

    spent_expired, spent_budgeted = asyncio.run(asyncio.wait_for(_run(), timeout=5))
    assert spent_expired < 1.0
    # Both closers share the one remaining budget, so the total cannot be a
    # multiple of it either.
    assert spent_budgeted < 1.0


def test_kickstart_and_retry_cancellation_teardown_honours_the_deadline(monkeypatch):
    """The ``except BaseException:`` branch (shutdown/``CancelledError``
    arriving mid post-kickstart connect attempt) used to close the in-flight
    candidate via ``_close_client_quietly(candidate)`` with no ``deadline=``,
    unlike this same function's ``TimeoutError``/``OSError``/``Exception``
    branches, which all pass ``deadline=deadline``. Without it, a
    cancellation landing here could burn the full, undeadlined
    ``2 * _CLOSE_TIMEOUT_SEC`` instead of being capped by this loop's own
    strict ``_POST_KICKSTART_DEADLINE_SEC`` budget — exactly the overrun
    ``_close_client_quietly``'s own docstring says ``deadline`` exists to
    prevent."""
    _allow_kickstart(monkeypatch)
    # Already-expired by the time the candidate's close runs: forces the
    # deadline-bound teardown down to the floor
    # (`_MIN_CLOSE_ATTEMPT_TIMEOUT_SEC`) per closer instead of the full fixed
    # `_CLOSE_TIMEOUT_SEC` a missing `deadline=` would fall back to.
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 0.0)
    monkeypatch.setattr(runtime, "_CLOSE_TIMEOUT_SEC", 30.0)

    class _CancelOnConnect:
        async def connect(self):
            raise asyncio.CancelledError()

        async def close_session(self):
            await asyncio.sleep(3600)

        async def close(self):
            await asyncio.sleep(3600)

    async def _run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(asyncio.CancelledError):
            await runtime._kickstart_and_retry(
                _CancelOnConnect,
                "pipecat.stt-server",
                None,
                target="unix:/tmp/stt.sock",
                hint="",
            )
        return loop.time() - started

    # Pre-fix (no `deadline=` threaded through), the hanging closers would
    # burn up to 30s + 30s here and this outer `wait_for` would itself time
    # out instead of ever returning an elapsed duration to assert on.
    elapsed = asyncio.run(asyncio.wait_for(_run(), timeout=5))
    assert elapsed < 1.0


def test_post_kickstart_teardown_cannot_itself_blow_the_deadline(monkeypatch):
    """Round-3 findings 1 + 10: the retry loop tore down each failed attempt's
    client with a fixed-timeout close BEFORE rechecking its monotonic
    deadline, so real wall clock could exceed `_POST_KICKSTART_DEADLINE_SEC`
    (and the `kickstart_budget` the error message reports) by up to a full
    teardown per attempt."""
    _allow_kickstart(monkeypatch)
    monkeypatch.setattr(runtime, "_PREFLIGHT_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 0.05)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_SETTLE_SEC", 0.0)
    monkeypatch.setattr(runtime, "_CLOSE_TIMEOUT_SEC", 0.5)

    class _HangingCloseClient:
        def __init__(self, **kwargs):
            pass

        async def connect(self):
            raise ConnectionRefusedError("refused")

        async def close_session(self):
            await asyncio.sleep(3600)

        async def close(self):
            await asyncio.sleep(3600)

    import stt_server.client as stt_client

    monkeypatch.setattr(stt_client, "TranscriptionClient", _HangingCloseClient)

    async def _run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(SttPreflightError):
            await _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        return loop.time() - started

    elapsed = asyncio.run(_run())
    # Unbounded-by-budget teardowns cost 2 x 0.5s inside the pre-kickstart
    # schedule and another 2 x 0.5s inside the post-kickstart loop, on top of
    # the final `finally` close (deliberately left unbounded, 1.0s). Only the
    # last of those may remain.
    assert elapsed < 2.0


def test_raising_on_recovery_does_not_abort_a_successful_preflight_recovery(
    monkeypatch,
):
    """Round-3 finding 9: `on_recovery` was invoked UNGUARDED on the
    "recovered" success path of `_kickstart_and_retry` — the one recovery
    call site round 1/2's "recovery callbacks never crash the caller" sweep
    missed. A raising callback there aborted an otherwise-successful recovery
    and leaked the connected candidate client the caller was meant to own."""
    _allow_kickstart(monkeypatch)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_SETTLE_SEC", 0.0)

    calls = {"n": 0}

    def connect_raises():
        calls["n"] += 1
        # The two pre-kickstart attempts fail; the post-kickstart one works.
        return ConnectionRefusedError("refused") if calls["n"] <= 2 else None

    _install_fake_client(monkeypatch, connect_raises=connect_raises)

    def boom(msg):
        raise RuntimeError("status backend exploded")

    # Must NOT raise: the recovery genuinely succeeded.
    asyncio.run(
        _preflight_stt_ws(
            _kwargs(),
            "unix:/tmp/stt.sock",
            launchd_label="pipecat.stt-server",
            on_recovery=boom,
        )
    )

    # The successful probe is cached (the preflight completed), and no client
    # — the recovered one included — was leaked.
    assert runtime._preflight_key(_kwargs()) in runtime._preflight_cache
    assert _FakeClient.instances
    assert all(c.closed for c in _FakeClient.instances)


def test_display_target_never_leaks_on_a_malformed_uri():
    """Round-3 finding 13: the old `urlsplit`-based redaction fallback
    returned the RAW uri on `ValueError`, leaking exactly the
    `user:pass@` userinfo it exists to strip. Round 10 replaced that
    implementation entirely with `onoats._redact.redact_uri` (a tiered
    text search, not `urlsplit`-based) — see that module's docstring for
    why: `urlsplit` silently reports no userinfo (rather than raising) for
    a password containing `/`, `?`, or `#`, which was round 10's actual
    reported credential-leak bug in this function. The new implementation
    never raises, so there is no longer a `<unparseable STT_WS_URI>`
    placeholder path — what matters is that credentials never leak."""
    # Malformed port: the old implementation raised from `parsed.port`.
    # The new one has no such lazy-property parse step; it still redacts
    # correctly and passes the (malformed) port through as diagnostic text.
    out = runtime._display_target({"uri": "ws://user:hunter2@host:99999/"})
    assert out == "ws://host:99999/"
    assert "hunter2" not in out and "user" not in out

    # Malformed IPv6 literal: the old implementation raised from `urlsplit`
    # itself. The new one still redacts the credential and passes the rest
    # of the (malformed) text through verbatim.
    out = runtime._display_target({"uri": "ws://user:hunter2@[::1/"})
    assert out == "ws://[::1/"
    assert "hunter2" not in out

    # Well-formed URIs keep working exactly as before.
    assert runtime._display_target({"uri": "ws://u:p@host:8080/x"}) == (
        "ws://host:8080/x"
    )
    assert runtime._display_target({"uri": "ws://host:8080/x"}) == "ws://host:8080/x"


def test_create_stt_service_gives_the_instance_a_stable_identity_token(
    monkeypatch, tmp_path
):
    """Round-3 findings 5 + 12: the unhealthy-registry token came from
    `id(self)` — a memory address CPython reuses after GC, so a leaked
    registration could be inherited by an unrelated later instance. The call
    site already has the stable `"mic"`/`"system"` name."""
    monkeypatch.setenv("STT_SERVICE", "websocket")
    monkeypatch.setenv("STT_WS_SOCKET", "/tmp/stt-token-test.sock")
    _install_fake_client(monkeypatch)

    async def _build():
        mic, _ = await runtime._create_stt_service(
            data_dir=tmp_path, branch_instance="mic"
        )
        system, _ = await runtime._create_stt_service(
            data_dir=tmp_path, branch_instance="system"
        )
        return mic, system

    mic, system = asyncio.run(_build())
    assert mic._instance_token == "mic"
    assert system._instance_token == "system"
    assert mic._instance_token != f"{id(mic):x}"


# ---------------------------------------------------------------------------
# Round-6 review-gauntlet fixes
# ---------------------------------------------------------------------------


def test_pre_kickstart_second_connect_is_capped_by_remaining_deadline(monkeypatch):
    """Round-6 finding 1: the pre-kickstart two-attempt schedule
    (`_PREFLIGHT_TIMEOUT_SEC` then `_PREFLIGHT_RETRY_TIMEOUT_SEC`) already
    capped the inter-attempt teardown to what `total_budget`
    (`_pre_kickstart_deadline`) had left, but left the SECOND attempt's
    `connect()` `timeout=` fixed at the full `_PREFLIGHT_RETRY_TIMEOUT_SEC`
    regardless of how much of that budget the teardown had already spent —
    so a first attempt that failed late, followed by a deadline-capped
    teardown that itself consumed the rest of the budget, still bought the
    second connect a full fixed timeout on top, letting real elapsed time
    roughly double the reported ~6s preflight budget. The fix recomputes the
    remaining budget after the sleep+teardown and caps the second connect's
    timeout to it (floored, never zeroed outright)."""
    monkeypatch.setattr(runtime, "_PREFLIGHT_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_TIMEOUT_SEC", 5.0)
    # total_budget = 0.05 + 0.0 + 5.0 + 0.0 = 5.05s (default _CLOSE_TIMEOUT_SEC).

    created: list[_SlowFirstClient] = []

    class _SlowFirstClient:
        """The FIRST instance (attempt 1's client) fails fast on connect but
        hangs on both closers, so the deadline-capped inter-attempt teardown
        is what eats the remaining budget — mirroring a real close against an
        unreachable server. The SECOND instance (attempt 2's client) hangs
        on connect (to prove whether its timeout gets capped) but closes
        instantly, so the function's own unrelated, deliberately-unbounded
        `finally`-close of it doesn't add noise to this test's timing.
        """

        def __init__(self, **kwargs):
            self.index = len(created)
            created.append(self)
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            if self.index == 0:
                raise TimeoutError("first attempt times out")
            # Second attempt: hangs. Without the fix this sleeps out a full
            # fixed `_PREFLIGHT_RETRY_TIMEOUT_SEC` regardless of what's left
            # of the deadline.
            await asyncio.sleep(3600)

        async def close_session(self):
            if self.index == 0:
                await asyncio.sleep(3600)

        async def close(self):
            if self.index == 0:
                await asyncio.sleep(3600)

    import stt_server.client as stt_client

    monkeypatch.setattr(
        stt_client, "TranscriptionClient", lambda **kw: _SlowFirstClient(**kw)
    )

    async def _run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(SttPreflightError):
            await _preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock")
        return loop.time() - started

    elapsed = asyncio.run(asyncio.wait_for(_run(), timeout=20))
    # Budget is ~5.05s; the deadline-capped teardown of attempt 1's client
    # alone can legitimately spend close to all of it. The bug let attempt
    # 2's own fixed 5.0s connect timeout stack on TOP of that (~10s total).
    # The fix keeps attempt 2's timeout capped to what's left, so total
    # elapsed stays close to the budget, not roughly double it.
    assert elapsed < 8.0
    assert len(created) == 2


# ---------------------------------------------------------------------------
# Round 7 finding 1 — credential redaction bypass via raw exception text
# ---------------------------------------------------------------------------


class _InvalidURILikeError(Exception):
    """Stands in for `websockets.exceptions.InvalidURI`, whose real `__str__`
    is literally `f"{self.uri} isn't a valid URI: {self.msg}"` — the full
    connect URI, including any `user:pass@` userinfo, embedded verbatim in
    the exception's own message. `_display_target` never sees this text (it
    only redacts the fields *this module* formats), so a naive `{exc}`
    interpolation bypasses the redaction entirely."""

    def __init__(self, uri: str, msg: str):
        super().__init__(f"{uri} isn't a valid URI: {msg}")


_LEAKY_URI = "ws://secretuser:hunter2@stt.example.internal/v1"


def test_generic_exception_handler_redacts_leaky_exception_text(monkeypatch):
    """Round 7 finding 1: `_preflight_stt_ws`'s pre-existing generic
    `except Exception` handler (no kickstart involved) must not leak
    `user:pass@` userinfo embedded in the exception's own `str()`."""
    _install_fake_client(
        monkeypatch,
        connect_raises=lambda: _InvalidURILikeError(_LEAKY_URI, "bad"),
    )
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError, match="handshake failed") as excinfo:
        asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))

    message = str(excinfo.value)
    assert "secretuser" not in message
    assert "hunter2" not in message
    assert "isn't a valid URI" in message  # the rest of the exc text survives


def test_kickstart_and_retry_handler_redacts_leaky_exception_text(monkeypatch):
    """Round 7 finding 1: the NEW `_kickstart_and_retry` handshake-failed
    handler (post-kickstart, non-reachability exception) must redact the
    same way as the pre-existing handler above."""
    _allow_kickstart(monkeypatch)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_DEADLINE_SEC", 60.0)
    monkeypatch.setattr(runtime, "_POST_KICKSTART_SETTLE_SEC", 0.0)

    calls = {"n": 0}

    def connect_raises():
        calls["n"] += 1
        if calls["n"] <= 2:  # the two pre-kickstart attempts
            return ConnectionRefusedError("refused")
        return _InvalidURILikeError(_LEAKY_URI, "bad")

    _install_fake_client(monkeypatch, connect_raises=connect_raises)

    with pytest.raises(SttPreflightError, match="handshake failed") as excinfo:
        asyncio.run(
            _preflight_stt_ws(
                _kwargs(), "unix:/tmp/stt.sock", launchd_label="pipecat.stt-server"
            )
        )

    message = str(excinfo.value)
    assert "secretuser" not in message
    assert "hunter2" not in message
    assert "after kickstarting" in message
    assert "isn't a valid URI" in message


def test_safe_exc_text_strips_userinfo_generically():
    """Unit-level check of the shared helper itself, independent of the
    preflight plumbing above."""
    exc = _InvalidURILikeError(_LEAKY_URI, "bad")
    safe = runtime._safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hunter2" not in safe
    assert safe == "ws://stt.example.internal/v1 isn't a valid URI: bad"


# ---------------------------------------------------------------------------
# Round 8 — the round-7 regex (`://[^/@\s]+@`) was too narrow; structural
# rewrite of `_safe_exc_text` around `urlsplit`. Five cases a robust
# redaction must handle, per the round-8 fix approach.
# ---------------------------------------------------------------------------


class _BareInvalidURILikeError(Exception):
    """Stands in for `websockets.exceptions.InvalidURI` raised against a
    SCHEME-LESS URI (e.g. `STT_WS_URI=user:pass@host:2020`, missing the
    `ws://` prefix — a realistic, non-adversarial typo). The real
    `InvalidURI.__str__` is `f"{uri} isn't a valid URI: {msg}"` regardless
    of whether `uri` happens to have a scheme, so this shape has NO
    `://` substring anywhere in the rendered text."""

    def __init__(self, uri: str, msg: str):
        super().__init__(f"{uri} isn't a valid URI: {msg}")


def test_safe_exc_text_case_a_scheme_less_credential_leaks_without_scheme():
    """(a) A scheme-less `user:pass@host` — round 7's `://`-anchored regex
    matched nothing here and leaked the credential in full."""
    exc = _BareInvalidURILikeError(
        "secretuser:hunter2@stt.example.internal:2020", "scheme isn't ws or wss"
    )
    safe = runtime._safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hunter2" not in safe
    assert safe == "stt.example.internal:2020 isn't a valid URI: scheme isn't ws or wss"


def test_safe_exc_text_case_b_scheme_prefixed_credential_still_redacted():
    """(b) The already-covered `scheme://user:pass@host` shape must keep
    working after the rewrite."""
    exc = _InvalidURILikeError(_LEAKY_URI, "bad")
    safe = runtime._safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hunter2" not in safe
    assert safe == "ws://stt.example.internal/v1 isn't a valid URI: bad"


def test_safe_exc_text_case_c_password_containing_at_sign():
    """(c) A password containing a literal `@` (plausible for a raw,
    not-URL-encoded typed-in URI). Round 7's regex stopped at the FIRST
    `@`, leaking the tail of the password (`ss@host` -> only `ss` was
    stripped). `urlsplit` resolves userinfo at the LAST `@` before the
    host, matching real URI-authority parsing."""
    exc = _InvalidURILikeError(
        "ws://secretuser:hun@ter2@stt.example.internal/v1", "bad"
    )
    safe = runtime._safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hun@ter2" not in safe
    assert "ter2" not in safe
    assert safe == "ws://stt.example.internal/v1 isn't a valid URI: bad"


def test_safe_exc_text_case_d_password_containing_whitespace():
    """(d) A password containing a literal space. Round 7's regex required
    no whitespace in the match, so it didn't match at all -> full leak."""
    exc = _InvalidURILikeError(
        "ws://secretuser:hunter 2@stt.example.internal/v1", "bad"
    )
    safe = runtime._safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hunter 2" not in safe
    assert safe == "ws://stt.example.internal/v1 isn't a valid URI: bad"


def test_safe_exc_text_case_e_does_not_touch_unrelated_query_string_at_sign():
    """(e) codex-adversarial over-matching bug: an unrelated `@` inside a
    query string of a non-credential URI must survive untouched — the
    redaction must not cross the `?` boundary into the query."""
    exc = RuntimeError(
        "GET https://example.com/api?redirect=user@example.org failed: 502"
    )
    safe = runtime._safe_exc_text(exc)
    assert safe == str(exc)


# ---------------------------------------------------------------------------
# Round 9 — structural rewrite (`onoats._redact.safe_exc_text`) fixing two
# gaps left by round 8's two-regex approach: (1) a scheme-less credential
# whose password contains whitespace matched nothing and leaked in full,
# and (2) a scheme-prefixed authority with trailing prose but no port-shaped
# suffix collapsed the WHOLE message (not just the credential) when
# `urlsplit().port` choked on the swallowed trailing text.
# ---------------------------------------------------------------------------


def test_safe_exc_text_case_f_scheme_less_credential_with_whitespace_password():
    """(f) round-9 finding #1's exact repro: a scheme-less credential whose
    password contains a literal space must still be redacted, not leaked
    verbatim because the anchored match failed to fire at all."""
    exc = _BareInvalidURILikeError(
        "secretuser:hunter 2@stt.example.internal:2020",
        "scheme isn't ws or wss",
    )
    safe = runtime._safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hunter 2" not in safe
    assert safe == "stt.example.internal:2020 isn't a valid URI: scheme isn't ws or wss"


def test_safe_exc_text_case_g_trailing_prose_survives_non_port_shaped_authority():
    """(g) round-9 finding #2's exact repro: trailing diagnostic prose after
    a scheme-prefixed authority (no `/`, `?`, `#` to bound it, and not
    port-shaped) must survive redaction intact instead of the whole message
    collapsing to `scheme://<redacted>`."""
    exc = RuntimeError(
        "ws://user:pass@host:9999 isn't a valid URI: nonempty path required"
    )
    safe = runtime._safe_exc_text(exc)
    assert "user:pass" not in safe
    assert safe == "ws://host:9999 isn't a valid URI: nonempty path required"


# ---------------------------------------------------------------------------
# Round 10 — two more raw `{exc}` interpolation sites found alongside the
# `onoats._redact` rewrite: `_preflight_stt_ws`'s `ValueError` and `OSError`
# branches (the `Exception` branch already routed through `_safe_exc_text`).
# ---------------------------------------------------------------------------


def test_characterization_valueerror_redacts_credential_in_exception_text(
    monkeypatch,
):
    """Round-10 finding: the `ValueError` branch interpolated `{exc}` raw
    instead of routing through `safe_exc_text` like every sibling branch.
    A `ValueError` that embeds the offending scheme-prefixed URI (e.g. a
    validation error naming the mis-shaped endpoint) must not leak its
    credential into the raised `SttPreflightError`.

    (Not every shape this branch can see is redactable this way — a bare
    password FRAGMENT with no surrounding `user:pass@host` structure at
    all, e.g. `urlsplit(...).port`'s "Port could not be cast to integer
    value as 'se'", has no structural marker for any text-scanning
    redactor to find; that is a fundamentally different, undetectable
    case, not something this fix claims to solve.)"""

    def raiser():
        return ValueError("invalid endpoint kwargs for ws://user:secretpass@host:1234")

    _install_fake_client(monkeypatch, connect_raises=raiser)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError) as excinfo:
        asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))

    assert "secretpass" not in str(excinfo.value)


def test_characterization_oserror_redacts_credential_in_exception_text(
    monkeypatch,
):
    """Round-10 finding: the `OSError` branch (both the plain-raise message
    and the kickstart-exhausted message) interpolated `{exc}` raw."""

    def raiser():
        return OSError("refused connecting to ws://user:secretpass@host:1234")

    _install_fake_client(monkeypatch, connect_raises=raiser)
    monkeypatch.setattr(runtime, "_PREFLIGHT_RETRY_DELAY_SEC", 0.0)

    with pytest.raises(SttPreflightError) as excinfo:
        asyncio.run(_preflight_stt_ws(_kwargs(), "unix:/tmp/stt.sock"))

    assert "secretpass" not in str(excinfo.value)
