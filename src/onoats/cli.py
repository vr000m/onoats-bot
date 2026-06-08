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
    from onoats.dual import main as dual_main

    return dual_main(rest)


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

    pid = _read_pid(data_dir)
    if pid is None:
        print(
            f"onoats flush: no running recorder found (no valid pid file at {_pid_path(data_dir)})",
            file=sys.stderr,
        )
        return 1
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
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
