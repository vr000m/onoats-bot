"""Bounded, best-effort teardown of a ``TranscriptionClient``.

Leaf module: imports nothing from ``onoats`` (stdlib-only), for the same
reason ``onoats._redact`` is one — both ``onoats.runtime`` (the startup /
preflight path) and ``onoats.stt.websocket_stt_service`` (the live-session
reconnect path) construct and tear down the *same* client type, and neither
may import the other at module scope.

**Why any of this is bounded.** ``TranscriptionClient.close_session()``
writes a ``session.close`` and then waits for the server's ``session.closed``
ack; ``close()`` performs the websocket closing handshake, which the
``websockets`` default lets run for ~10s. Every caller here is already on a
path that has concluded the server is unreachable, hung, or wedged — a
server that accepted the socket but never sent handshake frames is precisely
the shape a connect timeout exists to catch, and it is also precisely the
shape whose close never completes. An unbounded close on such a path can
hang the caller forever (in ``runtime``'s post-kickstart retry loop, forever
means never reaching the deadline check at all).

**Why it lives here rather than once per module.** The two modules used to
each own a teardown policy with opposite rules — ``runtime`` bounded every
close with a timeout, a floor, a deadline cap and cancellation safety;
``websocket_stt_service`` closed the same client unbounded in four places,
including the handler added specifically to clean up after a connect timeout
against a wedged server — plus two near-duplicate 5.0 second constants. One
mechanism, one constant, one set of invariants.

**Invariants every caller gets:**

* Never raises, with the single exception of ``asyncio.CancelledError``,
  which must always propagate.
* Every closer named is *attempted*, even if an earlier one was cancelled or
  timed out: ``close_session`` and ``close`` tear down different resources,
  and losing ``close`` because ``close_session`` was interrupted mid-await
  leaves the underlying socket/FD open. A cancellation seen along the way is
  remembered and re-raised only once every closer has had its turn.
* An expired ``deadline`` still gets each closer a real (if tiny) attempt —
  see ``_MIN_CLOSE_ATTEMPT_TIMEOUT_SEC``.
"""

from __future__ import annotations

import asyncio

# Bound on each best-effort client teardown. Sized to be comfortably longer
# than a healthy ``session.close`` round-trip and comfortably shorter than
# the ``websockets`` default close handshake, which is what an unbounded
# ``close()`` against a wedged peer actually waits out.
CLOSE_TIMEOUT_SEC = 5.0

# Floor for a deadline-capped closer's timeout. ``asyncio.wait_for(coro, 0)``
# does not run ``coro`` for even one step before cancelling it — the wrapping
# Task is cancelled while still pending, so the underlying ``close_session``/
# ``close`` call is never even issued, not merely cut short. A caller whose
# budget has already run out would otherwise silently skip closing the socket
# entirely instead of at least starting the close. Deliberately tiny (a hung
# closer still times out almost instantly): it exists only to guarantee the
# close is attempted, not to grant extra budget.
MIN_CLOSE_ATTEMPT_TIMEOUT_SEC = 0.05

# The full teardown sequence, in order. `close_session` is the graceful half
# (tells the server to end the session and waits for its ack); `close` is the
# transport half. A caller mid-handshake — before any session exists — passes
# `("close",)` instead.
FULL_CLOSERS = ("close_session", "close")
TRANSPORT_ONLY = ("close",)


async def close_quietly(
    client: object,
    *,
    closers: tuple[str, ...] = FULL_CLOSERS,
    deadline: float | None = None,
) -> None:
    """Best-effort, time-bounded teardown of ``client``.

    ``closers`` names the coroutine methods to await in order — use
    :data:`TRANSPORT_ONLY` for a client that never reached a live session
    (a failed/timed-out ``connect()``), :data:`FULL_CLOSERS` otherwise.

    ``deadline`` (an absolute ``loop.time()`` value) caps the whole teardown
    by the caller's *remaining* budget, on top of the per-closer
    :data:`CLOSE_TIMEOUT_SEC`. Without it, N closers at a fixed timeout each
    could burn ``N * CLOSE_TIMEOUT_SEC`` **before** a caller's own monotonic
    deadline is next re-checked — teardown must never be what blows the
    budget it is running inside. Omit it for callers with no budget of their
    own, which keeps the plain fixed-per-closer behaviour.

    See the module docstring for the never-raises / always-attempt-every-
    closer / floor-an-expired-deadline invariants.
    """
    loop = asyncio.get_running_loop()
    cancelled: asyncio.CancelledError | None = None
    for closer in closers:
        timeout = CLOSE_TIMEOUT_SEC
        if deadline is not None:
            # A non-positive remaining budget must not collapse to a bare
            # `0` timeout — see the floor rationale above.
            timeout = min(
                timeout, max(MIN_CLOSE_ATTEMPT_TIMEOUT_SEC, deadline - loop.time())
            )
        try:
            await asyncio.wait_for(getattr(client, closer)(), timeout=timeout)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception:
            pass
    if cancelled is not None:
        raise cancelled
