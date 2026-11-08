#!/bin/bash
set -euo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)
tmp_dir=$(mktemp -d)
trap 'rm -rf -- "$tmp_dir"' EXIT
export CLANG_MODULE_CACHE_PATH="$tmp_dir/clang-module-cache"

swiftc \
  -module-cache-path "$tmp_dir/swift-module-cache" \
  "$script_dir/onoats-capturer/Sources/Support.swift" \
  "$script_dir/onoats-capturer/Sources/ProcessSuspensionWatchdog.swift" \
  "$script_dir/onoats-capturer/Tests/ProcessSuspensionWatchdogHarness.swift" \
  -o "$tmp_dir/watchdog-harness"
"$tmp_dir/watchdog-harness"

make -C "$script_dir" build/onoats-capturer >/dev/null
SECONDS=0
set +e
"$script_dir/build/onoats-capturer" --selftest-heartbeat-hard-exit
rc=$?
set -e
if [[ $rc -ne 14 ]]; then
  echo "FAIL: hard-exit self-test returned $rc, expected 14" >&2
  exit 1
fi
if [[ $SECONDS -gt 2 ]]; then
  echo "FAIL: hard-exit self-test exceeded 2 seconds" >&2
  exit 1
fi

echo "PASS: capturer heartbeat hard-exit deadline"
