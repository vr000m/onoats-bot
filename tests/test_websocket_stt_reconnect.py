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

from onoats import _closing
from onoats.stt import launchd
from onoats.stt import websocket_stt_service as wss_module
from onoats.stt.websocket_stt_service import WebSocketSTTService


@pytest.fixture(autouse=True)
def _clear_cooldown_registry():
    """Mirrors `test_stt_launchd.py`'s fixture — the registry is process-wide
    and module-level, shared with the Phase 3 preflight path; must not leak
    between tests (or between this file and other test modules)."""
    launchd.REGISTRY.clear()
    yield
    launchd.REGISTRY.clear()


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

    monkeypatch.setattr(launchd.REGISTRY, "cooldown_elapsed", fake_elapsed)
    monkeypatch.setattr(launchd.REGISTRY, "stamp_cooldown", fake_stamp)

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
    Stamp it exactly as that path would (`launchd.REGISTRY.stamp_cooldown`) and
    assert the live reconnect path — running immediately after, well
    inside the real 30s window — reads the same stamp and skips."""
    launchd.REGISTRY.stamp_cooldown("shared-label")  # simulates the Phase 3 path

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
    assert launchd.REGISTRY.is_stamped("label-z")

    # Bare successful connect confirms health (on_recovery fires with the
    # kickstart message) but must NOT itself reset the cooldown — only a
    # transcript event does (Decision, 2026-09-15: no time-based fallback).
    exc_holder["exc"] = lambda: None

    async def _connect_then_complete():
        await svc._ensure_connected()
        assert launchd.REGISTRY.is_stamped("label-z")  # not reset by bare connect
        client = _FakeClient.instances[-1]
        await client.push_event(
            {"type": P.EVT_TRANSCRIPT_COMPLETED, "transcript": "hello"}
        )
        # Give the reader task a beat to observe the pushed event.
        for _ in range(50):
            if not launchd.REGISTRY.is_stamped("label-z"):
                break
            await asyncio.sleep(0.01)

    asyncio.run(_connect_then_complete())

    assert not launchd.REGISTRY.is_stamped("label-z")  # reset by transcript.completed
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


def test_preflight_confirm_callback_clears_on_first_transcript_event(monkeypatch):
    """`on_preflight_confirmed` (wired by `_create_stt_service` when the
    STARTUP preflight itself kickstart-recovered, against a throwaway client
    this instance never saw) must behave like a live-path kickstart for the
    purpose of clearing the warning — the instance's own first confirmed
    transcript event resets the shared cooldown and fires the callback, even
    though THIS instance never called `kickstart_stt_server` itself.

    It is a SEPARATE callback from `on_recovery` because it clears a
    different status branch: the preflight recovery is one probe of one
    shared server (branch `stt`), while this instance's own live recoveries
    are instance-scoped (`stt-mic`/`stt-system`)."""
    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    _install_fake_client_factory(monkeypatch, lambda: None)  # connects cleanly

    recovered = []
    confirmed = []
    svc = _make_service(
        launchd_label="label-preflight",
        on_recovery=lambda msg: recovered.append(msg),
        on_preflight_confirmed=lambda: confirmed.append(True),
    )

    async def _connect_then_complete():
        await svc._ensure_connected()
        client = _FakeClient.instances[-1]
        await client.push_event(
            {"type": P.EVT_TRANSCRIPT_COMPLETED, "transcript": "hello"}
        )
        # Give the reader task a beat to observe the pushed event and run
        # _maybe_confirm_kickstart_recovery (the label was never stamped by
        # THIS instance, so polling on the registry's stamped-labels membership — as
        # the sibling reset test does — can't distinguish "not yet
        # processed" from "nothing to reset"; poll on the callback instead).
        for _ in range(50):
            if confirmed:
                break
            await asyncio.sleep(0.01)

    # A plain successful connect must NOT itself fire on_recovery (that
    # would misreport "kickstarted" for a connection this instance never
    # kickstarted) — only the transcript-confirmed clear below should fire.
    asyncio.run(_connect_then_complete())

    assert kickstart_calls == []  # this instance never called kickstart itself
    assert confirmed == [True]
    # `on_recovery` belongs to this instance's OWN live branch, which never
    # recovered — it must stay untouched.
    assert recovered == []
    assert not launchd.REGISTRY.is_stamped("label-preflight")
    assert launchd.REGISTRY.stamped_labels() == frozenset()


# ---------------------------------------------------------------------------
# Round-2 review-gauntlet fixes
# ---------------------------------------------------------------------------


def test_raising_on_recovery_does_not_discard_a_successful_connection(monkeypatch):
    """Finding 14: `on_recovery` was called INSIDE `_ensure_connected`'s try
    block, so a raising callback (it is a status-file writer supplied by the
    caller) made the exception handler treat an already-successful connection
    as a failed attempt — tearing down a live client and discarding real
    state. Recovery reporting is best-effort; the connection is not."""
    monkeypatch.setattr(launchd, "kickstart_stt_server", lambda label, **kw: True)

    exc_holder = {"exc": _always_refused}
    _install_fake_client_factory(monkeypatch, lambda: exc_holder["exc"]())

    def boom(msg):
        raise RuntimeError("status backend exploded")

    svc = _make_service(launchd_label="label-raise", on_recovery=boom)

    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())

    # The post-kickstart connect succeeds; the callback then raises.
    exc_holder["exc"] = lambda: None

    async def _connect_and_snapshot():
        await svc._ensure_connected()
        # Snapshot INSIDE the loop: tearing the loop down cancels the reader
        # task, whose finally clause flips `_connected` back to False.
        return svc._connected, svc._client is not None

    connected, has_client = asyncio.run(_connect_and_snapshot())

    # Connection must have survived the callback's failure.
    assert connected is True
    assert has_client is True
    # And the gate must have advanced, so the confirm path still works.
    assert svc._kickstart_awaiting_connect is False
    assert svc._kickstart_awaiting_transcript is True


def test_stale_kickstart_confirmation_expires_after_the_cooldown_window(monkeypatch):
    """Finding 15: a kickstart launchd accepted but that never actually
    restored service left `_kickstart_awaiting_connect` armed forever, so a
    much later, wholly unrelated reconnect emitted a stale "server restarted
    automatically" warning. The claim expires with
    `KICKSTART_CONFIRM_WINDOW_SEC` — round-2 finding 17 decoupled that window
    from `KICKSTART_COOLDOWN_SEC`, because the confirming reconnect is
    demand-driven and can itself burn ~15.5s of backoff, so a cooldown-sized
    window dropped genuine recoveries."""
    monkeypatch.setattr(launchd, "kickstart_stt_server", lambda label, **kw: True)

    exc_holder = {"exc": _always_refused}
    _install_fake_client_factory(monkeypatch, lambda: exc_holder["exc"]())

    recovered: list = []
    svc = _make_service(
        launchd_label="label-stale", on_recovery=lambda msg: recovered.append(msg)
    )

    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())
    assert svc._kickstart_awaiting_connect is True

    # Jump well past the cooldown window without the server ever returning.
    real_monotonic = wss_module.time.monotonic
    monkeypatch.setattr(
        wss_module.time,
        "monotonic",
        lambda: real_monotonic() + launchd.KICKSTART_CONFIRM_WINDOW_SEC + 1.0,
    )

    exc_holder["exc"] = lambda: None

    async def _connect_and_snapshot():
        await svc._ensure_connected()
        return svc._connected

    assert asyncio.run(_connect_and_snapshot()) is True
    assert recovered == []  # no stale "restarted automatically" message
    assert svc._kickstart_awaiting_connect is False
    assert svc._kickstart_awaiting_transcript is False


def test_sibling_instance_still_failing_blocks_the_shared_cooldown_reset(monkeypatch):
    """Round-2 finding 18: the cooldown is PROCESS-WIDE but confirmation is
    per-instance. mic confirming a transcript used to clear the shared stamp
    outright, so system's very next exhaustion could kickstart — SIGKILL and
    restart the server mic was actively, successfully using."""
    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )

    exc_holder = {"exc": _always_refused}
    _install_fake_client_factory(monkeypatch, lambda: exc_holder["exc"]())

    mic = _make_service(launchd_label="shared-label")
    system = _make_service(launchd_label="shared-label")

    async def _both_exhaust():
        await asyncio.gather(
            mic._ensure_connected(),
            system._ensure_connected(),
            return_exceptions=True,
        )

    asyncio.run(_both_exhaust())
    assert len(kickstart_calls) == 1
    assert launchd.REGISTRY.is_stamped("shared-label")

    # mic reconnects and sees a real transcript; system is still down.
    exc_holder["exc"] = lambda: None

    async def _mic_confirms():
        await mic._ensure_connected()
        client = _FakeClient.instances[-1]
        await client.push_event(
            {"type": P.EVT_TRANSCRIPT_COMPLETED, "transcript": "hello"}
        )
        for _ in range(50):
            if mic._instance_token not in launchd.REGISTRY.unhealthy_tokens(
                "shared-label"
            ):
                break
            await asyncio.sleep(0.01)

    asyncio.run(_mic_confirms())

    # The shared stamp must SURVIVE: system has not confirmed health, so
    # its next exhaustion must not be free to restart the server mic is on.
    assert launchd.REGISTRY.is_stamped("shared-label")

    # And once system confirms too, the stamp drops as before.
    async def _system_confirms():
        await system._ensure_connected()
        client = _FakeClient.instances[-1]
        await client.push_event(
            {"type": P.EVT_TRANSCRIPT_COMPLETED, "transcript": "hello"}
        )
        for _ in range(50):
            if not launchd.REGISTRY.is_stamped("shared-label"):
                break
            await asyncio.sleep(0.01)

    asyncio.run(_system_confirms())
    assert not launchd.REGISTRY.is_stamped("shared-label")


# ---------------------------------------------------------------------------
# Round-3 review-gauntlet fixes
# ---------------------------------------------------------------------------


def test_protocol_error_exhaustion_never_kickstarts(monkeypatch):
    """Round-3 finding 3: the live-path kickstart fired on ANY final reconnect
    failure, including a protocol/auth rejection (a 401 from a server that is
    demonstrably up and answering). Restarting a healthy server neither fixes
    a misconfiguration nor is harmless — it SIGKILLs a working process. Only
    reachability failures (`TimeoutError`/`OSError`) qualify, matching the
    filter the preflight path already enforces."""
    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )
    _install_fake_client_factory(
        monkeypatch, lambda: RuntimeError("server rejected connection: HTTP 401")
    )

    svc = _make_service(launchd_label="label-auth")
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())

    assert kickstart_calls == []
    assert not launchd.REGISTRY.is_stamped("label-auth")

    # A reachability failure on the same service still kickstarts — the
    # filter narrows the trigger, it does not disable it.
    _install_fake_client_factory(monkeypatch, _always_refused)
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())
    assert kickstart_calls == ["label-auth"]


def test_wedged_connect_times_out_and_still_kickstarts(monkeypatch):
    """Deep-review finding: `client.connect()` inside the reconnect loop had
    no timeout, so a server that accepts the socket but never emits
    server.hello/session.created hung `_ensure_connected` forever — no
    attempt advanced, `_maybe_kickstart()` was never reached. `connect()`
    must be bounded, and the resulting `TimeoutError` must still flow into
    the reachability-failure kickstart gate like any other `TimeoutError`."""
    monkeypatch.setattr(wss_module, "_CONNECT_TIMEOUT_SECONDS", 0.01)
    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )

    class _HangingClient(_FakeClient):
        async def connect(self):
            self.connect_calls += 1
            await asyncio.sleep(999)

    monkeypatch.setattr(wss_module, "TranscriptionClient", _HangingClient)

    svc = _make_service(launchd_label="label-wedged")
    with pytest.raises(TimeoutError):
        asyncio.run(svc._ensure_connected())

    assert kickstart_calls == ["label-wedged"]


def test_wedged_connect_timeout_closes_the_leaked_client(monkeypatch):
    """Round-2 review-gauntlet finding: `client` is only assigned to
    `self._client` AFTER `client.connect()` succeeds, so on every timeout
    (the exact wedged-server case `test_wedged_connect_times_out_and_still_
    kickstarts` above exercises) the locally-constructed client was
    invisible to `_discard_stale()`'s cleanup and its open websocket leaked
    — accumulating server-side connections against the very server the
    kickstart is trying to recover. Every attempt's client must be closed
    even though it never becomes `self._client`."""
    monkeypatch.setattr(wss_module, "_CONNECT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(launchd, "kickstart_stt_server", lambda label, **kw: True)

    class _HangingClient(_FakeClient):
        async def connect(self):
            self.connect_calls += 1
            await asyncio.sleep(999)

    monkeypatch.setattr(wss_module, "TranscriptionClient", _HangingClient)

    svc = _make_service(launchd_label="label-wedged-leak")
    with pytest.raises(TimeoutError):
        asyncio.run(svc._ensure_connected())

    assert _FakeClient.instances  # sanity: attempts actually happened
    assert all(inst.closed for inst in _FakeClient.instances), (
        "every timed-out connect attempt's client must be closed, not leaked"
    )


def test_session_update_ack_timeout_is_classified_as_reachability(monkeypatch):
    """Deep-review finding: the `session.update` ack timeout was re-raised
    as a bare `RuntimeError`, which matches neither `TimeoutError` nor
    `OSError` in `_ensure_connected`'s kickstart gate — so a server that
    completes the websocket handshake but never acks `session.update` (a
    live-but-wedged server, exactly the reachability failure kickstart
    exists for) exhausted every attempt without ever self-healing. The
    re-raise must preserve the `TimeoutError` classification."""
    monkeypatch.setattr(wss_module, "_SESSION_READY_TIMEOUT_SECONDS", 0.01)
    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )

    class _NoAckClient(_FakeClient):
        async def update_session(self, **kwargs):
            pass  # never pushes EVT_SESSION_UPDATED — _session_ready hangs

    monkeypatch.setattr(wss_module, "TranscriptionClient", _NoAckClient)

    svc = _make_service(launchd_label="label-no-ack")
    with pytest.raises(TimeoutError, match="did not ack session.update"):
        asyncio.run(svc._ensure_connected())

    assert kickstart_calls == ["label-no-ack"]


def test_discard_stale_does_not_swallow_external_cancellation():
    """Deep-review finding: `_discard_stale` used to await the reader task
    under `except (asyncio.CancelledError, Exception): pass`, which cannot
    tell "the reader task I just cancelled finished as cancelled" (benign)
    from "someone cancelled ME while awaiting it" (must propagate) — both
    surface identically as `CancelledError` from that `await`. The old form
    swallowed BOTH, letting execution silently continue past a genuine
    external cancellation of the calling coroutine. The
    `asyncio.gather(..., return_exceptions=True)` fix must let a real
    external cancellation of the caller still propagate."""

    async def scenario():
        svc = _make_service()
        # A reader task that never finishes on its own, standing in for a
        # live reader awaiting the next websocket frame.
        svc._reader_task = asyncio.create_task(asyncio.sleep(999))
        marker = {"reached_past_discard": False}

        async def runner():
            await svc._discard_stale()
            marker["reached_past_discard"] = True

        task = asyncio.ensure_future(runner())
        await asyncio.sleep(0)  # let it start and reach the cancel+await
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not marker["reached_past_discard"]

    asyncio.run(scenario())


def test_sibling_that_has_only_just_started_failing_blocks_the_early_reset(monkeypatch):
    """Round-3 finding 2: round 2's unhealthy-set guard registered an instance
    only when it EXHAUSTED its ~15.5s backoff, so an instance that had just
    begun failing was invisible to the guard. Concrete race: mic exhausts and
    kickstarts; system's socket dies a second later and it starts its own
    backoff (unregistered); mic reconnects and confirms, clearing the shared
    stamp outright; system then exhausts and kickstarts AGAIN, SIGKILLing the
    server mic is actively using — the exact storm the guard exists to stop,
    with the timing merely shifted. Registration now happens on the FIRST
    failed connect attempt."""
    kickstart_calls = []
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: kickstart_calls.append(label) or True,
    )

    exc_holder = {"exc": _always_refused}
    _install_fake_client_factory(monkeypatch, lambda: exc_holder["exc"]())

    mic = _make_service(launchd_label="shared-label", instance_name="mic")
    system = _make_service(launchd_label="shared-label", instance_name="system")

    # T0: mic exhausts and wins the kickstart.
    with pytest.raises(Exception):
        asyncio.run(mic._ensure_connected())
    assert kickstart_calls == ["shared-label"]
    assert launchd.REGISTRY.is_stamped("shared-label")

    # T0+1: system's socket dies too. It fails its first attempt and then
    # reconnects — so it NEVER exhausts its backoff, and under the old
    # registration point it was never registered as unhealthy at all. It has
    # not produced a transcript, so its health is still unconfirmed.
    attempts = {"n": 0}

    def fail_once_then_connect():
        attempts["n"] += 1
        return ConnectionRefusedError("refused") if attempts["n"] == 1 else None

    _install_fake_client_factory(monkeypatch, fail_once_then_connect)
    asyncio.run(system._ensure_connected())
    assert system._instance_token in launchd.REGISTRY.unhealthy_tokens("shared-label")

    # T0+3: mic reconnects and sees a real transcript.
    _install_fake_client_factory(monkeypatch, lambda: None)

    async def _mic_confirms():
        await mic._ensure_connected()
        client = _FakeClient.instances[-1]
        await client.push_event(
            {"type": P.EVT_TRANSCRIPT_COMPLETED, "transcript": "hello"}
        )
        for _ in range(50):
            if mic._instance_token not in launchd.REGISTRY.unhealthy_tokens(
                "shared-label"
            ):
                break
            await asyncio.sleep(0.01)

    asyncio.run(_mic_confirms())

    # The shared stamp must SURVIVE: system is still unconfirmed, so its next
    # exhaustion must not be free to restart the server mic is running on.
    assert launchd.REGISTRY.is_stamped("shared-label")

    _install_fake_client_factory(monkeypatch, _always_refused)
    with pytest.raises(Exception):
        asyncio.run(system._ensure_connected())
    assert kickstart_calls == ["shared-label"]  # no second kickstart


def test_instance_token_is_the_branch_name_not_a_memory_address():
    """Round-3 findings 5 + 12 (one fix): `id(self)` is reused by CPython
    after GC, so a leaked unhealthy registration could be inherited by an
    unrelated later instance. The call site already has the stable
    `"mic"`/`"system"` identity; `self.name` (Pipecat's monotonic
    `<Class>#<n>`) is the single-pipeline fallback and is never reused."""
    mic = _make_service(launchd_label="l", instance_name="mic")
    assert mic._instance_token == launchd.MIC
    assert mic._instance_token.name == "mic"
    assert mic._instance_token.name != f"{id(mic):x}"

    # No branch name (single-pipeline path): still unique, still not an
    # address.
    a = _make_service(launchd_label="l")
    b = _make_service(launchd_label="l")
    assert a._instance_token != b._instance_token
    assert a._instance_token.name != f"{id(a):x}"


def test_launchd_registry_is_imported_once_at_module_top_level(monkeypatch):
    """Round-3 finding 6: four function-local `import onoats.stt.launchd`
    calls were justified by a "tests monkeypatch attributes, must re-import
    fresh" rationale that does not hold — attribute access through a
    module reference bound at import time is monkeypatch-transparent, which
    is what the repo's own tests already rely on."""
    from pathlib import Path

    source = Path(wss_module.__file__).read_text(encoding="utf-8")
    assert "from onoats.stt import launchd" in source
    assert "from onoats.stt.launchd import" not in source
    assert "import onoats.stt.launchd" not in source

    # Behavioural half: patching the module attribute still takes effect
    # through the top-level module reference.
    seen = []

    async def fake_try_kickstart(label):
        seen.append(label)
        return False

    monkeypatch.setattr(launchd, "try_kickstart", fake_try_kickstart)
    _install_fake_client_factory(monkeypatch, _always_refused)

    svc = _make_service(launchd_label="label-patched")
    with pytest.raises(Exception):
        asyncio.run(svc._ensure_connected())

    assert seen == ["label-patched"]


