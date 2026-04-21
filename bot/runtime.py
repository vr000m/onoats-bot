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
import socket
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


def _resolve_stt_ws_target(env: dict[str, str]) -> dict[str, object]:
    """Resolve STT_WS_* env vars into the kwargs for ``WebSocketSTTService``.

    Enforces documented precedence ``STT_WS_URI > STT_WS_SOCKET > HOST+PORT``
    by zeroing lower-priority fields when a higher-priority one is set —
    ``TranscriptionClient.connect()`` otherwise prefers ``socket_path``
    whenever it is set, silently ignoring a URI override.
    """
    socket_path = (env.get("STT_WS_SOCKET") or "").strip() or None
    host = (env.get("STT_WS_HOST") or "").strip() or None
    port_raw = (env.get("STT_WS_PORT") or "").strip()
    port: Optional[int] = int(port_raw) if port_raw else None
    uri = (env.get("STT_WS_URI") or "").strip() or None
    auth_token = (env.get("STT_WS_TOKEN") or "").strip() or None

    if not (socket_path or host or uri):
        default_sock = os.path.expanduser("~/Library/Caches/koda-stt/stt.sock")
        socket_path = env.get("STT_WS_DEFAULT_SOCKET") or default_sock

    if uri:
        socket_path = None
        host = None
        port = None
    elif socket_path:
        host = None
        port = None

    return {
        "socket_path": socket_path,
        "host": host,
        "port": port,
        "uri": uri,
        "auth_token": auth_token,
    }


def _preflight_stt_ws(kwargs: dict, target: str) -> None:
    """Fail fast if the stt_server endpoint is not reachable at startup.

    Without this, a missing server manifests as a 30–60 s cascade of
    VAD-driven connect warnings with no transcription, which is easy to
    miss in a long log stream. A single clear error up front pointing at
    ``./koda stt start`` is a lot easier to act on than 20 WARN lines.

    Runtime reconnect during a live session is still handled by
    ``WebSocketSTTService._ensure_connected`` — this check only runs once
    before the pipeline starts.
    """
    sock_path = kwargs.get("socket_path")
    host = kwargs.get("host")
    port = kwargs.get("port")

    hint = (
        "Start it with: ./koda stt start   (or: scripts/install_stt_agent.sh "
        "install — only needed once). Verify with: ./koda stt status"
    )

    # ``port is not None`` rather than ``port`` — port=0 is not a valid WS
    # endpoint but would be falsy and silently skip the probe, defeating
    # fail-fast when the caller misconfigured host+port.
    try:
        if sock_path:
            expanded = os.path.expanduser(sock_path)
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)
            try:
                s.connect(expanded)
            finally:
                s.close()
        elif host and port is not None:
            with socket.create_connection((host, int(port)), timeout=0.5):
                pass
        else:
            # ws://… URI — skip TCP probe; websockets.asyncio.connect handles
            # reconnects for us and URIs may encode paths/schemes we don't
            # want to re-parse here.
            return
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError) as exc:
        raise RuntimeError(f"STT: stt_server not reachable at {target} ({exc}). {hint}") from exc


def _create_stt_service():
    """Build the STT service based on STT_SERVICE / STT_MODEL env vars.

    Returns a pipecat STT service instance. Prefers Whisper MLX on Apple Silicon,
    falls back to CPU Whisper, or uses Deepgram when STT_SERVICE=deepgram.
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
        _preflight_stt_ws(kwargs, target)
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
