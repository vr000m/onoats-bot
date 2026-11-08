#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/onoats-recorder-model.XXXXXX")"
mkdir -p "$BUILD_DIR/home"
mkdir -p "$BUILD_DIR/Sources"
HOST_PREFERENCES="$HOME/Library/Preferences"

cleanup_harness_defaults() {
  local preference domain
  while IFS= read -r -d '' preference; do
    domain="$(basename "$preference" .plist)"
    defaults delete "$domain" >/dev/null 2>&1 || true
    rm -f "$preference"
  done < <(find "$HOST_PREFERENCES" -maxdepth 1 \
    -name 'onoats.harness.*.plist' -print0 2>/dev/null)
}

cleanup_on_exit() {
  local status=$?
  cleanup_harness_defaults || true
  rm -rf "$BUILD_DIR"
  exit "$status"
}

trap cleanup_on_exit EXIT
cleanup_harness_defaults
for source in AudioDevices.swift ConfigStore.swift LoginItemManager.swift RecorderModel.swift RecorderRecords.swift RecorderSystemOps.swift UiTelemetry.swift; do
  cp "$ROOT/native/onoats-menubar/Sources/$source" "$BUILD_DIR/Sources/$source"
done

swiftc -parse-as-library \
  -module-cache-path "$BUILD_DIR/module-cache" \
  -framework SwiftUI -framework AppKit -framework CoreAudio \
  "$BUILD_DIR/Sources/AudioDevices.swift" \
  "$BUILD_DIR/Sources/ConfigStore.swift" \
  "$BUILD_DIR/Sources/LoginItemManager.swift" \
  "$BUILD_DIR/Sources/UiTelemetry.swift" \
  "$BUILD_DIR/Sources/RecorderSystemOps.swift" \
  "$BUILD_DIR/Sources/RecorderRecords.swift" \
  "$BUILD_DIR/Sources/RecorderModel.swift" \
  "$ROOT/native/onoats-menubar/Tests/RecorderModelHarness.swift" \
  -o "$BUILD_DIR/recorder-model-harness"

CFFIXED_USER_HOME="$BUILD_DIR/home" HOME="$BUILD_DIR/home" \
  "$BUILD_DIR/recorder-model-harness" "$BUILD_DIR/home"

cleanup_harness_defaults
if find "$HOST_PREFERENCES" -maxdepth 1 -name 'onoats.harness.*.plist' -print -quit \
  2>/dev/null | grep -q .; then
  echo "RecorderModelHarness leaked a persistent onoats.harness defaults domain" >&2
  exit 1
fi
