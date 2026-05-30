"""Pipecat ``STTService`` wrapper over the local ``stt_server`` websocket.

Subclasses Pipecat's ``SegmentedSTTService`` so VAD-driven buffering, branch
VAD subclass dispatch, and ``TranscriptionFrame.finalized=True`` all continue
to work. Each instance owns exactly one websocket session, so Koda's dual
bot ends up with two independent sessions (``me`` / ``them``) exactly like
the two-in-process Whisper setup it replaces.

Lifecycle wire mapping (matches docs/dev_plans/20260420-design-whisper-websocket-server.md):

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
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Optional

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

# Wait at most this long for a decode round trip before surfacing an
# error frame and giving up on the segment. Covers the 16 kHz / 60 s
# server cap plus a little MLX decode slack.
_DECODE_TIMEOUT_SECONDS = 90.0

# Bounded wait for session.close to be acknowledged on pipeline shutdown.
_CLOSE_TIMEOUT_SECONDS = 5.0

# Bounded wait for session.updated after session.update.
_SESSION_READY_TIMEOUT_SECONDS = 5.0

# Reconnect back-off schedule. Doubles 0.5 → 8.0s before giving up, total
# ~15.5s of wall clock. Sized to cover the LaunchAgent keepalive window
# (``ThrottleInterval=10`` in scripts/koda-stt.plist.template) plus a
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
        language: str = "en",
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
        self._client: Optional[TranscriptionClient] = None
        self._reader_task: Optional[asyncio.Task] = None
        self._pending: Optional[asyncio.Future[str]] = None
        # Resolved by the reader when a session.updated or error event arrives
        # after session.update; _ensure_connected awaits this before returning
        # so the first commit cannot race the language config.
        self._session_ready: Optional[asyncio.Future[None]] = None
        self._run_stt_lock = asyncio.Lock()
        self._connected = False
        # Backend identity from the most recent server.hello. The model is
        # pinned server-side (launchd env), so the client cannot know it
        # until the handshake completes — these stay None until the first
        # successful connect, then reflect whatever ASR is actually serving.
        self._backend_name: Optional[str] = None
        self._backend_model: Optional[str] = None

    # ------------------------------------------------------------------
    # Backend identity (populated on connect from server.hello)
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> Optional[str]:
        """ASR backend the server reported on connect (e.g. ``parakeet``).

        ``None`` until the first successful handshake — the model is pinned
        server-side, so the client cannot know it before connecting.
        """
        return self._backend_name

    @property
    def backend_model(self) -> Optional[str]:
        """Model id the server reported on connect (e.g.
        ``mlx-community/parakeet-tdt-0.6b-v3``). ``None`` until connected."""
        return self._backend_model

    # ------------------------------------------------------------------
    # Pipecat lifecycle
    # ------------------------------------------------------------------

    async def start(self, frame: StartFrame) -> None:
        await super().start(frame)
        if frame.audio_in_sample_rate and frame.audio_in_sample_rate != P.AUDIO_SAMPLE_RATE_HZ:
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
                logger.warning(f"WebSocketSTTService: connect failed: {exc}")
                yield ErrorFrame(error=f"stt_server connect failed: {exc}")
                return

            assert self._client is not None
            loop = asyncio.get_running_loop()
            self._pending = loop.create_future()
            # Scale the decode timeout with audio length so long VAD turns
            # (the server accepts up to MAX_UNCOMMITTED_SECONDS ≈ 300 s)
            # don't trip the client while the server is still decoding.
            audio_seconds = len(audio) / (P.AUDIO_SAMPLE_RATE_HZ * P.AUDIO_SAMPLE_WIDTH_BYTES)
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
                except asyncio.TimeoutError:
                    # The server is still decoding; a late completed would
                    # otherwise resolve the NEXT segment's pending future with
                    # stale text (no item_id correlation in V1). Drop the
                    # socket so the next run_stt reconnects cleanly.
                    logger.warning(f"{self.name}: decode timed out — resetting connection")
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
                hello = await client.connect()
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
                await client.update_session(turn_detection=None, language=self._language)
                try:
                    await asyncio.wait_for(
                        self._session_ready, timeout=_SESSION_READY_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError:
                    raise RuntimeError("stt_server did not ack session.update")
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
                return
            except Exception as exc:
                last_exc = exc
                # Tear down this attempt's client + reader before retrying so
                # late events from the superseded socket can't poison the
                # next attempt's _session_ready / _pending futures.
                await self._discard_stale()
                if attempt + 1 < total_attempts:
                    delay = _RECONNECT_BACKOFF_SECONDS[attempt]
                    logger.warning(
                        f"{self.name}: connect attempt {attempt + 1} failed ({exc}) "
                        f"[endpoint={endpoint}], retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
        assert last_exc is not None
        logger.error(
            f"{self.name}: giving up after {total_attempts} connect attempts to {endpoint}"
        )
        raise last_exc

    def _endpoint_label(self) -> str:
        kw = self._connect_kwargs
        if kw.get("socket_path"):
            return f"unix:{kw['socket_path']}"
        if kw.get("uri"):
            return kw["uri"]
        host = kw.get("host") or "127.0.0.1"
        port = kw.get("port")
        return f"ws://{host}:{port}" if port else f"ws://{host}"

    async def _discard_stale(self) -> None:
        """Drop a dead client + reader without blocking on a broken socket."""
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        self._reader_task = None
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        self._client = None
        self._connected = False

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
                    if self._pending and not self._pending.done():
                        self._pending.set_result(ev.get("transcript", ""))
                elif etype == P.EVT_TRANSCRIPT_FAILED:
                    err = ev.get("error") or {}
                    msg = err.get("message") or err.get("code") or "stt_server transcription failed"
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
                    if self._session_ready is not None and not self._session_ready.done():
                        self._session_ready.set_exception(exc)
                    if self._pending and not self._pending.done():
                        self._pending.set_exception(exc)
                    elif self._session_ready is None or self._session_ready.done():
                        logger.warning(f"{self.name}: server error: {ev}")
                elif etype == P.EVT_SESSION_UPDATED:
                    if self._session_ready is not None and not self._session_ready.done():
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
                    logger.warning(f"{self.name}: connection lost (no session.closed received)")
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
        # Shutdown-phase timing: the Pipecat 20 s ``wait_for_cancel`` warning
        # is opaque by the time it fires — "STT close took Ns" from this
        # wrapper pins the blame here immediately instead.
        t0 = asyncio.get_running_loop().time()
        try:
            try:
                await asyncio.wait_for(client.close_session(), timeout=_CLOSE_TIMEOUT_SECONDS)
            except Exception:
                pass
            # Give the reader a bounded window to observe session.closed.
            if self._reader_task is not None:
                try:
                    await asyncio.wait_for(self._reader_task, timeout=_CLOSE_TIMEOUT_SECONDS)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    self._reader_task.cancel()
        finally:
            await client.close()
            self._client = None
            self._reader_task = None
            self._connected = False
            elapsed = asyncio.get_running_loop().time() - t0
            logger.info(f"{self.name}: graceful close took {elapsed:.3f}s")

    async def _cancel_and_close(self) -> None:
        if self._client is None:
            return
        client = self._client
        t0 = asyncio.get_running_loop().time()
        try:
            try:
                await client.cancel()
            except Exception:
                pass
            if self._pending and not self._pending.done():
                self._pending.cancel()
            if self._reader_task is not None:
                self._reader_task.cancel()
                try:
                    await self._reader_task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            await client.close()
            self._client = None
            self._reader_task = None
            self._connected = False
            elapsed = asyncio.get_running_loop().time() - t0
            logger.info(f"{self.name}: hard cancel took {elapsed:.3f}s")
