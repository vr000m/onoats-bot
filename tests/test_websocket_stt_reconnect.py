"""Phase 4 — live-session reconnect kickstart with a process-wide cooldown.

Design intent (dev plan Phase 4): under a rapid stream of reconnect
failures across multiple `WebSocketSTTService` instances sharing a
`launchd_label`, kickstart fires at most once per cooldown window — never
once-per-attempt, never once-per-instance. `on_recovery` fires only on
confirmed post-kickstart health (the next successful `_ensure_connected`
connect following a kickstart), not merely because `kickstart_stt_server`
returned True. The cooldown registry itself lives in
`src/onoats/stt/launchd.py` (Phase 3) — this module reads/stamps/resets it,
it does not redefine it (see `test_stt_launchd.py` for the registry's own
unit tests).

These tests drive the service through its public `_ensure_connected()`
entry point (as `run_stt`/`start` do) against a fake `TranscriptionClient`
that stands in for the websocket handshake + event stream, and assert on
the shared `onoats.stt.launchd` module (kickstart calls, cooldown registry
state) plus the `on_recovery` callback — never on private call-order
internals the implementation is free to choose.
"""

from __future__ import annotations

import asyncio

import pytest
from stt_server import protocol as P

from onoats.stt import launchd
from onoats.stt import websocket_stt_service as wss_module
from onoats.stt.websocket_stt_service import WebSocketSTTService


