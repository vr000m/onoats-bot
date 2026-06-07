# vendored from koda shared/store.py (data-dir resolution only — no SQLite)
"""Data-directory resolution for the onoats recorder.

The upstream ``shared/store.py`` is a SQLite overlay index; onoats vendors ONLY
the data-dir resolution helpers from it. The recorder opens no database, so
none of the ``TranscriptStore`` machinery is carried over.

Resolution precedence (highest first):

1. ``ONOATS_DATA_DIR`` env var.
2. The legacy data-dir env var — honored for one release with a
   ``DeprecationWarning`` so an upgraded box keeps working. ``ONOATS_DATA_DIR``
   wins when both are set.
3. ``$XDG_DATA_HOME/onoats`` (defaults to ``~/.local/share/onoats``).

``shadow_data_dir`` roots PCM dumps + shadow verdicts under the resolved data
dir, created ``0o700`` because it holds conversation audio.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

# Legacy data-dir env var honored for one release.  # vendored from koda
_LEGACY_DATA_DIR_ENV = "KODA_DATA_DIR"  # vendored from koda


def _xdg_data_home() -> Path:
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".local" / "share"
    return base / "onoats"


def onoats_data_dir() -> Path:
    """Resolve the recorder data dir (the durable, recorder-owned subtree).

    ``ONOATS_DATA_DIR`` wins; the legacy env var is honored with a
    ``DeprecationWarning`` for one release; otherwise ``$XDG_DATA_HOME/onoats``.
    """
    onoats_env = os.environ.get("ONOATS_DATA_DIR", "").strip()
    legacy_env = os.environ.get(_LEGACY_DATA_DIR_ENV, "").strip()
    if onoats_env:
        return Path(onoats_env).expanduser()
    if legacy_env:
        warnings.warn(
            f"{_LEGACY_DATA_DIR_ENV} is deprecated; set ONOATS_DATA_DIR instead. "
            f"{_LEGACY_DATA_DIR_ENV} will stop being honored in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return Path(legacy_env).expanduser()
    return _xdg_data_home()


def shadow_data_dir() -> Path:
    """Root for spike outputs (shadow verdicts + raw PCM dumps).

    Created with mode 0o700 because the subtree contains conversation audio
    (PCM) and turn-timing metadata (JSONL).
    """
    base = onoats_data_dir() / "shadow"
    base.mkdir(parents=True, exist_ok=True)
    try:
        base.chmod(0o700)
    except OSError:
        pass
    return base
