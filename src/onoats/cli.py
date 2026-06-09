"""``onoats`` console entrypoint — subcommand dispatch.

Subcommands::

    onoats init          # guided first-run setup (config.toml + secrets.env)
    onoats bot           # dual-input recorder (mic + system loopback)
    onoats bot-single    # legacy single-input (mic-only) recorder
    onoats flush         # signal the running recorder to rotate its buffer
    onoats convert       # render pending/*.jsonl -> markdown transcripts
    onoats devices       # list audio input/output devices
    onoats status        # report recorder pid / running state + data dir

Heavy modules (the recorder runtime, pyaudio, the STT stack) are imported
**lazily inside each handler** — never at module top — so ``onoats --help`` and
each subcommand's ``--help`` resolve via argparse without booting any service
or importing pyaudio / pipecat / MLX. This keeps the import-guard clean: a bare
``import onoats.cli`` pulls in nothing heavy.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

PID_FILENAME = "onoats.pid"


# ---------------------------------------------------------------------------
# pid / data-dir resolution (lightweight — no pyaudio / runtime import)
# ---------------------------------------------------------------------------


def _apply_config_data_dir() -> None:
    """Export ``ONOATS_DATA_DIR`` from config.toml ``[storage].data_dir`` so every
    downstream resolver (store, queue, recorder, flush, status) sees the
    configured location via the existing env path — keeping ``_vendor/store.py``
    free of any config import.

    Precedence is preserved: if ``ONOATS_DATA_DIR`` is already in the env
    (koda-driven / CI injection), it wins and this is a no-op.
    """
    if os.environ.get("ONOATS_DATA_DIR", "").strip():
        return
    from onoats.config import load_config

    configured = load_config().data_dir
    if configured:
        os.environ["ONOATS_DATA_DIR"] = os.path.expanduser(str(configured))


def _resolve_data_dir() -> Path:
    """Resolve the recorder data dir without importing the heavy runtime."""
    from onoats._vendor.store import onoats_data_dir

    return onoats_data_dir()


def _pid_path(data_dir: Path | None = None) -> Path:
    """Path to the recorder pid file (``<data_dir>/.active/onoats.pid``)."""
    base = data_dir if data_dir is not None else _resolve_data_dir()
    return base / ".active" / PID_FILENAME


def _read_pid(data_dir: Path | None = None) -> int | None:
    """Read + validate the recorder pid (marker-checked). None if absent."""
    from onoats._vendor.pid import read_pid_file

    return read_pid_file(_pid_path(data_dir))


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# Subcommand handlers (heavy imports stay inside each handler)
# ---------------------------------------------------------------------------


def _cmd_init(rest: list[str]) -> int:
    from onoats.init import main as init_main

    return init_main(rest)


def _cmd_bot(rest: list[str]) -> int:
    """Run the dual-input recorder.

    Two capture backends, selected by ``AUDIO_SOURCE`` (env / config.toml
    ``[audio].source``):

    * ``portaudio`` (default) — today's path, byte-for-byte unchanged: defer
      straight to ``onoats.dual.main`` which builds ``LocalAudioTransport`` s.
    * ``socket`` — the capturer/recorder supervisor (Phase 3 of the socket-audio
      plan). It owns the native capturer's process lifecycle: it mints a private
      socket directory + a fresh generation nonce, spawns the capturer
      (``ONOATS_CAPTURER_BIN``), waits for both branch sockets to appear, then
      runs the recorder against them. See :func:`_run_socket_supervisor`.

    The branch is read here (and only here) so the PortAudio path never imports
    or touches the supervisor / socket machinery.
    """
    from onoats.config import load_config

    if load_config().audio_source == "socket":
        return _run_socket_supervisor(rest)

    from onoats.dual import main as dual_main

    return dual_main(rest)


# ---------------------------------------------------------------------------
# AUDIO_SOURCE=socket supervisor: own the capturer↔recorder lifecycle.
#
# Phase 3 of docs/dev_plans/20260607-feature-menubar-coreaudio-socket-transport.md.
# The wire contract this supervisor + the capturer speak is pinned in
# docs/audio-socket-contract.md.
#
# Lifecycle (single owner — the supervisor owns the capturer process; the
# transport never self-reconnects, it surfaces an ErrorFrame and the session
# rotates):
#   1. mint a PRIVATE 0700 socket dir (NOT a shared /tmp path) + a fresh
#      generation nonce. The fresh per-generation dir is the primary stale-socket
#      defense: a leftover socket from a prior generation lives at a path the new
#      recorder never references, so it is structurally unreachable.
#   2. spawn the capturer (ONOATS_CAPTURER_BIN) pointed at the two sockets, with
#      the nonce + socket paths passed via env AND argv (documented in the
#      contract doc).
#   3. point ONOATS_MIC_SOCKET / ONOATS_SYSTEM_SOCKET at the private-dir sockets
#      so the recorder's dual._build_socket_transports(cfg) connects to them.
#   4. wait (bounded) for both sockets to appear, then run the recorder.
#   5. teardown: on recorder exit stop the capturer; on capturer death tear down
#      cleanly — the recorder's own ErrorFrame path flushes+rotates, and the
#      supervisor exits NON-ZERO.
#
# "Fail loud" is a testable observable: every failure path (capturer crash,
# permission denied, slow/silent reader) yields an ErrorFrame on the affected
# branch (the transport already does this), a non-zero supervisor exit code, AND
# a WARNING/ERROR log line — and the partial session still rotates (no hang).
# ---------------------------------------------------------------------------

# How long to wait for BOTH branch sockets to be created by the capturer before
# declaring the launch failed.
_SOCKET_WAIT_TIMEOUT_SEC = 10.0
_SOCKET_WAIT_POLL_SEC = 0.05

# Grace for the recorder to drain its own ErrorFrame-driven shutdown (flush +
# rotate) after the capturer dies, before the supervisor force-cancels it.
_RECORDER_DRAIN_GRACE_SEC = 30.0

# Grace for the capturer to exit on SIGTERM before SIGKILL during teardown.
_CAPTURER_TERM_GRACE_SEC = 5.0

MIC_SOCKET_NAME = "mic.sock"
SYSTEM_SOCKET_NAME = "system.sock"


def _run_socket_supervisor(rest: list[str]) -> int:
    """Synchronous entry: drive the async socket session, mirror dual.main's rc.

    Returns 0 on a clean recorder shutdown, non-zero on any fail-loud path
    (missing/failed capturer, sockets that never appeared, capturer death
    mid-session, or an STT preflight failure).
    """
    import asyncio as _asyncio

    from onoats.runtime import SttPreflightError

    try:
        return _asyncio.run(_supervise_socket_session(rest))
    except SttPreflightError as exc:
        # Mirror dual.main: actionable hint, not a traceback.
        print(f"\n{exc}\n", file=sys.stderr)
        return 1


async def _supervise_socket_session(rest: list[str]) -> int:
    """Mint sockets + nonce, spawn the capturer, run the recorder, tear down.

    Runs the recorder and a capturer-exit watcher concurrently in one event
    loop. Whichever finishes first decides teardown:

    * recorder finishes first  → normal shutdown / EndFrame: stop the capturer,
      return the recorder's rc (0 on clean exit).
    * capturer finishes first  → the capturer died: the recorder's socket reader
      already saw EOF / read-idle, surfaced an ``ErrorFrame``, and is rotating
      the partial session. Give it a bounded grace to drain, force-cancel if it
      overruns, log loudly, and return non-zero.
    """
    import asyncio
    import secrets
    import shutil
    import tempfile

    from loguru import logger

    capturer_bin = os.environ.get("ONOATS_CAPTURER_BIN", "").strip()
    if not capturer_bin:
        logger.error(
            "AUDIO_SOURCE=socket requires ONOATS_CAPTURER_BIN (path to the native "
            "capturer that writes framed PCM16 to the two branch sockets). It is "
            "unset; refusing to start. See docs/audio-socket-contract.md."
        )
        return 1

    # 1. Private, supervisor-owned socket dir (0700). mkdtemp already creates the
    # dir 0700 and owner-only; a fresh per-generation dir means any stale socket
    # from a prior generation lives at a path the new recorder never references
    # (structurally unreachable — the primary stale-socket defense). The system
    # temp dir keeps the AF_UNIX path well under the macOS ~104-byte sun_path
    # limit (a data-dir-nested path can blow it).
    sock_dir = tempfile.mkdtemp(prefix="onoats-sock-")
    os.chmod(sock_dir, 0o700)
    mic_sock = os.path.join(sock_dir, MIC_SOCKET_NAME)
    system_sock = os.path.join(sock_dir, SYSTEM_SOCKET_NAME)

    # 2. Fresh generation nonce. A restarted capturer presents a new nonce in its
    # handshake; combined with the fresh dir this invalidates stale fds.
    nonce = secrets.token_hex(16)

    # 3. Point the recorder at the private-dir sockets. The recorder resolves
    # these through OnoatsConfig (ONOATS_MIC_SOCKET / ONOATS_SYSTEM_SOCKET env >
    # [audio] toml), so exporting them here is all dual._build_socket_transports
    # needs. Capture any prior values so the `finally` can restore them — these
    # are process-global, and leaving them set would leak our private-dir paths
    # into any in-process caller (e.g. tests) that runs after us.
    _prior_socket_env = {
        k: os.environ.get(k) for k in ("ONOATS_MIC_SOCKET", "ONOATS_SYSTEM_SOCKET")
    }
    os.environ["ONOATS_MIC_SOCKET"] = mic_sock
    os.environ["ONOATS_SYSTEM_SOCKET"] = system_sock

    capturer_proc: asyncio.subprocess.Process | None = None
    rc = 0
    try:
        # 3b. Spawn the capturer pointed at both sockets. Pass the socket paths +
        # nonce via BOTH env and argv (documented in the contract doc) so a
        # capturer can read whichever it prefers.
        capturer_env = dict(os.environ)
        capturer_env["ONOATS_MIC_SOCKET"] = mic_sock
        capturer_env["ONOATS_SYSTEM_SOCKET"] = system_sock
        capturer_env["ONOATS_CAPTURER_NONCE"] = nonce
        logger.info(
            f"Socket supervisor: spawning capturer {capturer_bin!r} "
            f"(mic={mic_sock}, system={system_sock}, nonce={nonce[:8]}…)"
        )
        try:
            capturer_proc = await asyncio.create_subprocess_exec(
                capturer_bin,
                "--mic-socket",
                mic_sock,
                "--system-socket",
                system_sock,
                "--nonce",
                nonce,
                env=capturer_env,
            )
        except OSError as exc:
            # Missing binary / permission denied launching it — fail loud.
            logger.error(
                f"Socket supervisor: could not spawn capturer {capturer_bin!r}: {exc}. "
                "AUDIO_SOURCE=socket cannot capture without it. "
                "See docs/audio-socket-contract.md."
            )
            return 1

        # 4. Wait (bounded) for BOTH sockets to appear. If the capturer dies or
        # is too slow, fail loud rather than hang the recorder on a connect that
        # never succeeds.
        ready = await _wait_for_sockets(capturer_proc, (mic_sock, system_sock), logger)
        if not ready:
            # _wait_for_sockets already logged the cause (capturer death / timeout).
            await _stop_capturer(capturer_proc, logger)
            return 1

        # 5. Run the recorder against the sockets, watching the capturer
        # concurrently so its death tears the session down.
        rc = await _run_recorder_with_capturer(rest, capturer_proc, logger)
    finally:
        if capturer_proc is not None:
            await _stop_capturer(capturer_proc, logger)
        # Remove the private socket dir. Best-effort: a leftover here is harmless
        # (next generation mints a new one), but tidy up so private dirs don't
        # accumulate under the system temp root across restarts.
        try:
            shutil.rmtree(sock_dir, ignore_errors=True)
        except OSError:
            pass
        # Restore the socket env vars to their prior state so we don't leak our
        # private-dir paths into a subsequent in-process caller.
        for key, prior in _prior_socket_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior

    return rc


async def _wait_for_sockets(capturer_proc, socket_paths, logger) -> bool:
    """Wait (bounded) for every path in ``socket_paths`` to exist.

    Returns ``True`` once all sockets exist. Returns ``False`` — having logged
    the cause loudly — if the capturer exits before the sockets appear or the
    bounded timeout elapses. A short poll loop is used rather than inotify so the
    behaviour is identical on macOS / Linux and trivially testable.
    """
    import asyncio
    import time

    deadline = time.monotonic() + _SOCKET_WAIT_TIMEOUT_SEC
    while True:
        if all(os.path.exists(p) for p in socket_paths):
            logger.info("Socket supervisor: both branch sockets are present")
            return True

        # Capturer died before binding both sockets → permission denied / crash
        # at startup. Fail loud.
        if capturer_proc.returncode is not None:
            logger.error(
                "Socket supervisor: capturer exited (rc="
                f"{capturer_proc.returncode}) before both sockets appeared — it "
                "likely failed to start capture (permission denied / no device). "
                "AUDIO_SOURCE=socket cannot record. See docs/audio-socket-contract.md."
            )
            return False

        if time.monotonic() >= deadline:
            missing = [p for p in socket_paths if not os.path.exists(p)]
            logger.error(
                "Socket supervisor: capturer did not create "
                f"{missing} within {_SOCKET_WAIT_TIMEOUT_SEC}s — refusing to start "
                "the recorder rather than hang on a connect that never succeeds."
            )
            return False

        await asyncio.sleep(_SOCKET_WAIT_POLL_SEC)


async def _run_recorder_with_capturer(rest, capturer_proc, logger) -> int:
    """Run the recorder + a capturer-death watcher; return the supervisor rc.

    The recorder (``run_onoats_dual``) installs its own signal handlers and runs
    the pipeline; on capturer death the socket transport surfaces an
    ``ErrorFrame`` which terminates the pipeline (fatal-error → pipeline cancel),
    so the recorder coroutine returns on its own and flushes+rotates via its
    ``finally`` shutdown path. The watcher is the backstop that turns that into a
    non-zero supervisor exit and force-cancels the recorder if it overruns the
    drain grace.
    """
    import asyncio

    from onoats.dual import _parse_args, run_onoats_dual

    # Parse the same args dual.main would, so `onoats bot --live-terminal
    # --category X` behaves identically in socket mode.
    args = _parse_args(rest)
    if args.interactive:
        # Mirror dual.main: interactive mode is unimplemented for the dual-input
        # recorder. Warn rather than silently ignore so socket mode has parity.
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

    recorder_task = asyncio.create_task(
        run_onoats_dual(
            live_terminal=args.live_terminal, locked_category=args.category
        ),
        name="socket_supervisor_recorder",
    )
    capturer_task = asyncio.create_task(
        capturer_proc.wait(), name="socket_supervisor_capturer_wait"
    )

    done, _pending = await asyncio.wait(
        {recorder_task, capturer_task}, return_when=asyncio.FIRST_COMPLETED
    )

    if recorder_task in done:
        # The recorder coroutine finished. Re-await to surface any exception
        # (the caller maps SttPreflightError to rc=1), then honour its return
        # code: run_onoats_dual returns non-zero when the pipeline ended via a
        # fatal ErrorFrame (e.g. a silent/dead capturer tripped the read-idle
        # watchdog) rather than a clean shutdown. That must propagate as a
        # non-zero supervisor exit per the fail-loud contract — the capturer
        # process is still alive (capturer_task pending), so we cannot infer
        # success from "recorder finished first".
        capturer_task.cancel()
        try:
            await capturer_task
        except (asyncio.CancelledError, ProcessLookupError):
            pass
        rc = recorder_task.result()  # re-raise if the recorder failed
        rc = rc if rc is not None else 0
        if rc != 0:
            logger.error(
                f"Socket supervisor: recorder exited non-zero (rc={rc}) — a "
                "capture branch surfaced a fatal ErrorFrame (silent/failed "
                "capturer); stopping capturer and exiting non-zero."
            )
        else:
            logger.info("Socket supervisor: recorder exited; stopping capturer")
        return rc

    # Capturer finished first → it died mid-session. The recorder's own
    # ErrorFrame path is draining (flush + rotate). Give it a bounded grace, then
    # force-cancel so the supervisor never hangs.
    rc = capturer_task.result()
    logger.error(
        f"Socket supervisor: capturer exited mid-session (rc={rc}); the recorder "
        "branch surfaced an ErrorFrame and is rotating the partial session. "
        "Supervisor will exit non-zero."
    )
    try:
        await asyncio.wait_for(recorder_task, timeout=_RECORDER_DRAIN_GRACE_SEC)
    except asyncio.TimeoutError:
        logger.warning(
            "Socket supervisor: recorder did not finish draining within "
            f"{_RECORDER_DRAIN_GRACE_SEC}s after capturer death — force-cancelling."
        )
        recorder_task.cancel()
        try:
            await recorder_task
        except asyncio.CancelledError:
            pass
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # The recorder may surface its own teardown error; we already log + exit
        # non-zero for the capturer death, so just record it.
        logger.warning(f"Socket supervisor: recorder drain raised: {exc}")
    return 1


async def _stop_capturer(capturer_proc, logger) -> None:
    """Stop the capturer: SIGTERM, bounded wait, then SIGKILL. Idempotent."""
    import asyncio

    if capturer_proc.returncode is not None:
        return
    try:
        capturer_proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(capturer_proc.wait(), timeout=_CAPTURER_TERM_GRACE_SEC)
    except asyncio.TimeoutError:
        logger.warning(
            "Socket supervisor: capturer did not exit on SIGTERM within "
            f"{_CAPTURER_TERM_GRACE_SEC}s — sending SIGKILL."
        )
        try:
            capturer_proc.kill()
        except ProcessLookupError:
            return
        try:
            await capturer_proc.wait()
        except ProcessLookupError:
            pass


def _cmd_bot_single(rest: list[str]) -> int:
    from onoats.__main__ import main as single_main

    return single_main(rest)


def _cmd_convert(rest: list[str]) -> int:
    """Render pending sessions. ``onoats convert --once`` supersedes the P3
    ``python -m onoats.convert --once`` entry (which still works)."""
    from onoats.convert import main as convert_main

    # The converter's own parser requires --once; default to it so a bare
    # `onoats convert` does the obvious thing rather than erroring.
    if not rest:
        rest = ["--once"]
    return convert_main(rest)


def _cmd_flush(rest: list[str]) -> int:
    """Send SIGUSR1 to the running recorder so it rotates its buffer.

    This is the seam an integrating consumer's ``flush`` pass-through execs. It
    resolves the pid from ``<data_dir>/.active/onoats.pid`` (marker
    ``onoats-bot``) under the forwarded ``ONOATS_DATA_DIR`` root.
    """
    parser = argparse.ArgumentParser(prog="onoats flush")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Data dir override (else $ONOATS_DATA_DIR / XDG default).",
    )
    args = parser.parse_args(rest)
    data_dir = Path(args.data_dir) if args.data_dir else None

    from onoats._vendor.pid import resolve_flush_target

    pid_path = _pid_path(data_dir)
    target = resolve_flush_target(pid_path)
    if target.pid is None:
        # Identity could not be confirmed. Drop a now-untrustworthy pid file so
        # the next run starts clean, but never signal an unverified pid.
        if target.stale:
            try:
                pid_path.unlink()
            except OSError:
                pass
        print(f"onoats flush: {target.reason} (pid file {pid_path})", file=sys.stderr)
        return 1
    pid = target.pid
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        # Raced: the verified recorder exited between the identity check and
        # the signal. Treat as stale rather than signalling a recycled pid.
        try:
            pid_path.unlink()
        except OSError:
            pass
        print(
            f"onoats flush: recorder pid {pid} is not running (stale pid file)",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"onoats flush: could not signal pid {pid}: {exc}", file=sys.stderr)
        return 1
    print(f"onoats flush: sent SIGUSR1 to recorder pid {pid}")
    return 0


def _cmd_devices(rest: list[str]) -> int:
    """List audio input/output devices (reuses the device picker's enumeration)."""
    argparse.ArgumentParser(prog="onoats devices").parse_args(rest)
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        print("Input devices:")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                print(
                    f"  [{i}] {info['name']} "
                    f"({int(info['defaultSampleRate'])} Hz, "
                    f"{info['maxInputChannels']} ch)"
                )
        print("\nOutput devices:")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxOutputChannels"] > 0:
                print(
                    f"  [{i}] {info['name']} "
                    f"({int(info['defaultSampleRate'])} Hz, "
                    f"{info['maxOutputChannels']} ch)"
                )
    finally:
        pa.terminate()
    return 0


def _cmd_status(rest: list[str]) -> int:
    """Report recorder pid / running state + the resolved data dir."""
    parser = argparse.ArgumentParser(prog="onoats status")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Data dir override (else $ONOATS_DATA_DIR / XDG default).",
    )
    args = parser.parse_args(rest)
    data_dir = Path(args.data_dir) if args.data_dir else _resolve_data_dir()

    print(f"Data dir: {data_dir}")
    print(f"PID file: {_pid_path(data_dir)}")
    pid = _read_pid(data_dir)
    if pid is None:
        print("Recorder: not running (no valid pid file)")
        return 0
    if _process_alive(pid):
        print(f"Recorder: RUNNING (pid {pid})")
    else:
        print(f"Recorder: stale pid file (pid {pid} not running)")
    return 0


