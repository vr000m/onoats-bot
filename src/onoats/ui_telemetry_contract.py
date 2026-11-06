"""Shared schema and freshness constants for UI telemetry consumers.

The neutral ``onoats_contracts`` package is the sole literal owner.  This module
preserves the historical import surface for Python callers; the native smoke
loads the neutral source file by exact checkout path.
"""

from onoats_contracts.ui_telemetry import (  # noqa: F401
    AUDIO_OBSERVATION_EXPIRY_SECONDS,
    LEVEL_MAX,
    LEVEL_MIN,
    SNAPSHOT_EXPIRY_SECONDS,
    TRANSCRIPT_RUN_ID_PATTERN,
    UI_TELEMETRY_SCHEMA_VERSION,
)
