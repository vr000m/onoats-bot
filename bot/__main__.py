"""Koda — Always-on voice listener with memory.

Captures mic audio, transcribes with Whisper MLX (or Deepgram), and after a
configurable silence timeout classifies and stores the transcript to cold storage.

Modes:
  silent (default) — listens and transcribes, does NOT speak
  interactive      — voice responses enabled (Phase 2; flag accepted, not yet wired)

Run::

    ./koda bot                           # silent listener
    ./koda bot --interactive             # voice responses enabled (Phase 2 stub)

Config (.env or environment):
    STT_SERVICE          - STT backend: "whisper" (default, local MLX) or "deepgram"
    STT_MODEL            - Model override for chosen STT backend
    LLM_PROVIDER         - LLM provider for post-processing: "gemini" (default)
    KODA_DATA_DIR        - Override default ~/koda-data storage root
    INPUT_DEVICE         - Override mic device index (int); skips interactive picker
    SILENCE_TIMEOUT_SEC  - Seconds of mic silence before flushing buffer (default 300)
    SEGMENT_HINT_THRESHOLD - Seconds of silence to mark a segment hint (default 120)

Required API keys (set in ~/.secrets/ai.env):
    GEMINI_API_KEY       - Google Generative AI (default LLM provider for post-processing)

Optional API keys:
    DEEPGRAM_API_KEY     - Deepgram STT (only when STT_SERVICE=deepgram)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import signal
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

# ---------------------------------------------------------------------------
# Load config before anything else
# ---------------------------------------------------------------------------

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)
load_dotenv(os.path.expanduser("~/.secrets/ai.env"), override=False)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------

BOT_NAME = "Koda"
STT_SERVICE = os.getenv("STT_SERVICE", "whisper").lower().strip()
STT_MODEL = os.getenv("STT_MODEL", "").strip()
_input_dev_env = os.getenv("INPUT_DEVICE", "").strip()
INPUT_DEVICE: Optional[int] = int(_input_dev_env) if _input_dev_env else None

PIPELINE_SAMPLE_RATE = 16000  # Silero VAD requires 8kHz or 16kHz; 16kHz is standard

# ---------------------------------------------------------------------------
# MLX availability check (mirrors kai-pipecat pattern)
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


# Map simple model name strings to MLXModel enum member names
_MLX_MODEL_MAP: dict[str, str] = {
    "tiny": "TINY",
    "medium": "MEDIUM",
    "large-v3": "LARGE_V3",
    "large-v3-turbo": "LARGE_V3_TURBO",
    "large-v3-turbo-q4": "LARGE_V3_TURBO_Q4",
    "distil-large-v3": "DISTIL_LARGE_V3",
}


def _create_stt_service():
    """Build the STT service based on STT_SERVICE / STT_MODEL env vars.

    Returns a pipecat STT service instance. Prefers Whisper MLX on Apple Silicon,
    falls back to CPU Whisper, or uses Deepgram when STT_SERVICE=deepgram.
    """
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


async def _run_collation(transcript_id: str, store) -> None:
    """Fire-and-forget: collate topics related to a newly classified ideas transcript."""
    try:
        from shared.collation_service import CollationService
        from shared.llm_client import create_llm_client

        llm = create_llm_client(task="collate")
        service = CollationService(store, llm)
        paths = await service.collate_for_transcript(transcript_id)
        if paths:
            logger.info(f"Collation: updated {len(paths)} topic(s) for {transcript_id}")
    except Exception as exc:
        logger.warning(f"Collation failed for {transcript_id}: {exc}")


async def run_post_processing(
    buffer_contents: list[dict],
    segmenter,
    classifier,
    transcript_store,
    session_path: Optional[Path],
) -> None:
    """Process a flushed transcript buffer through segment → classify → write.

    Called from the silence timeout callback. Runs as a plain asyncio task,
    outside the pipecat pipeline.

    Args:
        buffer_contents:  List of buffer entry dicts from TranscriptBuffer.flush().
        segmenter:        Segmenter instance (may be None if not yet implemented).
        classifier:       Classifier instance (may be None if not yet implemented).
        transcript_store: TranscriptStore instance for cold storage writes + SQLite overlay.
        session_path:     Path to the .active/ JSONL file to delete on success.
    """
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
        # Leave the session file on disk so it can be retried on next startup
        # once the classifier is implemented.
        return

    try:
        # Step 1: Segment the buffer at conversation boundaries
        segments = await segmenter.segment(buffer_contents)
        logger.info(f"Post-processing: segmented into {len(segments)} conversation(s)")

        if not segments:
            logger.info("Post-processing: no segments produced — nothing to write")
            _cleanup_session(session_path)
            return

        # Step 2: Classify and write each segment
        for i, seg_entries in enumerate(segments, 1):
            try:
                classified = await classifier.classify(seg_entries)
                transcript_id, path = await transcript_store.ingest_segment(classified)
                logger.info(
                    f"Post-processing: segment {i}/{len(segments)} written — "
                    f"{classified.category} / {path.name} / {transcript_id}"
                )
                # Trigger collation for ideas (fire-and-forget)
                if classified.category == "ideas":
                    asyncio.create_task(
                        _run_collation(transcript_id, transcript_store),
                        name=f"collation_{transcript_id}",
                    )
            except Exception as exc:
                logger.error(f"Post-processing: failed to write segment {i}/{len(segments)}: {exc}")
                # Don't delete session file on partial failure — crash recovery will retry
                return

        # Step 3: Clean up working storage on full success
        _cleanup_session(session_path)

    except Exception as exc:
        logger.error(f"Post-processing: unexpected error: {exc}")
        # Leave session file on disk for crash recovery on next startup


def _cleanup_session(session_path: Optional[Path]) -> None:
    """Delete the .active/ session file after successful post-processing."""
    if session_path is None:
        return
    from shared.memory_writer import delete_session_file

    delete_session_file(session_path)


# ---------------------------------------------------------------------------
# Crash recovery: process orphaned .active/ session files on startup
# ---------------------------------------------------------------------------


async def run_crash_recovery(segmenter, classifier, transcript_store, data_dir: Path) -> None:
    """Check for orphaned .active/ session files and process them.

    Called once at startup, before the main pipeline begins. If the previous
    run crashed before post-processing completed, the session JSONL files
    are left in .active/. We process them now.
    """
    from shared.memory_writer import list_orphaned_sessions, read_session_file

    orphans = list_orphaned_sessions(data_dir)
    if not orphans:
        logger.debug("Crash recovery: no orphaned session files found")
        return

    logger.info(f"Crash recovery: found {len(orphans)} orphaned session file(s)")

    for session_path in orphans:
        logger.info(f"Crash recovery: processing {session_path.name}")
        try:
            entries = read_session_file(session_path)
            if entries is None:
                logger.error(
                    f"Crash recovery: could not read {session_path.name} — "
                    "leaving file for manual inspection"
                )
                continue
            if not entries:
                logger.warning(f"Crash recovery: {session_path.name} is empty, deleting")
                _cleanup_session(session_path)
                continue

            await run_post_processing(
                buffer_contents=entries,
                segmenter=segmenter,
                classifier=classifier,
                transcript_store=transcript_store,
                session_path=session_path,
            )
        except Exception as exc:
            logger.error(
                f"Crash recovery: failed to process {session_path.name}: {exc}. "
                "File left on disk for manual inspection."
            )


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


def _install_signal_handlers(
    shutdown_event: asyncio.Event,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Install SIGINT / SIGTERM handlers that trigger a clean shutdown."""

    def _handle_signal(sig):
        logger.info(f"Received signal {sig.name} — initiating graceful shutdown")
        loop.call_soon_threadsafe(shutdown_event.set)

    # Windows does not support add_signal_handler on the event loop
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal, sig)
    else:
        # Fallback: KeyboardInterrupt will propagate naturally on Windows
        logger.debug("Signal handlers: using default (Windows platform)")


