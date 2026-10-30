#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/onoats-ui-telemetry.XXXXXX")"
trap 'rm -rf "$BUILD_DIR"' EXIT

swiftc -parse-as-library \
  -module-cache-path "$BUILD_DIR/module-cache" \
  "$ROOT/native/onoats-menubar/Sources/UiTelemetry.swift" \
  "$ROOT/native/onoats-menubar/Tests/UiTelemetryDecoderHarness.swift" \
  -o "$BUILD_DIR/ui-telemetry-harness"

"$BUILD_DIR/ui-telemetry-harness" "$ROOT/tests/fixtures/ui_telemetry"
