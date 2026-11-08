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
python3 - "$script_dir/build/onoats-capturer" <<'PY'
import subprocess
import sys
import time

started = time.monotonic()
try:
    completed = subprocess.run(
        [sys.argv[1], "--selftest-heartbeat-hard-exit"],
        check=False,
        timeout=2.0,
    )
except subprocess.TimeoutExpired:
    print("FAIL: hard-exit self-test exceeded 2 seconds", file=sys.stderr)
    raise SystemExit(1)

elapsed = time.monotonic() - started
if completed.returncode != 14:
    print(
        f"FAIL: hard-exit self-test returned {completed.returncode}, expected 14",
        file=sys.stderr,
    )
    raise SystemExit(1)
if elapsed >= 2.0:
    print(f"FAIL: hard-exit self-test took {elapsed:.3f}s", file=sys.stderr)
    raise SystemExit(1)
PY

echo "PASS: capturer heartbeat hard-exit deadline"