# ---------------------------------------------------------------------------
# Pipeline builder
# ---------------------------------------------------------------------------


def _build_pipeline(transport, vad_processor, stt, transcript_buffer, silence_detector):
    """Assemble the Koda pipecat pipeline.

    Pipeline: Mic → VADProcessor (Silero) → Whisper STT → TranscriptBuffer → SilenceDetector

    VADProcessor emits VADUserStartedSpeakingFrame / VADUserStoppedSpeakingFrame.
    Whisper (SegmentedSTTService) uses those to segment audio for transcription.
    TranscriptBuffer and SilenceDetector observe the VAD frames downstream.
    """
    from pipecat.pipeline.pipeline import Pipeline

    return Pipeline(
        [
            transport.input(),  # Mic audio in (raw audio frames)
            vad_processor,  # Silero VAD → emits VAD start/stop speaking frames
            stt,  # Whisper MLX or Deepgram STT → TranscriptionFrames
            transcript_buffer,  # Accumulate TranscriptionFrames + mark silence gaps
            silence_detector,  # Watch VAD frames; fire callback on prolonged inactivity
        ]
    )


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------


async def run_koda(*, interactive: bool = False) -> None:
    """Build and run the full Koda listener pipeline.

    Args:
        interactive: If True, voice response mode is enabled (Phase 2 stub —
                     flag is accepted but full interactive mode is not yet wired).
    """
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    from bot.config.audio_devices import select_input_device
    from bot.processors.silence_detector import SilenceDetector
    from bot.processors.transcript_buffer import TranscriptBuffer
    from shared.llm_client import create_llm_client
    from shared.migrate import rebuild_index
    from shared.segmenter import Segmenter
    from shared.store import TranscriptStore

    # ----------------------------------------------------------------
    # Step 1: Load config
    # ----------------------------------------------------------------
    data_dir = Path(os.getenv("KODA_DATA_DIR", Path.home() / "koda-data"))

    # ----------------------------------------------------------------
    # Step 2: Select audio device (input only — silent listener)
    # ----------------------------------------------------------------
    input_dev = select_input_device(input_device_env=INPUT_DEVICE)

    # ----------------------------------------------------------------
    # Step 3: Create post-processing services
    # ----------------------------------------------------------------
    # Per-task provider routing: each service can use a different LLM provider
    # via LLM_PROVIDER_SEGMENT, LLM_PROVIDER_CLASSIFY env vars (falls back to LLM_PROVIDER)
    segmenter = Segmenter(create_llm_client(task="segment"))

    from shared.classifier import Classifier

    classifier = Classifier(create_llm_client(task="classify"))
    logger.info("Classifier: loaded")

    transcript_store = TranscriptStore(data_dir=data_dir)
    await transcript_store.init_db()
    await rebuild_index(data_dir=data_dir, db_path=transcript_store.db_path, full_rebuild=False)

    # ----------------------------------------------------------------
    # Step 4: Crash recovery — process any orphaned .active/ files
    # ----------------------------------------------------------------
    await run_crash_recovery(segmenter, classifier, transcript_store, data_dir)

    # ----------------------------------------------------------------
    # Step 5: Build pipecat pipeline components
    # ----------------------------------------------------------------

    # TranscriptBuffer: accumulates utterances + silence_gap hints → .active/session_*.jsonl
    transcript_buffer = TranscriptBuffer()

    # Track in-flight post-processing tasks so shutdown can await them
    _inflight_tasks: set[asyncio.Task] = set()

    # Silence timeout callback — triggered by SilenceDetector after N minutes of inactivity
    async def on_silence_timeout() -> None:
        logger.info("Silence timeout fired — flushing transcript buffer for post-processing")
        buffer_contents, session_path = await transcript_buffer.flush()
        t = asyncio.create_task(
            run_post_processing(
                buffer_contents=buffer_contents,
                segmenter=segmenter,
                classifier=classifier,
                transcript_store=transcript_store,
                session_path=session_path,
            ),
            name="post_processing",
        )
        _inflight_tasks.add(t)
        t.add_done_callback(_inflight_tasks.discard)

    silence_timeout_sec = float(os.getenv("SILENCE_TIMEOUT_SEC", "300"))
    silence_detector = SilenceDetector(
        on_silence_timeout=on_silence_timeout,
        silence_timeout=silence_timeout_sec,
    )

    # ----------------------------------------------------------------
    # Step 6: Build transport (input-only for silent mode)
    # ----------------------------------------------------------------
    stt = _create_stt_service()

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,  # Silent listener — no speaker output needed
            audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
            input_device_index=input_dev,
        )
    )

    vad_processor = VADProcessor(vad_analyzer=SileroVADAnalyzer(sample_rate=PIPELINE_SAMPLE_RATE))

    # ----------------------------------------------------------------
    # Step 7: Assemble pipeline
    # ----------------------------------------------------------------
    pipeline = _build_pipeline(transport, vad_processor, stt, transcript_buffer, silence_detector)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        idle_timeout_secs=None,
    )

    # Start the silence detector's background monitoring loop
    await silence_detector.start_monitoring()

    # ----------------------------------------------------------------
    # Step 8: Graceful shutdown wiring
    # ----------------------------------------------------------------
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    _install_signal_handlers(shutdown_event, loop)

    _shutdown_done = False

    async def _on_shutdown() -> None:
        """Flush in-memory buffer to disk and drain the memory writer queue.
        Guarded to run exactly once regardless of how many exit paths trigger it.
        """
        nonlocal _shutdown_done
        if _shutdown_done:
            return
        _shutdown_done = True

        logger.info("Shutdown: stopping silence detector monitoring")
        await silence_detector.stop_monitoring()

        # Wait for any in-flight post-processing tasks to complete
        if _inflight_tasks:
            logger.info(
                f"Shutdown: waiting for {len(_inflight_tasks)} in-flight post-processing task(s)"
            )
            await asyncio.gather(*_inflight_tasks, return_exceptions=True)

        # Flush the current buffer and process it before exiting.
        # The .active/ session file is only deleted after full success.
        logger.info("Shutdown: flushing and processing current transcript buffer")
        buffer_contents, session_path = await transcript_buffer.flush()
        if buffer_contents:
            utterance_count = sum(1 for e in buffer_contents if e.get("type") == "utterance")
            logger.info(
                f"Shutdown: processing {len(buffer_contents)} buffered entries "
                f"({utterance_count} utterances) before exit"
            )
            try:
                await run_post_processing(
                    buffer_contents=buffer_contents,
                    segmenter=segmenter,
                    classifier=classifier,
                    transcript_store=transcript_store,
                    session_path=session_path,
                )
            except Exception as exc:
                logger.error(
                    f"Shutdown: post-processing failed ({exc}). "
                    "Session file preserved in .active/ for crash recovery."
                )
        else:
            # No buffered content — still flush any unpersisted entries to disk
            await transcript_buffer.flush_to_disk()

        logger.info("Shutdown: closing transcript store")
        await transcript_store.close()

        logger.info("Shutdown: complete")

    # Monitor the shutdown event in the background and cancel the pipeline task
    async def _shutdown_watcher() -> None:
        await shutdown_event.wait()
        await _on_shutdown()
        await task.cancel()

    asyncio.create_task(_shutdown_watcher(), name="shutdown_watcher")

    # ----------------------------------------------------------------
    # Step 9: Log startup info
    # ----------------------------------------------------------------
    mode = "interactive (Phase 2 stub)" if interactive else "silent listener"
    logger.info(f"--- {BOT_NAME} starting ---")
    logger.info(f"  Mode:      {mode}")
    logger.info(f"  Data dir:  {data_dir}")
    logger.info(f"  STT:       {STT_SERVICE} / model={STT_MODEL or 'default'}")
    logger.info(f"  LLM:       {os.getenv('LLM_PROVIDER', 'gemini')}")
    logger.info(f"  Silence timeout:  {silence_timeout_sec}s")
    logger.info(f"  Input device: {input_dev if input_dev is not None else 'system default'}")
    if interactive:
        logger.warning(
            "Interactive mode is not yet fully implemented (Phase 2). "
            "Running in silent listener mode."
        )

    # ----------------------------------------------------------------
    # Step 10: Run the pipeline — cleanup runs on ALL exit paths
    # ----------------------------------------------------------------
    runner = PipelineRunner(handle_sigint=False)  # We handle SIGINT ourselves
    logger.info(f"{BOT_NAME} is listening. Press Ctrl+C to stop.")
    try:
        await runner.run(task)
    finally:
        await _on_shutdown()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Koda — always-on voice listener with memory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help=(
            "Enable voice response mode for brain dumps and memory queries. "
            "(Phase 2 — flag accepted, full interactive mode not yet implemented)"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(run_koda(interactive=args.interactive))