# ---------------------------------------------------------------------------
# Round 10 — `_endpoint_label` returned the raw, unredacted connect `uri`
# verbatim, re-leaking `user:pass@` userinfo into the `[endpoint=...]` log
# field `safe_exc_text(exc)` was added to protect — including on the
# happy-path "connected to {endpoint}" log line, not just on error.
# ---------------------------------------------------------------------------


def test_endpoint_label_redacts_uri_credentials():
    svc = _make_service(
        socket_path=None, uri="ws://secretuser:hunter2@stt.example.internal:2020/"
    )
    label = svc._endpoint_label()
    assert "secretuser" not in label
    assert "hunter2" not in label
    assert label == "ws://stt.example.internal:2020/"


def test_endpoint_label_redacts_uri_credentials_with_special_char_password():
    """Same class of bug as `_display_target`'s: a password containing an
    unencoded `/`, `?`, or `#` must still be redacted, not silently pass
    through because a naive `urlsplit`-based check missed it."""
    svc = _make_service(
        socket_path=None, uri="ws://secretuser:hunt/er2@stt.example.internal:2020/"
    )
    label = svc._endpoint_label()
    assert "secretuser" not in label
    assert "hunt" not in label
    assert label == "ws://stt.example.internal:2020/"


def test_endpoint_label_strips_a_scheme_less_uris_query_token():
    """Round-7 finding A (HIGH): `_endpoint_label` composes `display_uri`,
    which composed a `strip_query` built on `urlsplit` — and `urlsplit` gives
    a scheme-less URI an empty netloc, so the fail-closed `host[:port]` guard
    returned it unchanged, query token and all, into this label and every log
    line and `SttPreflightError` built from it. Finding B is the same sink
    with `localhost`, the project's own canonical local endpoint, which the
    round-6 dotted-host rule could never match."""
    for uri, expected in (
        ("stt.example.com:8765/v1?token=SEKRET", "stt.example.com:8765/v1"),
        ("localhost:8765/v1?token=SEKRET", "localhost:8765/v1"),
        ("u:pw@localhost:8765/v1?token=SEKRET", "localhost:8765/v1"),
    ):
        label = _make_service(socket_path=None, uri=uri)._endpoint_label()
        assert "SEKRET" not in label, uri
        assert "pw" not in label, uri
        assert label == expected, uri


