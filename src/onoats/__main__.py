"""onoats — Always-on voice recorder (single-input / mic-only path).

Captures mic audio, transcribes with Whisper (MLX on Apple Silicon, CPU
otherwise) or Deepgram, and after a configurable silence timeout rotates the
session file into the pending/ queue. The recorder opens no database and runs
no post-processing — a downstream consumer drains the queue.

Run::

    onoats bot-single                    # silent mic-only recorder

Config (config.toml / secrets.env or environment):
    STT_SERVICE          - STT backend: "whisper" (default, local) or "deepgram"
    STT_MODEL            - Model override for chosen STT backend
    ONOATS_DATA_DIR      - Override the XDG data root
    INPUT_DEVICE         - Override mic device index (int); skips interactive picker
    SILENCE_TIMEOUT_SEC  - Seconds of mic silence before flushing buffer (default 300)
    SEGMENT_HINT_THRESHOLD - Seconds of silence to mark a segment hint (default 120)
    SHUTDOWN_DRAIN_TIMEOUT_SEC - Max seconds to drain the pipeline on Ctrl+C so a
                           final in-flight transcript lands before flush (default 8.0)
    SHUTDOWN_CANCEL_TIMEOUT_SEC - Hard-cancel grace (s) if the drain stalls;
                           caps pipecat's 20s default so exit isn't slow (default 2.0)

Optional STT secrets (secrets.env):
    DEEPGRAM_API_KEY     - Deepgram STT (only when STT_SERVICE=deepgram)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from loguru import logger

# ---------------------------------------------------------------------------
# Load dev-local .env (convenience; config.toml / secrets.env is canonical)
# ---------------------------------------------------------------------------

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO"))

# ---------------------------------------------------------------------------
# Runtime helpers live in onoats/runtime.py so dual.py does not have to reach
# into this module for leading-underscore symbols.
# ---------------------------------------------------------------------------

from onoats.runtime import (  # noqa: E402
    BOT_NAME,
    PIPELINE_SAMPLE_RATE,
    RecorderAlreadyRunningError,
    SHUTDOWN_CANCEL_TIMEOUT_SEC,
    SttPreflightError,
    _acquire_instance_lock,
    _create_stt_service,
    stop_pipeline_for_shutdown,
    wait_or_force,
    _install_signal_handlers,
    _remove_pid_file,
    _restore_terminal,
    _start_keypress_reader,
    _topic_pipeline_tasks,
    _write_pid_file,
    flush_and_rotate,
    run_crash_recovery,
    stt_banner,
)

_input_dev_env = os.getenv("INPUT_DEVICE", "").strip()
INPUT_DEVICE: Optional[int] = int(_input_dev_env) if _input_dev_env else None


# ---------------------------------------------------------------------------
# Pipeline builder
# ---------------------------------------------------------------------------


def _build_pipeline(transport, vad_processor, stt, transcript_buffer, silence_detector):
    """Assemble the onoats pipecat pipeline.

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


