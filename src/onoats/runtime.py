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
import os
import platform
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

from loguru import logger

from onoats._vendor.pid import (  # noqa: F401
    PID_FILENAME,
    PID_MARKER,
    read_pid_file as _read_pid_file,
)

# termios/tty are Unix-only — guard for Windows compatibility
if sys.platform != "win32":
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
            f"STT: STT_WS_TOKEN will be sent in cleartext to {effective_uri}. "
            "Use wss:// for remote hosts, or bind to loopback (127.0.0.1 / ::1 / UDS)."
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
    """
    uri = kwargs.get("uri")
    if uri:
        import urllib.parse

        try:
            parsed = urllib.parse.urlsplit(uri)
        except ValueError:
            return uri
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port is not None:
                netloc = f"{netloc}:{parsed.port}"
            return urllib.parse.urlunsplit(
                (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
            )
        return uri
    return kwargs.get("socket_path") or f"{kwargs.get('host')}:{kwargs.get('port')}"


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
# Keyed on the endpoint tuple, not a bare bool, so that if a future
# caller builds kwargs for a *different* endpoint on the second call
# (dual path today uses the same resolved kwargs for both branches, but
# nothing in the type system pins that) the probe re-runs against the
# new endpoint instead of silently trusting a stale success.
_preflight_cache: set[tuple[object, object, object, object]] = set()


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
        from onoats.config import load_config
        from stt_server import protocol as P
        from stt_server.client import TranscriptionClient

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
            async for event in client.events():
                if event.get("type") != P.EVT_SERVER_STATUS:
                    continue
                pid = event.get("pid")
                rss = event.get("rss_bytes")
                uptime = event.get("uptime_seconds")
                rss_mb = (
                    (int(rss) / (1024 * 1024)) if isinstance(rss, (int, float)) else 0.0
                )
                uptime_s = float(uptime) if isinstance(uptime, (int, float)) else 0.0
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

        try:
            await asyncio.wait_for(_probe(), timeout=_RSS_PROBE_TIMEOUT_SEC)
        finally:
            try:
                await client.close_session()
            except Exception:
                pass
            try:
                await client.close()
            except Exception:
                pass
    except Exception as exc:
        logger.debug(f"stt_server RSS ({phase}): probe failed ({exc})")


def _preflight_key(kwargs: dict) -> tuple[object, object, object, object]:
    return (
        kwargs.get("uri"),
        kwargs.get("socket_path"),
        kwargs.get("host"),
        kwargs.get("port"),
    )


async def _preflight_stt_ws(kwargs: dict, target: str) -> None:
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
    """
    key = _preflight_key(kwargs)
    if key in _preflight_cache:
        return

    from stt_server.client import TranscriptionClient

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

    # Attempt schedule — first is the fast-path, second retries only on
    # cold-start races (OSError). Other exceptions fail on the first try.
    attempts = (
        (_PREFLIGHT_TIMEOUT_SEC, 0.0),
        (_PREFLIGHT_RETRY_TIMEOUT_SEC, _PREFLIGHT_RETRY_DELAY_SEC),
    )
    total_budget = sum(t + d for t, d in attempts)

    client = TranscriptionClient(
        socket_path=sock_path,
        host=host,
        port=port,
        uri=uri,
        auth_token=kwargs.get("auth_token"),
    )
    try:
        for idx, (timeout_s, delay_s) in enumerate(attempts):
            is_last = idx == len(attempts) - 1
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            try:
                await asyncio.wait_for(client.connect(), timeout=timeout_s)
                break
            except asyncio.TimeoutError as exc:
                if not is_last:
                    continue
                raise SttPreflightError(
                    f"STT: stt_server did not complete handshake within "
                    f"{total_budget:.1f}s at {target}. {hint}"
                ) from exc
            except ValueError as exc:
                # Mis-shaped kwargs slipped past our completeness check
                # (future callers may build kwargs differently). Still
                # actionable, still better than a traceback.
                raise SttPreflightError(
                    f"STT: misconfigured endpoint ({exc}). {hint}"
                ) from exc
            except OSError as exc:  # covers FileNotFoundError + ConnectionRefusedError
                # Cold-start races live here: socket path doesn't exist
                # yet, or TCP refused because serve() hasn't bound. Retry
                # once with a short delay so the bot doesn't exit just
                # because the LaunchAgent is still doing `import
                # mlx_whisper`.
                if not is_last:
                    continue
                raise SttPreflightError(
                    f"STT: stt_server not reachable at {target} ({exc}). {hint}"
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
                    f"STT: handshake failed at {target} ({type(exc).__name__}: {exc}). {hint}"
                ) from exc
    finally:
        try:
            await client.close_session()
        except Exception:
            pass
        try:
            await client.close()
        except Exception:
            pass

    _preflight_cache.add(key)


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