@pytest.fixture(autouse=True)
def _clear_cooldown_registry():
    """Mirrors `test_stt_launchd.py`'s fixture — the registry is process-wide
    and module-level, shared with the Phase 3 preflight path; must not leak
    between tests (or between this file and other test modules)."""
    launchd._last_kickstart.clear()
    yield
    launchd._last_kickstart.clear()


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Collapse the ~15.5s reconnect backoff schedule to near-zero so
    exhaustion tests run fast. Same attempt count (6), zero delay."""
    monkeypatch.setattr(
        wss_module, "_RECONNECT_BACKOFF_SECONDS", (0.0, 0.0, 0.0, 0.0, 0.0)
    )


class _FakeClient:
    """Stands in for `stt_server.client.TranscriptionClient`.

    Unlike `test_runtime_preflight.py`'s `_FakeClient` (handshake only),
    this one also fakes the post-connect event stream (`events()`) so tests
    can push `transcript.completed`/`transcript.failed` events to exercise
    the cooldown-reset path.
    """

    instances: list[_FakeClient] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.connect_calls = 0
        self.connect_exc: Exception | None = None
        self.hello = {"backend": {"name": "test", "model": "test-model"}}
        self._events: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.session_closed = False
        _FakeClient.instances.append(self)

    async def connect(self):
        self.connect_calls += 1
        if self.connect_exc is not None:
            raise self.connect_exc
        return self.hello

    async def update_session(self, **kwargs):
        # Stands in for the server's session.updated ack.
        await self._events.put({"type": P.EVT_SESSION_UPDATED})

    async def push_event(self, ev: dict) -> None:
        await self._events.put(ev)

    async def events(self):
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev

    async def send_audio(self, data):
        pass

    async def commit(self):
        pass

    async def cancel(self):
        pass

    async def close_session(self):
        self.session_closed = True

    async def close(self):
        self.closed = True
        await self._events.put(None)


@pytest.fixture(autouse=True)
def _reset_fake_client_instances():
    _FakeClient.instances = []
    yield
    _FakeClient.instances = []


def _install_fake_client_factory(monkeypatch, exc_factory):
    """`exc_factory()` returns the exception the NEXT constructed client
    should raise from `connect()`, or `None` for a successful connect.
    Mutate the closed-over value (e.g. via a one-item list) to change
    behaviour between `_ensure_connected()` calls."""

    def factory(**kwargs):
        client = _FakeClient(**kwargs)
        client.connect_exc = exc_factory()
        return client

    # WebSocketSTTService imports `TranscriptionClient` directly into its
    # own module namespace (`from stt_server import TranscriptionClient`),
    # so the patch target is the service module, not `stt_server.client`.
    monkeypatch.setattr(wss_module, "TranscriptionClient", factory)
    return factory


def _always_refused():
    return ConnectionRefusedError("refused")


def _make_service(**over) -> WebSocketSTTService:
    kwargs = dict(socket_path="/tmp/stt-reconnect-test.sock")
    kwargs.update(over)
    return WebSocketSTTService(**kwargs)


# ---------------------------------------------------------------------------
# 1. Concurrent exhaustion across two instances sharing a label -> one call
# ---------------------------------------------------------------------------


def test_two_instances_sharing_label_concurrent_exhaustion_kickstarts_once(
    monkeypatch,
):
    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    _install_fake_client_factory(monkeypatch, _always_refused)

    svc_a = _make_service(launchd_label="shared-label")
    svc_b = _make_service(launchd_label="shared-label")

    async def _run():
        results = await asyncio.gather(
            svc_a._ensure_connected(), svc_b._ensure_connected(), return_exceptions=True
        )
        return results

    results = asyncio.run(_run())
    assert all(isinstance(r, Exception) for r in results)
    assert len(kickstart_calls) == 1
    assert kickstart_calls == ["shared-label"]


# ---------------------------------------------------------------------------
# 2. connect -> drop -> exhaust, repeated: at most once per cooldown window
# ---------------------------------------------------------------------------


def test_repeated_exhaustion_within_cooldown_window_kickstarts_once(monkeypatch):
    """A fake clock (not the real one) drives the cooldown check so the
    test is deterministic and fast: exhaustion #1 sees the label never
    stamped (elapsed=True) and kickstarts; exhaustion #2, run immediately
    after with the fake clock still inside the window, must not kickstart
    again — the bare backoff-exhaustion retry loop does not reset it."""
    fake_now = {"t": 0.0}
    stamped_at: dict[str, float] = {}

    def fake_elapsed(label, **kw):
        last = stamped_at.get(label)
        if last is None:
            return True
        return (fake_now["t"] - last) >= launchd.KICKSTART_COOLDOWN_SEC

    def fake_stamp(label, **kw):
        stamped_at[label] = fake_now["t"]

    monkeypatch.setattr(launchd, "_cooldown_elapsed", fake_elapsed)
    monkeypatch.setattr(launchd, "_stamp_cooldown", fake_stamp)

    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    _install_fake_client_factory(monkeypatch, _always_refused)

    svc = _make_service(launchd_label="label-a")

    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())
    assert len(kickstart_calls) == 1

    # Still well inside the 30s window on the fake clock (only a few
    # simulated seconds pass, nowhere near KICKSTART_COOLDOWN_SEC).
    fake_now["t"] += 5.0
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())
    assert len(kickstart_calls) == 1  # unchanged — cooldown still active


# ---------------------------------------------------------------------------
# 3. Cross-path: a Phase-3 preflight kickstart's stamp blocks a live-path
#    exhaustion within the same window.
# ---------------------------------------------------------------------------


def test_preflight_kickstart_stamp_blocks_live_path_exhaustion(monkeypatch):
    """The registry is shared with `runtime._preflight_stt_ws` (Phase 3).
    Stamp it exactly as that path would (`launchd._stamp_cooldown`) and
    assert the live reconnect path — running immediately after, well
    inside the real 30s window — reads the same stamp and skips."""
    launchd._stamp_cooldown("shared-label")  # simulates the Phase 3 path

    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    _install_fake_client_factory(monkeypatch, _always_refused)

    svc = _make_service(launchd_label="shared-label")
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())

    assert kickstart_calls == []


# ---------------------------------------------------------------------------
# 4. kickstart_stt_server is invoked via `await asyncio.to_thread(...)`
# ---------------------------------------------------------------------------


def test_kickstart_call_uses_asyncio_to_thread(monkeypatch):
    to_thread_calls = []
    real_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *args, **kwargs):
        to_thread_calls.append((fn, args, kwargs))
        return await real_to_thread(fn, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)
    monkeypatch.setattr(launchd, "kickstart_stt_server", lambda label, **kw: True)
    _install_fake_client_factory(monkeypatch, _always_refused)

    svc = _make_service(launchd_label="label-x")
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())

    assert len(to_thread_calls) == 1
    fn, args, kwargs = to_thread_calls[0]
    assert fn.__name__ == "kickstart_stt_server" or callable(fn)
    assert "label-x" in args or kwargs.get("label") == "label-x"


# ---------------------------------------------------------------------------
# 5 & 6. on_recovery confirmation + cooldown reset on transcript.completed
# ---------------------------------------------------------------------------


async def _connect_and_drain_session_ready(svc: WebSocketSTTService) -> _FakeClient:
    """Runs `_ensure_connected()` to a successful connect and returns the
    live `_FakeClient` so the test can push post-connect events."""
    await svc._ensure_connected()
    client = _FakeClient.instances[-1]
    assert client.connect_calls >= 1
    return client


def test_on_recovery_fires_only_on_next_successful_connect_after_kickstart(
    monkeypatch,
):
    """Kickstart does NOT itself call on_recovery — only the next
    successful `_ensure_connected` connect that follows it does, with the
    "kickstarted <label>" message. A bare `kickstart_stt_server`/
    `asyncio.to_thread` return is not confirmation of health."""
    monkeypatch.setattr(launchd, "kickstart_stt_server", lambda label, **kw: True)

    exc_holder = {"exc": _always_refused}
    _install_fake_client_factory(monkeypatch, lambda: exc_holder["exc"]())

    recovered = []
    svc = _make_service(
        launchd_label="label-y", on_recovery=lambda msg: recovered.append(msg)
    )

    # Exhaustion #1: always-refused -> kickstart fires, but on_recovery must
    # NOT have been called yet (kickstart accepted != server confirmed up).
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())
    assert recovered == []

    # Now let the next connect attempt succeed — this is the confirmation.
    exc_holder["exc"] = lambda: None
    asyncio.run(svc._ensure_connected())

    assert len(recovered) == 1
    assert recovered[0] is not None
    assert "label-y" in recovered[0]
    assert "restarted automatically" in recovered[0]


def test_reset_path_clears_cooldown_and_fires_on_recovery_none_then_rekickstarts(
    monkeypatch,
):
    """The positive reset path: kickstart, then a REAL
    `transcript.completed` event (not a bare successful connect) clears
    the shared cooldown and calls `on_recovery(None)` exactly at that
    point. A subsequent exhaustion after the reset kickstarts again."""
    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )

    exc_holder = {"exc": _always_refused}
    _install_fake_client_factory(monkeypatch, lambda: exc_holder["exc"]())

    recovered = []
    svc = _make_service(
        launchd_label="label-z", on_recovery=lambda msg: recovered.append(msg)
    )

    # Exhaustion -> kickstart #1.
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())
    assert len(kickstart_calls) == 1
    assert "label-z" in launchd._last_kickstart

    # Bare successful connect confirms health (on_recovery fires with the
    # kickstart message) but must NOT itself reset the cooldown — only a
    # transcript event does (Decision, 2026-09-15: no time-based fallback).
    exc_holder["exc"] = lambda: None

    async def _connect_then_complete():
        await svc._ensure_connected()
        assert "label-z" in launchd._last_kickstart  # not reset by bare connect
        client = _FakeClient.instances[-1]
        await client.push_event(
            {"type": P.EVT_TRANSCRIPT_COMPLETED, "transcript": "hello"}
        )
        # Give the reader task a beat to observe the pushed event.
        for _ in range(50):
            if "label-z" not in launchd._last_kickstart:
                break
            await asyncio.sleep(0.01)

    asyncio.run(_connect_then_complete())

    assert "label-z" not in launchd._last_kickstart  # reset by transcript.completed
    assert recovered.count(None) == 1
    # on_recovery(None) is the reset signal, distinct from and after the
    # "kickstarted <label>" confirmation message.
    assert recovered[0] is not None
    assert recovered[-1] is None

    # Cooldown cleared -> a fresh exhaustion kickstarts again.
    exc_holder["exc"] = _always_refused
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())
    assert len(kickstart_calls) == 2


def test_no_launchd_label_never_kickstarts(monkeypatch):
    """Baseline: without a configured `launchd_label`, exhaustion behaves
    exactly as it does today — no kickstart, no cooldown interaction."""
    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    _install_fake_client_factory(monkeypatch, _always_refused)

    svc = _make_service(launchd_label=None)
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())

    assert kickstart_calls == []
    assert launchd._last_kickstart == {}
