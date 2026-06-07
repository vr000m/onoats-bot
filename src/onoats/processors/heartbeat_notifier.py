"""Platform-aware desktop notification helper for onoats heartbeat alerts.

Isolated from ``DualSilenceDetector`` so tests can mock the notification call
without spawning ``osascript`` and so non-macOS platforms degrade gracefully
to a WARNING log.
"""

from __future__ import annotations

import subprocess
import sys

from loguru import logger


def fire_desktop_notification(message: str, title: str = "onoats") -> None:
    """Fire a macOS desktop notification, falling back to a WARNING log.

    On macOS, runs ``osascript`` synchronously with a 5 s timeout. On any
    other platform — or if osascript is unavailable / times out — emits a
    WARNING-level log so the alert is still observable in operational logs.
    Quotes in ``message``/``title`` are sanitised to avoid breaking the
    AppleScript string literal.
    """
    if sys.platform != "darwin":
        logger.warning(f"Heartbeat (non-macOS): {message}")
        return

    safe_message = message.replace('"', "'")
    safe_title = title.replace('"', "'")
    script = f'display notification "{safe_message}" with title "{safe_title}"'
    try:
        subprocess.run(
            ["osascript", "-e", script],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning(f"Heartbeat: osascript failed ({exc}): {message}")
