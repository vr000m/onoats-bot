"""Shared runtime helpers for the Koda listener entrypoints.

Both ``bot/__main__.py`` (single-input) and ``bot/dual.py`` (dual-input)
need the same PID-file discipline, signal handlers, STT service builder,
crash recovery, and post-processing pipeline. Extracting them here avoids
having ``bot/dual.py`` reach into ``bot/__main__.py`` for leading-underscore
symbols, and gives both entrypoints a single canonical home for the
``_topic_pipeline_tasks`` set that previously lived at module scope on the
single-input runner but was mutated by both.

Nothing here is public API — the module is internal to ``bot/``.
"""

from __future__ import annotations

import asyncio
import os
import platform
import signal
import sys
import threading
import time
from pathlib import Path

from loguru import logger

from shared.koda_pid import PID_FILENAME, PID_MARKER, read_pid_file as _read_pid_file  # noqa: F401

# termios/tty are Unix-only — guard for Windows compatibility
if sys.platform != "win32":
    import termios
    import tty

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOT_NAME = "Koda"
STT_SERVICE = os.getenv("STT_SERVICE", "whisper").lower().strip()
STT_MODEL = os.getenv("STT_MODEL", "").strip()


class SttPreflightError(RuntimeError):
    """Raised when the stt_server endpoint is not reachable at startup.

    Caught at the CLI entrypoints (``bot/__main__.py``, ``bot/dual.py``) so
    the user sees the actionable hint — not a Python traceback — when the
    LaunchAgent isn't loaded.
    """


PIPELINE_SAMPLE_RATE = 16000  # Silero VAD requires 8kHz or 16kHz; 16kHz is standard

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


_DEFAULT_STT_WS_SOCKET = "~/Library/Caches/koda-stt/stt.sock"


