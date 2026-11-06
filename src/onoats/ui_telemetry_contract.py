"""Shared schema and freshness constants for UI telemetry consumers.

This module is deliberately dependency-free.  The native smoke loads this exact
repository file by path, so it cannot accidentally trust an installed Onoats
package from another checkout.
"""

UI_TELEMETRY_SCHEMA_VERSION = 2
SNAPSHOT_EXPIRY_SECONDS = 5.0
AUDIO_OBSERVATION_EXPIRY_SECONDS = 1.0
LEVEL_MIN = 0.0
LEVEL_MAX = 1.0
TRANSCRIPT_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
