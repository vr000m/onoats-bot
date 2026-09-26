"""Source pins for the capturer's mic liveness/recovery invariants.

There is no Swift test harness, so — like tests/test_native_contract_parity.py —
these grep the Swift source for the load-bearing structure. They pin *shape*,
not runtime behaviour: the stall itself (a CoreAudio ``AudioDeviceStart`` that
blocks for 60+ s in coreaudiod) cannot be triggered deterministically.
"""

from __future__ import annotations

import re
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[1] / "native" / "onoats-capturer" / "Sources"
MIC = (SOURCES / "MicCapture.swift").read_text()
RESAMPLER = (SOURCES / "Resampler.swift").read_text()
SYSTEM = (SOURCES / "SystemCapture.swift").read_text()


def _body(src: str, header: str) -> str:
    """Text from ``header`` to the next 4-space-indented ``func``/declaration."""
    start = src.index(header)
    m = re.search(
        r"\n    (?:@discardableResult\n    )?(?:private )?func ",
        src[start + len(header) :],
    )
    end = start + len(header) + (m.start() if m else len(src))
    return src[start:end]


def test_first_bind_is_bounded_and_off_the_main_thread():
    """start() must not run bind() inline: a blocked AudioDeviceStart would
    wedge main (no "streaming", no signal handling) — observed 2026-09-25."""
    start = _body(MIC, "    func start() throws {")
    assert "DispatchQueue.global().async" in start
    assert "done.wait(timeout:" in start
    assert not re.search(r"^ {8}try bind\(\)\s*$", start, re.M), (
        "bind() called inline on the caller's thread again"
    )


def test_bind_commit_is_guarded_against_double_iproc():
    """bind() can run on several threads (first attempt, rebind, stall retry);
    a late finisher must discard its own IOProc rather than double-feed."""
    bind = _body(MIC, "    private func bind() throws -> Bool {")
    assert "bindLock.lock()" in bind
    assert "ioProcID != nil" in bind and "bindClosed" in bind
    assert "AudioDeviceDestroyIOProcID(device, proc)" in bind.split("if stale")[1]
    assert "bindClosed = true" in _body(MIC, "    func stop() {")


def test_stale_data_rebind_is_mic_only_and_requires_a_committed_bind():
    """The system tap legitimately delivers nothing while nothing renders audio,
    so it must never get the stale-data rebind; on the mic it must skip the
    never-bound case (retryStalledBind's job) to avoid parking rebindQueue in a
    stuck AudioDeviceStart."""
    assert "onStale" not in SYSTEM
    assert "var onStale" in RESAMPLER
    hook = MIC[MIC.index("chunker.onStale = ") :].split("chunker.activate()")[0]
    assert "ioProcID != nil" in hook
    assert "guard bound else { return }" in hook


def test_mic_starts_before_the_system_tap() -> None:
    """AudioDeviceStart on the mic stalls for minutes when a process tap already
    exists in the same process (2026-09-26 bisect); the mic must start first in
    both the production path and the concurrent selftest."""
    main = (SOURCES / "main.swift").read_text()
    assert main.index("try micCapture.start()") < main.index(
        "try systemCapture.start()"
    )
    assert main.index("try mic.start()") < main.index("try sys.start()")
    # its frames are dropped until the mic writer exists, then attached
    assert "micWriterBox.attach(micWriter)" in main