async def run_onoats(
    *, interactive: bool = False, locked_category: str | None = None
) -> None:
    """Build and run the full onoats recorder pipeline.

    Args:
        interactive: If True, voice response mode is enabled (Phase 2 stub —
                     flag is accepted but full interactive mode is not yet wired).
        locked_category: If set, force all classified segments to this category.
                         The classifier still extracts summary/tags/action_items
                         but the category is overridden.
    """
    # ----------------------------------------------------------------
    # Step 1: Resolve the data dir + claim the single-instance slot FIRST —
    # before importing native deps (pyaudio via LocalAudioTransport /
    # audio_devices) or ANY capture setup. A losing concurrent `onoats bot-single`
    # (or `python -m onoats`) raises RecorderAlreadyRunningError here having
    # touched nothing — the same before-capture gate the socket supervisor and
    # run_onoats_dual apply. Idempotent; held for the process lifetime (kernel
    # releases on exit).
    # ----------------------------------------------------------------
    from onoats._vendor.store import onoats_data_dir

    data_dir = onoats_data_dir()
    _acquire_instance_lock(data_dir / ".active")

    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.transports.local.audio import (
        LocalAudioTransport,
        LocalAudioTransportParams,
    )

    from onoats.config.audio_devices import select_input_device
    from onoats.processors.silence_detector import SilenceDetector
    from onoats.processors.transcript_buffer import TranscriptBuffer

    # ----------------------------------------------------------------
    # Step 2: Select audio device (input only — silent recorder)
    # ----------------------------------------------------------------
    # INPUT_DEVICE is read only from the environment (single-input mode has no
    # config.toml key), so the provenance is unambiguously "from env".
    input_dev = select_input_device(input_device_env=INPUT_DEVICE, source="from env")

    # ----------------------------------------------------------------
    # Step 3: Zero SQLite — the recorder opens no database. It emits files
    # only; a downstream consumer drains the pending/ queue.
    # ----------------------------------------------------------------

    # ----------------------------------------------------------------
    # Step 4: Crash recovery — rotate any orphaned .active/ files into
    # pending/ (runs in background so the pipeline starts immediately)
    # ----------------------------------------------------------------
    _crash_recovery_task = asyncio.create_task(
        run_crash_recovery(data_dir=data_dir, locked_category=locked_category),
        name="crash_recovery",
    )

    # ----------------------------------------------------------------
    # Step 5: Build pipecat pipeline components
    # ----------------------------------------------------------------

    # TranscriptBuffer: accumulates utterances + silence_gap hints → .active/session_*.jsonl
    transcript_buffer = TranscriptBuffer(locked_category=locked_category)

    _flush_lock = asyncio.Lock()

    async def _rotate_flush(reason: str, *, continue_session: bool) -> None:
        """Flush the transcript buffer and rotate the session file into pending/.

        The bot is now a thin recorder — it does NOT run post-processing
        inline. A cron-driven worker drains the pending/ queue.
        ``continue_session`` distinguishes a continuation flush
        (silence-timeout / Ctrl+T / SIGUSR1 — opens a fresh .active/ session)
        from a terminal flush (EndFrame / shutdown). Serialized via
        _flush_lock so concurrent flushes do not race.
        """
        async with _flush_lock:
            await flush_and_rotate(
                transcript_buffer,
                reason,
                continue_session=continue_session,
                data_dir=data_dir,
                locked_category=locked_category,
            )

    async def _flush_continuation(reason: str) -> None:
        """Continuation-flush entry point for Ctrl+T / SIGUSR1 / silence.

        Signal and keypress handlers call this with a single ``reason``
        string; it always rotates with ``continue_session=True`` so the
        ongoing recording gets a fresh .active/ session.
        """
        await _rotate_flush(reason, continue_session=True)

    # Silence timeout callback — triggered by SilenceDetector after N minutes of inactivity
    async def on_silence_timeout() -> None:
        await _rotate_flush("Silence timeout fired", continue_session=True)

    silence_timeout_sec = float(os.getenv("SILENCE_TIMEOUT_SEC", "300"))
    silence_detector = SilenceDetector(
        on_silence_timeout=on_silence_timeout,
        silence_timeout=silence_timeout_sec,
    )

    # ----------------------------------------------------------------
    # Step 6: Build transport (input-only for silent mode)
    # ----------------------------------------------------------------
    stt = await _create_stt_service()

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,  # Silent listener — no speaker output needed
            audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
            input_device_index=input_dev,
        )
    )

    vad_processor = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(sample_rate=PIPELINE_SAMPLE_RATE)
    )

    # ----------------------------------------------------------------
    # Step 7: Assemble pipeline
    # ----------------------------------------------------------------
    pipeline = _build_pipeline(
        transport, vad_processor, stt, transcript_buffer, silence_detector
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        idle_timeout_secs=None,
        cancel_timeout_secs=SHUTDOWN_CANCEL_TIMEOUT_SEC,
    )

    # Start the silence detector's background monitoring loop
    await silence_detector.start_monitoring()

    # ----------------------------------------------------------------
    # Step 8: Graceful shutdown wiring + signal handlers
    # (must be installed BEFORE the PID file is published, otherwise a
    # `onoats flush` during the startup window will send SIGUSR1 to a
    # process that still has the default disposition for that signal —
    # terminating the fresh bot instead of flushing.)
    # ----------------------------------------------------------------
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    force_exit_event = asyncio.Event()
    # SIGUSR1 is a continuation flush — wired to _flush_continuation.
    _install_signal_handlers(
        shutdown_event, force_exit_event, _flush_continuation, silence_detector, loop
    )

    # ----------------------------------------------------------------
    # Step 9: Write PID file for onoats flush discovery
    # (after signal handlers so SIGUSR1 is already wired by the time
    # the PID file is visible.)
    # ----------------------------------------------------------------
    pid_path = _write_pid_file(data_dir)

    _shutdown_started = False
    _shutdown_complete = asyncio.Event()

    async def _drain_tasks(tasks: set[asyncio.Task], label: str) -> None:
        """Wait for a set of tasks, interruptible by force exit."""
        if not tasks:
            return
        logger.info(f"Shutdown: waiting for {len(tasks)} {label}")
        await wait_or_force(
            asyncio.gather(*tasks, return_exceptions=True),
            label,
            force_exit_event=force_exit_event,
        )

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
        logger.info(
            "Shutdown: graceful shutdown started. Press Ctrl+C again to force exit."
        )
        await silence_detector.stop_monitoring()

        # Wait for crash recovery to finish if still running
        if _crash_recovery_task and not _crash_recovery_task.done():
            logger.info("Shutdown: waiting for crash recovery to finish")
            await wait_or_force(
                _crash_recovery_task,
                "crash recovery",
                force_exit_event=force_exit_event,
            )

        if force_exit_event.is_set():
            logger.warning("Shutdown: force exit — skipping flush")
        else:
            # Terminal flush: rotate the final buffer into pending/ for the
            # cron worker. The bot no longer runs post-processing inline.
            try:
                await _rotate_flush("Shutdown", continue_session=False)
            except Exception as exc:
                logger.error(
                    f"Shutdown: flush rotation failed ({exc}). "
                    "Session file preserved in .active/ for crash recovery."
                )
            # _topic_pipeline_tasks is no longer populated by the bot, but
            # drain it defensively in case a legacy task is still pending.
            await _drain_tasks(_topic_pipeline_tasks, "topic pipeline task(s)")

        logger.info("Shutdown: complete")

    # Monitor the shutdown event in the background and cancel the pipeline task
    async def _shutdown_watcher() -> None:
        await shutdown_event.wait()
        # Graceful drain first: an EndFrame lets a segment whose transcription
        # is in flight finish and reach TranscriptBuffer before teardown, so the
        # terminal flush in _on_shutdown captures the last spoken segment. Falls
        # back to a hard cancel if the drain stalls or a second Ctrl+C forces.
        logger.info("Shutdown: draining pipeline (Ctrl+C again to force)")
        await stop_pipeline_for_shutdown(task, force_exit_event)
        await _on_shutdown()

    asyncio.create_task(_shutdown_watcher(), name="shutdown_watcher")

    # ----------------------------------------------------------------
    # Step 9: Log startup info
    # ----------------------------------------------------------------
    mode = "interactive (stub)" if interactive else "silent recorder"
    logger.info(f"--- {BOT_NAME} starting ---")
    logger.info(f"  Mode:      {mode}")
    if locked_category:
        logger.info(f"  Category:  {locked_category} (locked via --category)")
    logger.info(f"  Data dir:  {data_dir}")
    logger.info(f"  STT:       {stt_banner()}")
    logger.info(f"  Silence timeout:  {silence_timeout_sec}s")
    logger.info(
        f"  Input device: {input_dev if input_dev is not None else 'system default'}"
    )
    if interactive:
        logger.warning(
            "Interactive mode is not implemented. Running in silent recorder mode."
        )

    # ----------------------------------------------------------------
    # Step 10: Start Ctrl+T keypress reader (cbreak mode)
    # ----------------------------------------------------------------
    # Ctrl+T (0x14) is a continuation flush — wired to _flush_continuation.
    _old_terminal_settings = _start_keypress_reader(
        _flush_continuation, silence_detector, loop
    )

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
        _remove_pid_file(pid_path, owner_pid=os.getpid())
        # The single-instance lock is held for the process lifetime; the kernel
        # releases it on exit (see runtime._release_instance_lock).


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="onoats bot-single",
        description="onoats — always-on voice recorder (single-input)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help="Accepted for CLI compatibility; the recorder still runs silently.",
    )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        metavar="NAME",
        help=(
            "Lock the session to this category. The category rides in the queue "
            "contract as a session_meta line so a consumer can honor it."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Console entrypoint for the single-input recorder (``onoats bot-single``)."""
    args = _parse_args(argv)
    if args.category:
        from onoats.categories import InvalidCategoryError, validate_category

        try:
            args.category = validate_category(args.category)
        except InvalidCategoryError as exc:
            print(f"Error: {exc}")
            return 1
    try:
        asyncio.run(
            run_onoats(interactive=args.interactive, locked_category=args.category)
        )
    except (SttPreflightError, RecorderAlreadyRunningError) as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
