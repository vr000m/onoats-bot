"""Pipecat ``STTService`` wrapper over the local ``stt_server`` websocket.

Subclasses Pipecat's ``SegmentedSTTService`` so VAD-driven buffering, branch
VAD subclass dispatch, and ``TranscriptionFrame.finalized=True`` all continue
to work. Each instance owns exactly one websocket session, so onoats's dual
recorder ends up with two independent sessions (``me`` / ``them``) exactly
like the two-in-process Whisper setup it replaces.

Lifecycle wire mapping:

- ``start(StartFrame)``    -> open websocket, ``session.update`` with
  ``turn_detection=null``, await ``session.updated``
- ``run_stt(audio)``       -> ``send_audio`` + ``commit``, wait for
  ``conversation.item.input_audio_transcription.completed``
- ``stop(EndFrame)``       -> graceful ``session.close`` + socket close
- ``cancel(CancelFrame)``  -> best-effort ``session.cancel``, then close;
  also resolves any in-flight ``_pending`` so ``run_stt`` unwinds promptly
- ``cleanup()``            -> fallback teardown; takes the cancel path if
  Pipecat is already cancelling so we don't wait out ``session.closed`` on
  a server that's still mid-decode.

Both frame-flow (via the pipeline) and direct calls (``bot/dual.py``'s
shutdown helper calls ``stop(EndFrame)`` directly) are covered because
the close helpers are idempotent — ``_graceful_close`` / ``_cancel_and_close``
early-return once ``self._client is None``.

The MLX V1 backend is commit-oriented, so we emit a single finalised
``TranscriptionFrame`` per segment. ``InterimTranscriptionFrame`` is a
no-op in this path.

**Live-session self-healing kickstart** (``launchd_label``/``on_recovery``
constructor params): when the reconnect backoff in ``_ensure_connected`` is
fully exhausted, and a label is configured, and the shared process-wide
cooldown (``onoats.stt.launchd`` — the same registry the startup preflight
path stamps) has elapsed for it, this fires one kickstart via that module's
``try_kickstart`` primitive (the single owner of check-then-stamp-then-kick,
shared with the preflight path), then lets the normal reconnect schedule
continue on the caller's next attempt — no extra blocking wait here. Two
instances racing the same exhaustion only ever produce one kickstart because
``try_kickstart``'s check and stamp happen with no ``await`` between them.
``on_recovery(<message>)`` fires only once a
post-kickstart connect actually succeeds (never merely because
``kickstart_stt_server`` returned ``True``); the cooldown itself is reset
— and ``on_recovery(None)`` fired to clear the warning — only once that
reconnected session sees its first ``transcript.completed``/
``transcript.failed`` event, not on the bare connect.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable

from loguru import logger
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.services.settings import STTSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.utils.time import time_now_iso8601
from stt_server import TranscriptionClient
from stt_server import protocol as P

# Module reference, not `from ... import <names>`: attribute access through
# the module object is what makes the registry monkeypatch-transparent
# (tests patch `launchd.kickstart_stt_server`, `launchd._cooldown_elapsed`,
# ... and every read below goes through `launchd.<name>` at call time), which
# is exactly what the four function-local imports this replaced were written
# to achieve — they were never necessary. A top-level import is safe and
# cycle-free: `launchd` is a leaf module that imports nothing from `onoats`.
#
# `safe_exc_text` lives in `onoats._redact`, a leaf module with no `onoats`
# imports, so this is a top-level, cycle-free import of a *public* name —
# not a private symbol reached across a module boundary (`runtime.py` only
# ever imports this `stt` subpackage lazily, to avoid a cycle).
# `_closing` is a leaf module for the same reason and holds the ONE bounded
# teardown both this module and `runtime.py` use: the two construct and tear
# down the same `TranscriptionClient`, and each used to own a teardown policy
# (plus a near-duplicate 5.0s constant) with opposite rules.
from onoats import _closing
from onoats._redact import redact_uri, safe_exc_text, strip_query
from onoats.stt import launchd

# Wait at most this long for a decode round trip before surfacing an
# error frame and giving up on the segment. Covers the 16 kHz / 60 s
# server cap plus a little MLX decode slack.
_DECODE_TIMEOUT_SECONDS = 90.0

# The client-close bound is NOT re-aliased here: every close in this module
# goes through `onoats._closing.close_quietly`, which owns
# `CLOSE_TIMEOUT_SEC`. The two modules bound the teardown of the same client
# type, and a second, independently-edited 5.0 is exactly the drift review
# found (one side bounded, one side not).
#
# Bounded wait for the reader task to observe `session.closed` during a
# graceful close. Deliberately its OWN constant: joining a task is not
# closing a client, so the same no-alias reasoning applies in the other
# direction — retuning the client-close bound must not silently retune how
# long shutdown waits on a reader coroutine.
_READER_JOIN_TIMEOUT_SEC = 5.0

# Bounded wait for session.updated after session.update.
_SESSION_READY_TIMEOUT_SECONDS = 5.0

# Bounded wait for `client.connect()` (the server.hello + session.created
# handshake) per reconnect attempt. Without this, a server that accepts the
# TCP/UDS connection but never emits its handshake frames (the event loop is
# alive but its decode path is wedged — websockets' own ping/pong keepalive
# does not catch this) hangs `_ensure_connected` forever: no attempt
# advances, `_maybe_kickstart()` is never reached, and no ErrorFrame is ever
# yielded, all while `_run_stt_lock` blocks every queued VAD segment behind
# it. Mirrors the bound the preflight path already applies to the identical
# call (`runtime._preflight_stt_ws`'s `asyncio.wait_for(client.connect(),
# timeout=timeout_s)`).
_CONNECT_TIMEOUT_SECONDS = 5.0

# Reconnect back-off schedule. Doubles 0.5 → 8.0s before giving up, total
# ~15.5s of wall clock. Sized to cover the LaunchAgent keepalive window
# (the ``ThrottleInterval`` rendered by the external
# ``pipecat-local-stt-server`` repo's plist renderer) plus a
# couple of seconds for the freshly-respawned server to load its MLX
# model, which is the common case where our short retry window fired too
# early and surfaced an ErrorFrame for the segment in flight during a
# restart. The final entry is the delay *before* the last attempt — if
# that attempt also fails, ``_ensure_connected`` re-raises.
_RECONNECT_BACKOFF_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0)


class WebSocketSTTService(SegmentedSTTService):
    """STTService that forwards VAD-delimited audio to the stt_server.

    Audio is PCM16LE mono at ``stt_server.protocol.AUDIO_SAMPLE_RATE_HZ``
    (16000 Hz). Off-rate audio is rejected rather than silently
    resampled — Pipecat's segmented parent buffers raw ``frame.audio``
    and we cannot safely reinterpret mismatched sample rates at this
    seam.
    """

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        host: str | None = None,
        port: int | None = None,
        uri: str | None = None,
        auth_token: str | None = None,
        language: str | None = "en",
        launchd_label: str | None = None,
        instance_name: str | None = None,
        on_recovery: Callable[[str | None], None] | None = None,
        on_preflight_confirmed: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        # Pin the parent's sample_rate to the server's fixed wire format
        # so StartFrame cannot silently bump us off-rate. Supply model +
        # language explicitly so STTSettings.validate_complete() doesn't
        # log NOT_GIVEN errors — the server pins the model via launchd env,
        # we just carry a tag for metrics.
        settings = kwargs.pop("settings", None) or STTSettings(
            model="whisper-large-v3-turbo",
            language=language,
        )
        super().__init__(
            sample_rate=P.AUDIO_SAMPLE_RATE_HZ,
            settings=settings,
            **kwargs,
        )
        self._connect_kwargs = dict(
            socket_path=socket_path,
            host=host,
            port=port,
            uri=uri,
            auth_token=auth_token,
        )
        self._language = language
        self._client: TranscriptionClient | None = None
        self._reader_task: asyncio.Task | None = None
        self._pending: asyncio.Future[str] | None = None
        # Resolved by the reader when a session.updated or error event arrives
        # after session.update; _ensure_connected awaits this before returning
        # so the first commit cannot race the language config.
        self._session_ready: asyncio.Future[None] | None = None
        self._run_stt_lock = asyncio.Lock()
        self._connected = False
        # Backend identity from the most recent server.hello. The model is
        # pinned server-side (launchd env), so the client cannot know it
        # until the handshake completes — these stay None until the first
        # successful connect, then reflect whatever ASR is actually serving.
        self._backend_name: str | None = None
        self._backend_model: str | None = None
        # Self-healing kickstart (live-session path). ``launchd_label``/
        # ``on_recovery`` mirror the preflight path's constructor-injected
        # seam (never read from kwargs). Per-instance state tracks where in
        # the "kickstarted, awaiting confirmation" sequence this instance is
        # so each instance fires its own ``on_recovery`` independently even
        # though the cooldown registry itself is process-wide/shared.
        self._launchd_label = launchd_label
        self._on_recovery = on_recovery
        # Identifies this instance in the process-wide unhealthy registry
        # (`onoats.stt.launchd._unhealthy`), which gates the early cooldown
        # reset so one instance's confirmed transcript is not mistaken for
        # its sibling's health.
        #
        # Deliberately NOT `id(self)`: CPython reuses a freed object's memory
        # address, so a token from an instance that leaked a registration
        # (torn down without `cleanup()`) could be inherited wholesale by a
        # later instance — silently either stealing or resurrecting an
        # unhealthy mark. `instance_name` ("mic"/"system") is the stable
        # identity the call site (`runtime._create_stt_service`) already has
        # — an opaque identity token to this leaf service, which has no
        # notion of status-warning branches (that concept is owned by
        # `status.stt_branch()`/`format_warning_branch()`; the call site
        # happens to reuse the same string for both purposes, but this class
        # only ever uses it as a registry key). `self.name` (Pipecat's
        # `<Class>#<monotonic counter>`) is the single-pipeline fallback and
        # is never reused within a process.
        self._instance_token = instance_name or self.name
        # True from the moment this instance's own reconnect exhaustion
        # triggers a kickstart until that instance's *next* successful
        # connect — gates the one-time "server restarted automatically"
        # on_recovery call.
        self._kickstart_awaiting_connect = False
        # Monotonic deadline for the flag above. Without it, a kickstart that
        # launchd accepted but that never actually brought the server back
        # leaves the flag armed indefinitely — a much later, wholly unrelated
        # reconnect would then emit a stale "server restarted automatically"
        # warning. The window is `KICKSTART_CONFIRM_WINDOW_SEC`, deliberately
        # NOT the 30s cooldown: the confirming reconnect is demand-driven (the
        # next VAD-triggered segment) and can itself burn ~15.5s of backoff, so
        # a cooldown-sized window silently dropped genuine recoveries — and
        # with them the arming of `_kickstart_awaiting_transcript`, so the
        # cooldown never reset either.
        self._kickstart_awaiting_connect_until = 0.0
        # True from that successful post-kickstart connect until the first
        # transcript.completed/transcript.failed event on it — gates the
        # cooldown reset + on_recovery(None) clear.
        self._kickstart_awaiting_transcript = False
        # Separate gate for a recovery that happened during the STARTUP
        # PREFLIGHT, against a throwaway ``TranscriptionClient`` before this
        # instance existed — so this instance's own connect-triggered path
        # above never runs for it. Without it, the warning the preflight
        # recovery threads into the session's initial status record
        # (``_create_stt_service`` / ``dual.py``) would linger in ``onoats
        # status``/the menu bar for the whole session even once STT is
        # healthy. It is a distinct gate (not a seed of the live one) because
        # it clears a DIFFERENT status branch: the preflight recovery is one
        # probe of one shared server, so it lands on the shared ``stt``
        # branch, while this instance's own live recoveries land on its
        # instance-scoped ``stt-mic``/``stt-system`` branch.
        self._on_preflight_confirmed = on_preflight_confirmed
        self._preflight_confirm_pending = on_preflight_confirmed is not None

    # ------------------------------------------------------------------
    # Backend identity (populated on connect from server.hello)
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str | None:
        """ASR backend the server reported on connect (e.g. ``parakeet``).

        ``None`` until the first successful handshake — the model is pinned
        server-side, so the client cannot know it before connecting.
        """
        return self._backend_name

    @property
    def backend_model(self) -> str | None:
        """Model id the server reported on connect (e.g.
        ``mlx-community/parakeet-tdt-0.6b-v3``). ``None`` until connected."""
        return self._backend_model

    # ------------------------------------------------------------------
    # Pipecat lifecycle
    # ------------------------------------------------------------------

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if (
            frame.audio_in_sample_rate
            and frame.audio_in_sample_rate != P.AUDIO_SAMPLE_RATE_HZ
        ):
            raise RuntimeError(
                f"WebSocketSTTService requires {P.AUDIO_SAMPLE_RATE_HZ} Hz "
                f"input; StartFrame declared {frame.audio_in_sample_rate} Hz"
            )
        await self._ensure_connected()

    async def stop(self, frame: EndFrame) -> None:
        # Pipecat invokes this via the pipeline AND ``bot/dual.py`` calls it
        # directly during shutdown, bypassing the pipeline. Either way we
        # must close the websocket session cleanly here — waiting until
        # cleanup() leaves the socket open for the whole drain window.
        await self._graceful_close()
        await super().stop(frame)

    async def cancel(self, frame: CancelFrame) -> None:
        # Direct cancel() during an in-flight run_stt would otherwise leave
        # ``_pending`` unresolved until the 90 s decode timeout. The hard
        # cancel path cancels the pending future and tears the socket down
        # immediately so run_stt unwinds promptly.
        await self._cancel_and_close()
        await super().cancel(frame)

    async def cleanup(self) -> None:
        # Called by the pipeline on task teardown. If the pipeline is
        # cancelling (Ctrl+C / CancelFrame), don't wait out session.closed
        # — the server may still be mid-decode and would burn the full
        # 5 s timeout. Use the hard cancel path instead.
        try:
            if getattr(self, "_cancelling", False):
                await self._cancel_and_close()
            else:
                await self._graceful_close()
        finally:
            # Drop this instance's unhealthy registration so a stopped
            # instance cannot hold its sibling's early cooldown reset hostage
            # for the rest of the process's life.
            if self._launchd_label is not None:
                launchd.clear_unhealthy(self._launchd_label, self._instance_token)
            await super().cleanup()

    # ------------------------------------------------------------------
    # STTService contract
    # ------------------------------------------------------------------

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if not audio:
            return

        async with self._run_stt_lock:
            # Keep _ensure_connected inside the lock so two racing run_stt
            # calls can't each spawn their own reader task on the same
            # _client. SegmentedSTTService is single-segment-at-a-time by
            # VAD design, but pipeline cancel boundaries can still race.
            try:
                await self._ensure_connected()
            except Exception as exc:
                # `_ensure_connected` can raise straight from
                # `TranscriptionClient.connect()` (e.g. a malformed
                # STT_WS_URI raises `websockets.exceptions.InvalidURI`,
                # whose own message embeds the raw URI including userinfo)
                # before any of runtime's own redaction ever sees it —
                # sanitize before this reaches the log or a user-visible
                # ErrorFrame. Same leak class as runtime.py's preflight
                # error messages.
                safe_exc = safe_exc_text(exc)
                logger.warning(f"WebSocketSTTService: connect failed: {safe_exc}")
                yield ErrorFrame(error=f"stt_server connect failed: {safe_exc}")
                return

            assert self._client is not None
            loop = asyncio.get_running_loop()
            self._pending = loop.create_future()
            # Scale the decode timeout with audio length so long VAD turns
            # (the server accepts up to MAX_UNCOMMITTED_SECONDS ≈ 300 s)
            # don't trip the client while the server is still decoding.
            audio_seconds = len(audio) / (
                P.AUDIO_SAMPLE_RATE_HZ * P.AUDIO_SAMPLE_WIDTH_BYTES
            )
            decode_timeout = max(_DECODE_TIMEOUT_SECONDS, 1.5 * audio_seconds)
            try:
                await self.start_processing_metrics()
                # Chunk under MAX_APPEND_BYTES (1 MiB) so long VAD turns
                # don't hit payload_too_large. 512 KiB leaves headroom for
                # websocket framing overhead.
                chunk = 512 * 1024
                for i in range(0, len(audio), chunk):
                    await self._client.send_audio(audio[i : i + chunk])
                await self._client.commit()
                try:
                    text = await asyncio.wait_for(self._pending, timeout=decode_timeout)
                except TimeoutError:
                    # The server is still decoding; a late completed would
                    # otherwise resolve the NEXT segment's pending future with
                    # stale text (no item_id correlation in V1). Drop the
                    # socket so the next run_stt reconnects cleanly.
                    logger.warning(
                        f"{self.name}: decode timed out — resetting connection"
                    )
                    await self._discard_stale()
                    yield ErrorFrame(error="stt_server decode timed out")
                    return
            except Exception as exc:
                logger.warning(f"{self.name}: decode failed: {exc}")
                yield ErrorFrame(error=f"stt_server decode failed: {exc}")
                return
            finally:
                await self.stop_processing_metrics()
                self._pending = None

        text = (text or "").strip()
        if text:
            yield TranscriptionFrame(
                text,
                self._user_id,
                time_now_iso8601(),
                self._language,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_connected(self) -> None:
        if self._connected and self._client is not None:
            return

        # Close any stale client/reader from a prior (crashed) session so we
        # don't leak the websocket or race a dying reader with the new one.
        await self._discard_stale()

        # Exponential-backoff reconnect: first attempt is immediate, then
        # the schedule in ``_RECONNECT_BACKOFF_SECONDS`` inserts a delay
        # before each subsequent attempt. Sized to ride out a LaunchAgent
        # ``ThrottleInterval=10`` restart plus a few seconds of MLX model
        # warm-up on the respawned server.
        endpoint = self._endpoint_label()
        last_exc: Exception | None = None
        total_attempts = 1 + len(_RECONNECT_BACKOFF_SECONDS)
        for attempt in range(total_attempts):
            try:
                client = TranscriptionClient(**self._connect_kwargs)
                try:
                    hello = await asyncio.wait_for(
                        client.connect(), timeout=_CONNECT_TIMEOUT_SECONDS
                    )
                except BaseException:
                    # `client` never became `self._client` (that assignment
                    # is below, only on success), so it is invisible to
                    # `_discard_stale()`'s cleanup — close it here or a
                    # wedged server (TCP/UDS accepted, handshake frames never
                    # sent, exactly the case `_CONNECT_TIMEOUT_SECONDS` exists
                    # to catch) leaks one open websocket per attempt. Closing
                    # is safe at any point mid-handshake: `client.close()`
                    # no-ops on a `_ws` that never got set and closes it
                    # otherwise, whether `connect()` timed out or raised
                    # (e.g. the "expected server.hello" `RuntimeError`).
                    # Bounded (`_closing.close_quietly`), not a bare
                    # `await client.close()`: the wedged server this handler
                    # exists for — socket accepted, handshake frames never
                    # sent — is exactly the peer whose closing handshake also
                    # never completes, so an unbounded close here would hang
                    # the reconnect loop the connect timeout just rescued.
                    # `TRANSPORT_ONLY`: no session exists yet to close.
                    await _closing.close_quietly(
                        client, closers=_closing.TRANSPORT_ONLY
                    )
                    raise
                loop = asyncio.get_running_loop()
                self._session_ready = loop.create_future()
                self._client = client
                self._connected = True
                # Start reader BEFORE update_session so the session.updated /
                # error response is routed into _session_ready instead of
                # sitting unread in the socket buffer.
                self._reader_task = asyncio.create_task(
                    self._read_events(client), name=f"{self.name}:ws_reader"
                )
                await client.update_session(
                    turn_detection=None, language=self._language
                )
                try:
                    await asyncio.wait_for(
                        self._session_ready, timeout=_SESSION_READY_TIMEOUT_SECONDS
                    )
                except TimeoutError as exc:
                    # Re-raise as TimeoutError, not RuntimeError: a server
                    # that completes the websocket handshake but never acks
                    # session.update is a live-but-wedged server — exactly
                    # the reachability failure the `isinstance(last_exc,
                    # (TimeoutError, OSError))` kickstart gate below exists
                    # to catch — not the auth/TLS/protocol misconfiguration
                    # that gate is meant to filter out. A RuntimeError here
                    # matched neither branch, so this path used to exhaust
                    # all attempts and raise without ever self-healing.
                    raise TimeoutError("stt_server did not ack session.update") from exc
                # Backend identity from server.hello — surfaces an operational
                # misconfig (wrong ASR behind STT_WS_SOCKET) directly in the log,
                # and is stashed on the instance so callers (banner, metrics,
                # health) can report the real model the server pinned.
                _backend = hello.get("backend") or {}
                self._backend_name = _backend.get("name")
                self._backend_model = _backend.get("model")
                backend_desc = (
                    f" [backend={self._backend_name} model={self._backend_model}]"
                    if _backend
                    else ""
                )
                if attempt > 0:
                    logger.info(
                        f"{self.name}: reconnected to {endpoint}{backend_desc} "
                        f"on attempt {attempt + 1}"
                    )
                else:
                    logger.info(f"{self.name}: connected to {endpoint}{backend_desc}")
                # A kickstart this instance itself triggered (on a prior
                # exhaustion) is confirmed live only once we reach here —
                # fire on_recovery's "restarted" message now, and arm the
                # transcript-event gate that will actually reset the shared
                # cooldown. A bare successful connect never resets the
                # cooldown by itself (Requirements: no time-based fallback).
                if self._kickstart_awaiting_connect:
                    self._kickstart_awaiting_connect = False
                    # A kickstart launchd accepted but that never actually
                    # restored service leaves this pending; past the cooldown
                    # window this connect is no longer attributable to it, so
                    # drop the claim rather than emit a stale "restarted
                    # automatically" message.
                    if time.monotonic() <= self._kickstart_awaiting_connect_until:
                        self._kickstart_awaiting_transcript = True
                        self._fire_recovery(
                            launchd.recovery_message(self._launchd_label)
                        )
                return
            except Exception as exc:
                last_exc = exc
                # Register this instance as unhealthy on its FIRST failed
                # attempt, not at full-backoff exhaustion. The registration
                # blocks a *sibling* instance's confirmed transcript from
                # clearing the shared cooldown stamp early; registering only
                # at exhaustion left a ~15.5s hole in which a sibling that
                # had just started failing was invisible, so its own
                # exhaustion moments later was free to SIGKILL the server the
                # healthy instance was using (see `launchd.mark_unhealthy`).
                # Idempotent (set add); cleared by this instance's next
                # confirmed transcript or by `cleanup()`.
                if self._launchd_label is not None:
                    launchd.mark_unhealthy(self._launchd_label, self._instance_token)
                # Tear down this attempt's client + reader before retrying so
                # late events from the superseded socket can't poison the
                # next attempt's _session_ready / _pending futures.
                await self._discard_stale()
                if attempt + 1 < total_attempts:
                    delay = _RECONNECT_BACKOFF_SECONDS[attempt]
                    logger.warning(
                        f"{self.name}: connect attempt {attempt + 1} failed "
                        f"({safe_exc_text(exc)}) [endpoint={endpoint}], "
                        f"retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
        assert last_exc is not None
        logger.error(
            f"{self.name}: giving up after {total_attempts} connect attempts to {endpoint}"
        )
        # Only a *reachability* failure can plausibly be fixed by restarting
        # the server. A protocol/auth failure (a 401 from a server that is
        # demonstrably up and answering, a TLS error, an unexpected first
        # frame) means the config is wrong, and SIGKILLing a healthy server
        # neither fixes it nor is harmless. This mirrors the contract the
        # preflight path already enforces (`runtime._preflight_stt_ws` only
        # calls `_kickstart_and_retry` from its `TimeoutError`/`OSError`
        # handlers, and `_kickstart_and_retry` only retries those shapes).
        if isinstance(last_exc, (TimeoutError, OSError)):
            await self._maybe_kickstart()
        raise last_exc

    def _fire_recovery(self, message: str | None) -> None:
        """Invoke ``on_recovery`` without ever letting it escape.

        The callback is a status-file writer supplied by the caller. It is
        invoked from inside ``_ensure_connected``'s ``try`` (right after a
        connect that *succeeded*) and from the reader task — in both places a
        raising callback would be misattributed: ``_ensure_connected`` would
        treat an already-established connection as a failed attempt and
        discard the live client, and the reader would log a "reader crashed".
        Recovery reporting is best-effort; the connection is not.
        """
        if self._on_recovery is None:
            return
        try:
            self._on_recovery(message)
        except Exception as exc:
            logger.warning(f"{self.name}: on_recovery callback failed: {exc}")

    async def _maybe_kickstart(self) -> None:
        """Best-effort self-heal after the reconnect backoff is exhausted.

        Called only for reachability exhaustion (``TimeoutError``/``OSError``)
        — the caller filters; a protocol/auth failure never reaches here.

        Fires at most once per process-wide cooldown window across every
        ``WebSocketSTTService`` instance sharing ``launchd_label`` (and
        across the startup preflight path, which stamps the same shared
        registry in ``onoats.stt.launchd``) — never once-per-attempt, never
        once-per-instance. The cooldown check and stamp happen back-to-back
        with no ``await`` between them, so two instances exhausting
        concurrently on the same event loop can't both pass the check
        before either stamps.

        Never blocks the caller's own reconnect schedule: this only asks
        launchd to restart the job and stamps the cooldown, then returns.
        The next ``_ensure_connected`` call (triggered by the caller's next
        ``run_stt``) does the actual reconnecting on its normal backoff.
        """
        if self._launchd_label is None:
            return
        # `mark_unhealthy` is NOT called here: `_ensure_connected` already
        # registered this instance on its first failed attempt, ~15.5s before
        # this point (see `launchd.mark_unhealthy` for why the earlier
        # registration matters).
        if await launchd.try_kickstart(self._launchd_label):
            self._kickstart_awaiting_connect = True
            self._kickstart_awaiting_connect_until = (
                time.monotonic() + launchd.KICKSTART_CONFIRM_WINDOW_SEC
            )

    def _endpoint_label(self) -> str:
        """Human-readable connect target for logs — never the raw `uri`.

        `uri` may carry `user:pass@` userinfo, and this feeds both error
        logs and the happy-path "connected to {endpoint}" line, so it must
        never return the raw kwarg verbatim. Routed through the same
        `onoats._redact.redact_uri` `_display_target` uses, so there is one
        redaction owner, not two.
        """
        kw = self._connect_kwargs
        if kw.get("socket_path"):
            return f"unix:{kw['socket_path']}"
        if kw.get("uri"):
            return strip_query(redact_uri(kw["uri"]))
        host = kw.get("host") or "127.0.0.1"
        port = kw.get("port")
        return f"ws://{host}:{port}" if port else f"ws://{host}"

    async def _discard_stale(self) -> None:
        """Drop a dead client + reader without blocking on a broken socket.

        State is cleared BEFORE the awaited teardown, not after: every await
        below can raise `CancelledError` (`close_quietly` re-raises it by
        contract, and that is exactly what `_cancel_and_close`/`cleanup` run
        under), which skips every assignment that follows it. Leaving
        `_client` set and `_connected` True there strands the instance so
        the next `_ensure_connected` short-circuits on a dead client. The
        objects being torn down are held in locals, so the teardown itself
        is unaffected by the early reset.
        """
        reader = self._reader_task
        client = self._client
        self._reader_task = None
        self._client = None
        self._connected = False
        if reader is not None and not reader.done():
            reader.cancel()
            # `asyncio.gather(..., return_exceptions=True)`, not a plain
            # `await self._reader_task` under `except (CancelledError,
            # Exception): pass`: the plain form cannot distinguish "the
            # reader task I just cancelled finished as cancelled" (the
            # expected, benign outcome of the `.cancel()` two lines above)
            # from "someone cancelled ME while I was awaiting it" — both
            # surface identically as `CancelledError` from the `await`.
            # `gather` absorbs the *awaited task's own* `CancelledError`
            # into its result list without raising, while still letting a
            # genuine external cancellation of the current coroutine
            # propagate normally through the `await gather(...)` itself.
            await asyncio.gather(reader, return_exceptions=True)
        if client is not None:
            # Bounded: this runs on the reconnect path, i.e. precisely when
            # the peer has already proven unreachable — an unbounded close
            # would stall every subsequent reconnect attempt behind a dead
            # server's closing handshake.
            await _closing.close_quietly(client, closers=_closing.TRANSPORT_ONLY)

    def _maybe_confirm_kickstart_recovery(self) -> None:
        """First transcript.completed/transcript.failed event following a
        kickstart-triggered reconnect: reset the shared cooldown and clear
        the warning. Never fires on a bare successful connect — only here, on
        confirmed sustained health.

        Two independent gates, because they clear two different status
        branches: ``_preflight_confirm_pending`` clears the SHARED ``stt``
        branch a startup-preflight recovery wrote (every instance is armed
        for it, so whichever sees a transcript first clears it — a
        system-audio-only session must not leave it pinned), while
        ``_kickstart_awaiting_transcript`` clears this instance's own
        ``stt-mic``/``stt-system`` branch.

        The cooldown reset is attempted on EVERY confirmed transcript event,
        not only behind those gates: only the instance that actually won the
        kickstart arms them, so gating the reset on them left a sibling's
        unhealthy registration (and with it the shared stamp) held until the
        cooldown expired on its own. ``reset_cooldown`` is token-scoped and
        drops the stamp only once no instance sharing the label is still
        exhausted-and-unconfirmed, so this stays strictly stronger than the
        plan's "confirmed transcript, never a bare connect" rule."""
        if self._launchd_label is not None:
            # Token-scoped: the cooldown is process-wide but confirmation is
            # per-instance, so the stamp only drops once no sibling instance
            # sharing this label is still exhausted-and-unconfirmed.
            launchd.reset_cooldown(self._launchd_label, self._instance_token)
        if not (self._preflight_confirm_pending or self._kickstart_awaiting_transcript):
            return
        if self._preflight_confirm_pending:
            self._preflight_confirm_pending = False
            if self._on_preflight_confirmed is not None:
                try:
                    self._on_preflight_confirmed()
                except Exception as exc:
                    logger.warning(
                        f"{self.name}: on_preflight_confirmed callback failed: {exc}"
                    )
        if self._kickstart_awaiting_transcript:
            self._kickstart_awaiting_transcript = False
            self._fire_recovery(None)

    async def _read_events(self, client: TranscriptionClient) -> None:
        saw_session_closed = False
        try:
            async for ev in client.events():
                # Ignore any event from a superseded client (e.g. a failed
                # handshake that's still draining while a retry is in flight).
                if client is not self._client:
                    continue
                etype = ev.get("type")
                if etype == P.EVT_TRANSCRIPT_COMPLETED:
                    self._maybe_confirm_kickstart_recovery()
                    if self._pending and not self._pending.done():
                        self._pending.set_result(ev.get("transcript", ""))
                elif etype == P.EVT_TRANSCRIPT_FAILED:
                    self._maybe_confirm_kickstart_recovery()
                    err = ev.get("error") or {}
                    msg = (
                        err.get("message")
                        or err.get("code")
                        or "stt_server transcription failed"
                    )
                    if self._pending and not self._pending.done():
                        self._pending.set_exception(RuntimeError(msg))
                    else:
                        logger.warning(
                            f"{self.name}: transcription failed with no pending decode: {ev}"
                        )
                elif etype == P.EVT_ERROR:
                    err = ev.get("error") or {}
                    msg = (
                        err.get("message")
                        or err.get("code")
                        or ev.get("message")
                        or ev.get("code")
                        or "stt_server error"
                    )
                    exc = RuntimeError(msg)
                    # Route the error to whichever future is still waiting;
                    # errors before session.updated fail the connect path.
                    if (
                        self._session_ready is not None
                        and not self._session_ready.done()
                    ):
                        self._session_ready.set_exception(exc)
                    if self._pending and not self._pending.done():
                        self._pending.set_exception(exc)
                    elif self._session_ready is None or self._session_ready.done():
                        logger.warning(f"{self.name}: server error: {ev}")
                elif etype == P.EVT_SESSION_UPDATED:
                    if (
                        self._session_ready is not None
                        and not self._session_ready.done()
                    ):
                        self._session_ready.set_result(None)
                elif etype == P.EVT_SESSION_CLOSED:
                    saw_session_closed = True
                    break
                # Other events (delta, committed, status) are ignored;
                # MLX V1 is commit-oriented.
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"{self.name}: reader crashed: {exc}")
            if client is self._client and self._pending and not self._pending.done():
                self._pending.set_exception(exc)
        finally:
            # Teardown signals only apply when this reader still owns the
            # live client; superseded readers exit quietly.
            if client is self._client:
                was_connected = self._connected
                self._connected = False
                if saw_session_closed:
                    logger.info(f"{self.name}: session closed cleanly by server")
                elif was_connected:
                    # Socket dropped without a graceful session.closed — server
                    # crash, launchd restart, or network blip. Next run_stt
                    # will trigger _ensure_connected and log a reconnect.
                    logger.warning(
                        f"{self.name}: connection lost (no session.closed received)"
                    )
                # If the socket closed while a decode was in flight, fail
                # fast instead of letting run_stt hit its 90 s timeout.
                if (
                    not saw_session_closed
                    and self._pending is not None
                    and not self._pending.done()
                ):
                    self._pending.set_exception(
                        ConnectionError("stt_server connection lost mid-decode")
                    )

    async def _graceful_close(self) -> None:
        if self._client is None:
            return
        client = self._client
        reader = self._reader_task
        # Reset BEFORE the awaited teardown, not inside the `finally` after
        # it: `_closing.close_quietly` re-raises `CancelledError` by
        # contract, so every assignment sitting after that await is skipped
        # on the cancellation path (Ctrl+C, `CancelFrame`, task
        # cancellation) — leaving `_client` set and `_connected` True, and
        # the next `_ensure_connected` short-circuiting on a dead client.
        # The client and reader are held in locals, so the teardown below is
        # unaffected.
        self._client = None
        self._reader_task = None
        self._connected = False
        # Shutdown-phase timing: the Pipecat 20 s ``wait_for_cancel`` warning
        # is opaque by the time it fires — "STT close took Ns" from this
        # wrapper pins the blame here immediately instead.
        t0 = asyncio.get_running_loop().time()
        # `_closing` owns the whole close sequence, including the
        # "attempt every closer, remember a cancellation, re-raise it once"
        # invariant. This used to hand-roll `close_session` with a raw
        # `wait_for` + bare `except Exception: pass`, which dropped exactly
        # that invariant for the graceful half of the teardown.
        cancelled: asyncio.CancelledError | None = None
        try:
            try:
                await _closing.close_quietly(client, closers=("close_session",))
            except asyncio.CancelledError as exc:
                cancelled = exc
            # Give the reader a bounded window to observe session.closed.
            if reader is not None:
                try:
                    await asyncio.wait_for(reader, timeout=_READER_JOIN_TIMEOUT_SEC)
                except TimeoutError:
                    reader.cancel()
                except asyncio.CancelledError as exc:
                    reader.cancel()
                    cancelled = exc
        finally:
            try:
                await _closing.close_quietly(client, closers=_closing.TRANSPORT_ONLY)
            except asyncio.CancelledError as exc:
                cancelled = exc
            elapsed = asyncio.get_running_loop().time() - t0
            logger.info(f"{self.name}: graceful close took {elapsed:.3f}s")
        if cancelled is not None:
            raise cancelled

    async def _cancel_and_close(self) -> None:
        if self._client is None:
            return
        client = self._client
        reader = self._reader_task
        # See `_graceful_close`: reset before the awaited teardown, because
        # `close_quietly` re-raises `CancelledError` and this method is the
        # one that runs *under* cancellation in the first place.
        self._client = None
        self._reader_task = None
        self._connected = False
        t0 = asyncio.get_running_loop().time()
        try:
            try:
                await client.cancel()
            except Exception:
                pass
            if self._pending and not self._pending.done():
                self._pending.cancel()
            if reader is not None:
                reader.cancel()
                # See `_discard_stale`'s matching comment: `gather(...,
                # return_exceptions=True)` absorbs the reader task's own
                # `CancelledError` from the `.cancel()` above without
                # raising, while a genuine external cancellation of this
                # coroutine still propagates normally.
                await asyncio.gather(reader, return_exceptions=True)
        finally:
            # Bounded, and swallowing: see the module's teardown invariants
            # in `onoats._closing`.
            await _closing.close_quietly(client, closers=_closing.TRANSPORT_ONLY)
            elapsed = asyncio.get_running_loop().time() - t0
            logger.info(f"{self.name}: hard cancel took {elapsed:.3f}s")
