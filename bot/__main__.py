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
# Shared runtime helpers live in bot/runtime.py so bot/dual.py does not have
# to reach into this module for leading-underscore symbols or for the shared
# _topic_pipeline_tasks set.

from bot.runtime import (  # noqa: E402
    BOT_NAME,
    PIPELINE_SAMPLE_RATE,
    STT_MODEL,
    STT_SERVICE,
    SttPreflightError,
    _create_stt_service,
    _install_signal_handlers,
    _remove_pid_file,
    _restore_terminal,
    _start_keypress_reader,
    _topic_pipeline_tasks,
    _write_pid_file,
    run_crash_recovery,
    run_post_processing,
)

_input_dev_env = os.getenv("INPUT_DEVICE", "").strip()
INPUT_DEVICE: Optional[int] = int(_input_dev_env) if _input_dev_env else None


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


async def run_koda(*, interactive: bool = False, locked_category: str | None = None) -> None:
    """Build and run the full Koda listener pipeline.

    Args:
        interactive: If True, voice response mode is enabled (Phase 2 stub —
                     flag is accepted but full interactive mode is not yet wired).
        locked_category: If set, force all classified segments to this category.
                         The classifier still extracts summary/tags/action_items
                         but the category is overridden.
    """
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    from bot.config.audio_devices import select_input_device
    from bot.processors.silence_detector import SilenceDetector
    from bot.processors.transcript_buffer import TranscriptBuffer
    from shared.dictionary import Dictionary
    from shared.llm_client import create_llm_client
    from shared.migrate import rebuild_index
    from shared.segmenter import Segmenter
    from shared.store import TranscriptStore

    # ----------------------------------------------------------------
    # Step 1: Load config
    # ----------------------------------------------------------------
    data_dir = Path(os.getenv("KODA_DATA_DIR", Path.home() / "koda-data")).expanduser()

    # ----------------------------------------------------------------
    # Step 2: Select audio device (input only — silent listener)
    # ----------------------------------------------------------------
    input_dev = select_input_device(input_device_env=INPUT_DEVICE)

    # ----------------------------------------------------------------
    # Step 3: Create post-processing services
    # ----------------------------------------------------------------
    # Per-task provider routing: each service can use a different LLM provider
    # via LLM_PROVIDER_SEGMENT, LLM_PROVIDER_CLASSIFY env vars (falls back to LLM_PROVIDER)
    dictionary = Dictionary(data_dir=data_dir, auto_create=True)
    segmenter = Segmenter(create_llm_client(task="segment"))

    from shared.classifier import Classifier
    from shared.transcript_cleaner import TranscriptCleaner

    classifier = Classifier(create_llm_client(task="classify"))
    logger.info("Classifier: loaded")

    # LLM-assisted transcript cleanup (optional — graceful skip if LLM unavailable)
    # Uses LLM_PROVIDER_CLEANUP env var for provider routing (default: same as LLM_PROVIDER)
    try:
        cleanup_llm = create_llm_client(task="cleanup")
        transcript_cleaner = TranscriptCleaner(cleanup_llm)
        logger.info("TranscriptCleaner: loaded")
    except Exception as exc:
        logger.warning(f"TranscriptCleaner: not available ({exc}), cleanup will be skipped")
        transcript_cleaner = None

    transcript_store = TranscriptStore(data_dir=data_dir)
    await transcript_store.init_db()
    await rebuild_index(data_dir=data_dir, db_path=transcript_store.db_path, full_rebuild=False)

    # ----------------------------------------------------------------
    # Step 4: Crash recovery — process any orphaned .active/ files
    # (runs in background so the pipeline starts immediately)
    # ----------------------------------------------------------------
    _crash_recovery_task = asyncio.create_task(
        run_crash_recovery(
            dictionary,
            segmenter,
            classifier,
            transcript_store,
            data_dir,
            transcript_cleaner=transcript_cleaner,
            locked_category=locked_category,
        ),
        name="crash_recovery",
    )

    # ----------------------------------------------------------------
    # Step 5: Build pipecat pipeline components
    # ----------------------------------------------------------------

    # TranscriptBuffer: accumulates utterances + silence_gap hints → .active/session_*.jsonl
    transcript_buffer = TranscriptBuffer()

    # Track in-flight post-processing tasks so shutdown can await them
    _inflight_tasks: set[asyncio.Task] = set()
    _flush_lock = asyncio.Lock()

    async def _flush_and_process(reason: str) -> None:
        """Flush the transcript buffer and kick off post-processing.

        Shared by silence timeout, SIGUSR1 manual flush, Ctrl+T, and shutdown.
        Serialized via _flush_lock to prevent concurrent flushes from racing.
        """
        async with _flush_lock:
            await _flush_impl(reason)

    async def _flush_impl(reason: str) -> None:
        logger.info(f"{reason} — flushing transcript buffer for post-processing")
        buffer_contents, session_path = await transcript_buffer.flush()
        if not buffer_contents:
            logger.info("Flush: buffer was empty, nothing to process")
            # Still persist any unpersisted in-memory entries to disk
            await transcript_buffer.flush_to_disk()
            return
        t = asyncio.create_task(
            run_post_processing(
                buffer_contents=buffer_contents,
                dictionary=dictionary,
                segmenter=segmenter,
                classifier=classifier,
                transcript_store=transcript_store,
                session_path=session_path,
                transcript_cleaner=transcript_cleaner,
                locked_category=locked_category,
            ),
            name="post_processing",
        )
        _inflight_tasks.add(t)
        t.add_done_callback(_inflight_tasks.discard)

    # Silence timeout callback — triggered by SilenceDetector after N minutes of inactivity
    async def on_silence_timeout() -> None:
        await _flush_and_process("Silence timeout fired")

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
    # Step 8: Graceful shutdown wiring + signal handlers
    # (must be installed BEFORE the PID file is published, otherwise a
    # `./koda flush` during the startup window will send SIGUSR1 to a
    # process that still has the default disposition for that signal —
    # terminating the fresh bot instead of flushing.)
    # ----------------------------------------------------------------
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    force_exit_event = asyncio.Event()
    _install_signal_handlers(
        shutdown_event, force_exit_event, _flush_and_process, silence_detector, loop
    )

    # ----------------------------------------------------------------
    # Step 9: Write PID file for ./koda flush discovery
    # (after signal handlers so SIGUSR1 is already wired by the time
    # the PID file is visible.)
    # ----------------------------------------------------------------
    pid_path = _write_pid_file(data_dir)

    _shutdown_started = False
    _shutdown_complete = asyncio.Event()

    async def _wait_or_force(coro_or_future, label: str) -> None:
        """Await a coroutine/future, but cancel it immediately if force_exit_event fires."""
        wait_task = asyncio.ensure_future(coro_or_future)
        force_task = asyncio.create_task(force_exit_event.wait(), name="force_exit_wait")
        done, _ = await asyncio.wait({wait_task, force_task}, return_when=asyncio.FIRST_COMPLETED)
        if force_task in done:
            logger.warning(f"Shutdown: force-cancelling {label}")
            wait_task.cancel()
            try:
                await wait_task
            except asyncio.CancelledError:
                pass
        else:
            force_task.cancel()

    async def _drain_tasks(tasks: set[asyncio.Task], label: str) -> None:
        """Wait for a set of tasks, interruptible by force exit."""
        if not tasks:
            return
        logger.info(f"Shutdown: waiting for {len(tasks)} {label}")
        await _wait_or_force(asyncio.gather(*tasks, return_exceptions=True), label)

    async def _on_shutdown() -> None:
        """Flush in-memory buffer to disk and drain the memory writer queue.
        Runs exactly once; concurrent callers await the first caller's
        completion so the event loop cannot tear down in-flight
        post-processing. A second Ctrl+C during shutdown sets
        force_exit_event, which cancels any pending waits immediately.
        """
        nonlocal _shutdown_started
        if _shutdown_started:
            await _shutdown_complete.wait()
            return
        _shutdown_started = True
        try:
            await _run_shutdown()
        finally:
            _shutdown_complete.set()

    async def _run_shutdown() -> None:
        logger.info("Shutdown: graceful shutdown started. Press Ctrl+C again to force exit.")
        await silence_detector.stop_monitoring()

        # Wait for crash recovery to finish if still running
        if _crash_recovery_task and not _crash_recovery_task.done():
            logger.info("Shutdown: waiting for crash recovery to finish")
            await _wait_or_force(_crash_recovery_task, "crash recovery")

        if force_exit_event.is_set():
            logger.warning("Shutdown: force exit — skipping task drain")
        else:
            await _drain_tasks(_inflight_tasks, "in-flight post-processing task(s)")
            await _drain_tasks(_topic_pipeline_tasks, "topic pipeline task(s)")

        if not force_exit_event.is_set():
            # Flush the current buffer and process it before exiting.
            try:
                await _flush_and_process("Shutdown")
            except Exception as exc:
                logger.error(
                    f"Shutdown: post-processing failed ({exc}). "
                    "Session file preserved in .active/ for crash recovery."
                )

            # Wait for the shutdown flush task and any topic pipeline tasks it spawned
            await _drain_tasks(_inflight_tasks, "post-processing task(s)")
            await _drain_tasks(_topic_pipeline_tasks, "topic pipeline task(s)")

        logger.info("Shutdown: closing transcript store")
        await transcript_store.close()

        logger.info("Shutdown: complete")

    # Monitor the shutdown event in the background and cancel the pipeline task
    async def _shutdown_watcher() -> None:
        await shutdown_event.wait()
        # Stop the STT pipeline first — no point capturing audio during shutdown.
        # Race against force_exit_event so a second Ctrl+C can interrupt a stuck
        # pipeline teardown (e.g. hung transport/STT cleanup).
        logger.info("Shutdown: stopping pipeline (STT/VAD)")
        await _wait_or_force(task.cancel(), "pipeline cancel")
        await _on_shutdown()

    asyncio.create_task(_shutdown_watcher(), name="shutdown_watcher")

    # ----------------------------------------------------------------
    # Step 9: Log startup info
    # ----------------------------------------------------------------
    mode = "interactive (Phase 2 stub)" if interactive else "silent listener"
    logger.info(f"--- {BOT_NAME} starting ---")
    logger.info(f"  Mode:      {mode}")
    if locked_category:
        logger.info(f"  Category:  {locked_category} (locked via --category)")
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
    # Step 10: Start Ctrl+T keypress reader (cbreak mode)
    # ----------------------------------------------------------------
    _old_terminal_settings = _start_keypress_reader(_flush_and_process, silence_detector, loop)

    # ----------------------------------------------------------------
    # Step 11: Run the pipeline — cleanup runs on ALL exit paths
    # ----------------------------------------------------------------
    runner = PipelineRunner(handle_sigint=False)  # We handle SIGINT ourselves
    logger.info(
        f"{BOT_NAME} is listening. "
        "Ctrl+T = flush transcript. Ctrl+C = quit. Ctrl+C twice = force quit."
    )
    try:
        await runner.run(task)
    finally:
        await _on_shutdown()
        _restore_terminal(_old_terminal_settings)
        _remove_pid_file(pid_path)


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
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        metavar="NAME",
        help=(
            "Lock all segments to this category (e.g., seminars, work, advisory). "
            "Useful when you know the context upfront — attending a talk, a specific "
            "meeting type, etc. The classifier still runs to extract summary/tags/actions "
            "but the category is forced."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.category:
        from shared.models import VALID_CATEGORIES

        cat = args.category.lower().strip()
        if cat not in VALID_CATEGORIES or cat == "uncategorized":
            print(
                f"Error: --category must be one of: "
                f"{', '.join(sorted(VALID_CATEGORIES - {'uncategorized'}))}"
            )
            sys.exit(1)
        args.category = cat
    try:
        asyncio.run(run_koda(interactive=args.interactive, locked_category=args.category))
    except SttPreflightError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        sys.exit(1)
