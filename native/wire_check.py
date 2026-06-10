#!/usr/bin/env python3
"""Wire-contract v1 checker for the native capturer (docs/audio-socket-contract.md).

Plays the recorder's role against a RUNNING capturer: connects to both unix
sockets, validates the handshake (rate/width/channels/v/nonce echo), then reads
frames for a few seconds and asserts framing invariants per branch:

- 4-byte big-endian length prefix, payload in 1..1MiB
- JSON object with int seq / int captured_monotonic_ns / str pcm_b64
- seq starts at 0 and is monotonic (gaps reported as drops, not failures)
- PCM decodes, is a whole number of samples, ~640 bytes (reference 20 ms)
- captured_monotonic_ns is non-decreasing and frames keep arriving

Usage:
  python3 wire_check.py --mic-socket P --system-socket P [--nonce HEX] [--seconds N]

Exit 0 = all checks pass on both branches. Prints a per-branch summary
including peak amplitude so silent-vs-real capture is visible.
"""

import argparse
import array
import base64
import json
import socket
import struct
import sys
import threading
import time

MAX_PAYLOAD = 1 << 20


class BranchResult:
    def __init__(self, label):
        self.label = label
        self.handshake = None
        self.frames = 0
        self.drops = 0
        self.pcm_bytes = 0
        self.peak = 0.0
        self.first_ns = None
        self.last_ns = None
        self.errors = []


def read_exactly(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"EOF after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


def check_branch(path, label, nonce, seconds, result):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        sock.connect(path)

        # Handshake: one JSON line.
        line = b""
        while not line.endswith(b"\n"):
            line += read_exactly(sock, 1)
        hs = json.loads(line.decode("utf-8"))
        result.handshake = hs
        for key, want in (("rate", 16000), ("width", 2), ("channels", 1), ("v", 1)):
            if hs.get(key) != want:
                result.errors.append(f"handshake {key}={hs.get(key)!r}, want {want}")
        if nonce is not None and hs.get("nonce") != nonce:
            result.errors.append(f"nonce not echoed: {hs.get('nonce')!r} != {nonce!r}")

        deadline = time.monotonic() + seconds
        expected_seq = 0
        prev_ns = None
        while time.monotonic() < deadline:
            (n,) = struct.unpack(">I", read_exactly(sock, 4))
            if not (1 <= n <= MAX_PAYLOAD):
                result.errors.append(f"length prefix {n} out of 1..{MAX_PAYLOAD}")
                return
            obj = json.loads(read_exactly(sock, n).decode("utf-8"))
            seq = obj["seq"]
            ns = obj["captured_monotonic_ns"]
            pcm = base64.b64decode(obj["pcm_b64"], validate=True)
            if not isinstance(seq, int) or not isinstance(ns, int):
                result.errors.append(f"non-int seq/ns in frame: {obj.keys()}")
                return
            if seq != expected_seq:
                if seq > expected_seq:
                    result.drops += seq - expected_seq
                else:
                    result.errors.append(f"seq went backwards: {seq} < {expected_seq}")
                    return
            expected_seq = seq + 1
            if len(pcm) % 2 != 0:
                result.errors.append(f"odd PCM byte count {len(pcm)} (seq={seq})")
                return
            # The transport contract says 640 bytes is SHOULD, not MUST. This
            # is a stricter self-check of OUR capturer (which always emits
            # 640) — do not mistake it for the wire-contract rule.
            if len(pcm) != 640:
                result.errors.append(
                    f"frame size {len(pcm)} != 640 reference (seq={seq})"
                )
                return
            if prev_ns is not None and ns < prev_ns:
                result.errors.append(
                    f"captured_monotonic_ns regressed: {ns} < {prev_ns}"
                )
                return
            prev_ns = ns
            if result.first_ns is None:
                result.first_ns = ns
            result.last_ns = ns
            result.frames += 1
            result.pcm_bytes += len(pcm)
            # array('h') keeps this C-speed: a per-sample Python loop ran
            # slower than real time and made the checker itself the bottleneck.
            samples = array.array("h", pcm)
            frame_peak = max(abs(max(samples)), abs(min(samples))) / 32768.0
            if frame_peak > result.peak:
                result.peak = frame_peak
        sock.close()
    except Exception as exc:  # noqa: BLE001 — report, don't crash the other branch
        result.errors.append(f"{type(exc).__name__}: {exc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mic-socket", required=True)
    ap.add_argument("--system-socket", required=True)
    ap.add_argument("--nonce", default=None)
    ap.add_argument("--seconds", type=float, default=4.0)
    args = ap.parse_args()

    branches = [
        (args.mic_socket, "mic"),
        (args.system_socket, "system"),
    ]
    results = [BranchResult(label) for _, label in branches]
    threads = [
        threading.Thread(
            target=check_branch, args=(path, label, args.nonce, args.seconds, res)
        )
        for (path, label), res in zip(branches, results)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ok = True
    for res in results:
        wall_s = (res.last_ns - res.first_ns) / 1e9 if res.frames > 1 else 0.0
        status = "PASS" if not res.errors and res.frames > 0 else "FAIL"
        if status == "FAIL":
            ok = False
        print(
            f"{res.label:7s} {status}  frames={res.frames} drops={res.drops} "
            f"pcm={res.pcm_bytes}B peak={res.peak:.4f} ts_span={wall_s:.2f}s "
            f"handshake={res.handshake}"
        )
        for err in res.errors:
            print(f"        ERROR: {err}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