async def _create_stt_service():
    """Build the STT service based on STT_SERVICE / STT_MODEL env vars.

    Returns a pipecat STT service instance. Prefers Whisper MLX on Apple Silicon,
    falls back to CPU Whisper, or uses Deepgram when STT_SERVICE=deepgram.

    Dictionary vocabulary terms (if any) are fed to the backend as recognition
    bias (Deepgram ``keywords`` / Whisper ``initial_prompt``).

    Async because the websocket preflight now does a real handshake
    (rather than a raw TCP probe), which must be awaited from inside the
    running event loop. Non-websocket backends don't ``await`` anything
    but the signature is uniform so callers don't have to branch.

    MLX / Whisper imports are kept lazy (inside the backend branch) so a plain
    ``import onoats.runtime`` with no ``STT_SERVICE`` set never imports
    ``mlx_whisper`` — the off-mac baseline ships MLX-free.
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

        kwargs = _resolve_stt_ws_target(_ws_env(cfg))
        target = _display_target(kwargs)
        logger.info(f"STT: websocket (server={target})")
        await _preflight_stt_ws(kwargs, target)
        # The language is forwarded to the server's decoder via
        # ``update_session`` (see ``WebSocketSTTService``). Resolved from
        # ``cfg.stt_language`` above (env STT_LANGUAGE > legacy STT_WS_LANGUAGE
        # > config.toml [stt].language > "en"). Not threaded through
        # ``_resolve_stt_ws_target`` because that dict also feeds
        # ``TranscriptionClient``, which takes no ``language`` kwarg.
        return WebSocketSTTService(language=language, **kwargs)

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
        return DeepgramSTTService(**dg_kwargs)

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
        return WhisperSTTServiceMLX(
            settings=WhisperSTTServiceMLX.Settings(
                model=mlx_model.value, language=language
            )
        )
    else:
        from pipecat.services.whisper.stt import WhisperSTTService

        model = model_name or "base"
        logger.info(f"STT: whisper-cpu (model={model}, language={language or 'auto'})")
        # device/compute_type are WhisperSTTService constructor kwargs, NOT
        # Settings fields — passing device into Settings raises TypeError.
        return WhisperSTTService(
            device="cpu",
            settings=WhisperSTTService.Settings(model=model, language=language),
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


def _write_pid_file(data_dir: Path) -> Path:
    """Write the current process PID, identity marker, and cmdline fingerprint.

    Single-instance guard: refuses to start over a still-live recorder (raising
    ``RecorderAlreadyRunningError``). The check reuses the same identity gate as
    ``onoats stop``/``flush`` (``resolve_flush_target``: marker + cmdline
    fingerprint + liveness), so a *positively stale* pid — dead, or recycled to a
    foreign process (identity mismatch) — does NOT block a legitimate start. But a
    marker-valid pid file naming a LIVE process whose identity cannot be verified
    (``ps`` probe failed, or a legacy fingerprint-less file) is treated the same
    way ``flush``/``stop`` treat that indeterminate state — refuse, never
    overwrite — so a transient probe failure can't spawn a second recorder over a
    live one. This closes the stop-then-immediate-start race (and its degraded
    variants): without it, a second start would overwrite a draining recorder's
    pid file, and the drainer would later unlink THIS start's file (see also the
    ownership check in ``_remove_pid_file``).
    """
    active_dir = data_dir / ".active"
    active_dir.mkdir(parents=True, exist_ok=True)
    pid_path = active_dir / PID_FILENAME

    from onoats._vendor.pid import read_pid_record, resolve_flush_target

    verified = resolve_flush_target(pid_path)
    if verified.pid is not None and verified.pid != os.getpid():
        raise RecorderAlreadyRunningError(
            f"An onoats recorder is already running (pid {verified.pid}). "
            "Stop it first with `onoats stop`, then retry."
        )
    # Indeterminate-but-live: the resolver refused (pid is None) WITHOUT declaring
    # the file stale — i.e. it is neither a verified recorder nor a proven-dead /
    # recycled-foreign pid, but a marker-valid file whose process is still alive
    # and merely unverifiable (ps probe failed / legacy fingerprint-less). Refuse,
    # exactly as flush/stop do for the same state; only ``stale=True`` is safe to
    # clobber.
    if verified.pid is None and not verified.stale:
        rec = read_pid_record(pid_path)
        if rec is not None and rec.pid != os.getpid() and _pid_alive(rec.pid):
            raise RecorderAlreadyRunningError(
                f"A recorder pid file names a live process (pid {rec.pid}) whose "
                "identity could not be verified (ps probe failed / legacy pid "
                "file) — refusing to start over a possibly-live recorder. Stop it "
                "(`onoats stop`) or remove the stale pid file, then retry."
            )

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


def _write_status_running(data_dir: Path, *, audio_source: str, stt_label: str) -> None:
    """Write the start-of-session status (``running=true``). Best-effort."""
    from onoats import status as _status

    try:
        _status.write_running(
            data_dir,
            pid=os.getpid(),
            audio_source=audio_source,
            stt_label=stt_label,
        )
    except OSError as exc:
        logger.warning(f"Could not write status file (start): {exc}")


def _mark_status_rotation(data_dir: Path) -> None:
    """Stamp ``last_rotation_time`` on the current status record. Best-effort."""
    from onoats import status as _status

    try:
        _status.mark_rotation(data_dir)
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
    from onoats import status as _status

    try:
        _status.write_stopped(
            data_dir,
            exit_reason=exit_reason,
            last_error=last_error,
            supervisor_rc=supervisor_rc,
        )
    except OSError as exc:
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