def test_endpoint_label_socket_path_and_host_port_shapes_unaffected():
    assert (
        _make_service(socket_path="/tmp/x.sock")._endpoint_label() == "unix:/tmp/x.sock"
    )
    svc = _make_service(socket_path=None, host="127.0.0.1", port=1234)
    assert svc._endpoint_label() == "ws://127.0.0.1:1234"


# ---------------------------------------------------------------------------
# Round-3 review-gauntlet: one bounded teardown, shared with runtime.py
#
# `TranscriptionClient` teardown used to have two owners with opposite rules:
# `_closing.close_quietly` bounded every close (because an unreachable
# server's close never completes), while this module closed the same client
# type unbounded in four places — including the handler added specifically to
# clean up after a connect timeout against a wedged server, i.e. exactly the
# peer whose closing handshake also never completes. Both now go through
# `onoats._closing.close_quietly`.
# ---------------------------------------------------------------------------


def test_wedged_connect_teardown_is_itself_bounded(monkeypatch):
    """Round-3 finding 5: the connect-timeout cleanup closed the client with
    a bare `await client.close()`. The `websockets` closing handshake has its
    own ~10s default wait, so against the wedged server that handler exists
    for, the teardown hung the reconnect loop the connect timeout had just
    rescued — one unbounded close per attempt."""
    monkeypatch.setattr(wss_module, "_CONNECT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(_closing, "CLOSE_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(launchd, "kickstart_stt_server", lambda label, **kw: True)

    class _WedgedClient(_FakeClient):
        async def connect(self):
            self.connect_calls += 1
            await asyncio.sleep(999)

        async def close(self):
            # A peer that never completes the closing handshake.
            await asyncio.sleep(999)

    monkeypatch.setattr(wss_module, "TranscriptionClient", _WedgedClient)

    svc = _make_service(launchd_label="label-wedged-close")

    async def _run():
        with pytest.raises(TimeoutError):
            await svc._ensure_connected()

    # The whole reconnect loop must finish well inside this bound; with an
    # unbounded close it hangs on the first attempt's teardown.
    asyncio.run(asyncio.wait_for(_run(), timeout=5))


def test_discard_stale_teardown_is_bounded(monkeypatch):
    """Same root cause, second call site: `_discard_stale` runs on the
    reconnect path — precisely when the peer has already proven unreachable
    — so an unbounded close there stalls every subsequent attempt."""
    monkeypatch.setattr(_closing, "CLOSE_TIMEOUT_SEC", 0.01)

    class _HangingCloseClient(_FakeClient):
        async def close(self):
            await asyncio.sleep(999)

    svc = _make_service()
    stale = _HangingCloseClient()
    svc._client = stale
    svc._connected = True

    asyncio.run(asyncio.wait_for(svc._discard_stale(), timeout=5))
    assert svc._client is None and svc._connected is False


def test_graceful_close_resets_state_even_if_close_hangs(monkeypatch):
    """Same root cause, third call site — and it cost more than time: the
    state reset (`_client`/`_reader_task`/`_connected`) sits AFTER the
    `await client.close()` in the `finally`, so a close that hung or raised
    left the instance half torn down, with a stale `_client` a later
    `_ensure_connected` would treat as live."""
    monkeypatch.setattr(_closing, "CLOSE_TIMEOUT_SEC", 0.01)

    class _HangingCloseClient(_FakeClient):
        async def close(self):
            await asyncio.sleep(999)

    svc = _make_service()
    svc._client = _HangingCloseClient()
    svc._connected = True

    asyncio.run(asyncio.wait_for(svc._graceful_close(), timeout=5))
    assert svc._client is None
    assert svc._reader_task is None
    assert svc._connected is False


def test_cancel_and_close_resets_state_even_if_close_raises(monkeypatch):
    """Fourth call site, raising rather than hanging: an exception from
    `close()` in the `finally` used to propagate out and skip the state
    reset entirely."""

    class _RaisingCloseClient(_FakeClient):
        async def close(self):
            raise OSError("socket already gone")

    svc = _make_service()
    svc._client = _RaisingCloseClient()
    svc._connected = True

    asyncio.run(asyncio.wait_for(svc._cancel_and_close(), timeout=5))
    assert svc._client is None
    assert svc._connected is False


def test_close_timeout_constant_is_not_re_aliased_locally():
    """The two modules must not drift back to two independently-edited 5.0s
    constants — that duplication is what let one side be bounded and the
    other not. Round 4 went one step further: this module no longer names
    the close bound at all (every close goes through `_closing`), so the
    re-alias that was being applied to a non-close wait is gone. The
    reader-join wait now has its own, separately tunable constant."""
    assert not hasattr(wss_module, "_CLOSE_TIMEOUT_SECONDS")
    assert wss_module._READER_JOIN_TIMEOUT_SEC is not _closing.CLOSE_TIMEOUT_SEC


# ---------------------------------------------------------------------------
# Round-4 regression: cancellation-path state reset.
# ---------------------------------------------------------------------------


class _CancellingCloseClient:
    """A client whose `close()` raises `CancelledError` — the shape
    `_closing.close_quietly` re-raises by contract, and the shape every
    teardown here actually runs under (Ctrl+C, `CancelFrame`, task
    cancellation)."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True
        raise asyncio.CancelledError()

    async def close_session(self):
        return None

    async def cancel(self):
        return None


@pytest.mark.parametrize(
    "method", ("_discard_stale", "_graceful_close", "_cancel_and_close")
)
def test_teardown_resets_state_even_when_close_is_cancelled(method):
    """Round-4 finding 8: `_client`/`_reader_task`/`_connected` were assigned
    AFTER the awaited `close_quietly`, so a `CancelledError` from it skipped
    every one of them — stranding the instance with `_client` set and
    `_connected` True, and making the next `_ensure_connected` short-circuit
    on a dead client. The reset now happens before the awaited teardown."""
    svc = _make_service()
    client = _CancellingCloseClient()
    svc._client = client
    svc._connected = True

    async def _run():
        with pytest.raises(asyncio.CancelledError):
            await getattr(svc, method)()

    asyncio.run(_run())
    assert client.closed is True, "the close must still be attempted"
    assert svc._client is None
    assert svc._reader_task is None
    assert svc._connected is False


# ---------------------------------------------------------------------------
# Round-5 regressions: shutdown drain ownership, and the cancellation window
# between "marked connected" and "session.update acked".
# ---------------------------------------------------------------------------


def test_graceful_close_reader_observes_session_closed_and_does_not_time_out(
    monkeypatch,
):
    """Round-5 finding 5: `_graceful_close` nulled `self._client` BEFORE
    sending `session.close`, and the reader's ownership test is
    `client is self._client` — so the `session.closed` ack the close is
    *waiting for* was `continue`d past, the reader's `break` became
    unreachable, and the bounded join burned its FULL timeout on every
    close (doubled for the mic/system pair). Asserted on wall-clock: the
    join must finish well inside `_READER_JOIN_TIMEOUT_SEC`."""
    monkeypatch.setattr(wss_module, "_READER_JOIN_TIMEOUT_SEC", 5.0)
    _install_fake_client_factory(monkeypatch, lambda: None)

    class _AckingClient(_FakeClient):
        async def close_session(self):
            self.session_closed = True
            await self._events.put({"type": P.EVT_SESSION_CLOSED})

    monkeypatch.setattr(
        wss_module, "TranscriptionClient", lambda **kw: _AckingClient(**kw)
    )
    svc = _make_service()

    async def _run():
        await svc._ensure_connected()
        reader = svc._reader_task
        assert reader is not None
        t0 = asyncio.get_running_loop().time()
        await svc._graceful_close()
        elapsed = asyncio.get_running_loop().time() - t0
        # The reader exited on its own `break`, not on the join's cancel.
        assert reader.done() and not reader.cancelled()
        assert elapsed < 1.0, elapsed

    asyncio.run(asyncio.wait_for(_run(), timeout=10))
    assert svc._client is None
    assert svc._draining_client is None
    assert svc._connected is False


def test_cancellation_while_awaiting_session_update_ack_resets_state(monkeypatch):
    """Round-5 finding 6: `_client`/`_connected` are set before
    `update_session` and the `_session_ready` wait complete, but the attempt
    loop's handler is `except Exception`, which does NOT catch
    `CancelledError` — so cancellation in that window skipped
    `_discard_stale()` and left the instance falsely marked connected on a
    session whose `session.update` was never acked. The next
    `_ensure_connected` then short-circuits on it."""

    class _NeverAckingClient(_FakeClient):
        async def update_session(self, **kwargs):
            # No session.updated ack: `_ensure_connected` parks on
            # `_session_ready`, which is exactly the cancellation window.
            return None

    monkeypatch.setattr(
        wss_module, "TranscriptionClient", lambda **kw: _NeverAckingClient(**kw)
    )
    svc = _make_service()

    async def _run():
        task = asyncio.create_task(svc._ensure_connected())
        # Let it get past connect() and into the session_ready wait.
        for _ in range(20):
            await asyncio.sleep(0)
            if svc._connected:
                break
        assert svc._connected is True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(asyncio.wait_for(_run(), timeout=10))
    assert svc._connected is False
    assert svc._client is None
    assert svc._reader_task is None


# ---------------------------------------------------------------------------
# Round-6 regressions.
# ---------------------------------------------------------------------------


def test_draining_client_bypass_admits_only_session_closed(monkeypatch):
    """Round-6 finding (`_read_events`): the `_draining_client` exemption is
    documented as existing solely so the `session.closed` ack a
    `_graceful_close` is *waiting for* is not discarded — but it was written
    as a blanket "this client is exempt", admitting every event type. A
    closing session's late `transcript.completed` could therefore set a
    result on `self._pending` (which by then belongs to the next client's
    decode) and a late `transcript.failed` could reach
    `_maybe_confirm_kickstart_recovery`, firing `on_recovery` and resetting
    the shared launchd cooldown off an already-torn-down session."""
    _install_fake_client_factory(monkeypatch, lambda: None)
    recovered: list[object] = []
    svc = _make_service(on_recovery=lambda label: recovered.append(label))

    async def _run():
        await svc._ensure_connected()
        client = svc._client
        assert client is not None
        # Enter the drain window by hand, exactly as `_graceful_close` does.
        svc._client = None
        svc._draining_client = client
        svc._kickstart_awaiting_transcript = True
        pending: asyncio.Future = asyncio.get_running_loop().create_future()
        svc._pending = pending

        await client.push_event(
            {"type": P.EVT_TRANSCRIPT_COMPLETED, "transcript": "late"}
        )
        await client.push_event({"type": P.EVT_SESSION_CLOSED})
        await asyncio.wait_for(svc._reader_task, timeout=5)
        return pending

    pending = asyncio.run(asyncio.wait_for(_run(), timeout=10))
    # The late transcript was ignored: the next decode's future is untouched,
    # and no recovery/cooldown-reset fired off the dying session.
    assert not pending.done()
    assert recovered == []
    assert svc._kickstart_awaiting_transcript is True


def test_graceful_close_does_not_mistake_the_readers_cancellation_for_its_own(
    monkeypatch,
):
    """Round-6 finding (`_graceful_close`): the reader join used a plain
    `wait_for(reader, ...)` under `except CancelledError`, which cannot tell
    "the reader task I am joining was cancelled" from "someone cancelled
    *me*" — both surface as `CancelledError` from the await. The former was
    therefore recorded in the `CancellationLedger` and re-raised at the end,
    aborting a close that was never cancelled. `_discard_stale` and
    `_cancel_and_close` already use `gather(..., return_exceptions=True)` to
    keep the two apart; this path now does too."""
    monkeypatch.setattr(wss_module, "_READER_JOIN_TIMEOUT_SEC", 5.0)
    _install_fake_client_factory(monkeypatch, lambda: None)
    svc = _make_service()

    async def _run():
        await svc._ensure_connected()
        reader = svc._reader_task
        assert reader is not None
        # A third party cancels the reader task — not this coroutine.
        reader.cancel()
        await svc._graceful_close()
        return reader

    # No CancelledError escapes: `_graceful_close` completes normally.
    reader = asyncio.run(asyncio.wait_for(_run(), timeout=10))
    assert reader.cancelled()
    assert svc._client is None
    assert svc._draining_client is None
    assert svc._connected is False
    # The transport close still ran, despite the reader's cancellation.
    assert _FakeClient.instances[-1].closed is True


# ---------------------------------------------------------------------------
# Round-9 regressions.
# ---------------------------------------------------------------------------


def test_graceful_close_has_no_live_reader_at_the_transport_close(monkeypatch):
    """Round 9 reported this path as a leak of the "cancel, then join"
    discipline its two siblings follow — it cancels the reader and walks
    straight on to the transport close with no join. Investigated and found
    NOT to be a bug: `asyncio.wait_for` cancels the awaitable it wraps and
    awaits that cancellation to completion before raising `TimeoutError`, and
    cancelling a `gather` cancels its children, so the reader is already
    joined by then even when its own cleanup is slow. The `.cancel()` that
    follows is a defensive no-op.

    The test stays because the *property* is worth pinning even though the
    reported fix was not needed: no reader may still be live on the client at
    the instant that client is released. It is `wait_for`'s cancellation
    semantics that supply it here, so a refactor away from `wait_for` — the
    realistic way this would actually break — trips this rather than shipping
    the bug the finding described.
    """
    monkeypatch.setattr(wss_module, "_READER_JOIN_TIMEOUT_SEC", 0.05)
    _install_fake_client_factory(monkeypatch, lambda: None)

    class _NeverAckingCloseClient(_FakeClient):
        async def close_session(self):
            # No `session.closed` ack, so the reader never breaks on its own
            # and the bounded join below times out — the path under test.
            self.session_closed = True

    monkeypatch.setattr(
        wss_module, "TranscriptionClient", lambda **kw: _NeverAckingCloseClient(**kw)
    )
    svc = _make_service()

    observed: list[bool] = []

    async def _run():
        await svc._ensure_connected()
        reader = svc._reader_task
        assert reader is not None
        real_close_quietly = wss_module._closing.close_quietly

        async def _spy(*args, **kwargs):
            observed.append(reader.done())
            return await real_close_quietly(*args, **kwargs)

        monkeypatch.setattr(wss_module._closing, "close_quietly", _spy)
        await svc._graceful_close()
        return reader, observed

    reader, observed = asyncio.run(asyncio.wait_for(_run(), timeout=10))
    # Asserted at the moment the TRANSPORT close begins, not after
    # `_graceful_close` returns: by then the loop has run the cancelled task
    # anyway, so a post-hoc `reader.done()` passes either way and proves
    # nothing. The invariant is that no reader is still live on the client at
    # the instant that client is released.
    assert observed and observed[-1] is True, (
        f"a reader is still live at the transport close: reader.done() at "
        f"each close_quietly call = {observed}"
    )
    assert reader.done()
    assert svc._client is None
    assert svc._draining_client is None
    assert svc._connected is False
    assert _FakeClient.instances[-1].closed is True


# ---------------------------------------------------------------------------
# Round 10: the teardown paths' second shared helper, and the divergence that
# deliberately survives it.
# ---------------------------------------------------------------------------


def test_cancel_and_join_reader_absorbs_the_readers_own_cancellation():
    """The reason the join is `gather(..., return_exceptions=True)` and not a
    plain `await`: a plain await cannot tell "the task I just cancelled
    finished as cancelled" from "someone cancelled me", because both arrive
    as `CancelledError` from the same expression."""

    async def _run():
        async def _never():
            await asyncio.sleep(3600)

        reader = asyncio.create_task(_never())
        await asyncio.sleep(0)
        # Must NOT raise, and must leave the task actually joined.
        await WebSocketSTTService._cancel_and_join_reader(reader)
        return reader

    reader = asyncio.run(asyncio.wait_for(_run(), timeout=5))
    assert reader.done() and reader.cancelled()


def test_cancel_and_join_reader_still_propagates_an_external_cancellation():
    """The other half of the same property: `gather` absorbs only the awaited
    task's own cancellation. A genuine cancellation of the *caller* must still
    unwind it, or a teardown would silently continue through a shutdown."""

    async def _run():
        async def _slow_to_die():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                await asyncio.sleep(3600)  # refuses to finish unwinding

        reader = asyncio.create_task(_slow_to_die())
        await asyncio.sleep(0)
        joiner = asyncio.create_task(
            WebSocketSTTService._cancel_and_join_reader(reader)
        )
        await asyncio.sleep(0)
        joiner.cancel()  # external cancellation of the joining coroutine
        with pytest.raises(asyncio.CancelledError):
            await joiner
        reader.cancel()
        return True

    assert asyncio.run(asyncio.wait_for(_run(), timeout=5)) is True


def test_cancel_and_join_reader_retrieves_a_failed_readers_exception():
    """`_discard_stale` used to skip the join entirely when the reader was
    already `done()`. A reader that finished by *raising* then had its
    exception never retrieved, which asyncio logs at collection time. The
    join is unconditional now; `.cancel()` on a finished task is a no-op."""

    async def _run():
        async def _boom():
            raise RuntimeError("reader died")

        reader = asyncio.create_task(_boom())
        await asyncio.sleep(0)
        assert reader.done()
        await WebSocketSTTService._cancel_and_join_reader(reader)
        return reader

    reader = asyncio.run(asyncio.wait_for(_run(), timeout=5))
    assert reader.exception() is not None  # retrieved, not orphaned


def test_graceful_close_deliberately_does_not_use_the_shared_reader_join():
    """The divergence that must survive consolidation.

    `_cancel_and_join_reader` cancels first by definition. `_graceful_close`
    has just asked the server to close the session, and the reader is the only
    thing that can observe the `session.closed` ack — cancelling it up front
    would throw away the event the whole path exists to collect. It therefore
    *waits*, bounded, and cancels only as a fallback.

    Pinned against the source because the failure mode is a well-meaning
    refactor that "finishes the job" by routing all three paths through the
    helper. That would not fail any behavioural test quickly: it would merely
    stop collecting acks, and show up much later as sessions that never
    confirm a clean close.
    """
    import inspect

    src = inspect.getsource(WebSocketSTTService._graceful_close)
    # The docstring and two comments name the helper (to say why it is NOT
    # used here); only a real call is a violation. Strip the docstring and
    # every comment line before asserting on what is left.
    code = "\n".join(
        line
        for line in src.split('"""')[2].splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "_cancel_and_join_reader" not in code, (
        "_graceful_close must wait for the session.closed ack, not cancel the "
        "reader up front -- see its docstring's teardown-divergence note"
    )
    assert "_READER_JOIN_TIMEOUT_SEC" in code
    # ...while both siblings DO share it.
    for method in (
        WebSocketSTTService._discard_stale,
        WebSocketSTTService._cancel_and_close,
    ):
        body = inspect.getsource(method)
        assert "self._cancel_and_join_reader(reader)" in body, method.__name__
        assert "asyncio.gather(reader" not in body, (
            f"{method.__name__} re-hand-rolled the reader join"
        )
