"""Dependency-free schema constants shared by UI telemetry consumers."""

UI_TELEMETRY_SCHEMA_VERSION = 2
# Leaf name under ``<data_dir>/.active/``; the recorder, the native smoke and
# the Swift decoder (``UiTelemetry.fileName``, parity-pinned) all read it.
UI_TELEMETRY_FILENAME = "onoats.ui.json"
SNAPSHOT_EXPIRY_SECONDS = 5.0
AUDIO_OBSERVATION_EXPIRY_SECONDS = 1.0
LEVEL_MIN = 0.0
LEVEL_MAX = 1.0
TRANSCRIPT_RUN_ID_PATTERN = r"^[0-9a-f]{32}$"
