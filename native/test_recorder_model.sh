#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR="$(mktemp -d "${TMPDIR:-/tmp}/onoats-recorder-model.XXXXXX")"
trap 'rm -rf "$BUILD_DIR"' EXIT
mkdir -p "$BUILD_DIR/home"
mkdir -p "$BUILD_DIR/Sources"
HOST_PREFERENCES="$HOME/Library/Preferences"
find "$HOST_PREFERENCES" -maxdepth 1 -name 'onoats.harness.*.plist' -print 2>/dev/null \
  | sort > "$BUILD_DIR/host-preferences-before"
for source in AudioDevices.swift ConfigStore.swift LoginItemManager.swift RecorderModel.swift UiTelemetry.swift; do
  cp "$ROOT/native/onoats-menubar/Sources/$source" "$BUILD_DIR/Sources/$source"
done

swiftc -parse-as-library \
  -module-cache-path "$BUILD_DIR/module-cache" \
  -framework SwiftUI -framework AppKit -framework CoreAudio \
  "$BUILD_DIR/Sources/AudioDevices.swift" \
  "$BUILD_DIR/Sources/ConfigStore.swift" \
  "$BUILD_DIR/Sources/LoginItemManager.swift" \
  "$BUILD_DIR/Sources/UiTelemetry.swift" \
  "$BUILD_DIR/Sources/RecorderModel.swift" \
  "$ROOT/native/onoats-menubar/Tests/RecorderModelHarness.swift" \
  -o "$BUILD_DIR/recorder-model-harness"

CFFIXED_USER_HOME="$BUILD_DIR/home" HOME="$BUILD_DIR/home" \
  "$BUILD_DIR/recorder-model-harness" "$BUILD_DIR/home"

find "$HOST_PREFERENCES" -maxdepth 1 -name 'onoats.harness.*.plist' -print 2>/dev/null \
  | sort > "$BUILD_DIR/host-preferences-after"
if ! cmp -s "$BUILD_DIR/host-preferences-before" "$BUILD_DIR/host-preferences-after"; then
  echo "RecorderModelHarness leaked a persistent onoats.harness defaults domain" >&2
  exit 1
fi
