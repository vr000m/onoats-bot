#!/bin/bash
# Phase 4 manual-smoke helper: build the signed capturer, run it against
# wire_check.py for N seconds, report PASS/FAIL per branch, clean up.
#
# Usage:  ./smoke_wire_check.sh [seconds]     (default 10)
#
# SPEAK and PLAY AUDIO during the capture window — the point is to see
# peak > 0 on BOTH branches. Run from a LOCAL terminal on the Mac: over
# SSH there is no mic TCC/audio context and the mic branch will be
# silence-paced no matter what.
set -u
cd "$(dirname "$0")"

SECONDS_TO_RUN="${1:-10}"
NONCE="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"

if [ -n "${SSH_CONNECTION:-}" ]; then
    echo "⚠️  This looks like an SSH session (SSH_CONNECTION is set)."
    echo "   Mic capture will NOT work over SSH — run from a local terminal."
fi

echo "→ building + signing Onoats.app…"
make -s sign >/dev/null || { echo "✗ make sign failed"; exit 1; }
BIN="$(make -s print-bin)"

SOCKDIR="$(mktemp -d)"
LOG="$SOCKDIR/capturer.log"
trap 'kill "$CAPPID" 2>/dev/null; wait "$CAPPID" 2>/dev/null; rm -rf "$SOCKDIR"' EXIT

echo "→ starting capturer (log: $LOG)"
"$BIN" --mic-socket "$SOCKDIR/mic.sock" --system-socket "$SOCKDIR/system.sock" \
    --nonce "$NONCE" 2>"$LOG" &
CAPPID=$!

# Wait for both socket files (the supervisor does the same, bounded).
for _ in $(seq 1 50); do
    [ -S "$SOCKDIR/mic.sock" ] && [ -S "$SOCKDIR/system.sock" ] && break
    kill -0 "$CAPPID" 2>/dev/null || { echo "✗ capturer died at startup:"; cat "$LOG"; exit 1; }
    sleep 0.1
done

echo ""
echo "🎤 SPEAK and 🔊 PLAY AUDIO for the next ${SECONDS_TO_RUN}s…"
echo ""
python3 wire_check.py --mic-socket "$SOCKDIR/mic.sock" --system-socket "$SOCKDIR/system.sock" \
    --nonce "$NONCE" --seconds "$SECONDS_TO_RUN"
RC=$?

kill -TERM "$CAPPID" 2>/dev/null
wait "$CAPPID" 2>/dev/null

echo ""
echo "--- capturer log ---"
cat "$LOG"
echo "--------------------"
if grep -q "mic: pacing silence" "$LOG"; then
    echo "⚠️  mic branch delivered NO capture data (silence-paced)."
    echo "   Over SSH this is expected; locally it is a bug — report it."
fi
[ "$RC" -eq 0 ] && echo "✅ wire check PASS (both branches)" || echo "❌ wire check FAIL (rc=$RC)"
exit "$RC"