_HANDLERS = {
    "init": _cmd_init,
    "bot": _cmd_bot,
    "bot-single": _cmd_bot_single,
    "flush": _cmd_flush,
    "convert": _cmd_convert,
    "devices": _cmd_devices,
    "status": _cmd_status,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onoats",
        description=(
            "onoats — Always-on Organized Audio Transcript System. "
            "A standalone voice recorder + self-contained markdown converter."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.add_parser("init", help="Guided first-run setup (config.toml + secrets.env).")
    sub.add_parser("bot", help="Dual-input recorder (mic + system loopback).")
    sub.add_parser("bot-single", help="Legacy single-input (mic-only) recorder.")
    sub.add_parser("flush", help="Signal the running recorder to rotate its buffer.")
    sub.add_parser("convert", help="Render pending/*.jsonl into markdown transcripts.")
    sub.add_parser("devices", help="List audio input/output devices.")
    sub.add_parser("status", help="Report recorder pid / running state + data dir.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch ``onoats <command> [args...]``.

    Unknown options after the subcommand are forwarded verbatim to the
    subcommand's own parser (so e.g. ``onoats bot --help`` reaches the dual
    recorder's parser, and ``onoats convert --once`` reaches the converter).
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    # No command (or a bare -h/--help) → top-level help. Print + return 0
    # rather than letting argparse raise SystemExit, so callers get a clean rc.
    if not raw or raw[0] in ("-h", "--help"):
        parser.print_help()
        return 0

    command = raw[0]
    rest = raw[1:]
    handler = _HANDLERS.get(command)
    if handler is None:
        # Let argparse render the standard "invalid choice" error + usage.
        parser.parse_args(raw)
        return 2
    # Honor config.toml [storage].data_dir for every command (env still wins).
    # `init` writes config to XDG_CONFIG_HOME, unaffected by the data dir.
    if command != "init":
        _apply_config_data_dir()
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())
