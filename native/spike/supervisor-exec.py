#!/usr/bin/env python3
"""Faithfully mimic the onoats supervisor's capturer-exec path for the TCC spike.

The plan's Pre-req spike 3 insists TCC be tested "on the FINAL launch topology" —
the *supervisor* launching the embedded helper, not double-clicking the .app. A
binary launched by the Python supervisor can be attributed to a different TCC
responsible-process identity than the GUI-launched app, so we must exercise the
exact exec path we ship.

This reuses the real `onoats.cli._build_capturer_env` (the deny-by-default
allowlist) and `create_subprocess_exec(..., start_new_session=True)` — the same
two things the supervisor does at cli.py:360-391 — pointed at the embedded helper.

Usage:
    uv run python native/spike/supervisor-exec.py [tcc|tap|concurrent] [--seconds N]

It defaults to the bundle the Makefile builds:
    native/spike/Onoats.app/Contents/MacOS/onoats-capturer
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import tempfile
from pathlib import Path

from onoats.cli import _build_capturer_env

HERE = Path(__file__).resolve().parent
DEFAULT_BIN = HERE / "Onoats.app" / "Contents" / "MacOS" / "onoats-capturer"


async def main() -> int:
    extra = [a for a in sys.argv[1:]]
    capturer_bin = os.environ.get("ONOATS_CAPTURER_BIN", str(DEFAULT_BIN))
    if not Path(capturer_bin).exists():
        print(
            f"✗ capturer not found: {capturer_bin}\n  run `make sign` first.",
            file=sys.stderr,
        )
        return 1

    # Same shape as the supervisor: private 0700 socket dir + fresh nonce, both
    # passed via argv AND the allowlisted env. The spike helper ignores the socket
    # flags; what matters is that we exec exactly like the supervisor does.
    sock_dir = tempfile.mkdtemp(prefix="onoats-spike-sock-")
    mic_sock = os.path.join(sock_dir, "mic.sock")
    system_sock = os.path.join(sock_dir, "system.sock")
    nonce = secrets.token_hex(16)
    env = _build_capturer_env(
        os.environ, mic_sock=mic_sock, system_sock=system_sock, nonce=nonce
    )

    argv = [
        capturer_bin,
        *extra,  # mode (tcc/tap/concurrent) + --seconds N
        "--mic-socket",
        mic_sock,
        "--system-socket",
        system_sock,
        "--nonce",
        nonce,
    ]
    print(f"→ supervisor-exec: {capturer_bin} {' '.join(extra)}", file=sys.stderr)
    proc = await asyncio.create_subprocess_exec(*argv, env=env, start_new_session=True)
    rc = await proc.wait()
    print(f"← capturer exited rc={rc}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
