"""``launchctl kickstart`` self-healing for the STT server + shared cooldown.

Leaf module: no ``runtime``/``status``/``dual`` imports. Both the preflight
path (``onoats.runtime._preflight_stt_ws``) and the live reconnect path
(``onoats.stt.websocket_stt_service.WebSocketSTTService``) import from here
without either importing the other — this module is the shared meeting
point for the process-wide, label-keyed kickstart cooldown.

``kickstart_stt_server`` never raises — it follows the same
subprocess-shellout convention as ``runtime._own_ps_cmdline``:
``subprocess.run(argv, capture_output=True, text=True, timeout=N,
check=False)`` wrapped in a broad, non-raising ``except``. Every async call
site invokes it via ``asyncio.to_thread`` so a wedged ``launchd`` job never
blocks the event loop.

**GUI-domain caveat**: ``gui/$UID/<label>`` assumes ``onoats bot`` runs
inside a GUI (Aqua) session, matching how the shipped LaunchAgents are
registered today. A non-GUI invocation (ssh session, headless daemon with no
Aqua session) gets a domain-not-found error from ``launchctl``, not the
"label not loaded" case this module's fall-through is written for —
the broad non-raising exception handling still absorbs it (returns
``False``), but the caller's log text may say "kickstart failed" when the
real cause is "no such domain." This is a pre-existing constraint of
launchd's GUI-domain model, not something this module can fix.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable

from loguru import logger

_KICKSTART_TIMEOUT_SEC = 5

# Must exceed WebSocketSTTService's live reconnect backoff total (~15.5s,
# see runtime.py's `_PREFLIGHT_RETRY_*` comment) plus margin, so a
# kickstart's own retry window can never itself trigger a second kickstart.
KICKSTART_COOLDOWN_SEC = 30

# label -> last-kickstart monotonic time. Process-wide and label-keyed
# (not per-WebSocketSTTService-instance): dual.py constructs two
# independent instances (mic + system) against the same server, and the
# startup preflight path stamps the same registry, so a preflight kickstart
# counts against the live-session budget too.
_last_kickstart: dict[str, float] = {}


def kickstart_stt_server(label: str, uid: int | None = None) -> bool:
    """Ask launchd to restart the managed job ``label``. Never raises.

    Runs ``launchctl kickstart -k gui/<uid>/<label>`` synchronously
    (best-effort shellout, mirrors ``runtime._own_ps_cmdline``). ``uid``
    defaults to ``os.getuid()`` when not passed. Returns ``True`` only on
    rc 0 — a non-zero exit (label not loaded, permission denied, no such
    domain) or any exception is swallowed and returns ``False``, so callers
    can treat every failure mode identically: fall through to today's
    behavior.

    A ``True`` return means only that launchd accepted the restart
    request, not that the server is answering again — callers must confirm
    recovery with their own post-kickstart handshake/reconnect.
    """
    resolved_uid = uid if uid is not None else os.getuid()
    argv = ["launchctl", "kickstart", "-k", f"gui/{resolved_uid}/{label}"]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_KICKSTART_TIMEOUT_SEC,
            check=False,
        )
        if result.returncode == 0:
            return True
        logger.warning(
            f"STT: kickstart of {label!r} failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return False
    except (FileNotFoundError, subprocess.SubprocessError, OSError) as exc:
        logger.warning(f"STT: kickstart of {label!r} failed: {exc}")
        return False


def _cooldown_elapsed(label: str, clock: Callable[[], float] = time.monotonic) -> bool:
    """True if ``label`` has never been kickstarted, or its last kickstart
    was more than ``KICKSTART_COOLDOWN_SEC`` ago."""
    last = _last_kickstart.get(label)
    if last is None:
        return True
    return (clock() - last) >= KICKSTART_COOLDOWN_SEC


def _stamp_cooldown(label: str, clock: Callable[[], float] = time.monotonic) -> None:
    """Record ``label`` as kickstarted now (per ``clock``)."""
    _last_kickstart[label] = clock()


def _reset_cooldown(label: str) -> None:
    """Clear ``label``'s cooldown stamp — called on confirmed sustained
    health (a ``transcript.*`` event following a kickstart), never on a
    bare successful connect. Test-only convenience is not exposed here;
    this is the one production reset path (Phase 4)."""
    _last_kickstart.pop(label, None)
