"""Experimental dual-input Koda listener.

Runs separate microphone and loopback capture branches, keeps them isolated
through VAD and STT, tags final transcription frames as `me` / `them`, and
merges only after STT into the shared post-processing path.

Run::

    ./koda bot-dual
    ./koda bot-dual --live-terminal

Config:
    MIC_INPUT_DEVICE     - Microphone input device index or stable name
    SYSTEM_INPUT_DEVICE  - Loopback input device index or stable name

Notes:
    - INPUT_DEVICE is ignored by this experimental entrypoint.
    - The legacy `./koda bot` path remains unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from loguru import logger

from bot.__main__ import (
    BOT_NAME,
    STT_MODEL,
    STT_SERVICE,
    _create_stt_service,
    _install_signal_handlers,
    _restore_terminal,
    _start_keypress_reader,
    _topic_pipeline_tasks,
    run_crash_recovery,
    run_post_processing,
)


def _build_dual_pipeline(mic_transport, system_transport, mic_vad, system_vad, mic_stt, system_stt):
    from pipecat.pipeline.parallel_pipeline import ParallelPipeline
    from pipecat.pipeline.pipeline import Pipeline

    from bot.processors.source_tagger import SourceTagger

    return Pipeline(
        [
            ParallelPipeline(
                [
                    mic_transport.input(),
                    mic_vad,
                    mic_stt,
                    SourceTagger(source="me", source_order=0),
                ],
                [
                    system_transport.input(),
                    system_vad,
                    system_stt,
                    SourceTagger(source="them", source_order=1),
                ],
            )
        ]
    )


async def run_koda_dual(*, live_terminal: bool = False, locked_category: str | None = None) -> None:
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    from bot.config.audio_devices import select_dual_input_devices
    from bot.processors.dual_silence_detector import DualSilenceDetector
    from bot.processors.live_terminal import LiveTerminalRenderer
    from bot.processors.transcript_buffer import TranscriptBuffer
    from shared.dictionary import Dictionary
    from shared.llm_client import create_llm_client
    from shared.migrate import rebuild_index
    from shared.segmenter import Segmenter
    from shared.store import TranscriptStore

    data_dir = Path(os.getenv("KODA_DATA_DIR", Path.home() / "koda-data")).expanduser()

    if os.getenv("INPUT_DEVICE", "").strip():
        logger.info("bot-dual ignores INPUT_DEVICE; use MIC_INPUT_DEVICE and SYSTEM_INPUT_DEVICE")

    mic_input = os.getenv("MIC_INPUT_DEVICE", "").strip() or None
    system_input = os.getenv("SYSTEM_INPUT_DEVICE", "").strip() or None
    mic_dev, system_dev = select_dual_input_devices(
        mic_input_env=mic_input,
        system_input_env=system_input,
    )

    dictionary = Dictionary(data_dir=data_dir, auto_create=True)
    segmenter = Segmenter(create_llm_client(task="segment"))

    from shared.classifier import Classifier
    from shared.transcript_cleaner import TranscriptCleaner

    classifier = Classifier(create_llm_client(task="classify"))
    logger.info("Classifier: loaded")

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

    crash_recovery_task = asyncio.create_task(
        run_crash_recovery(
            dictionary,
            segmenter,
            classifier,
            transcript_store,
            data_dir,
            transcript_cleaner=transcript_cleaner,
            locked_category=locked_category,
        ),
        name="dual_crash_recovery",
    )

    transcript_buffer = TranscriptBuffer(track_vad_gaps=False, use_frame_source=True)
    inflight_tasks: set[asyncio.Task] = set()
    flush_lock = asyncio.Lock()

    async def _flush_and_process(reason: str) -> None:
        async with flush_lock:
            logger.info(f"{reason} — flushing dual transcript buffer for post-processing")
            buffer_contents, session_path = await transcript_buffer.flush()
            if not buffer_contents:
                logger.info("Dual flush: buffer was empty, nothing to process")
                await transcript_buffer.flush_to_disk()
                return
            task = asyncio.create_task(
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
                name="dual_post_processing",
            )
            inflight_tasks.add(task)
            task.add_done_callback(inflight_tasks.discard)

    async def on_silence_timeout() -> None:
        await _flush_and_process("Dual silence timeout fired")

    silence_timeout_sec = float(os.getenv("SILENCE_TIMEOUT_SEC", "300"))
    silence_detector = DualSilenceDetector(
        on_silence_timeout=on_silence_timeout,
        silence_timeout=silence_timeout_sec,
    )

    pipeline_sample_rate = 16000
    mic_transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,
            audio_in_sample_rate=pipeline_sample_rate,
            input_device_index=mic_dev,
        )
    )
    system_transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,
            audio_in_sample_rate=pipeline_sample_rate,
            input_device_index=system_dev,
        )
    )

    mic_vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(sample_rate=pipeline_sample_rate))
    system_vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(sample_rate=pipeline_sample_rate))
    mic_stt = _create_stt_service()
    system_stt = _create_stt_service()

    pipeline = _build_dual_pipeline(
        mic_transport,
        system_transport,
        mic_vad,
        system_vad,
        mic_stt,
        system_stt,
    )

    processors = list(pipeline.processors)
    processors.append(transcript_buffer)
    if live_terminal:
        processors.append(LiveTerminalRenderer())
    processors.append(silence_detector)
    pipeline = Pipeline(processors)

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        idle_timeout_secs=None,
    )

    await silence_detector.start_monitoring()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    force_exit_event = asyncio.Event()
    _install_signal_handlers(
        shutdown_event,
        force_exit_event,
        _flush_and_process,
        silence_detector,
        loop,
    )

    shutdown_done = False

    async def _wait_or_force(coro_or_future, label: str) -> None:
        wait_task = asyncio.ensure_future(coro_or_future)
        force_task = asyncio.create_task(force_exit_event.wait(), name="force_exit_wait_dual")
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
        if not tasks:
            return
        logger.info(f"Shutdown: waiting for {len(tasks)} {label}")
        await _wait_or_force(asyncio.gather(*tasks, return_exceptions=True), label)

    async def _on_shutdown() -> None:
        nonlocal shutdown_done
        if shutdown_done:
            return
        shutdown_done = True

        logger.info("Shutdown: graceful dual-input shutdown started. Press Ctrl+C again to force.")
        await silence_detector.stop_monitoring()

        if crash_recovery_task and not crash_recovery_task.done():
            logger.info("Shutdown: waiting for crash recovery to finish")
            await _wait_or_force(crash_recovery_task, "crash recovery")

        if force_exit_event.is_set():
            logger.warning("Shutdown: force exit — skipping task drain")
        else:
            await _drain_tasks(inflight_tasks, "in-flight post-processing task(s)")
            await _drain_tasks(_topic_pipeline_tasks, "topic pipeline task(s)")

        if not force_exit_event.is_set():
            try:
                await _flush_and_process("Shutdown")
            except Exception as exc:
                logger.error(
                    f"Shutdown: post-processing failed ({exc}). "
                    "Session file preserved in .active/ for crash recovery."
                )
            await _drain_tasks(inflight_tasks, "post-processing task(s)")
            await _drain_tasks(_topic_pipeline_tasks, "topic pipeline task(s)")

        logger.info("Shutdown: closing transcript store")
        await transcript_store.close()
        logger.info("Shutdown: complete")

    async def _shutdown_watcher() -> None:
        await shutdown_event.wait()
        logger.info("Shutdown: stopping dual pipeline (STT/VAD)")
        await _wait_or_force(task.cancel(), "dual pipeline cancel")
        await _on_shutdown()

    asyncio.create_task(_shutdown_watcher(), name="shutdown_watcher_dual")

    logger.info(f"--- {BOT_NAME} dual-input experiment starting ---")
    if locked_category:
        logger.info(f"  Category:         {locked_category} (locked via --category)")
    logger.info(f"  Data dir:         {data_dir}")
    logger.info(f"  STT:              {STT_SERVICE} / model={STT_MODEL or 'default'}")
    logger.info(f"  LLM:              {os.getenv('LLM_PROVIDER', 'gemini')}")
    logger.info(f"  Silence timeout:  {silence_timeout_sec}s")
    logger.info(f"  Mic input:        {mic_dev}")
    logger.info(f"  System input:     {system_dev}")
    if live_terminal:
        logger.info("  Live terminal:    enabled")

    old_terminal_settings = _start_keypress_reader(_flush_and_process, silence_detector, loop)
    runner = PipelineRunner(handle_sigint=False)
    logger.info(
        f"{BOT_NAME} dual-input is listening. "
        "Ctrl+T = flush transcript. Ctrl+C = quit. Ctrl+C twice = force quit."
    )
    try:
        await runner.run(task)
    finally:
        await _on_shutdown()
        _restore_terminal(old_terminal_settings)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Koda experimental dual-input listener",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--category",
        type=str,
        default=None,
        metavar="NAME",
        help=(
            "Lock all segments to this category while preserving summary, tags, "
            "and action-item extraction."
        ),
    )
    parser.add_argument(
        "--live-terminal",
        action="store_true",
        default=False,
        help="Print finalized `Me:` / `Them:` lines to stdout for routing checks.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    if args.category:
        from shared.models import VALID_CATEGORIES

        cat = args.category.lower().strip()
        if cat not in VALID_CATEGORIES or cat == "uncategorized":
            print(
                "Error: --category must be one of: "
                f"{', '.join(sorted(VALID_CATEGORIES - {'uncategorized'}))}"
            )
            sys.exit(1)
        args.category = cat
    asyncio.run(run_koda_dual(live_terminal=args.live_terminal, locked_category=args.category))
