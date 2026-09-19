"""Shared runtime helpers for the onoats recorder entrypoints.

Both ``onoats/__main__.py`` (single-input) and ``onoats/dual.py`` (dual-input)
need the same PID-file discipline, signal handlers, STT service builder, crash
recovery, and graceful shutdown / pipeline-lifecycle coordination
(``wait_or_force``, ``stop_pipeline_for_shutdown``). Extracting them here avoids
having ``dual.py`` reach into ``__main__.py`` for leading-underscore symbols,
and gives both entrypoints a single canonical home for the
``_topic_pipeline_tasks`` set.

The recorder emits files only — it opens no SQLite and runs no
post-processing. A downstream consumer drains the ``pending/`` queue.

Nothing here is public API — the module is internal to ``onoats``.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import os
import platform
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    # Type-checking only: this module keeps MLX/pipecat-service imports lazy
    # (see `_create_stt_service`'s docstring) so a plain `import onoats.runtime`
    # with no `STT_SERVICE` set never pulls in `mlx_whisper`. `from __future__
    # import annotations` (above) makes every annotation a string, so this
    # import never runs at module load time — only under a type checker.
    from pipecat.services.stt_service import STTService

from loguru import logger

from onoats import _closing

# Module scope, like `_closing` above: `status` is an equally leaf-level
# module (stdlib + loguru only) and imports nothing from `onoats`, so the
# four function-local imports these replaced were deferring a cycle that
# does not exist — the same finding that hoisted `launchd.py`'s.
from onoats import status as _status_mod
from onoats._redact import display_uri, safe_exc_text
from onoats._vendor.pid import (  # noqa: F401
    PID_FILENAME,
    PID_MARKER,
)
from onoats._vendor.pid import (
    read_pid_file as _read_pid_file,
)

# termios/tty/fcntl are Unix-only — guard for Windows compatibility
if sys.platform != "win32":
    import fcntl
    import termios
    import tty

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_NAME = "onoats"
STT_SERVICE = os.getenv("STT_SERVICE", "whisper").lower().strip()
STT_MODEL = os.getenv("STT_MODEL", "").strip()


class SttPreflightError(RuntimeError):
    """Raised when the stt_server endpoint is not reachable at startup.

    Caught at the CLI entrypoints (``bot/__main__.py``, ``bot/dual.py``) so
    the user sees the actionable hint — not a Python traceback — when the
    LaunchAgent isn't loaded.
    """


class RecorderAlreadyRunningError(RuntimeError):
    """Raised at startup when an identity-verified live recorder already owns the
    pid file.

    Caught at the same CLI entrypoints as ``SttPreflightError`` so the user sees
    an actionable hint, not a traceback. The existing recorder's pid file is left
    intact — we refuse BEFORE overwriting it — which closes the
    stop-then-immediate-start race: a second start can no longer clobber a
    draining recorder's pid file (and the drainer can no longer later unlink the
    second start's file). The flagged recorder is verified via the same identity
    gate as ``onoats stop``/``flush`` (marker + cmdline fingerprint + liveness),
    so a stale/recycled/foreign pid never blocks a legitimate start.
    """


PIPELINE_SAMPLE_RATE = 16000  # Silero VAD requires 8kHz or 16kHz; 16kHz is standard


def _env_float(
    env_name: str, default: float, *, min_value: float | None = None
) -> float:
    """Read a float tunable from the environment, falling back on bad input.

    A malformed value must not crash the recorder at import time — log and
    use the default instead. Mirrors the ValueError-tolerance of
    ``OnoatsConfig._tuning_float`` for the env-only tunables read here.

    ``min_value`` clamps an out-of-range value (e.g. a negative timeout, which
    would otherwise make ``asyncio.wait(timeout=...)`` fire immediately and
    silently defeat the feature) up to the floor, with a warning.
    """
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"{env_name}={raw!r} is not a number; using {default}")
        return default
    if min_value is not None and value < min_value:
        logger.warning(f"{env_name}={value} is below minimum {min_value}; clamping")
        return min_value
    return value


# --- Shutdown timing -------------------------------------------------------
#
# Graceful two-phase shutdown (see _shutdown_watcher in __main__/dual):
#
#   1. DRAIN — queue an EndFrame (``task.stop_when_done()``). An EndFrame, unlike
#      a CancelFrame, lets an in-flight segment finish: SegmentedSTTService
#      awaits ``run_stt`` inline in ``process_frame``, so the EndFrame queues
#      behind it, the final ``TranscriptionFrame`` is delivered to
#      TranscriptBuffer, and only then does the pipeline end. This is what makes
#      the terminal flush capture the last spoken segment. The drain ends as
#      soon as the pipeline finishes, so the common (nothing-pending) case exits
#      promptly; SHUTDOWN_DRAIN_TIMEOUT_SEC bounds a stalled drain.
#
#   2. CANCEL (fallback) — if the drain stalls past its timeout, or a second
#      Ctrl+C forces exit, hard-cancel with ``task.cancel()`` (a CancelFrame).
#      pipecat then waits up to ``cancel_timeout_secs`` for that frame to reach
#      the pipeline end; the local-audio transport tends to block it, so this is
#      capped at SHUTDOWN_CANCEL_TIMEOUT_SEC instead of pipecat's 20s default to
#      avoid a hung-feeling exit. A CancelFrame *aborts* in-flight STT, so this
#      fallback is best-effort teardown, not a transcript-preserving path.
#
# Both are env-only operator escape hatches (no config.toml [tuning] key),
# matching how ``__main__`` reads its other tunables (e.g. SILENCE_TIMEOUT_SEC)
# raw from the environment without loading config.
SHUTDOWN_DRAIN_TIMEOUT_SEC = _env_float(
    "SHUTDOWN_DRAIN_TIMEOUT_SEC", 8.0, min_value=0.0
)
SHUTDOWN_CANCEL_TIMEOUT_SEC = _env_float(
    "SHUTDOWN_CANCEL_TIMEOUT_SEC", 2.0, min_value=0.0
)


async def wait_or_force(coro_or_future, label: str, *, force_exit_event) -> None:
    """Await a coroutine/future, cancelling it immediately if force_exit fires.

    Shared by both recorders' shutdown paths (crash-recovery wait, task drain)
    so a second Ctrl+C (``force_exit_event``) can interrupt a stuck wait.
    """
    wait_task = asyncio.ensure_future(coro_or_future)
    force_task = asyncio.create_task(force_exit_event.wait(), name="force_exit_wait")
    done, _ = await asyncio.wait(
        {wait_task, force_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if force_task in done:
        logger.warning(f"Shutdown: force-cancelling {label}")
        wait_task.cancel()
        try:
            await wait_task
        except asyncio.CancelledError:
            pass
    else:
        force_task.cancel()


async def stop_pipeline_for_shutdown(
    task, force_exit_event, *, drain_timeout_sec: float = SHUTDOWN_DRAIN_TIMEOUT_SEC
) -> None:
    """Graceful two-phase pipeline shutdown shared by both recorders.

    Despite the "stop" name this both drains and (as a fallback) hard-cancels.

    Phase 1 (drain): queue an EndFrame via ``task.stop_when_done()`` so an STT
    segment whose transcription is in flight finishes and reaches
    TranscriptBuffer before teardown. Wait for the pipeline to finish, bounded
    by ``drain_timeout_sec`` and by a second Ctrl+C (``force_exit_event``). The
    wait ends as soon as the pipeline drains, so the common case (nothing
    pending) returns promptly.

    Phase 2 (fallback): if the drain stalls past its timeout or is force-exited,
    hard-cancel with ``task.cancel()`` (a CancelFrame, internally capped at the
    task's ``cancel_timeout_secs`` = SHUTDOWN_CANCEL_TIMEOUT_SEC). A CancelFrame
    aborts in-flight STT, so this is best-effort teardown only.

    The caller flushes (rotates the buffer into pending/) AFTER this returns —
    in the drained case that flush captures the final segment.
    """
    if task.has_finished():
        return

    await task.stop_when_done()

    # pipecat's PipelineTask exposes no awaitable "finished" event, only the
    # synchronous has_finished() predicate — poll it at 50ms (cheap on a
    # one-shot shutdown path) and race it against force_exit + the drain bound.
    async def _await_finished() -> None:
        while not task.has_finished():
            await asyncio.sleep(0.05)

    finished_task = asyncio.ensure_future(_await_finished())
    force_task = asyncio.ensure_future(force_exit_event.wait())
    try:
        await asyncio.wait(
            {finished_task, force_task},
            timeout=drain_timeout_sec,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        # Cancel the loser AND await both so a still-pending task settles its
        # CancelledError this turn (no "Task was destroyed but it is pending").
        for t in (finished_task, force_task):
            if not t.done():
                t.cancel()
        await asyncio.gather(finished_task, force_task, return_exceptions=True)

    if task.has_finished():
        return

    reason = "forced (2nd Ctrl+C)" if force_exit_event.is_set() else "drain timed out"
    logger.info(f"Shutdown: {reason} — hard-cancelling pipeline")
    await task.cancel()


# Map simple model name strings to MLXModel enum member names
_MLX_MODEL_MAP: dict[str, str] = {
    "tiny": "TINY",
    "medium": "MEDIUM",
    "large-v3": "LARGE_V3",
    "large-v3-turbo": "LARGE_V3_TURBO",
    "large-v3-turbo-q4": "LARGE_V3_TURBO_Q4",
    "distil-large-v3": "DISTIL_LARGE_V3",
}

# Shared across entrypoints: topic-pipeline tasks spawned during
# post-processing. Drained on shutdown. Assumes ``bot/__main__.py`` and
# ``bot/dual.py`` are mutually exclusive within a single process — both
# entrypoints drain this set, so running them side-by-side would have them
# cancelling each other's tasks on shutdown.
_topic_pipeline_tasks: set[asyncio.Task] = set()


# ---------------------------------------------------------------------------
# STT service construction
# ---------------------------------------------------------------------------


def _mlx_available() -> bool:
    """Return True if MLX Whisper can run on this machine (Apple Silicon)."""
    if platform.machine() != "arm64":
        return False
    try:
        import mlx_whisper  # noqa: F401

        return True
    except ImportError:
        return False


_DEFAULT_STT_WS_SOCKET = "~/Library/Caches/pipecat-stt/stt.sock"


def _resolve_stt_ws_target(
    env: dict[str, str], *, warn_on_cleartext: bool = True
) -> dict[str, object]:
    """Resolve STT_WS_* env vars into the kwargs for ``WebSocketSTTService``.

    Delegates precedence handling to ``stt_server.client.resolve_endpoint_from_env``
    and layers on the onoats default socket plus the ``STT_WS_TOKEN``
    bearer read. When operators point at a cleartext remote host, warn
    before attaching the token so a passive on-path observer cannot
    silently capture it.

    Set ``warn_on_cleartext=False`` for secondary callers (e.g. the RSS
    probe at startup/shutdown) that resolve the same endpoint and would
    otherwise emit the warning repeatedly in a single session.
    """
    from stt_server.client import (
        format_host_for_uri,
        is_cleartext_remote,
        resolve_endpoint_from_env,
    )

    resolved = resolve_endpoint_from_env(env)
    socket_path = resolved["socket_path"]
    host = resolved["host"]
    port = resolved["port"]
    uri = resolved["uri"]
    auth_token = (env.get("STT_WS_TOKEN") or "").strip() or None

    if not (socket_path or host or uri):
        socket_path = env.get("STT_WS_DEFAULT_SOCKET") or os.path.expanduser(
            _DEFAULT_STT_WS_SOCKET
        )

    # Cleartext-token guard covers *any* cleartext-ws endpoint, not just
    # STT_WS_URI. host+port paths get lowered to ``ws://host:port/`` via
    # the same formatter the client uses (IPv6 literals bracketed) so the
    # ``is_cleartext_remote`` check is identical regardless of which
    # supported config surface the operator chose.
    effective_uri = uri
    if not effective_uri and host and port is not None and not socket_path:
        effective_uri = f"ws://{format_host_for_uri(host)}:{port}/"
    if (
        warn_on_cleartext
        and auth_token
        and effective_uri
        and is_cleartext_remote(effective_uri)
    ):
        logger.warning(
            f"STT: STT_WS_TOKEN will be sent in cleartext to "
            f"{_display_target({'uri': effective_uri})}. Use wss:// for remote "
            "hosts, or bind to loopback (127.0.0.1 / ::1 / UDS)."
        )

    return {
        "socket_path": socket_path,
        "host": host,
        "port": port,
        "uri": uri,
        "auth_token": auth_token,
    }


def _display_target(kwargs: dict) -> str:
    """Render an endpoint as a safe human-readable string for logs/errors.

    ``STT_WS_URI`` is user-controlled and may contain userinfo
    (``ws://user:pass@host/``). Strip it before rendering so a typoed
    secret doesn't echo into stderr or a log line.

    Delegates to ``onoats._redact.display_uri`` — the single owner of the
    redact-then-strip-query composition both display call sites need —
    rather than ``urlsplit``'s ``username``/``password`` properties: those
    silently report NO userinfo at all (rather than raising) when the
    password contains an unencoded ``/``, ``?``, or ``#`` — exactly the
    malformed-but-realistic credential shape this function most needs to
    catch — which would otherwise let the raw, credential-bearing ``uri``
    fall through unchanged. One redaction implementation, not two
    independently-maintained ones.
    """
    uri = kwargs.get("uri")
    if uri:
        return display_uri(uri)
    return kwargs.get("socket_path") or f"{kwargs.get('host')}:{kwargs.get('port')}"


# `safe_exc_text`/`redact_uri` live in `onoats._redact` — a leaf module with
# no `onoats` imports — so both this module and
# `onoats.stt.websocket_stt_service` can import them top-level without either
# reaching into the other's private symbols. No `_safe_exc_text` compat
# alias: `_redact` is a new module on an unreleased branch, so there is no
# external compatibility to preserve, and mixing `_safe_exc_text` with the
# public `safe_exc_text` in the same module invited exactly the split this
# note used to describe (see git history) — every call site below uses the
# public name.


def stt_banner() -> str:
    """One-line STT description for the startup banner.

    For the websocket backend the model is pinned by the server (via the
    LaunchAgent env), not by ``STT_MODEL`` — that env var routes nowhere
    on this path, so echoing it here is misleading (e.g. printing
    ``model=large-v3-turbo`` while the server actually runs Parakeet).
    Show the resolved server target instead; the real backend + model is
    logged on connect by ``WebSocketSTTService._ensure_connected``.
    """
    from onoats.config import load_config

    cfg = load_config()
    if cfg.stt_service == "websocket":
        target = _display_target(
            _resolve_stt_ws_target(_ws_env(cfg), warn_on_cleartext=False)
        )
        return f"websocket (server={target}, model pinned by server)"
    return f"{cfg.stt_service} / model={cfg.stt_model or 'default'}"


def _ws_env(cfg) -> dict[str, str]:
    """``os.environ`` with config.toml ``[stt]`` ws_* layered in (env wins).

    ``cfg.stt_ws_*`` already resolve env-over-file, so assigning their values
    back is env-preserving. The socket path is ``expanduser``-ed so a ``~`` in
    config.toml resolves the same way the built-in default socket does. This is
    what lets ``onoats init``'s written ``ws_socket`` actually reach the
    recorder — env vars are no longer the only source.
    """
    env = dict(os.environ)
    socket = cfg.stt_ws_socket
    if socket:
        env["STT_WS_SOCKET"] = os.path.expanduser(str(socket))
    if cfg.stt_ws_host:
        env["STT_WS_HOST"] = str(cfg.stt_ws_host)
    if cfg.stt_ws_port:
        env["STT_WS_PORT"] = str(cfg.stt_ws_port)
    if cfg.stt_ws_uri:
        env["STT_WS_URI"] = str(cfg.stt_ws_uri)
    return env


_PREFLIGHT_TIMEOUT_SEC = 2.0
# Cold-start tolerance: a single 2s connect is tight when the
# LaunchAgent was just kicked (e.g. starting the STT server then the recorder).
# `stt_server.serve()` binds the socket AFTER `backend.start()` runs
# `import mlx_whisper`, which can take 1-3s on a cold Python. Without a
# retry, preflight rejects the bot on transient "socket-not-yet-bound"
# conditions that `WebSocketSTTService._ensure_connected` (15.5s total
# budget) would have tolerated at session time. Retry once on OSError
# (socket absent / connection refused) after a short delay; auth or
# protocol failures still fail on the first attempt since those aren't
# startup races.
_PREFLIGHT_RETRY_DELAY_SEC = 1.0
_PREFLIGHT_RETRY_TIMEOUT_SEC = 3.0

# Floor for a pre-kickstart retry attempt's CONNECT timeout (used only when
# the remaining pre-kickstart budget is recomputed after an intermediate
# teardown — see the retry loop in `_preflight_stt_ws` below). Deliberately
# its OWN constant, not `_closing.MIN_CLOSE_ATTEMPT_TIMEOUT_SEC`: that one
# floors a *teardown* attempt, where even a near-zero timeout is meaningful
# (a close either finishes or is cancelled cleanly either way). A CONNECT
# attempt needs enough wall-clock to plausibly complete a handshake — reusing
# the close-domain floor here would silently retune whenever that floor is
# retuned for teardown reasons, and would guarantee this connect attempt
# fails before it can do anything useful (see the budget-exhausted check
# below, which skips the attempt outright once even this floor can't be met).
_MIN_CONNECT_ATTEMPT_TIMEOUT_SEC = 0.05

# How long the preflight's final teardown may spend on a client whose connect
# has already failed. Small on purpose: the `SttPreflightError` the caller is
# about to see quotes `total_budget`, and it cannot be raised until the
# teardown returns, so every second spent here is a second the user waits
# past a number the message told them to expect. A close against a server
# that would not complete a handshake has nothing to accomplish.
_FAILED_TEARDOWN_BUDGET_SEC = 0.5

# Post-kickstart retry budget. Deliberately NOT the `_PREFLIGHT_RETRY_*`
# constants above: those were sized for "already running but slow to answer",
# a different scenario from "the process was just SIGKILLed and is reloading a
# model from cold". The dev plan explicitly sanctions a dedicated constant if
# the measured cold-restart-to-handshake time exceeds the preflight-retry
# window. What matters here is total elapsed budget, not attempt count — a
# connect against a socket that launchd has not re-bound yet fails in
# microseconds, so an attempt-count loop's real budget is only its sleeps.
_POST_KICKSTART_DEADLINE_SEC = 45.0
# Settle delay between post-kickstart connect attempts — long enough that a
# fast-failing attempt doesn't spin the loop, short enough that the deadline
# still gets ~20 attempts.
_POST_KICKSTART_SETTLE_SEC = 2.0
_POST_KICKSTART_ATTEMPT_TIMEOUT_SEC = _PREFLIGHT_RETRY_TIMEOUT_SEC
# Teardown bounds (`CLOSE_TIMEOUT_SEC`, `MIN_CLOSE_ATTEMPT_TIMEOUT_SEC`) and
# the teardown itself live in `onoats._closing`, the leaf module this shares
# with `stt.websocket_stt_service`: both modules tear down the same
# `TranscriptionClient` type and used to own opposite policies plus a
# near-duplicate 5.0s constant. Deliberately NOT re-exported under the old
# module-level names here — a local alias would look monkeypatchable while
# having no effect on the shared implementation.
# Keyed on the endpoint tuple, not a bare bool, so that if a future
# caller builds kwargs for a *different* endpoint on the second call
# (dual path today uses the same resolved kwargs for both branches, but
# nothing in the type system pins that) the probe re-runs against the
# new endpoint instead of silently trusting a stale success.
#
# Maps the endpoint tuple to the recovery message (``None`` when the probe
# succeeded without ever needing a kickstart). Round 4: this used to be a
# bare `set` (outcome-blind — recorded only "a probe ran"), which forced
# `dual.py` to manually relay a `preflight_recovered: bool` from the first
# `_create_stt_service` call to the second so the second instance's confirm
# gate could be armed even though its own preflight call was a cache hit.
# Memoizing the *outcome* here instead means a cache-hit call replays the
# same `on_recovery(message)` the original probe fired (see the cache-hit
# branch in `_preflight_stt_ws`), so every caller reads the one memoized
# result directly — no caller-to-caller relay, and no assumption about
# which of the two calls runs first.
_preflight_cache: dict[tuple[object, object, object, object], str | None] = {}


_RSS_PROBE_TIMEOUT_SEC = 2.0


async def log_stt_server_rss(phase: str) -> None:
    """Log the stt_server's PID + peak RSS at ``phase`` (``startup`` / ``shutdown``).

    Queries the running server via the ``server.status`` wire probe
    (``pid`` + ``rss_bytes`` fields) rather than discovering the process
    by command-line pattern. Topology-agnostic: works the same whether
    the server runs from a LaunchAgent, a wrapper script, a compiled
    binary, or a remote host.

    Best-effort: swallows everything and logs ``debug`` on miss so an
    unreachable server never fails bot lifecycle.
    """
    try:
        from stt_server import protocol as P
        from stt_server.client import TranscriptionClient

        from onoats.config import load_config

        # The primary STT service path already logged any cleartext-token
        # warning at session start; suppress here so startup+shutdown
        # probes don't duplicate it.
        #
        # Resolve from the config-layered env (``_ws_env``), not bare
        # ``os.environ`` — the socket lives in config.toml ``[stt] ws_socket``,
        # which the data path (``_create_stt_service``) and banner
        # (``stt_banner``) both layer in. Skipping it lands on the default
        # socket and probes a stale/wrong server (e.g. mlx instead of nemotron).
        kwargs = _resolve_stt_ws_target(_ws_env(load_config()), warn_on_cleartext=False)
        client = TranscriptionClient(
            socket_path=kwargs.get("socket_path"),
            host=kwargs.get("host"),
            port=kwargs.get("port"),
            uri=kwargs.get("uri"),
            auth_token=kwargs.get("auth_token"),
        )

        async def _probe() -> None:
            await client.connect()
            await client.status()
            # Held as a local so `finally` can explicitly `aclose()` it: a
            # `wait_for` timeout/cancellation raises inside the generator's
            # own suspended frame, which unwinds it but does not mark it
            # closed — the underlying websocket-read state the generator
            # owns can outlive this function's return without an explicit
            # `aclose()` call.
            events = client.events()
            try:
                async for event in events:
                    if event.get("type") != P.EVT_SERVER_STATUS:
                        continue
                    pid = event.get("pid")
                    rss = event.get("rss_bytes")
                    uptime = event.get("uptime_seconds")
                    rss_mb = (
                        (int(rss) / (1024 * 1024))
                        if isinstance(rss, (int, float))
                        else 0.0
                    )
                    uptime_s = (
                        float(uptime) if isinstance(uptime, (int, float)) else 0.0
                    )
                    # server.status mirrors the server.hello backend identity, so
                    # the probe line names the real ASR behind the socket — a
                    # wrong-model misconfig shows up in the RSS log too, not just
                    # at connect. Additive field: omit cleanly on older servers.
                    backend = event.get("backend") or {}
                    backend_desc = (
                        f" backend={backend.get('name', '?')}/{backend.get('model', '?')}"
                        if backend
                        else ""
                    )
                    logger.info(
                        f"stt_server RSS ({phase}): pid={pid} rss={rss_mb:.1f}MB "
                        f"session_uptime={uptime_s:.1f}s{backend_desc}"
                    )
                    return
                logger.debug(f"stt_server RSS ({phase}): status reply missing")
            finally:
                with contextlib.suppress(Exception):
                    await events.aclose()

        # Captured *before* the probe, not after it. Recomputing
        # `loop.time() + _RSS_PROBE_TIMEOUT_SEC` in the `finally` handed
        # teardown a fresh full window on top of whatever the probe had
        # already spent, so a probe that timed out and then hung in
        # `close_session` took ~2 + ~2 seconds inside a function whose
        # docstring calls itself 2-second-bounded. One absolute deadline
        # bounds the whole thing, probe and teardown together.
        deadline = asyncio.get_running_loop().time() + _RSS_PROBE_TIMEOUT_SEC
        try:
            await asyncio.wait_for(_probe(), timeout=_RSS_PROBE_TIMEOUT_SEC)
        finally:
            # Blast-radius sweep (round 3): the `wait_for` above bounds the
            # probe, not this teardown — and a server unreachable enough to
            # fail the probe is exactly the one whose `close_session` ack
            # never arrives. Same bounded closer as every other teardown of
            # this client type.
            #
            # Round 4: deadline-bound this too. Without it, `_close_client_
            # quietly`'s two fixed-timeout closers (`close_session`, `close`)
            # can run up to ~10s combined against an unreachable server, on
            # top of the ~2s `wait_for` above already spent — costing up to
            # ~12s total against a probe this function's own docstring calls
            # "2s-bounded". Give teardown the *remaining* share of the one
            # window this whole function gets — not a fresh one. Recomputing
            # the deadline here handed a probe that had already burned its
            # full timeout another full timeout to hang in `close_session`,
            # which is ~2 + ~2 seconds inside a 2-second budget. An expired
            # deadline still gets each closer a real attempt
            # (`_closing.MIN_CLOSE_ATTEMPT_TIMEOUT_SEC`), so reusing it
            # bounds the teardown without ever skipping it.
            await _closing.close_quietly(client, deadline=deadline)
    except Exception as exc:
        # Same leak class as `_preflight_stt_ws`/`_ensure_connected`: this
        # probe builds its own `TranscriptionClient` from the same
        # `STT_WS_URI` and calls `connect()`, so a malformed URI can raise
        # `websockets.exceptions.InvalidURI` with the raw, unredacted URI
        # embedded in its message here too. Gated by LOG_LEVEL=DEBUG, but
        # the fix is the same — route through `safe_exc_text`.
        logger.debug(f"stt_server RSS ({phase}): probe failed ({safe_exc_text(exc)})")


def _preflight_key(kwargs: dict) -> tuple[object, object, object, object]:
    return (
        kwargs.get("uri"),
        kwargs.get("socket_path"),
        kwargs.get("host"),
        kwargs.get("port"),
    )


class KickstartOutcome(enum.Enum):
    """``_kickstart_and_retry``'s outcome — see its docstring for what each
    member means. An enum (checkable, no typo-silently-falls-through-to-
    ``None`` risk), not the three magic strings this replaced: a producer/
    consumer spelling mismatch on a bare string outcome would previously
    fall through ``_resolve_kickstart_outcome``'s comparisons silently
    (treated as "kickstart failed") with no error."""

    RECOVERED = "recovered"
    RETRY_EXHAUSTED = "retry_exhausted"
    KICKSTART_FAILED = "kickstart_failed"


class KickstartResult(NamedTuple):
    """``_kickstart_and_retry``'s return value — a named pair, not a bare
    tuple, for the same reason ``SttServiceResult`` replaced `_create_stt_
    service`'s: a future field needs a seam that doesn't re-break every
    caller's positional unpacking. `NamedTuple`, not a plain dataclass,
    because both call sites destructure it with tuple-unpacking
    (``outcome, recovered = await _kickstart_and_retry(...)``) and that
    calling convention is preserved unchanged."""

    outcome: KickstartOutcome
    client: object | None


async def _kickstart_and_retry(
    make_client: Callable[[], object],
    label: str,
    on_recovery: Callable[[str | None], None] | None,
    *,
    target: str,
    hint: str,
) -> KickstartResult:
    """Best-effort kickstart + deadline-bounded post-kickstart connect retry.

    Called only from ``_preflight_stt_ws``'s final-attempt exhaustion and only
    for handshake-unreachable exceptions (``TimeoutError``/``OSError``). The
    cooldown check, stamp and kickstart are delegated wholesale to
    ``onoats.stt.launchd.try_kickstart`` — the one owner of that sequence,
    shared with the live path — so this function no longer re-implements the
    ordering guarantee. A failed kickstart still counts against the window.

    **Budget, not attempt count.** The retry loop runs against a monotonic
    deadline (``_POST_KICKSTART_DEADLINE_SEC``) rather than a fixed number of
    attempts: a connect issued microseconds after ``launchctl kickstart -k``
    fails instantly (socket unlinked / connection refused) instead of
    consuming its per-attempt timeout, so an attempt-count loop's *real*
    wall-clock budget collapses to just the inter-attempt sleeps. A cold
    mlx/nemotron model load after a genuine restart takes far longer than
    that, which made a SUCCESSFUL kickstart still abort the session with
    "still unreachable after retry".

    **Fresh client per attempt.** Each attempt constructs its own client and
    closes it on failure. Reusing one client meant a connect that got past
    ``ws_connect`` but failed at handshake left an orphaned websocket which
    the next attempt silently overwrote — a leaked socket/FD per failed
    attempt, with only the last one ever closed.

    Returns a ``KickstartResult(outcome, client)``:
      ``RECOVERED, client``       — kickstart succeeded AND a post-kickstart
                                     handshake succeeded on ``client`` (now
                                     the live, connected client, which the
                                     caller owns and must close);
                                     ``on_recovery`` already fired.
      ``KICKSTART_FAILED, None``  — cooldown still active, or ``launchctl
                                     kickstart`` itself failed — caller falls
                                     through to today's unchanged
                                     ``SttPreflightError``.
      ``RETRY_EXHAUSTED, None``   — kickstart succeeded but the deadline
                                     expired with the server still
                                     unreachable — caller raises a
                                     kickstart-noting error.

    **Only reachability failures are retried.** A post-kickstart attempt that
    fails with anything other than ``TimeoutError``/``OSError`` (auth rejected,
    protocol error, an unexpected first frame) is a client-side
    misconfiguration, not a restart race: retrying it just delays the real
    error until the whole deadline expires. Those raise ``SttPreflightError``
    immediately, matching the pre-kickstart schedule's fail-fast behaviour for
    the same exception shapes. ``asyncio.CancelledError`` (task cancellation
    during shutdown) propagates — but not before the in-flight candidate is
    closed, so a cancelled preflight cannot leak its socket.

    **The deadline is strict from the second attempt on.** The first probe
    always runs (there is no point kickstarting and then not looking), but
    every attempt after it checks the remaining budget before sleeping, caps
    the settle-sleep to it, re-checks, and caps the connect timeout to what is
    left — so a failure landing just under the deadline cannot overrun it by a
    whole sleep-plus-timeout. The per-attempt client **teardown** is capped by
    the same remaining budget: it runs before the next budget check, so a
    fixed-timeout teardown (up to ``2 * _closing.CLOSE_TIMEOUT_SEC``) was
    itself able to push real wall-clock past the cap this docstring calls
    strict.
    """
    from onoats.stt.launchd import recovery_message, try_kickstart

    if not await try_kickstart(label):
        return KickstartResult(KickstartOutcome.KICKSTART_FAILED, None)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _POST_KICKSTART_DEADLINE_SEC
    attempt = 0
    while True:
        timeout_s = _POST_KICKSTART_ATTEMPT_TIMEOUT_SEC
        if attempt > 0:
            # Strict deadline from the second attempt on: check the budget
            # before the settle-sleep, cap the sleep to it, re-check after,
            # and cap the connect timeout to what is still left. Previously a
            # failure landing just under the deadline still bought itself a
            # full sleep plus a full connect timeout, overrunning by ~5s.
            remaining = deadline - loop.time()
            if remaining <= 0:
                return KickstartResult(KickstartOutcome.RETRY_EXHAUSTED, None)
            await asyncio.sleep(min(_POST_KICKSTART_SETTLE_SEC, remaining))
            remaining = deadline - loop.time()
            if remaining <= 0:
                return KickstartResult(KickstartOutcome.RETRY_EXHAUSTED, None)
            timeout_s = min(timeout_s, remaining)
        candidate: object | None = None
        try:
            # Inside the try: a factory that raises (mis-shaped kwargs reaching
            # `TranscriptionClient.__init__`) must still surface as this
            # function's documented `SttPreflightError` contract, not as a raw
            # exception escaping `_preflight_stt_ws`.
            candidate = make_client()
            await asyncio.wait_for(candidate.connect(), timeout=timeout_s)
        except (TimeoutError, OSError):
            if candidate is not None:
                # Bounded by what is LEFT of the deadline, not a fixed 5s per
                # closer: this teardown runs before the loop's next budget
                # check, so an unbounded-by-budget close is itself able to
                # overrun the cap this loop documents as strict.
                #
                # `TRANSPORT_ONLY`: `connect()` just failed, so no session
                # exists. `close_quietly` spends ONE deadline-derived budget
                # across all its closers in order, and `close_session` against
                # the wedged server these paths exist for cannot succeed —
                # it hangs to its own timeout and leaves `close`, the call
                # that actually releases the FD, with the 0.05s floor.
                await _closing.close_quietly(
                    candidate, deadline=deadline, closers=_closing.TRANSPORT_ONLY
                )
            attempt += 1
            continue
        except Exception as exc:
            if candidate is not None:
                # `TRANSPORT_ONLY`: the failed `connect()` above left no
                # session to close. See the `TimeoutError`/`OSError` branch.
                await _closing.close_quietly(
                    candidate, deadline=deadline, closers=_closing.TRANSPORT_ONLY
                )
            raise SttPreflightError(
                f"STT: handshake failed at {target} after kickstarting "
                f"{label!r} ({type(exc).__name__}: {safe_exc_text(exc)}). {hint}"
            ) from exc
        except BaseException:
            # asyncio.CancelledError is a BaseException: without this the
            # in-flight candidate's socket/FD leaks on shutdown cancellation.
            # `deadline=deadline` matches the TimeoutError/OSError/Exception
            # branches above: without it this teardown falls back to the full
            # fixed `_closing.CLOSE_TIMEOUT_SEC` per closer (up to 10s
            # combined) instead of being capped by this loop's own strict budget,
            # so a shutdown/CancelledError arriving mid-attempt could hang
            # process exit far longer than the rest of the self-healing
            # design's shutdown-responsiveness budget promises.
            if candidate is not None:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    # `TRANSPORT_ONLY` for the same reason as the branches
                    # above, and doubly so here: shutdown cancellation is the
                    # one path where spending the budget on a `close_session`
                    # that cannot complete directly delays process exit.
                    await _closing.close_quietly(
                        candidate, deadline=deadline, closers=_closing.TRANSPORT_ONLY
                    )
            raise
        if on_recovery is not None:
            # Swallow-and-log, like every other recovery-callback site
            # (`WebSocketSTTService._fire_recovery`, the `_create_stt_service`
            # closure). This one was missed: it is on the SUCCESS path, so an
            # unguarded raise here aborts an otherwise-completed recovery
            # *and* leaks `candidate` — the live, connected client the caller
            # is supposed to take ownership of and close. Recovery reporting
            # is best-effort; the recovered connection is not.
            try:
                on_recovery(recovery_message(label))
            except Exception as exc:
                logger.warning(
                    f"STT: on_recovery callback failed: {safe_exc_text(exc)}"
                )
        return KickstartResult(KickstartOutcome.RECOVERED, candidate)


def _resolve_kickstart_outcome(
    outcome: KickstartOutcome,
    recovered: object | None,
    *,
    exhausted_message: str,
    exc: Exception,
) -> object | None:
    """Shared "what does this ``_kickstart_and_retry`` outcome mean" branch,
    called from both the ``TimeoutError`` and ``OSError`` except-blocks of
    ``_preflight_stt_ws`` below — their only difference is the message
    template for ``RETRY_EXHAUSTED``.

    Returns the recovered, live client on a ``"recovered"`` outcome — but
    deliberately does NOT perform the caller's ownership-transfer-then-close
    dance itself, and cannot be made to: that dance's ordering requirement
    (assign the caller's ``client`` local to the new, live client BEFORE
    awaiting the stale client's close) needs the assignment to happen in the
    CALLER's own stack frame with no ``await`` boundary in between, so that a
    cancellation arriving mid-close still leaves the caller's `finally`
    block (``await _closing.close_quietly(client)``) pointing at the live
    client rather than the already-dead stale one. Moving the swap-then-
    close pair into an ``await``ed helper was tried and reverted: by the
    time such a helper returns and its result is assigned, the caller's
    `client` local has already sat unassigned across the internal
    `await`, so a cancellation landing inside that internal close would
    have left `finally` re-closing the dead stale client while the
    genuinely live, recovered one — reachable only from the now-abandoned
    helper call — leaked. The swap must stay inline at each call site and
    happen SYNCHRONOUSLY (``client = _resolve_kickstart_outcome(...)`` with
    no ``await`` on the right-hand side) before anything is awaited — see
    the call sites' own comments for why that ordering matters.
    Raises ``SttPreflightError`` (chained from ``exc``) on
    ``RETRY_EXHAUSTED``. Returns ``None`` on ``KICKSTART_FAILED``
    (kickstart itself failed, or the cooldown was still active) — the caller
    falls through to its own unchanged, un-kickstarted error.
    """
    if outcome == KickstartOutcome.RETRY_EXHAUSTED:
        raise SttPreflightError(exhausted_message) from exc
    if outcome == KickstartOutcome.RECOVERED:
        return recovered
    return None


async def _preflight_stt_ws(
    kwargs: dict,
    target: str,
    *,
    launchd_label: str | None = None,
    on_recovery: Callable[[str | None], None] | None = None,
) -> None:
    """Fail fast if the stt_server endpoint is not reachable at startup.

    Runs a real websocket handshake (``TranscriptionClient.connect()`` —
    which awaits ``server.hello`` + ``session.created``), so auth, TLS,
    wrong path, and "non-STT service on the port" failures are all
    surfaced here as ``SttPreflightError`` instead of leaking through as
    generic tracebacks or the old 30–60 s VAD-driven reconnect cascade.

    Idempotent per endpoint: the dual entrypoint calls
    ``_create_stt_service()`` twice and today both use the same resolved
    kwargs; a repeated call for the same ``(uri, socket_path, host,
    port)`` tuple is a no-op. A call for a *different* tuple re-runs the
    probe.

    Runtime reconnect during a live session is still handled by
    ``WebSocketSTTService._ensure_connected`` — this check only runs once
    before the pipeline starts.

    ``launchd_label``/``on_recovery`` gate the self-healing kickstart:
    never read from ``kwargs`` (which stays exactly the
    ``uri``/``socket_path``/``host``/``port``/``auth_token`` shape
    ``WebSocketSTTService(**kwargs)`` expects). Kickstart is attempted only
    on final-attempt exhaustion of a handshake-unreachable exception
    (``TimeoutError``/``OSError``) — never for ``ValueError``/other
    protocol errors, which continue to raise immediately, unchanged — and
    only when ``launchd_label`` is set and the shared cooldown
    (``onoats.stt.launchd``) has elapsed for it. With no label configured,
    or an active cooldown, or a failed kickstart, this reproduces today's
    ``SttPreflightError`` byte-for-byte.

    A cache hit (``key`` already probed by an earlier call in this
    process, for this or a different caller) replays the memoized outcome:
    if that earlier call recovered via kickstart, ``on_recovery`` is
    invoked here too, with the SAME message, so a second caller that never
    itself probes still observes the recovery (see ``_preflight_cache``'s
    module docstring).
    """
    key = _preflight_key(kwargs)
    if key in _preflight_cache:
        cached_message = _preflight_cache[key]
        if cached_message is not None and on_recovery is not None:
            try:
                on_recovery(cached_message)
            except Exception as exc:
                logger.warning(
                    f"STT: on_recovery callback failed: {safe_exc_text(exc)}"
                )
        return

    import stt_server.client as _stt_client

    hint = (
        "Start the local STT server (pipecat-local-stt-server) and verify it "
        "is reachable, or set STT_WS_SOCKET / STT_WS_URI explicitly."
    )

    # Endpoint completeness. ``TranscriptionClient.__init__`` already
    # raises ``ValueError`` when nothing is configured, but a half-set
    # host-without-port (or vice versa) slips through the resolver
    # precedence as well and deserves the actionable preflight message
    # instead of a raw ``ValueError`` traceback.
    uri = kwargs.get("uri")
    sock_path = kwargs.get("socket_path")
    host = kwargs.get("host")
    port = kwargs.get("port")
    if not (uri or sock_path):
        if (host and port is None) or (port is not None and not host):
            raise SttPreflightError(
                f"STT: incomplete endpoint config (host={host!r}, port={port!r}). "
                "Set both STT_WS_HOST and STT_WS_PORT, or use STT_WS_URI / "
                f"STT_WS_SOCKET. {hint}"
            )

    # Captured so the eventual `_preflight_cache[key] = ...` store below can
    # memoize the OUTCOME, not just the fact that a probe ran — see
    # `_preflight_cache`'s module docstring. Wraps rather than replaces the
    # caller's own `on_recovery`: the caller must still be notified in real
    # time on an actual recovery; this only ADDS memoization on the side.
    recovered_message: str | None = None

    def _record_and_forward(msg: str | None) -> None:
        nonlocal recovered_message
        recovered_message = msg
        if on_recovery is not None:
            on_recovery(msg)

    # Attempt schedule — first is the fast-path, second retries only on
    # cold-start races (OSError). Other exceptions fail on the first try.
    attempts = (
        (_PREFLIGHT_TIMEOUT_SEC, 0.0),
        (_PREFLIGHT_RETRY_TIMEOUT_SEC, _PREFLIGHT_RETRY_DELAY_SEC),
    )
    total_budget = sum(t + d for t, d in attempts)
    # The budget the user's wall clock actually saw when a kickstart-and-retry
    # was also attempted: reporting the bare pre-kickstart `total_budget`
    # (~6s) after burning the ~45s post-kickstart deadline is a misleading
    # diagnostic.
    kickstart_budget = total_budget + _POST_KICKSTART_DEADLINE_SEC

    def _make_client() -> object:
        # A factory, not a single shared instance: the post-kickstart retry
        # loop needs a fresh client per attempt (see `_kickstart_and_retry`).
        # Re-read through the module each call so tests can monkeypatch
        # `stt_server.client.TranscriptionClient`.

        return _stt_client.TranscriptionClient(
            socket_path=sock_path,
            host=host,
            port=port,
            uri=uri,
            auth_token=kwargs.get("auth_token"),
        )

    # A fresh client per attempt, like `WebSocketSTTService._ensure_connected`
    # (and like the post-kickstart loop in `_kickstart_and_retry`). Reusing one
    # client across retries leaks a websocket per attempt that got past
    # ws_connect but failed at handshake: the next `connect()` overwrites the
    # handle and only the last one is ever closed.
    client: object = _make_client()
    # Same blast radius as the post-kickstart loop's teardown bound: the
    # intermediate teardown between attempt 1 and attempt 2 sits INSIDE the
    # window whose length `total_budget` reports in the error message, so a
    # fixed-timeout close could add up to 10s to a ~6s quoted budget. The
    # `finally` teardown below is deliberately left unbounded — it runs after
    # the outcome is already decided, and on the SUCCESS path it is the normal
    # graceful close, which should be allowed its full 5s.
    loop = asyncio.get_running_loop()
    _pre_kickstart_deadline = loop.time() + total_budget
    # Which closers the `finally` below owes this client. Every exit from the
    # `try` other than a `break` is a raise from a failed/timed-out
    # `connect()`, which leaves no session to close; only the three `break`s
    # (the fast-path one and the two post-kickstart recovery ones) hand
    # `finally` a live one.
    connected = False
    try:
        for idx, (timeout_s, delay_s) in enumerate(attempts):
            is_last = idx == len(attempts) - 1
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            if idx > 0:
                # `TRANSPORT_ONLY`: this client's `connect()` failed on the
                # previous attempt, so it has no session. See `_closing`.
                #
                # The deadline RESERVES this attempt's own connect timeout
                # rather than handing the teardown everything that is left.
                # Capping at `_pre_kickstart_deadline` looks conservative and
                # is the opposite: against a wedged server the close hangs
                # until the deadline, `remaining` then comes back at ~0, and
                # the `timeout_s <= _MIN_CONNECT_ATTEMPT_TIMEOUT_SEC` branch
                # below raises "budget exhausted before a connect attempt
                # could be made" — so the retry this whole schedule exists
                # for never issues a second connect, in exactly the cold-start
                # case it is meant to cover. `close_quietly` floors an already
                # expired deadline at `MIN_CLOSE_ATTEMPT_TIMEOUT_SEC`, so the
                # teardown still gets a real (if tiny) attempt.
                await _closing.close_quietly(
                    client,
                    deadline=_pre_kickstart_deadline - timeout_s,
                    closers=_closing.TRANSPORT_ONLY,
                )
                client = _make_client()
                # Recheck the remaining pre-kickstart budget after the sleep
                # and the deadline-capped teardown above: when attempt 1 ran
                # long and/or the teardown ate most of what `total_budget`
                # had left, this attempt's connect must be capped to what
                # actually remains — otherwise it always uses the full fixed
                # `timeout_s` regardless, letting startup overrun the
                # documented `total_budget`/`kickstart_budget` by up to a
                # whole retry timeout.
                remaining = _pre_kickstart_deadline - loop.time()
                timeout_s = min(timeout_s, remaining)
            try:
                if idx > 0 and timeout_s <= _MIN_CONNECT_ATTEMPT_TIMEOUT_SEC:
                    # The intermediate teardown (and/or the inter-attempt
                    # sleep) already consumed the whole pre-kickstart
                    # budget. Previously this floored `timeout_s` at
                    # `_closing.MIN_CLOSE_ATTEMPT_TIMEOUT_SEC` (0.05s) and
                    # attempted the connect anyway — a connect with ~0.05s
                    # to work with cannot succeed, so that "attempt" was
                    # really just a guaranteed, near-instant `TimeoutError`
                    # that then got reported as "did not complete handshake
                    # within {budget}s", misrepresenting budget exhaustion
                    # (spent on teardown, not on waiting for the server) as
                    # a real handshake timeout. Skip the doomed attempt and
                    # raise the same `TimeoutError` type so the existing
                    # kickstart-and-retry / `SttPreflightError` handling
                    # below is unchanged — only the message differs, via
                    # `exc`'s own text.
                    raise TimeoutError(
                        "preflight retry budget exhausted before a connect "
                        "attempt could be made (spent on inter-attempt "
                        "teardown/delay)"
                    )
                await asyncio.wait_for(client.connect(), timeout=timeout_s)
                connected = True
                break
            except TimeoutError as exc:
                if not is_last:
                    continue
                # Routed through `safe_exc_text` like every sibling branch.
                # This was the one exception-text site that interpolated
                # `exc` raw — and `TimeoutError` is an `OSError` subclass
                # caught first here, so the sibling `except OSError` branch's
                # redaction never covered it. Low exploitability, but an
                # asymmetry in a redaction boundary is exactly what the last
                # five rounds were spent removing.
                safe_detail = safe_exc_text(exc)
                detail = f" ({safe_detail})" if safe_detail else ""
                if launchd_label is not None:
                    outcome, recovered = await _kickstart_and_retry(
                        _make_client,
                        launchd_label,
                        _record_and_forward,
                        target=target,
                        hint=hint,
                    )
                    result = _resolve_kickstart_outcome(
                        outcome,
                        recovered,
                        exhausted_message=(
                            f"STT: stt_server did not complete handshake within "
                            f"{kickstart_budget:.1f}s at {target}{detail} "
                            f"(kickstarted {launchd_label!r}, still unreachable "
                            f"after retry). {hint}"
                        ),
                        exc=exc,
                    )
                    if result is not None:
                        # The stale pre-kickstart client is dead; hand `finally`
                        # the live one instead so exactly one socket is closed
                        # and none is leaked. Transfer ownership to `client`
                        # BEFORE awaiting the stale close: if cancellation
                        # (shutdown) arrives during that await, the outer
                        # `finally` must still see `client` pointing at the
                        # live, connected `result` socket rather than the
                        # already-dead stale one — otherwise `result` is
                        # never closed and its socket/FD leaks.
                        stale = client
                        client = result
                        connected = True
                        # `TRANSPORT_ONLY`: `stale` is the pre-kickstart
                        # client whose `connect()` timed out — no session.
                        await _closing.close_quietly(
                            stale, closers=_closing.TRANSPORT_ONLY
                        )
                        break
                    # "kickstart_failed" (also: cooldown still active) ->
                    # fall through, unchanged message.
                raise SttPreflightError(
                    f"STT: stt_server did not complete handshake within "
                    f"{total_budget:.1f}s at {target}{detail}. {hint}"
                ) from exc
            except ValueError as exc:
                # Mis-shaped kwargs slipped past our completeness check
                # (future callers may build kwargs differently). Still
                # actionable, still better than a traceback. `urlsplit`'s
                # own port-cast error message can echo a raw fragment of
                # the password verbatim (e.g. "Port could not be cast to
                # integer value as 'se'" from a malformed
                # `user:se/cret@host` endpoint) with no `@` anywhere in
                # the message for `safe_exc_text`'s tiered search to
                # anchor on, so routing it through `safe_exc_text` does
                # not actually redact it. Drop the exception text
                # entirely instead — it adds little beyond "misconfigured
                # endpoint" — and report only the already-redacted
                # `target` plus the exception's type name.
                raise SttPreflightError(
                    f"STT: misconfigured endpoint at {target} "
                    f"({type(exc).__name__}). {hint}"
                ) from exc
            except OSError as exc:  # covers FileNotFoundError + ConnectionRefusedError
                # Cold-start races live here: socket path doesn't exist
                # yet, or TCP refused because serve() hasn't bound. Retry
                # once with a short delay so the bot doesn't exit just
                # because the LaunchAgent is still doing `import
                # mlx_whisper`.
                if not is_last:
                    continue
                if launchd_label is not None:
                    outcome, recovered = await _kickstart_and_retry(
                        _make_client,
                        launchd_label,
                        _record_and_forward,
                        target=target,
                        hint=hint,
                    )
                    result = _resolve_kickstart_outcome(
                        outcome,
                        recovered,
                        exhausted_message=(
                            f"STT: stt_server not reachable at {target} "
                            f"({safe_exc_text(exc)}) (kickstarted {launchd_label!r}, "
                            f"still unreachable after retry). {hint}"
                        ),
                        exc=exc,
                    )
                    if result is not None:
                        # See the matching TimeoutError branch above for why
                        # ownership must transfer before the awaited close.
                        stale = client
                        client = result
                        connected = True
                        # `TRANSPORT_ONLY`: `stale` is the pre-kickstart
                        # client whose `connect()` timed out — no session.
                        await _closing.close_quietly(
                            stale, closers=_closing.TRANSPORT_ONLY
                        )
                        break
                    # "kickstart_failed" (also: cooldown still active) ->
                    # fall through, unchanged message.
                # A plain connection-refused OSError doesn't normally carry
                # a credential, but nothing prevents a future socket/TLS
                # error class from echoing the connect target — route
                # through safe_exc_text uniformly like every sibling branch.
                raise SttPreflightError(
                    f"STT: stt_server not reachable at {target} "
                    f"({safe_exc_text(exc)}). {hint}"
                ) from exc
            except Exception as exc:
                # Catches websockets.exceptions.WebSocketException (401/400 on
                # wrong token / wrong path, TLS errors, protocol errors) and
                # the RuntimeError branches in ``connect()`` when the server
                # returns an unexpected first frame. These are all
                # misconfiguration shapes the bot cannot recover from, so
                # translate them to the CLI-friendly error rather than
                # letting them bubble as tracebacks. No retry — this isn't
                # a race, the config is wrong.
                raise SttPreflightError(
                    f"STT: handshake failed at {target} "
                    f"({type(exc).__name__}: {safe_exc_text(exc)}). {hint}"
                ) from exc
    finally:
        # Mixed site: on the `break` paths `client` is the live, connected
        # socket and owes a real `session.close`; on every raising path it
        # never reached a session, and `close_session` would spend the shared
        # budget hanging against the wedged server before `close` — the call
        # that releases the FD — gets its turn.
        #
        # Bounded on the FAILURE path only. The success path keeps the full
        # graceful close it has always had. The failure path was left
        # unbounded on the reasoning that "the outcome is already decided" —
        # but the outcome being decided is precisely why the user is still
        # waiting: the `SttPreflightError` quotes `total_budget` (~6s) and
        # cannot be raised until this returns, so a close hanging its default
        # 5s against the same wedged server that just failed the connect made
        # the measured wall clock ~11s for an error that says 6.0s. Nothing
        # useful can happen in a close against a server that would not
        # complete a handshake; the FD goes with the process either way.
        await _closing.close_quietly(
            client,
            closers=_closing.FULL_CLOSERS if connected else _closing.TRANSPORT_ONLY,
            deadline=(None if connected else loop.time() + _FAILED_TEARDOWN_BUDGET_SEC),
        )

    _preflight_cache[key] = recovered_message


def _vocabulary_bias() -> list[str]:
    """Return the dictionary's vocabulary terms for STT recognition bias.

    These feed Deepgram ``keywords`` and the Whisper ``initial_prompt`` so the
    backend is biased toward the user's domain terms. The dictionary ships
    empty (seeds stripped); an empty list is the common case. Best-effort — a
    read failure must not block STT creation.
    """
    try:
        from onoats._vendor.dictionary import Dictionary

        return Dictionary().get_vocabulary()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(f"STT: could not load vocabulary bias: {exc}")
        return []


def _resolve_stt_language(cfg) -> str | None:
    """Map ``cfg.stt_language`` to the value the STT backends expect.

    ``auto`` maps to ``None`` (omit the field) rather than the literal string
    "auto": ``None`` is the only value that means auto-detect uniformly across
    backends — whisper/mlx *rejects* a literal "auto" (ValueError -> failed
    decode) and uses ``None`` for built-in detection, while nemotron maps
    client-``None`` to its own "auto" language-ID. onoats is backend-agnostic
    over the socket, so it cannot branch per backend. Resolved in one place so
    the websocket and local whisper branches cannot drift.
    """
    raw = cfg.stt_language
    return None if raw.lower() == "auto" else raw


# Canonical set of STT_SERVICE values dispatched by _create_stt_service below
# ("whisper" is the fall-through default branch). The menu bar's STT picker
# (native/onoats-menubar/Sources/RecorderModel.swift `sttServices`) mirrors
# this tuple; tests/test_native_contract_parity.py keeps the two in sync.
VALID_STT_SERVICES = ("whisper", "websocket", "deepgram")


@dataclass(frozen=True)
class SttServiceResult:
    """Return value of :func:`_create_stt_service`.

    A named type instead of a bare ``(service, preflight_recovery_message)``
    tuple (deep-review finding): a tuple forced every non-websocket backend
    branch to spell out a second element that means nothing to it
    (``return X, None``), and offered no seam for a future field without
    re-breaking every caller's unpacking. ``preflight_recovery_message`` is
    the ``"stt: server restarted automatically ..."`` string when this
    call's own preflight kickstart-recovered, else ``None`` — only the
    ``websocket`` backend can ever populate it. Callers that only need the
    service instance can ignore ``.preflight_recovery_message``.
    """

    service: STTService
    preflight_recovery_message: str | None = None


async def _create_stt_service(
    *,
    data_dir: Path | None = None,
    branch_instance: str | None = None,
) -> SttServiceResult:
    """Build the STT service based on STT_SERVICE / STT_MODEL env vars.

    Returns an :class:`SttServiceResult`.

    Prefers Whisper MLX on Apple Silicon, falls back to CPU Whisper, or uses
    Deepgram when STT_SERVICE=deepgram.

    Dictionary vocabulary terms (if any) are fed to the backend as recognition
    bias (Deepgram ``keywords`` / Whisper ``initial_prompt``).

    Async because the websocket preflight now does a real handshake
    (rather than a raw TCP probe), which must be awaited from inside the
    running event loop. Non-websocket backends don't ``await`` anything
    but the signature is uniform so callers don't have to branch.

    MLX / Whisper imports are kept lazy (inside the backend branch) so a plain
    ``import onoats.runtime`` with no ``STT_SERVICE`` set never imports
    ``mlx_whisper`` — the off-mac baseline ships MLX-free.

    ``data_dir``, when given, resolves ``cfg.stt_launchd_label`` and builds
    the ``on_recovery`` callbacks wired into the websocket preflight and the
    service instance, enabling both self-healing kickstart and status-warning
    surfacing. ``None`` (the default) means "build no callback" — kickstart
    still functions with a configured label alone; only the status-surfacing
    seam is skipped.

    ``branch_instance`` names which of ``dual.py``'s two independent STT
    instances this is (``"mic"``/``"system"``). It selects the
    **live-session** status-warning branch (``stt-mic``/``stt-system`` via
    ``status.stt_branch``) so the two instances' warnings set and clear
    independently, the way the ``mic``/``system`` capture branches already
    do. ``None`` (single-pipeline path) keeps the plain ``stt`` branch.
    The **startup preflight** recovery is deliberately NOT instance-scoped:
    it is one probe against one shared server, so it always lands on the
    shared ``stt`` branch.

    Only the FIRST ``_create_stt_service`` call for a given endpoint
    actually probes; a later call for the same endpoint is a
    ``_preflight_cache`` hit inside ``_preflight_stt_ws``. That cache
    memoizes the recovery OUTCOME (not just "a probe ran"), so a cache-hit
    call still gets its own ``on_recovery`` replayed with the same message
    if an earlier call's probe kickstart-recovered — no caller-side
    relay needed, and no assumption about which call runs first. This
    arms the instance's confirm gate too, so whichever instance sees a
    transcript first clears the shared ``stt`` warning (a session where
    only the second instance ever produces transcripts, e.g. system-audio
    only, would otherwise leave that warning pinned for the whole session).
    """
    from onoats.config import load_config

    cfg = load_config()
    service = cfg.stt_service
    model_name = cfg.stt_model
    language = _resolve_stt_language(cfg)
    vocabulary = _vocabulary_bias()
    # Enforce the canonical set, not just document it: a typo'd STT_SERVICE
    # used to silently fall through to the whisper branch — fail loud instead.
    if service not in VALID_STT_SERVICES:
        raise RuntimeError(
            f"Unknown STT_SERVICE {service!r} — valid values: "
            f"{', '.join(VALID_STT_SERVICES)}"
        )
    if service == "websocket":
        try:
            from onoats.stt.websocket_stt_service import WebSocketSTTService
        except ImportError as exc:
            raise RuntimeError(
                "STT_SERVICE=websocket requires the 'websockets' package, "
                "a root dependency installed by `uv sync`. Re-run `uv sync` "
                f"to repair the environment. Original error: {exc}"
            ) from exc

        launchd_label = cfg.stt_launchd_label
        # Shared branch for the one startup probe; per-instance branch for
        # this instance's own live-session reconnect recoveries.
        preflight_branch = _status_mod.stt_branch(None)
        live_branch = _status_mod.stt_branch(branch_instance)
        # Captures the display-ready message so the caller (dual.py) can
        # thread it into `write_running(warning=...)` — a plain callback-only
        # seam is overwritten the moment `_write_status_running` next builds a
        # fresh record, since no running record exists yet at preflight time.
        recovery_holder: dict[str, str | None] = {}

        def _branch_writer(branch: str) -> Callable[[str | None], None]:
            def _write(msg: str | None) -> None:
                # Best-effort, like every other status write in this module.
                # This fires from inside `_preflight_stt_ws`'s and
                # `_ensure_connected`'s own exception handlers, on the one code
                # path that just recovered — a status-file failure must not
                # turn a successful self-heal into an uncaught crash, and must
                # not be misread by `_ensure_connected` as a failed connect.
                try:
                    _status_mod.set_warning_branch(data_dir, branch, msg)
                except Exception as exc:
                    logger.warning(
                        f"STT: could not write recovery status ({safe_exc_text(exc)})"
                    )

            return _write

        preflight_on_recovery: Callable[[str | None], None] | None = None
        live_on_recovery: Callable[[str | None], None] | None = None
        on_preflight_confirmed: Callable[[], None] | None = None
        if data_dir is not None:
            _clear_preflight = _branch_writer(preflight_branch)
            live_on_recovery = _branch_writer(live_branch)

            def preflight_on_recovery(msg: str | None) -> None:
                # ONE write path for the preflight recovery: capture the
                # display-ready message for `write_running(warning=...)`.
                # A `set_warning_branch` call here would be dead at best —
                # it no-ops on a non-running record, and no running record
                # exists yet at preflight time — and actively wrong at worst,
                # annotating a stale `running=True` record left behind by a
                # crashed earlier session. The CLEAR side
                # (`on_preflight_confirmed`) does use `set_warning_branch`,
                # because by then `write_running` has run.
                recovery_holder["message"] = (
                    _status_mod.format_warning_branch(preflight_branch, msg)
                    if msg is not None
                    else None
                )

            def on_preflight_confirmed() -> None:
                _clear_preflight(None)

        kwargs = _resolve_stt_ws_target(_ws_env(cfg))
        target = _display_target(kwargs)
        logger.info(f"STT: websocket (server={target})")
        await _preflight_stt_ws(
            kwargs,
            target,
            launchd_label=launchd_label,
            on_recovery=preflight_on_recovery,
        )
        # The language is forwarded to the server's decoder via
        # ``update_session`` (see ``WebSocketSTTService``). Resolved from
        # ``cfg.stt_language`` above (env STT_LANGUAGE > legacy STT_WS_LANGUAGE
        # > config.toml [stt].language > "en"). Not threaded through
        # ``_resolve_stt_ws_target`` because that dict also feeds
        # ``TranscriptionClient``, which takes no ``language`` kwarg.
        # The preflight probe above (a throwaway ``TranscriptionClient``, run
        # before this instance exists) may have already kickstart-recovered —
        # either because THIS call actually probed, or because an EARLIER
        # ``_create_stt_service`` call's probe did and this call's own
        # `_preflight_stt_ws` call was a `_preflight_cache` hit that replayed
        # the memoized outcome into `preflight_on_recovery` above (see that
        # function's cache-hit branch). Either way, `recovery_holder` is
        # populated identically, so this instance gets a clear callback for
        # the SHARED ``stt`` branch: its own first transcript event clears
        # the warning.
        recovered_in_preflight = recovery_holder.get("message") is not None
        service = WebSocketSTTService(
            language=language,
            launchd_label=launchd_label,
            # Stable cross-instance identity for the shared unhealthy
            # registry — the same "mic"/"system" name that already selects
            # this instance's status branch. `WebSocketSTTService` has no
            # notion of status-warning branches (that's this module's/
            # `status.py`'s concept); to the leaf service this is just an
            # opaque instance identity token, hence `instance_name`, not
            # `branch_instance`. Previously the service derived its own
            # token from `id(self)`, a memory address CPython reuses after
            # GC.
            instance_name=branch_instance,
            on_recovery=live_on_recovery,
            on_preflight_confirmed=(
                on_preflight_confirmed if recovered_in_preflight else None
            ),
            **kwargs,
        )
        return SttServiceResult(service, recovery_holder.get("message"))

    if service == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        from onoats.config import looks_like_bearer_token

        dg_kwargs: dict = {
            "api_key": cfg.require_secret(
                "DEEPGRAM_API_KEY",
                validate=looks_like_bearer_token,
                hint="Get one at https://console.deepgram.com",
            )
        }
        live_opts: dict = {}
        if model_name:
            live_opts["model"] = model_name
        if vocabulary:
            # Deepgram keyword boosting: bias recognition toward dictionary terms.
            live_opts["keywords"] = list(vocabulary)
        if live_opts:
            from deepgram import LiveOptions

            dg_kwargs["live_options"] = LiveOptions(**live_opts)
        logger.info(
            f"STT: deepgram (model={model_name or 'default'}, "
            f"vocabulary_bias={len(vocabulary)} term(s))"
        )
        return SttServiceResult(DeepgramSTTService(**dg_kwargs))

    # Whisper recognition bias would be supplied via an initial_prompt seed,
    # but pipecat 1.3.0's Whisper wrapper exposes no such field (Settings =
    # model/language/extra/no_speech_prob[/temperature,engine for MLX]); passing
    # it crashes. Log-and-skip when the dictionary has terms. Only Deepgram
    # honours vocabulary bias (keywords). The websocket/stt_server path does
    # NOT: the runtime never forwards `vocabulary` to WebSocketSTTService, and
    # the wire protocol's `update_session` carries no hotwords field — so the
    # dictionary is silently ignored there too, same as Whisper.
    if vocabulary:
        logger.debug(
            f"Whisper: dictionary vocabulary bias ({len(vocabulary)} term(s)) is "
            "not supported by this pipecat Whisper wrapper; ignoring."
        )

    # Default: Whisper (MLX on Apple Silicon, CPU otherwise). The MLX import
    # lives inside this branch so the off-mac baseline never imports it.
    if _mlx_available():
        from pipecat.services.whisper.stt import MLXModel, WhisperSTTServiceMLX

        mlx_key = _MLX_MODEL_MAP.get(
            model_name or "large-v3-turbo", "LARGE_V3_TURBO"
        ).upper()
        mlx_model = getattr(MLXModel, mlx_key, None)
        if mlx_model is None:
            logger.warning(
                f"Unknown MLX model name '{model_name}', falling back to large-v3-turbo"
            )
            mlx_model = MLXModel.LARGE_V3_TURBO
        logger.info(
            f"STT: whisper-mlx (model={mlx_model.name}, device=Apple Silicon, "
            f"language={language or 'auto'})"
        )
        # language=None reaches mlx_whisper.transcribe unchanged, which then
        # auto-detects per segment.
        return SttServiceResult(
            WhisperSTTServiceMLX(
                settings=WhisperSTTServiceMLX.Settings(
                    model=mlx_model.value, language=language
                )
            )
        )
    else:
        from pipecat.services.whisper.stt import WhisperSTTService

        model = model_name or "base"
        logger.info(f"STT: whisper-cpu (model={model}, language={language or 'auto'})")
        # device/compute_type are WhisperSTTService constructor kwargs, NOT
        # Settings fields — passing device into Settings raises TypeError.
        return SttServiceResult(
            WhisperSTTService(
                device="cpu",
                settings=WhisperSTTService.Settings(model=model, language=language),
            )
        )


# ---------------------------------------------------------------------------
# Flush: rotate the .active/ file into the pending/ queue
# ---------------------------------------------------------------------------


async def flush_and_rotate(
    transcript_buffer,
    reason: str,
    *,
    continue_session: bool,
    data_dir: Path,
    locked_category: str | None = None,
) -> None:
    """Flush the transcript buffer and rotate its session file into ``pending/``.

    The recorder emits files only — no SQLite, no ``processing_jobs`` row. It
    flushes the in-memory buffer to disk, then rotates the finalised
    ``.active/`` session file into the ``pending/`` queue. A downstream
    consumer drains the queue and back-fills its own bookkeeping from the
    rowless file.

    ``locked_category`` (the ``--category`` lock) is carried in the queue
    contract as a typed ``session_meta`` FIRST line, written by the transcript
    buffer when the session file is created — NOT recorded here (there is no
    DB to record it in). See ``onoats.categories.session_meta_line``.

    Flush kinds:

    * ``continue_session=False`` — terminal flush (``EndFrame`` / shutdown):
      rotate ``.active/X.jsonl`` → ``pending/X.jsonl`` and stop.
    * ``continue_session=True`` — continuation flush (silence-timeout,
      Ctrl+T, ``SIGUSR1``): rotate FIRST, then a fresh ``.active/`` session
      is opened and adopted by the buffer so the ongoing recording has
      somewhere to land.
    """
    from onoats._vendor import session_queue

    # Phase 5 — queue dirs are no longer created at module import; each
    # rotation site ensures them itself (idempotent mkdir).
    session_queue.ensure_queue_dirs(data_dir)

    logger.info(f"{reason} — flushing transcript buffer, rotating to pending/")

    # Pre-mint the fresh .active/ session BEFORE the flush so the buffer can
    # swap _session_file atomically under its _write_lock. Without this
    # pre-mint there is a race window where flush() releases the lock with
    # _session_file=None and an arriving utterance creates a stray .active/
    # file before we reassign — the "silently drops audio after manual flush"
    # risk the plan flags. Crash safety unchanged: a crash between pre-mint
    # and the rotation leaves both the old file and an empty fresh file in
    # .active/; run_crash_recovery rotates both into pending/ (the empty one
    # is a harmless no-op job).
    next_active_path: Path | None = None
    if continue_session:
        try:
            next_active_path, _next_session_id = session_queue.new_active_session(
                data_dir
            )
        except OSError as exc:
            logger.error(f"Flush: could not pre-mint fresh .active/ session: {exc}")
            return

    buffer_contents, session_path = await transcript_buffer.flush(
        next_session_file=next_active_path
    )
    if not buffer_contents or session_path is None:
        logger.info("Flush: buffer was empty, nothing to rotate")
        # Persist any unpersisted in-memory entries (defensive — flush()
        # already materialises them, but mirrors the old behaviour).
        await transcript_buffer.flush_to_disk()
        # Clean up the pre-minted fresh .active/ file we no longer need —
        # otherwise an empty .active/ session leaks until the next bot
        # restart's crash_recovery rotates it as a no-op job. The buffer
        # still points at ``next_active_path`` (flush() swapped it under
        # the write lock); revert that swap atomically before unlinking,
        # otherwise an utterance arriving between flush release and unlink
        # writes into the file and the unlink silently deletes it.
        if next_active_path is not None:
            reverted = await transcript_buffer.discard_pending_session(next_active_path)
            if reverted:
                try:
                    next_active_path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    logger.debug(
                        f"Flush: could not remove unused pre-minted {next_active_path.name}: {exc}"
                    )
            else:
                logger.debug(
                    f"Flush: buffer no longer points at {next_active_path.name} — "
                    "leaving file in place (crash_recovery will rotate as no-op)"
                )
        return

    try:
        session_id = session_queue.rotate_active_to_pending(
            session_path, data_dir=data_dir
        )
    except FileNotFoundError:
        logger.warning(
            f"Flush: session file {session_path.name} vanished before rotation — nothing to queue"
        )
        return
    except OSError as exc:
        logger.error(f"Flush: could not rotate {session_path.name} to pending/: {exc}")
        return

    # File-only: no DB row. The category travels in the session_meta first
    # line (written by the transcript buffer); a consumer back-fills from the
    # rowless pending/ file. ``locked_category`` is accepted for call-site
    # compatibility but is not recorded here.
    logger.info(f"Flush: rotated {session_id} → pending/ (consumer will process it)")
    if continue_session and next_active_path is not None:
        logger.debug(
            f"Flush: buffer swapped to fresh active session {next_active_path.name} under lock"
        )


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


async def run_crash_recovery(
    data_dir: Path | None = None,
    locked_category: str | None = None,
) -> None:
    """Rotate orphaned ``.active/`` session files into the ``pending/`` queue.

    Crash recovery *rotates* every orphaned ``.active/`` file into
    ``pending/`` (file-only — no DB row), just like a live flush does; a
    downstream consumer then drains it. This removes the recorder
    end-vs-start race entirely.

    First-run backfill: this also picks up any pre-existing
    ``.active/session_*.jsonl`` AND legacy ``.recovering`` files and rotates
    them into ``pending/`` so nothing stranded by a previous deploy is lost.
    The ``rename(2)`` into ``pending/`` is the only claim.
    """
    from onoats._vendor import session_queue
    from onoats._vendor.store import onoats_data_dir

    base = Path(data_dir) if data_dir is not None else onoats_data_dir()
    # Ensure queue dirs exist before crash recovery rotates anything in.
    session_queue.ensure_queue_dirs(base)
    active_dir = base / session_queue.ACTIVE_DIR

    if not active_dir.exists():
        logger.debug("Crash recovery: no .active/ directory — nothing to recover")
        return

    # Orphans: both normal session files and legacy .recovering files left
    # by the superseded flock-based scheme. The bot's own live recording
    # file is created *after* this runs, so anything here at startup is an
    # orphan from a previous process.
    try:
        orphans = sorted(active_dir.glob("session_*.jsonl"))
        legacy_recovering = sorted(active_dir.glob("session_*.recovering"))
    except OSError as exc:
        logger.warning(f"Crash recovery: could not scan {active_dir}: {exc}")
        return

    if not orphans and not legacy_recovering:
        logger.debug("Crash recovery: no orphaned session files found")
        return

    logger.info(
        f"Crash recovery: rotating {len(orphans)} orphaned + "
        f"{len(legacy_recovering)} legacy .recovering file(s) into pending/"
    )

    # Normalise legacy .recovering files back to a .jsonl name so the queue
    # treats them uniformly. rename(2) within .active/ is atomic.
    normalised: list[Path] = list(orphans)
    for rec_path in legacy_recovering:
        jsonl_path = rec_path.with_suffix(".jsonl")
        # Carried Phase 2 minor finding: refuse to silently overwrite a
        # same-id orphan already present as a ``.jsonl`` in ``.active/``.
        # Move the legacy file aside instead so a manual inspection can
        # decide which copy wins.
        if jsonl_path.exists():
            stash = rec_path.with_suffix(".recovering.collision")
            try:
                os.rename(rec_path, stash)
                logger.warning(
                    f"Crash recovery: refused to overwrite {jsonl_path.name} with "
                    f"legacy {rec_path.name}; moved aside to {stash.name}"
                )
            except OSError as exc:
                logger.warning(
                    f"Crash recovery: could not stash colliding {rec_path.name}: {exc}"
                )
            continue
        try:
            os.rename(rec_path, jsonl_path)
            normalised.append(jsonl_path)
        except OSError as exc:
            logger.warning(
                f"Crash recovery: could not normalise legacy {rec_path.name}: {exc}"
            )

    for session_path in normalised:
        try:
            rotation = session_queue.rotate_to_pending(
                session_path, continue_session=False, data_dir=base
            )
        except FileNotFoundError:
            # Another actor moved it between the glob and the rename.
            continue
        except OSError as exc:
            logger.error(
                f"Crash recovery: could not rotate {session_path.name} to pending/: {exc}"
            )
            continue

        # File-only: no DB row. A consumer back-fills its own bookkeeping
        # from the rowless pending/ file; the category travels in the
        # session_meta first line.
        logger.info(
            f"Crash recovery: rotated {rotation.session_id} → pending/ (consumer will process it)"
        )


# ---------------------------------------------------------------------------
# PID file / signal handlers / terminal cbreak
# ---------------------------------------------------------------------------


def _own_ps_cmdline() -> str:
    """Return the ``ps -p <self> -o command=`` string for the current process."""
    try:
        import subprocess

        result = subprocess.run(
            ["ps", "-p", str(os.getpid()), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return ""


def _pid_alive(pid: int) -> bool:
    """True if ``pid`` exists. ``ProcessLookupError`` is the only positive proof
    of death; any other error (``EPERM`` — owned by another user — or an odd
    ``OSError``) is treated as alive, so a liveness guard fails *safe*."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


# Single-instance lock — an advisory ``flock`` held for the recorder's process
# lifetime. This is the ATOMIC instance-slot gate the pid file cannot be: the pid
# file is a readable data record written check-then-replace, but ``flock`` either
# acquires or fails with no window, and the kernel releases it automatically when
# the holder exits — graceful OR crash/SIGKILL — so there is never a stale lock to
# reclaim. The fd is kept open (module-global) for the whole process; closing it
# or process exit releases the lock. The lock file itself is never unlinked.
LOCK_FILENAME = "onoats.lock"
_instance_lock_fd: int | None = None


def _refuse_if_live_recorder(pid_path: Path) -> None:
    """Raise ``RecorderAlreadyRunningError`` if the pid file names a live recorder.

    The ``flock`` catches a concurrent SAME-version start, but a recorder from an
    OLDER build holds no flock — so this no-write identity preflight (the same gate
    ``flush``/``stop`` use: ``resolve_flush_target`` + the indeterminate-but-live
    refusal) catches a live legacy/cross-version recorder the flock cannot. Run it
    right after acquiring the flock and BEFORE any capture side effect, so a start
    over a live orphan refuses before spawning the capturer / opening a device.
    """
    from onoats._vendor.pid import read_pid_record, resolve_flush_target

    verified = resolve_flush_target(pid_path)
    if verified.pid is not None and verified.pid != os.getpid():
        raise RecorderAlreadyRunningError(
            f"An onoats recorder is already running (pid {verified.pid}). "
            "Stop it first with `onoats stop`, then retry."
        )
    # Indeterminate-but-live: a marker-valid file whose process is still alive but
    # unverifiable (ps probe failed / legacy fingerprint-less). Refuse, exactly as
    # flush/stop do; only ``stale=True`` is safe to clobber.
    if verified.pid is None and not verified.stale:
        rec = read_pid_record(pid_path)
        if rec is not None and rec.pid != os.getpid() and _pid_alive(rec.pid):
            raise RecorderAlreadyRunningError(
                f"A recorder pid file names a live process (pid {rec.pid}) whose "
                "identity could not be verified (ps probe failed / legacy pid "
                "file) — refusing to start over a possibly-live recorder. Stop it "
                "(`onoats stop`) or remove the stale pid file, then retry."
            )


def _acquire_instance_lock(active_dir: Path) -> None:
    """Atomically claim the single-instance slot; raise if another holds it.

    Two layered guards, BOTH run here so a losing start refuses before any capture
    side effect (the call sites hoist this ahead of capturer spawn / device open):

    1. ``flock(LOCK_EX|LOCK_NB)`` — the atomic gate. Of N concurrent SAME-version
       starts exactly one wins; the rest raise ``RecorderAlreadyRunningError``.
    2. ``_refuse_if_live_recorder`` — a no-write identity preflight that catches a
       live LEGACY/cross-version recorder (which holds no flock). Without this, a
       start over a live legacy orphan would acquire the flock and proceed to spawn
       the capturer, only refusing later in ``_write_pid_file``.

    Held for the process lifetime via the module-global fd; the kernel releases it
    on exit (so there is no stale lock, and a chained ``stop`` then ``start``
    refuses until the draining recorder's process exits). On Windows ``flock`` is
    unavailable, so the identity preflight is the only guard (onoats is macOS-only
    in practice).
    """
    global _instance_lock_fd
    # Idempotent: one lock per process. A nested acquire (the socket supervisor
    # takes it before spawning the capturer, then the recorder's
    # run_onoats_dual/_write_pid_file call it again) returns without re-acquiring —
    # no release-then-reacquire gap, and the identity preflight runs exactly once.
    if _instance_lock_fd is not None:
        return
    active_dir.mkdir(parents=True, exist_ok=True)
    pid_path = active_dir / PID_FILENAME
    if sys.platform == "win32":
        # No flock available — the identity preflight is the only guard.
        _refuse_if_live_recorder(pid_path)
        return
    lock_path = active_dir / LOCK_FILENAME
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        # EWOULDBLOCK/EAGAIN → a live SAME-version recorder holds the slot. Read
        # the pid file (if any) only to name it in the error — never to gate on.
        from onoats._vendor.pid import read_pid_record

        rec = read_pid_record(pid_path)
        who = f" (pid {rec.pid})" if rec is not None else ""
        raise RecorderAlreadyRunningError(
            f"An onoats recorder is already running{who} and holds the "
            "single-instance lock. Stop it first with `onoats stop`, then retry."
        ) from exc
    # We hold the flock. Now refuse a live LEGACY recorder (no flock) before any
    # capture side effect — releasing the flock we just took if we must refuse.
    try:
        _refuse_if_live_recorder(pid_path)
    except BaseException:
        os.close(fd)
        raise
    _instance_lock_fd = fd


def _release_instance_lock() -> None:
    """Release the single-instance lock if held (no-op otherwise).

    Deliberately NOT called on the normal recorder teardown path: the lock is held
    for the whole process lifetime and the kernel releases it on exit (graceful OR
    crash). Releasing during shutdown would free the slot while the socket
    supervisor is still tearing down its capturer — letting a chained start spawn a
    second capturer into a device the old one hasn't finished releasing. Provided
    for explicit lifecycle control in tests (and any future caller that genuinely
    owns the whole start→stop span).
    """
    global _instance_lock_fd
    if _instance_lock_fd is None:
        return
    try:
        fcntl.flock(_instance_lock_fd, fcntl.LOCK_UN)
    except OSError:
        pass
    try:
        os.close(_instance_lock_fd)
    except OSError:
        pass
    _instance_lock_fd = None


def _write_pid_file(data_dir: Path) -> Path:
    """Write the current process PID, identity marker, and cmdline fingerprint.

    Single-instance enforcement lives in ``_acquire_instance_lock`` (the flock +
    ``_refuse_if_live_recorder`` identity preflight), which the capture entrypoints
    call EARLY — before any capture side effect — and which this function also
    calls as a backstop. By the time we publish a pid file we are the sole
    instance and any pid file on disk is stale/dead, so the atomic replace below is
    safe. The write itself is atomic (temp + ``os.replace``) and paired with the
    ownership-checked ``_remove_pid_file`` so a draining recorder never deletes a
    newer recorder's file.
    """
    active_dir = data_dir / ".active"
    active_dir.mkdir(parents=True, exist_ok=True)
    pid_path = active_dir / PID_FILENAME

    # Single-instance acquisition + identity preflight (universal backstop). The
    # capture entrypoints acquire this EARLY — the socket supervisor before
    # spawning the capturer, run_onoats_dual / run_onoats before opening a device —
    # so by the time we publish a pid file the lock is already held and this call
    # is an idempotent no-op. It stays here so any entrypoint that reaches pid
    # publication without an earlier acquire is still gated. ``_acquire_instance_lock``
    # raises ``RecorderAlreadyRunningError`` for a concurrent (flock) OR live legacy
    # (identity) recorder, so by here we are the sole instance and any pid file on
    # disk is stale/dead — safe to atomically replace. Held until process exit.
    _acquire_instance_lock(active_dir)

    existing = _read_pid_file(pid_path)
    if existing is not None:
        try:
            os.kill(existing, 0)
            logger.warning(
                f"PID file exists and process {existing} is still running. "
                "Overwriting — another bot instance may be active."
            )
        except ProcessLookupError:
            logger.info("Removing stale PID file (process gone)")
        except PermissionError:
            logger.warning("PID file exists, process may be running as different user")

    cmdline = _own_ps_cmdline()
    # Wall-clock start_epoch is included as the 4th line so live-view
    # readers can distinguish a freshly-started bot from one that
    # happens to have inherited a recycled pid (see onoats._vendor.pid).
    start_epoch = time.time()
    payload = f"{os.getpid()}\n{PID_MARKER}\n{cmdline}\n{start_epoch}\n"
    # Atomic replace (temp + os.replace in the SAME dir) — never truncate the pid
    # file in place. A draining recorder's owner-checked `_remove_pid_file` reads
    # this path concurrently; an in-place write_text would expose an empty/partial
    # file mid-write, `read_pid_file` would return None, and the drainer would then
    # delete this (newer) recorder's pid file. os.replace makes a concurrent reader
    # see either the complete old record or the complete new one — never a partial.
    # Mirrors the status-file writer idiom (onoats.status.write_status).
    fd, tmp = tempfile.mkstemp(
        dir=str(active_dir), prefix=".onoats-pid-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, pid_path)
    except BaseException:
        # Never leak a temp file on failure.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    logger.debug(
        f"PID file written: {pid_path} (PID {os.getpid()}, cmdline={cmdline!r})"
    )
    return pid_path


def _remove_pid_file(pid_path: Path, *, owner_pid: int | None = None) -> None:
    """Remove the PID file on shutdown.

    When ``owner_pid`` is given, unlink ONLY if the file still records exactly that
    pid — fail closed. A recorder tearing down must never delete a pid file a NEWER
    recorder has since taken over (the stop-then-immediate-start race), which would
    leave the new session running with no pid file, invisible to
    ``status``/``stop``/``flush``. We must also refuse to delete when the file reads
    back as ``None``: paired with the atomic writer (``_write_pid_file`` uses
    ``os.replace``, never a truncating in-place write) a ``None`` here is no longer
    a benign mid-write of *our own* file, but either (a) a foreign/invalid record we
    have no business removing, or (b) a file already gone — in both cases leaving it
    is correct. A leftover invalid pid file is self-healing: ``status`` reports no
    valid recorder and the next ``_write_pid_file`` atomically replaces it.
    """
    if owner_pid is not None:
        current = _read_pid_file(pid_path)
        if current != owner_pid:
            if current is None:
                logger.debug(
                    f"PID file {pid_path} is unreadable/absent during owner-checked "
                    f"removal (owner {owner_pid}) — leaving in place (fail-closed)."
                )
            else:
                logger.warning(
                    f"PID file {pid_path} now records pid {current}, not ours "
                    f"({owner_pid}) — a newer recorder owns it; not removing."
                )
            return
    try:
        pid_path.unlink()
        logger.debug(f"PID file removed: {pid_path}")
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(f"Could not remove PID file {pid_path}: {exc}")


# ---------------------------------------------------------------------------
# Status file (liveness + failure-state for ``onoats status`` / the menu bar)
#
# Thin recorder-process-only wrappers over ``onoats.status`` — mirror the pid
# write/remove split (the schema + atomic I/O live in the standalone module; the
# producer calls live here, alongside the pid producer, and are imported by
# ``dual.py``). Status writes are best-effort: a status-file failure must never
# take down a recording, so each wrapper swallows + logs and carries on.
# ---------------------------------------------------------------------------


def _write_status_running(
    data_dir: Path,
    *,
    audio_source: str,
    stt_label: str,
    warning: str | None = None,
) -> None:
    """Write the start-of-session status (``running=true``). Best-effort.

    ``warning`` threads a preflight-path kickstart-recovery message straight
    into the fresh record (see ``status.write_running``'s docstring) — the
    live-path equivalent still goes through ``status.set_warning_branch``.
    """
    try:
        _status_mod.write_running(
            data_dir,
            pid=os.getpid(),
            audio_source=audio_source,
            stt_label=stt_label,
            warning=warning,
        )
    except OSError as exc:
        logger.warning(f"Could not write status file (start): {exc}")


def _mark_status_rotation(data_dir: Path) -> None:
    """Stamp ``last_rotation_time`` on the current status record. Best-effort."""
    try:
        _status_mod.mark_rotation(data_dir)
    except OSError as exc:
        logger.warning(f"Could not update status file (rotation): {exc}")


def _write_status_stopped(
    data_dir: Path,
    *,
    exit_reason: str = "graceful",
    last_error: str | None = None,
    supervisor_rc: int | None = None,
) -> None:
    """Write the end-of-session status (``running=false``) + failure detail.

    Called inside the single-writer shutdown path BEFORE the pid file is removed,
    so the pid backstop and the status file never disagree about a live recorder.
    """
    try:
        _status_mod.write_stopped(
            data_dir,
            exit_reason=exit_reason,
            last_error=last_error,
            supervisor_rc=supervisor_rc,
        )
    except Exception as exc:
        # Not `except OSError`. This runs in the single-writer shutdown tail,
        # before the pid file is removed — if it raises, the pid file survives
        # a stopped session and every later `onoats status` reads a live
        # recorder that is not there. `write_stopped` reads the existing
        # record first, and `read_status`'s "never an exception" contract has
        # been broken by a `ValueError` subclass before (`UnicodeDecodeError`
        # on a mojibake status file); `json.dumps` can raise `TypeError` on a
        # field a future `StatusRecord` adds. Neither is an `OSError`. The
        # shutdown tail's job is to finish, so the net is the exception
        # hierarchy, not one branch of it.
        logger.warning(f"Could not write status file (stop): {exc}")


def _install_signal_handlers(
    shutdown_event: asyncio.Event,
    force_exit_event: asyncio.Event,
    flush_callback,
    silence_detector,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Install signal handlers.

    - SIGINT (Ctrl+C once): graceful shutdown (flush + drain tasks)
    - SIGINT (Ctrl+C again during shutdown): force exit (cancel pending tasks)
    - SIGTERM: graceful shutdown
    - SIGUSR1: flush current transcript, keep listening (used by ``onoats flush``)
    """

    def _handle_shutdown(sig):
        if shutdown_event.is_set():
            logger.warning(
                "Received second Ctrl+C — forcing exit (cancelling pending tasks)"
            )
            loop.call_soon_threadsafe(force_exit_event.set)
        else:
            logger.info(f"Received signal {sig.name} — initiating graceful shutdown")
            loop.call_soon_threadsafe(shutdown_event.set)

    def _handle_flush(sig):
        logger.info(f"Received {sig.name} — manual flush requested")
        silence_detector.reset_timer()
        asyncio.ensure_future(flush_callback("Manual flush (SIGUSR1)"))

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_shutdown, sig)
        loop.add_signal_handler(signal.SIGUSR1, _handle_flush, signal.SIGUSR1)
    else:
        logger.debug("Signal handlers: using default (Windows platform)")


def _start_keypress_reader(flush_callback, silence_detector, loop) -> list | None:
    """Start a background thread that reads stdin keypresses in cbreak mode.

    Maps Ctrl+T (0x14) to flush the current transcript.
    Returns the original terminal settings (for restore on shutdown),
    or None if stdin is not a TTY.
    """
    if sys.platform == "win32":
        logger.debug("Keypress reader: not supported on Windows")
        return None
    if not sys.stdin.isatty():
        logger.debug("Keypress reader: stdin is not a TTY, skipping cbreak setup")
        return None

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    logger.debug("Keypress reader: terminal set to cbreak mode")

    def _reader():
        try:
            while True:
                ch = sys.stdin.read(1)
                if not ch:
                    break
                if ch == "\x14":
                    silence_detector.reset_timer()
                    loop.call_soon_threadsafe(
                        asyncio.ensure_future,
                        flush_callback("Manual flush (Ctrl+T)"),
                    )
        except (OSError, ValueError):
            pass

    thread = threading.Thread(target=_reader, daemon=True, name="keypress_reader")
    thread.start()
    return old_settings


def _restore_terminal(old_settings: list | None) -> None:
    """Restore terminal settings from cbreak mode."""
    if old_settings is None or sys.platform == "win32":
        return
    try:
        fd = sys.stdin.fileno()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        logger.debug("Keypress reader: terminal settings restored")
    except (OSError, ValueError):
        pass