def _resolve_stt_ws_target(
    env: dict[str, str], *, warn_on_cleartext: bool = True
) -> dict[str, object]:
    """Resolve STT_WS_* env vars into the kwargs for ``WebSocketSTTService``.

    Delegates precedence handling to ``stt_server.client.resolve_endpoint_from_env``
    and layers on the Koda-specific default socket plus the ``STT_WS_TOKEN``
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
        socket_path = env.get("STT_WS_DEFAULT_SOCKET") or os.path.expanduser(_DEFAULT_STT_WS_SOCKET)

    # Cleartext-token guard covers *any* cleartext-ws endpoint, not just
    # STT_WS_URI. host+port paths get lowered to ``ws://host:port/`` via
    # the same formatter the client uses (IPv6 literals bracketed) so the
    # ``is_cleartext_remote`` check is identical regardless of which
    # supported config surface the operator chose.
    effective_uri = uri
    if not effective_uri and host and port is not None and not socket_path:
        effective_uri = f"ws://{format_host_for_uri(host)}:{port}/"
    if warn_on_cleartext and auth_token and effective_uri and is_cleartext_remote(effective_uri):
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
    if STT_SERVICE == "websocket":
        target = _display_target(_resolve_stt_ws_target(os.environ.copy(), warn_on_cleartext=False))
        return f"websocket (server={target}, model pinned by server)"
    return f"{STT_SERVICE} / model={STT_MODEL or 'default'}"


_PREFLIGHT_TIMEOUT_SEC = 2.0
# Cold-start tolerance: a single 2s connect is tight when the
# LaunchAgent was just kicked (e.g. `./koda stt start && ./koda bot`).
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
        from stt_server import protocol as P
        from stt_server.client import TranscriptionClient

        # The primary STT service path already logged any cleartext-token
        # warning at session start; suppress here so startup+shutdown
        # probes don't duplicate it.
        kwargs = _resolve_stt_ws_target(os.environ.copy(), warn_on_cleartext=False)
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
                rss_mb = (int(rss) / (1024 * 1024)) if isinstance(rss, (int, float)) else 0.0
                uptime_s = float(uptime) if isinstance(uptime, (int, float)) else 0.0
                # server.status mirrors the server.hello backend identity, so
                # the probe line names the real ASR behind the socket — a
                # wrong-model misconfig shows up in the RSS log too, not just
                # at connect. Additive field: omit cleanly on older servers.
                backend = event.get("backend") or {}
                backend_desc = (
                    f" backend={backend.get('name')}/{backend.get('model')}" if backend else ""
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
        "Start it with: ./koda stt start   (or: scripts/install_stt_agent.sh "
        "install — only needed once). Verify with: ./koda stt status"
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
                raise SttPreflightError(f"STT: misconfigured endpoint ({exc}). {hint}") from exc
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


async def _create_stt_service():
    """Build the STT service based on STT_SERVICE / STT_MODEL env vars.

    Returns a pipecat STT service instance. Prefers Whisper MLX on Apple Silicon,
    falls back to CPU Whisper, or uses Deepgram when STT_SERVICE=deepgram.

    Async because the websocket preflight now does a real handshake
    (rather than a raw TCP probe), which must be awaited from inside the
    running event loop. Non-websocket backends don't ``await`` anything
    but the signature is uniform so callers don't have to branch.
    """
    if STT_SERVICE == "websocket":
        try:
            from bot.stt.websocket_stt_service import WebSocketSTTService
        except ImportError as exc:
            raise RuntimeError(
                "STT_SERVICE=websocket requires the 'websockets' package. "
                "Install via `uv sync --extra stt-server-client` "
                f"(or add websockets to the root deps). Original error: {exc}"
            ) from exc

        kwargs = _resolve_stt_ws_target(os.environ)
        target = _display_target(kwargs)
        logger.info(f"STT: websocket (server={target})")
        await _preflight_stt_ws(kwargs, target)
        return WebSocketSTTService(language="en", **kwargs)

    if STT_SERVICE == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        from shared.config import looks_like_bearer_token, require_secret

        dg_kwargs: dict = {
            "api_key": require_secret(
                "DEEPGRAM_API_KEY",
                validate=looks_like_bearer_token,
                hint="Get one at https://console.deepgram.com",
            )
        }
        if STT_MODEL:
            from deepgram import LiveOptions

            dg_kwargs["live_options"] = LiveOptions(model=STT_MODEL)
        logger.info(f"STT: deepgram (model={STT_MODEL or 'default'})")
        return DeepgramSTTService(**dg_kwargs)

    # Default: Whisper (MLX on Apple Silicon, CPU otherwise)
    if _mlx_available():
        from pipecat.services.whisper.stt import MLXModel, WhisperSTTServiceMLX

        mlx_key = _MLX_MODEL_MAP.get(STT_MODEL or "large-v3-turbo", "LARGE_V3_TURBO").upper()
        mlx_model = getattr(MLXModel, mlx_key, None)
        if mlx_model is None:
            logger.warning(f"Unknown MLX model name '{STT_MODEL}', falling back to large-v3-turbo")
            mlx_model = MLXModel.LARGE_V3_TURBO
        logger.info(f"STT: whisper-mlx (model={mlx_model.name}, device=Apple Silicon)")
        return WhisperSTTServiceMLX(
            settings=WhisperSTTServiceMLX.Settings(model=mlx_model.value, language="en")
        )
    else:
        from pipecat.services.whisper.stt import WhisperSTTService

        model = STT_MODEL or "base"
        logger.info(f"STT: whisper-cpu (model={model})")
        return WhisperSTTService(
            settings=WhisperSTTService.Settings(model=model, device="cpu", language="en")
        )


# ---------------------------------------------------------------------------
# Post-processing: segment → classify → write
# ---------------------------------------------------------------------------


# Deep-review A1: ``_run_topic_pipeline`` and ``run_post_processing`` were
# moved to ``shared.post_processing_services`` (worker → bot was the wrong
# layer direction). Thin re-exports are kept here so callers / tests that
# imported them from ``bot.runtime`` continue to work. ``_cleanup_session``
# is intentionally NOT re-exported — it is a private delete-on-success
# helper that no live external caller needs.
from shared.post_processing_services import (  # noqa: E402,PLC0414
    _run_topic_pipeline as _run_topic_pipeline,
)
from shared.post_processing_services import (  # noqa: E402,PLC0414
    run_post_processing as run_post_processing,
)


# ---------------------------------------------------------------------------
# Bot-side flush: rotate the .active/ file into the pending/ queue
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

    Decoupling-plan Phase 2: this replaces the old ``_flush_and_process``
    body. The bot is now a thin recorder — it does NOT run the
    post-processing pipeline. It flushes the in-memory buffer to disk, then
    rotates the finalised ``.active/`` session file into the ``pending/``
    queue and inserts a ``processing_jobs`` row. A cron-driven worker drains
    the queue.

    Flush kinds (see the plan's "Flush triggers" table):

    * ``continue_session=False`` — terminal flush (``EndFrame`` / shutdown):
      rotate ``.active/X.jsonl`` → ``pending/X.jsonl`` and stop.
    * ``continue_session=True`` — continuation flush (silence-timeout,
      Ctrl+T, ``SIGUSR1``): rotate FIRST, then a fresh ``.active/`` session
      is opened by :func:`session_queue.rotate_to_pending` and adopted by
      the buffer so the ongoing recording has somewhere to land.

    Ordering invariant: file rename FIRST, DB insert SECOND. A crash between
    the two leaves a ``pending/`` file with no row — recoverable, because
    :func:`session_queue.claim` back-fills the row.
    """
    from shared import session_queue

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
            next_active_path, _next_session_id = session_queue.new_active_session(data_dir)
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
        session_id = session_queue.rotate_active_to_pending(session_path, data_dir=data_dir)
    except FileNotFoundError:
        logger.warning(
            f"Flush: session file {session_path.name} vanished before rotation — nothing to queue"
        )
        return
    except OSError as exc:
        logger.error(f"Flush: could not rotate {session_path.name} to pending/: {exc}")
        return

    # Insert the processing_jobs row AFTER the rename (file-first ordering).
    try:
        session_queue.enqueue_job(session_id, data_dir, locked_category=locked_category)
    except Exception as exc:
        logger.warning(
            f"Flush: rotated {session_id} but DB insert failed ({exc}) — "
            "claim() will back-fill the row."
        )

    logger.info(f"Flush: rotated {session_id} → pending/ (worker will post-process it)")
    if continue_session and next_active_path is not None:
        logger.debug(
            f"Flush: buffer swapped to fresh active session {next_active_path.name} under lock"
        )


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


