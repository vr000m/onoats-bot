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

swiftc \
  -module-cache-path "$tmp_dir/swift-module-cache" \
  -framework AVFoundation \
  -framework CoreAudio \
  -framework AudioToolbox \
  "$script_dir/onoats-capturer/Sources/Support.swift" \
  "$script_dir/onoats-capturer/Sources/ProcessSuspensionWatchdog.swift" \
  "$script_dir/onoats-capturer/Sources/Resampler.swift" \
  "$script_dir/onoats-capturer/Tests/FrameChunkerSuspensionHarness.swift" \
  -o "$tmp_dir/frame-chunker-suspension-harness"

append_after_stop=$(
  "$tmp_dir/frame-chunker-suspension-harness" --append-after-stop
)
if [[ "$append_after_stop" != "APPEND_AFTER_STOP_OK" ]]; then
  echo "FAIL: stopped FrameChunker accepted queued capture data" >&2
  exit 1
fi

echo "PASS: stopped FrameChunker rejects queued capture data"

python3 - "$tmp_dir/frame-chunker-suspension-harness" <<'PY'
import os
import signal
import subprocess
import sys
import time


def run_pause(seconds: float, *, should_trip: bool) -> None:
    child = subprocess.Popen(
        [sys.argv[1]],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        ready = child.stdout.readline().strip()
        if ready != "READY":
            raise AssertionError(f"FrameChunker harness did not become ready: {ready!r}")

        os.kill(child.pid, signal.SIGSTOP)
        time.sleep(seconds)
        resumed = time.monotonic()
        os.kill(child.pid, signal.SIGCONT)

        if should_trip:
            rc = child.wait(timeout=2.0)
            elapsed = time.monotonic() - resumed
            if rc != 14:
                raise AssertionError(f"{seconds}s pause returned {rc}, expected 14")
            if elapsed >= 2.0:
                raise AssertionError(
                    f"{seconds}s pause took {elapsed:.3f}s to terminate after resume"
                )
        else:
            time.sleep(0.3)
            if child.poll() is not None:
                raise AssertionError(
                    f"{seconds}s pause tripped with rc {child.returncode}"
                )
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=2.0)


run_pause(1.5, should_trip=False)
run_pause(2.5, should_trip=True)
PY

echo "PASS: production FrameChunker detects real process suspension"

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
