"""Dual-input onoats recorder.

Runs separate microphone and loopback capture branches, keeps them isolated
through VAD and STT, tags final transcription frames as `me` / `them`, and
writes a single chronological session file to the pending/ queue.

Run::

    onoats bot
    onoats bot --live-terminal

Config:
    MIC_INPUT_DEVICE     - Microphone input device index or stable name
    SYSTEM_INPUT_DEVICE  - Loopback input device index or stable name
    SHUTDOWN_DRAIN_TIMEOUT_SEC - Max seconds to drain the pipeline on Ctrl+C so a
                           final in-flight transcript lands before flush (default 8.0)
    SHUTDOWN_CANCEL_TIMEOUT_SEC - Hard-cancel grace (s) if the drain stalls;
                           caps pipecat's 20s default so exit isn't slow (default 2.0)

Notes:
    - INPUT_DEVICE is ignored by the dual-input recorder.
    - The legacy mic-only path remains available as `onoats bot-single`.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from dotenv import load_dotenv
from loguru import logger

# Load STT secrets before importing onoats.runtime — runtime reads STT_SERVICE,
# STT_MODEL, and BOT_NAME at module load. onoats's consolidated config
# (config.toml + secrets.env) is the source of truth; this dotenv load is a
# convenience for a project-local .env in dev. Env vars still override.
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)

from onoats.runtime import (  # noqa: E402
    BOT_NAME,
    PIPELINE_SAMPLE_RATE,
    SHUTDOWN_CANCEL_TIMEOUT_SEC,
    SttPreflightError,
    stop_pipeline_for_shutdown,
    wait_or_force,
    _remove_pid_file,
    _create_stt_service,
    log_stt_server_rss,
    _install_signal_handlers,
    _restore_terminal,
    _start_keypress_reader,
    _topic_pipeline_tasks,
    _write_pid_file,
    flush_and_rotate,
    run_crash_recovery,
    stt_banner,
)


async def _shutdown_stt_service(stt_service, label: str) -> None:
    """Best-effort drain for STT services used by the dual-input path.

    The dual pipeline instantiates two Whisper services in one process. When
    shutdown is abrupt, MLX can still have GPU work in flight. Explicitly
    stopping and cleaning up each service gives Pipecat a chance to tear down
    those resources before interpreter exit.
    """

    from pipecat.frames.frames import EndFrame

    stop = getattr(stt_service, "stop", None)
    cleanup = getattr(stt_service, "cleanup", None)

    if stop is not None:
        try:
            await stop(EndFrame())
        except Exception as exc:
            logger.warning(
                f"Shutdown: failed to stop {label} STT service cleanly: {exc}"
            )

    if cleanup is not None:
        try:
            await cleanup()
        except Exception as exc:
            logger.warning(f"Shutdown: failed to clean up {label} STT service: {exc}")


def _generate_call_id() -> str:
    """Return a per-session id for shadow + dump output joining.

    Timestamp at second granularity plus a 6-hex-char suffix so two
    pipelines built within the same wall-clock second do not collide
    (the audio_dump processor opens with append mode — collision would
    silently interleave bytes).
    """
    import secrets
    import time as _time

    return f"{_time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


def _build_dual_pipeline(
    mic_transport,
    system_transport,
    mic_vad,
    system_vad,
    mic_stt,
    system_stt,
    transcript_buffer,
    silence_detector,
    *,
    live_terminal: bool = False,
    call_id: str | None = None,
):
    from pipecat.pipeline.parallel_pipeline import ParallelPipeline
    from pipecat.pipeline.pipeline import Pipeline

    from onoats.processors.audio_dump import (
        RawAudioDumpProcessor,
        audio_dump_enabled,
        resolve_dump_dir,
    )
    from onoats.processors.live_terminal import LiveTerminalRenderer
    from onoats.processors.smart_turn_shadow import (
        SmartTurnShadowObserver,
        resolve_verdict_dir,
        smart_turn_shadow_enabled,
    )
    from onoats.processors.source_tagger import SourceTagger

    if call_id is None:
        call_id = _generate_call_id()

    mic_arm: list = [mic_transport.input(), mic_vad]
    system_arm: list = [system_transport.input(), system_vad]

    dump_on = audio_dump_enabled()
    shadow_on = smart_turn_shadow_enabled()
    if dump_on and not shadow_on:
        # The dump's stated purpose is offline replay against shadow
        # verdicts joined by call_id — flag the unjoined-output case so
        # an operator who set only one flag notices.
        logger.warning(
            "ONOATS_AUDIO_DUMP=1 but ONOATS_SMART_TURN_SHADOW is unset — "
            "PCM will not be joinable with shadow verdicts."
        )

    if dump_on:
        # PCM dump runs *before* the shadow observer in the arm so the
        # file captures exactly what the analyser sees. Lossless,
        # append-only — see bot/processors/audio_dump.py for format and
        # safety details (O_NOFOLLOW, mode 0o600, size cap).
        dump_dir = resolve_dump_dir()
        logger.info(f"Audio dump enabled (call_id={call_id}, dir={dump_dir})")
        mic_arm.append(
            RawAudioDumpProcessor(source="me", call_id=call_id, dump_dir=dump_dir)
        )
        system_arm.append(
            RawAudioDumpProcessor(source="them", call_id=call_id, dump_dir=dump_dir)
        )

    if shadow_on:
        # `me` validated on 2026-05-04 (303 verdicts in one real call, 54 %
        # reduction in Whisper decodes if commits were SmartTurn-gated).
        # Mirroring to `them` to characterise the model's behaviour on
        # loopback audio (codec-compressed remote speech, occasional
        # music/notifications) before considering the commit-gate flip.
        verdict_dir = resolve_verdict_dir()
        logger.info(
            f"SmartTurn shadow enabled on `me` and `them` (call_id={call_id}, "
            f"verdicts -> {verdict_dir})"
        )
        mic_arm.append(
            SmartTurnShadowObserver(
                source="me",
                sample_rate=PIPELINE_SAMPLE_RATE,
                call_id=call_id,
                verdict_dir=verdict_dir,
            )
        )
        system_arm.append(
            SmartTurnShadowObserver(
                source="them",
                sample_rate=PIPELINE_SAMPLE_RATE,
                call_id=call_id,
                verdict_dir=verdict_dir,
            )
        )

    mic_arm.extend([mic_stt, SourceTagger(source="me", source_order=0)])
    system_arm.extend([system_stt, SourceTagger(source="them", source_order=1)])

    processors = [
        ParallelPipeline(mic_arm, system_arm),
        transcript_buffer,
    ]
    if live_terminal:
        processors.append(LiveTerminalRenderer())
    processors.append(silence_detector)

    return Pipeline(processors)


def _build_portaudio_transports(cfg):
    """Build the two ``LocalAudioTransport`` branches (today's default path).

    This is the ONLY site that resolves PortAudio devices: it calls
    ``select_dual_input_devices`` (which enumerates PyAudio devices) and
    constructs ``LocalAudioTransport``. The ``socket`` branch never reaches
    here, so socket mode never imports or invokes the PyAudio path.

    Returns ``(mic_transport, system_transport, mic_dev, system_dev)`` where
    ``mic_dev`` / ``system_dev`` are the resolved device indices (logged in the
    startup banner).
    """
    from pipecat.transports.local.audio import (
        LocalAudioTransport,
        LocalAudioTransportParams,
    )

    from onoats.config.audio_devices import select_dual_input_devices

    if os.getenv("INPUT_DEVICE", "").strip():
        logger.info(
            "Dual-input bot ignores INPUT_DEVICE; use MIC_INPUT_DEVICE and SYSTEM_INPUT_DEVICE"
        )

    # Resolve devices through the config loader: env (MIC_INPUT_DEVICE /
    # SYSTEM_INPUT_DEVICE) wins, else config.toml [devices] mic/system written
    # by `onoats init`. Without this the recorder ignored the saved config and
    # re-prompted on every launch.
    mic_input = cfg.mic_device or None
    system_input = cfg.system_device or None
    mic_dev, system_dev = select_dual_input_devices(
        mic_input_env=mic_input,
        system_input_env=system_input,
        mic_source=cfg.mic_device_source,
        system_source=cfg.system_device_source,
    )

    mic_transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,
            audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
            input_device_index=mic_dev,
        )
    )
    system_transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,
            audio_in_sample_rate=PIPELINE_SAMPLE_RATE,
            input_device_index=system_dev,
        )
    )
    return mic_transport, system_transport, mic_dev, system_dev


def _build_socket_transports(cfg):
    """Build the two ``UnixSocketAudioTransport`` branches (``AUDIO_SOURCE=socket``).

    Reads the per-branch socket paths from the config loader (``ONOATS_MIC_SOCKET``
    / ``ONOATS_SYSTEM_SOCKET`` env, else ``[audio] mic_socket/system_socket``).
    Imports no PortAudio code and never enumerates PyAudio devices — that is the
    "no PortAudio on the socket path" invariant.

    Never-mix guard: refuses to start if the two sockets resolve to the same
    path. The comparison is on ``Path.expanduser().resolve()``, not raw strings,
    so a symlink or relative-path alias cannot collapse both branches onto one
    socket (which would silently destroy ``me``/``them`` speaker attribution).

    Returns ``(mic_transport, system_transport, mic_label, system_label)`` where
    the labels are the socket paths (logged in the startup banner).
    """
    from pathlib import Path

    from onoats.transports.socket_audio import UnixSocketAudioTransport

    mic_socket = cfg.mic_socket
    system_socket = cfg.system_socket
    if not mic_socket or not system_socket:
        raise ValueError(
            "AUDIO_SOURCE=socket requires both mic and system socket paths "
            "(ONOATS_MIC_SOCKET / ONOATS_SYSTEM_SOCKET, or [audio] "
            "mic_socket/system_socket in config.toml); "
            f"got mic_socket={mic_socket!r} system_socket={system_socket!r}"
        )

    # Expand ~ once: asyncio.open_unix_connection does NOT expand ~, so a config
    # path like ~/onoats/mic.sock must be expanded before it reaches the
    # transport or it silently fails to connect. (Supervisor paths are already
    # absolute, so this is a no-op there.)
    mic_path = Path(mic_socket).expanduser()
    system_path = Path(system_socket).expanduser()

    # Never-mix guard: compare RESOLVED paths so a symlink / relative-path alias
    # can't slip two branches onto one socket. Refuse before the pipeline runs.
    mic_resolved = mic_path.resolve()
    system_resolved = system_path.resolve()
    if mic_resolved == system_resolved:
        raise ValueError(
            "AUDIO_SOURCE=socket: mic and system sockets resolve to the SAME path "
            f"({mic_resolved}) — the two branches must never share a socket or "
            "`me`/`them` speaker attribution collapses. mic_socket="
            f"{mic_socket!r}, system_socket={system_socket!r}."
        )

    # Generation nonce (Phase-3 supervisor mints one per launch and exports
    # ONOATS_CAPTURER_NONCE). Threading it into the transports enforces the
    # stale/foreign-generation handshake check: a capturer presenting a missing
    # or wrong nonce on these supervisor-created paths is rejected. None when no
    # supervisor set it (socket mode driven manually) — then no nonce gating.
    expected_nonce = cfg.capturer_nonce

    # Pass the EXPANDED paths to the transports (so ~ actually connects); the
    # returned labels stay fully resolved for the startup banner.
    mic_transport = UnixSocketAudioTransport(
        str(mic_path), sample_rate=PIPELINE_SAMPLE_RATE, expected_nonce=expected_nonce
    )
    system_transport = UnixSocketAudioTransport(
        str(system_path),
        sample_rate=PIPELINE_SAMPLE_RATE,
        expected_nonce=expected_nonce,
    )
    return mic_transport, system_transport, str(mic_resolved), str(system_resolved)


async def run_onoats_dual(
    *, live_terminal: bool = False, locked_category: str | None = None
) -> int:
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask

    from onoats._vendor.store import onoats_data_dir
    from onoats.processors.dual_silence_detector import DualSilenceDetector
    from onoats.processors.transcript_buffer import TranscriptBuffer

    data_dir = onoats_data_dir()

    from onoats.config import load_config

    cfg = load_config()
    audio_source = cfg.audio_source

    # Build the two capture-branch transports. The branch is the AUDIO_SOURCE
    # swap seam: `portaudio` keeps today's LocalAudioTransport path verbatim;
    # `socket` reads framed PCM16 from per-branch unix sockets. The socket
    # branch MUST short-circuit PortAudio device resolution entirely — it must
    # neither import nor invoke the PyAudio device-enumeration path
    # (select_dual_input_devices), or socket mode would still touch PortAudio.
    # The last two tuple elements are the source-appropriate input identifier
    # for the startup banner: a PortAudio device index in portaudio mode, a
    # resolved socket path in socket mode. Name them source-neutrally here.
    if audio_source == "socket":
        mic_transport, system_transport, mic_input_label, system_input_label = (
            _build_socket_transports(cfg)
        )
    elif audio_source == "portaudio":
        mic_transport, system_transport, mic_input_label, system_input_label = (
            _build_portaudio_transports(cfg)
        )
    else:
        raise ValueError(
            f"Unknown AUDIO_SOURCE {audio_source!r}: expected 'portaudio' or 'socket'"
        )

    # Zero SQLite: the recorder opens no database. It emits files only —
    # a downstream consumer drains the pending/ queue.

    crash_recovery_task = asyncio.create_task(
        run_crash_recovery(data_dir=data_dir, locked_category=locked_category),
        name="dual_crash_recovery",
    )

    # Source-aware gap tracking: TranscriptBuffer only advances its
    # ``last_vad_stop`` when every branch is idle, so cross-branch
    # overlapping speech won't produce spurious silence_gap entries.
    # Enabling this restores Segmenter's ability to split short sessions
    # (segmenter fast-skips no-gap buffers with <=10 utterances).
    transcript_buffer = TranscriptBuffer(
        track_vad_gaps=True,
        use_frame_source=True,
        locked_category=locked_category,
    )
    flush_lock = asyncio.Lock()

    async def _rotate_flush(reason: str, *, continue_session: bool) -> None:
        """Flush the dual buffer and rotate the session file into pending/.

        The bot no longer runs post-processing inline — a cron worker drains
        the queue. ``continue_session`` distinguishes a continuation flush
        (silence-timeout / Ctrl+T / SIGUSR1 — opens a fresh .active/ session)
        from a terminal flush (EndFrame / shutdown).
        """
        async with flush_lock:
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

    async def on_silence_timeout() -> None:
        await _rotate_flush("Dual silence timeout fired", continue_session=True)

    silence_timeout_sec = float(os.getenv("SILENCE_TIMEOUT_SEC", "300"))
    silence_detector = DualSilenceDetector(
        on_silence_timeout=on_silence_timeout,
        silence_timeout=silence_timeout_sec,
    )

    mic_vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(sample_rate=PIPELINE_SAMPLE_RATE)
    )
    system_vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(sample_rate=PIPELINE_SAMPLE_RATE)
    )
    mic_stt = await _create_stt_service()
    system_stt = await _create_stt_service()
    # RSS baseline for the stt_server at bot start. Pair with the
    # ``shutdown`` entry logged from `_run_shutdown` to get a free
    # soak datapoint out of every real-world session — no dedicated
    # harness needed.
    await log_stt_server_rss("startup")

    call_id = _generate_call_id()
    logger.info(f"Dual pipeline session call_id={call_id}")

    pipeline = _build_dual_pipeline(
        mic_transport,
        system_transport,
        mic_vad,
        system_vad,
        mic_stt,
        system_stt,
        transcript_buffer,
        silence_detector,
        live_terminal=live_terminal,
        call_id=call_id,
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        idle_timeout_secs=None,
        cancel_timeout_secs=SHUTDOWN_CANCEL_TIMEOUT_SEC,
    )

    await silence_detector.start_monitoring()

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()
    force_exit_event = asyncio.Event()
    # SIGUSR1 is a continuation flush — wired to _flush_continuation.
    _install_signal_handlers(
        shutdown_event,
        force_exit_event,
        _flush_continuation,
        silence_detector,
        loop,
    )
    pid_path = _write_pid_file(data_dir)

    shutdown_started = False
    shutdown_complete = asyncio.Event()
    old_terminal_settings: object | None = None

    async def _drain_tasks(tasks: set[asyncio.Task], label: str) -> None:
        if not tasks:
            return
        logger.info(f"Shutdown: waiting for {len(tasks)} {label}")
        await wait_or_force(
            asyncio.gather(*tasks, return_exceptions=True),
            label,
            force_exit_event=force_exit_event,
        )

    async def _on_shutdown() -> None:
        nonlocal shutdown_started
        if shutdown_started:
            # Second caller: wait for the first to finish so we don't
            # return early and let the event loop tear down in-flight
            # post-processing tasks.
            await shutdown_complete.wait()
            return
        shutdown_started = True
        try:
            await _run_shutdown()
        finally:
            shutdown_complete.set()

    async def _run_shutdown() -> None:
        logger.info(
            "Shutdown: graceful dual-input shutdown started. Press Ctrl+C again to force."
        )
        await silence_detector.stop_monitoring()

        if crash_recovery_task and not crash_recovery_task.done():
            logger.info("Shutdown: waiting for crash recovery to finish")
            await wait_or_force(
                crash_recovery_task,
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

        logger.info("Shutdown: draining dual STT services")
        await _shutdown_stt_service(mic_stt, "mic")
        await _shutdown_stt_service(system_stt, "system")
        await log_stt_server_rss("shutdown")

        # Restore terminal and remove PID file inside the single-writer
        # shutdown path so the two call sites (_shutdown_watcher and the
        # outer ``finally`` block) are truly idempotent regardless of ordering.
        _restore_terminal(old_terminal_settings)
        _remove_pid_file(pid_path)
        logger.info("Shutdown: complete")

    async def _shutdown_watcher() -> None:
        await shutdown_event.wait()
        # Graceful drain first (EndFrame) so an in-flight transcription reaches
        # TranscriptBuffer before the flush; hard-cancel fallback on stall/force.
        logger.info("Shutdown: draining dual pipeline (Ctrl+C again to force)")
        await stop_pipeline_for_shutdown(task, force_exit_event)
        await _on_shutdown()

    asyncio.create_task(_shutdown_watcher(), name="shutdown_watcher_dual")

    logger.info(f"--- {BOT_NAME} dual-input starting ---")
    if locked_category:
        logger.info(f"  Category:         {locked_category} (locked via --category)")
    logger.info(f"  Data dir:         {data_dir}")
    logger.info(f"  STT:              {stt_banner()}")
    logger.info(f"  Silence timeout:  {silence_timeout_sec}s")
    logger.info(f"  Mic input:        {mic_input_label}")
    logger.info(f"  System input:     {system_input_label}")
    if live_terminal:
        logger.info("  Live terminal:    enabled")

    # Ctrl+T (0x14) is a continuation flush — wired to _flush_continuation.
    old_terminal_settings = _start_keypress_reader(
        _flush_continuation, silence_detector, loop
    )
    runner = PipelineRunner(handle_sigint=False)
    logger.info(
        f"{BOT_NAME} dual-input is listening. "
        "Ctrl+T = flush transcript. Ctrl+C = quit. Ctrl+C twice = force quit."
    )
    ended_by_error = False
    try:
        await runner.run(task)
        # runner.run returned. If nobody requested shutdown (no Ctrl+C / SIGINT
        # set shutdown_event), the pipeline ended on its own. This is keyed on the
        # load-bearing invariant that **the only self-end path for this recorder
        # is a fatal ErrorFrame** cancelling the task (a capture branch hit EOF /
        # read-idle / a framing or pump failure and surfaced one): there is no
        # EndFrame source other than shutdown, so "ended without a shutdown
        # request" is exactly "ended on a fatal error". Report it as a failure so
        # a supervisor can honour the fail-loud contract, even when the capturer
        # process is still alive.
        #
        # (pipecat 1.3.0 exposes no on_pipeline_error event or PipelineTask
        # observer hook to read the ErrorFrame directly; if a future version adds
        # one, switch to observing the fatal frame and drop this inference.)
        if not shutdown_event.is_set():
            ended_by_error = True
            logger.error(
                "Dual pipeline ended without a shutdown request — a capture "
                "branch surfaced a fatal ErrorFrame (e.g. capturer EOF / "
                "read-idle). Reporting non-zero exit."
            )
    finally:
        await _on_shutdown()
    return 1 if ended_by_error else 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="onoats bot",
        description="onoats dual-input recorder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        default=False,
        help=(
            "Accepted for CLI compatibility with the legacy single-input bot. "
            "The dual-input listener still runs in silent mode."
        ),
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
    return parser.parse_args(argv)


def _apply_recorder_args(args: argparse.Namespace) -> int | None:
    """Apply the shared post-parse handling for the dual recorder.

    Emits the interactive-mode warning and validates/normalizes ``--category``
    in place. Returns an error rc to abort (non-``None``) or ``None`` to proceed.
    Shared by :func:`main` and the socket supervisor
    (``cli._run_recorder_with_capturer``) so the two launch paths can never drift
    on argument handling.
    """
    if args.interactive:
        logger.warning(
            "Interactive mode is not implemented for the dual-input recorder. "
            "Running in silent mode."
        )
    if args.category:
        from onoats.categories import InvalidCategoryError, validate_category

        try:
            args.category = validate_category(args.category)
        except InvalidCategoryError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return None


def main(argv: list[str] | None = None) -> int:
    """Console entrypoint for the dual-input recorder (``onoats bot``)."""
    args = _parse_args(argv)
    rc = _apply_recorder_args(args)
    if rc is not None:
        return rc
    try:
        rc = asyncio.run(
            run_onoats_dual(
                live_terminal=args.live_terminal, locked_category=args.category
            )
        )
    except SttPreflightError as exc:
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