async def run_crash_recovery(
    dictionary=None,
    segmenter=None,
    classifier=None,
    transcript_store=None,
    data_dir: Path | None = None,
    transcript_cleaner=None,
    locked_category: str | None = None,
) -> None:
    """Rotate orphaned ``.active/`` session files into the ``pending/`` queue.

    Decoupling-plan Phase 2: crash recovery no longer runs the
    post-processing pipeline inline. Instead it *rotates* every orphaned
    ``.active/`` file into ``pending/`` and inserts a ``processing_jobs``
    row, just like a live flush does — the cron-driven worker then drains
    it. This removes the bot end-vs-start race entirely.

    First-run backfill: this also picks up any pre-existing
    ``.active/session_*.jsonl`` AND legacy ``.recovering`` files left behind
    by the old crash-recovery scheme and rotates them into ``pending/`` so
    nothing stranded by the previous deploy is lost. The old ``flock`` /
    ``.recovering`` claim is no longer used on session files — the
    ``rename(2)`` into ``pending/`` is the only claim now.

    The post-processing service arguments are accepted only for call-site
    compatibility with the previous signature; they are unused.
    """
    from shared import session_queue
    from shared.store import koda_data_dir

    base = Path(data_dir) if data_dir is not None else koda_data_dir()
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
                logger.warning(f"Crash recovery: could not stash colliding {rec_path.name}: {exc}")
            continue
        try:
            os.rename(rec_path, jsonl_path)
            normalised.append(jsonl_path)
        except OSError as exc:
            logger.warning(f"Crash recovery: could not normalise legacy {rec_path.name}: {exc}")

    for session_path in normalised:
        try:
            rotation = session_queue.rotate_to_pending(
                session_path, continue_session=False, data_dir=base
            )
        except FileNotFoundError:
            # Another actor moved it between the glob and the rename.
            continue
        except OSError as exc:
            logger.error(f"Crash recovery: could not rotate {session_path.name} to pending/: {exc}")
            continue

        # Insert the processing_jobs row AFTER the rename (file-first
        # ordering). A crash between the two is recoverable: session_queue
        # .claim() back-fills a missing row when a worker claims the file.
        try:
            session_queue.enqueue_job(rotation.session_id, base, locked_category=locked_category)
        except Exception as exc:
            logger.warning(
                f"Crash recovery: rotated {rotation.session_id} but DB insert "
                f"failed ({exc}) — claim() will back-fill the row."
            )
        logger.info(
            f"Crash recovery: rotated {rotation.session_id} → pending/ (worker will process it)"
        )


def _insert_pending_job(
    session_id: str,
    data_dir: Path,
    *,
    locked_category: str | None = None,
) -> None:
    """Deprecated thin alias for :func:`shared.session_queue.enqueue_job`.

    Deep-review A2: the enqueue-side DB write now lives in
    ``shared.session_queue`` (FSM writes consolidated in one module). This
    alias is kept ONLY so pre-existing tests that imported
    ``bot.runtime._insert_pending_job`` (see
    ``tests/test_processing_worker.py``) continue to import cleanly. New
    code MUST call :func:`shared.session_queue.enqueue_job` directly.
    """
    from shared.session_queue import enqueue_job

    enqueue_job(session_id, data_dir, locked_category=locked_category)


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


def _write_pid_file(data_dir: Path) -> Path:
    """Write the current process PID, identity marker, and cmdline fingerprint."""
    active_dir = data_dir / ".active"
    active_dir.mkdir(parents=True, exist_ok=True)
    pid_path = active_dir / PID_FILENAME

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
    # happens to have inherited a recycled pid (see shared.koda_pid).
    start_epoch = time.time()
    pid_path.write_text(
        f"{os.getpid()}\n{PID_MARKER}\n{cmdline}\n{start_epoch}\n",
        encoding="utf-8",
    )
    logger.debug(f"PID file written: {pid_path} (PID {os.getpid()}, cmdline={cmdline!r})")
    return pid_path


def _remove_pid_file(pid_path: Path) -> None:
    """Remove the PID file on shutdown."""
    try:
        pid_path.unlink()
        logger.debug(f"PID file removed: {pid_path}")
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning(f"Could not remove PID file {pid_path}: {exc}")


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
    - SIGUSR1: flush current transcript, keep listening (used by ``./koda flush``)
    """

    def _handle_shutdown(sig):
        if shutdown_event.is_set():
            logger.warning("Received second Ctrl+C — forcing exit (cancelling pending tasks)")
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
