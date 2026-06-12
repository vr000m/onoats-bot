#!/bin/bash
# Phase 4 manual-smoke step 8: aggregate/tap residue after hard kills.
#
# Loop ×N (default 3): start the PRODUCTION capturer with live socket
# clients (so the Core Audio tap + private aggregate actually exist),
# kill -9 it mid-capture, then use the same binary's maintenance
# subcommands (Maintenance.swift) to assert nothing survived:
#   list-aggregates → RESIDUE: none   (any onoats-* aggregate UID)
#   list-taps       → TAPS: none      (all process taps, system-wide)
#
# Usage:  ./residue_check.sh [rounds]    (default 3)
#
# Run from a LOCAL terminal: the production capturer requests the mic TCC
# grant before creating sockets, and over SSH there is no mic context.
# Audio playback is NOT required — silence-paced branches still build the
# full tap + aggregate chain, which is all this test cares about.
set -u
cd "$(dirname "$0")"

ROUNDS="${1:-3}"
CAPTURE_START_TIMEOUT=15  # seconds to wait for "capturing via process tap"

if [ -n "${SSH_CONNECTION:-}" ]; then
    echo "⚠️  SSH session detected — mic TCC will likely fail and the capturer"
    echo "   will exit before creating the tap. Run from a local terminal."
fi

echo "→ building + signing production capturer…"
make -s sign >/dev/null || { echo "✗ make sign failed"; exit 1; }
BIN="$(make -s print-bin)"
CHECKER="$BIN"   # the production binary carries its own residue enumeration

# Baseline: refuse to run against a dirty system, otherwise a pre-existing
# leak would be misattributed to this test's kills.
if ! "$CHECKER" list-aggregates 2>/dev/null | grep -q "^RESIDUE: none" \
   || ! "$CHECKER" list-taps 2>/dev/null | grep -q "^TAPS: none"; then
    echo "✗ residue present BEFORE the test — clean up first:"
    "$CHECKER" list-aggregates
    "$CHECKER" list-taps
    echo "  ('$CHECKER' clean-taps sweeps leftover taps;"
    echo "   leftover aggregates clear on reboot or via AudioHardwareDestroyAggregateDevice)"
    exit 1
fi
echo "✓ baseline clean"

for round in $(seq 1 "$ROUNDS"); do
    echo ""
    echo "── round $round/$ROUNDS ──"
    SOCKDIR="$(mktemp -d)"
    LOG="$SOCKDIR/capturer.log"
    NONCE="$(python3 -c 'import secrets; print(secrets.token_hex(8))')"

    "$BIN" --mic-socket "$SOCKDIR/mic.sock" --system-socket "$SOCKDIR/system.sock" \
        --nonce "$NONCE" 2>"$LOG" &
    CAPPID=$!

    for _ in $(seq 1 50); do
        [ -S "$SOCKDIR/mic.sock" ] && [ -S "$SOCKDIR/system.sock" ] && break
        kill -0 "$CAPPID" 2>/dev/null || { echo "✗ capturer died at startup:"; cat "$LOG"; exit 1; }
        sleep 0.1
    done

    # Drive the handshake so the capturer reaches step 5 (captures start) —
    # the tap + aggregate only exist once a client is connected.
    python3 wire_check.py --mic-socket "$SOCKDIR/mic.sock" \
        --system-socket "$SOCKDIR/system.sock" --nonce "$NONCE" \
        --seconds $((CAPTURE_START_TIMEOUT + 10)) >/dev/null 2>&1 &
    WCPID=$!

    started=0
    for _ in $(seq 1 $((CAPTURE_START_TIMEOUT * 10))); do
        grep -q "capturing via process tap" "$LOG" && { started=1; break; }
        kill -0 "$CAPPID" 2>/dev/null || break
        sleep 0.1
    done
    if [ "$started" -ne 1 ]; then
        echo "✗ capture never started (no tap/aggregate to leak — test invalid):"
        cat "$LOG"
        kill -9 "$CAPPID" "$WCPID" 2>/dev/null
        wait "$CAPPID" "$WCPID" 2>/dev/null
        rm -rf "$SOCKDIR"
        exit 1
    fi

    echo "→ tap live; kill -9 $CAPPID"
    kill -9 "$CAPPID"
    wait "$CAPPID" 2>/dev/null
    wait "$WCPID" 2>/dev/null   # exits on EOF once the capturer is gone
    rm -rf "$SOCKDIR"
done

echo ""
echo "── residue check after $ROUNDS hard kills ──"
AGG_OUT="$("$CHECKER" list-aggregates)"
TAP_OUT="$("$CHECKER" list-taps)"
echo "$AGG_OUT"
echo "$TAP_OUT"

if echo "$AGG_OUT" | grep -q "^RESIDUE: none" && echo "$TAP_OUT" | grep -q "^TAPS: none"; then
    echo "✅ residue check PASS — no stale aggregate or tap after ${ROUNDS}× kill -9"
    exit 0
else
    echo "❌ residue check FAIL — leftovers above survived the kills"
    exit 1
fi
