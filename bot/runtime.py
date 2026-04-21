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
from pathlib import Path
from typing import Optional

from loguru import logger

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

PID_FILENAME = "koda.pid"
_PID_MARKER = "koda-bot"

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


def _resolve_stt_ws_target(env: dict[str, str]) -> dict[str, object]:
    """Resolve STT_WS_* env vars into the kwargs for ``WebSocketSTTService``.

    Delegates precedence handling to ``stt_server.client.resolve_endpoint_from_env``
    and layers on the Koda-specific default socket plus the ``STT_WS_TOKEN``
    bearer read. When operators point at a cleartext remote host, warn
    before attaching the token so a passive on-path observer cannot
    silently capture it.
    """
    from stt_server.client import (
        _format_host_for_uri,
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
        effective_uri = f"ws://{_format_host_for_uri(host)}:{port}/"
    if auth_token and effective_uri and is_cleartext_remote(effective_uri):
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


_PREFLIGHT_TIMEOUT_SEC = 2.0
_preflight_done = False


async def _preflight_stt_ws(kwargs: dict, target: str) -> None:
    """Fail fast if the stt_server endpoint is not reachable at startup.

    Runs a real websocket handshake (``TranscriptionClient.connect()`` —
    which awaits ``server.hello`` + ``session.created``), so auth, TLS,
    wrong path, and "non-STT service on the port" failures are all
    surfaced here as ``SttPreflightError`` instead of leaking through as
    generic tracebacks or the old 30–60 s VAD-driven reconnect cascade.

    Idempotent via ``_preflight_done`` so the dual entrypoint can call
    ``_create_stt_service()`` twice without paying for two handshakes
    against the same endpoint.

    Runtime reconnect during a live session is still handled by
    ``WebSocketSTTService._ensure_connected`` — this check only runs once
    before the pipeline starts.
    """
    global _preflight_done
    if _preflight_done:
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

    client = TranscriptionClient(
        socket_path=sock_path,
        host=host,
        port=port,
        uri=uri,
        auth_token=kwargs.get("auth_token"),
    )
    try:
        try:
            await asyncio.wait_for(client.connect(), timeout=_PREFLIGHT_TIMEOUT_SEC)
        except asyncio.TimeoutError as exc:
            raise SttPreflightError(
                f"STT: stt_server did not complete handshake within "
                f"{_PREFLIGHT_TIMEOUT_SEC:.1f}s at {target}. {hint}"
            ) from exc
        except ValueError as exc:
            # Mis-shaped kwargs slipped past our completeness check
            # (future callers may build kwargs differently). Still
            # actionable, still better than a traceback.
            raise SttPreflightError(f"STT: misconfigured endpoint ({exc}). {hint}") from exc
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
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
            # letting them bubble as tracebacks.
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

    _preflight_done = True


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
        target = kwargs["uri"] or kwargs["socket_path"] or f"{kwargs['host']}:{kwargs['port']}"
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


async def _run_topic_pipeline(transcript_id: str, store) -> None:
    """Fire-and-forget: match tags → extract passages → refresh collations.

    Also runs legacy collate_for_transcript for ideas transcripts to keep
    flat-file topics up to date even when directory topics also matched.
    """
    try:
        from shared.llm_client import create_llm_client
        from shared.topic_pipeline import process_transcript

        llm = create_llm_client(task="collate")
        matched = await process_transcript(transcript_id, store, llm)
        if matched:
            logger.info(f"Topic pipeline: processed {len(matched)} topic(s) for {transcript_id}")

        summary = await store.get_transcript_summary(transcript_id)
        if summary and summary.category == "ideas":
            from shared.collation_service import CollationService

            service = CollationService(store, llm)
            paths = await service.collate_for_transcript(transcript_id)
            if paths:
                logger.info(f"Legacy collation: updated {len(paths)} topic(s) for {transcript_id}")
    except Exception as exc:
        logger.warning(f"Topic pipeline failed for {transcript_id}: {exc}")


def _cleanup_session(session_path: Optional[Path]) -> None:
    """Delete the .active/ session file after successful post-processing."""
    if session_path is None:
        return
    from shared.memory_writer import delete_session_file

    delete_session_file(session_path)


async def run_post_processing(
    buffer_contents: list[dict],
    dictionary,
    segmenter,
    classifier,
    transcript_store,
    session_path: Optional[Path],
    transcript_cleaner=None,
    locked_category: str | None = None,
) -> None:
    """Process a flushed transcript buffer through dictionary → segment → cleanup → classify → write."""
    if not buffer_contents:
        logger.debug("Post-processing: empty buffer — nothing to process")
        return

    utterance_count = sum(1 for e in buffer_contents if e.get("type") == "utterance")
    logger.info(
        f"Post-processing: {len(buffer_contents)} buffer entries ({utterance_count} utterances)"
    )

    if segmenter is None or classifier is None:
        logger.warning(
            "Post-processing: segmenter or classifier not available — "
            "skipping classification and write. "
            "Implement services/classifier.py to enable full post-processing."
        )
        return

    try:
        dictionary_hash = ""
        if dictionary is not None:
            for entry in buffer_contents:
                if entry.get("type") != "utterance":
                    continue
                text = entry.get("text")
                if isinstance(text, str) and text:
                    entry["text"] = dictionary.apply(text)
            dictionary_hash = dictionary.content_hash()

        segments = await segmenter.segment(buffer_contents)
        logger.info(f"Post-processing: segmented into {len(segments)} conversation(s)")

        if not segments:
            logger.info("Post-processing: no segments produced — nothing to write")
            _cleanup_session(session_path)
            return

        for i, seg_entries in enumerate(segments, 1):
            try:
                classified = await classifier.classify(
                    seg_entries,
                    dictionary_hash=dictionary_hash,
                    transcript_cleaner=transcript_cleaner,
                    locked_category=locked_category,
                )
                transcript_id, path, _was_new = await transcript_store.ingest_segment(classified)
                logger.info(
                    f"Post-processing: segment {i}/{len(segments)} written — "
                    f"{classified.category} / {path.name} / {transcript_id}"
                )
                tp_task = asyncio.create_task(
                    _run_topic_pipeline(transcript_id, transcript_store),
                    name=f"topic_pipeline_{transcript_id}",
                )
                _topic_pipeline_tasks.add(tp_task)
                tp_task.add_done_callback(_topic_pipeline_tasks.discard)
            except Exception as exc:
                logger.error(f"Post-processing: failed to write segment {i}/{len(segments)}: {exc}")
                return

        _cleanup_session(session_path)

    except Exception as exc:
        logger.error(f"Post-processing: unexpected error: {exc}")


# ---------------------------------------------------------------------------
# Crash recovery
# ---------------------------------------------------------------------------


async def run_crash_recovery(
    dictionary,
    segmenter,
    classifier,
    transcript_store,
    data_dir: Path,
    transcript_cleaner=None,
    locked_category: str | None = None,
) -> None:
    """Check for orphaned .active/ session files and process them."""
    from shared.memory_writer import (
        claim_session_file,
        list_orphaned_sessions,
        read_session_file,
        unclaim_session_file,
    )

    orphans = list_orphaned_sessions(data_dir)
    if not orphans:
        logger.debug("Crash recovery: no orphaned session files found")
        return

    logger.info(f"Crash recovery: found {len(orphans)} orphaned session file(s)")

    for session_path in orphans:
        claimed_path = claim_session_file(session_path)
        if claimed_path is None:
            continue

        logger.info(f"Crash recovery: processing {claimed_path.name}")
        try:
            entries = read_session_file(claimed_path)
            if entries is None:
                logger.error(
                    f"Crash recovery: could not read {claimed_path.name} — "
                    "renaming back for next retry"
                )
                unclaim_session_file(claimed_path)
                continue
            if not entries:
                logger.warning(f"Crash recovery: {claimed_path.name} is empty, deleting")
                _cleanup_session(claimed_path)
                continue

            await run_post_processing(
                buffer_contents=entries,
                dictionary=dictionary,
                segmenter=segmenter,
                classifier=classifier,
                transcript_store=transcript_store,
                session_path=claimed_path,
                transcript_cleaner=transcript_cleaner,
                locked_category=locked_category,
            )
            if claimed_path.exists():
                logger.warning(
                    f"Crash recovery: {claimed_path.name} still on disk after "
                    "post-processing — renaming back for next retry"
                )
                unclaim_session_file(claimed_path)
        except Exception as exc:
            logger.error(
                f"Crash recovery: failed to process {claimed_path.name}: {exc}. "
                "Renaming back for next retry."
            )
            unclaim_session_file(claimed_path)


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


def _read_pid_file(pid_path: Path) -> int | None:
    """Read and validate a PID file. Returns the PID if valid, None otherwise."""
    if not pid_path.exists():
        return None
    try:
        lines = pid_path.read_text(encoding="utf-8").strip().splitlines()
        if len(lines) < 2 or lines[1].strip() != _PID_MARKER:
            logger.warning(f"PID file {pid_path} missing identity marker — ignoring")
            return None
        return int(lines[0].strip())
    except (ValueError, OSError):
        return None


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
    pid_path.write_text(
        f"{os.getpid()}\n{_PID_MARKER}\n{cmdline}\n",
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
