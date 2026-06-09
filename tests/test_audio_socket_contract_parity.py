"""Parity guard: the wire-contract doc must mirror the module constants.

``docs/audio-socket-contract.md`` declares itself a *mirror* of
``socket_audio.py`` — "that module is the source of truth and this doc mirrors
it". Nothing enforced that, so a ``WIRE_VERSION`` / constant bump could land in
code without the doc (or vice versa). This test parses the doc's
``## Constants`` table and asserts each value equals the live module constant, so
the two can never silently drift.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import onoats.transports.socket_audio as mod

_DOC = Path(__file__).resolve().parents[1] / "docs" / "audio-socket-contract.md"

# The named integer constants the doc's "## Constants" table mirrors. The parser
# discovers these from the table; this set guards against it silently matching
# nothing (e.g. if the table format changes).
_REQUIRED = {
    "WIRE_VERSION",
    "DEFAULT_SAMPLE_RATE",
    "DEFAULT_SAMPLE_WIDTH",
    "DEFAULT_CHANNELS",
    "LENGTH_PREFIX_BYTES",
    "MAX_FRAME_PAYLOAD_BYTES",
}


def _constants_table_rows() -> list[list[str]]:
    """Return the cell lists of the doc's ``## Constants`` markdown table."""
    section = _DOC.read_text(encoding="utf-8").split("## Constants", 1)
    assert len(section) == 2, "contract doc has no '## Constants' section"
    rows: list[list[str]] = []
    for line in section[1].splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if rows:  # the table has ended
                break
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 2:
            rows.append(cells)
    return rows


def test_contract_constants_mirror_module() -> None:
    rows = _constants_table_rows()
    checked: set[str] = set()
    for name_cell, value_cell, *_ in rows:
        name_match = re.search(r"`([A-Z_][A-Z0-9_]*)`", name_cell)
        if not name_match:
            continue
        name = name_match.group(1)
        if not hasattr(mod, name):
            continue
        value_match = re.search(r"`(\d+)`", value_cell)
        assert value_match, f"no integer value cell for {name} in the table"
        doc_value = int(value_match.group(1))
        assert getattr(mod, name) == doc_value, (
            f"{name}: contract doc says {doc_value}, module is {getattr(mod, name)}"
        )
        checked.add(name)

    missing = _REQUIRED - checked
    assert not missing, f"contract table did not verify: {sorted(missing)}"


def test_contract_frame_size_and_defaults_match() -> None:
    # 20 ms frame @ 16 kHz row.
    assert mod.frame_size_bytes(16000) == 640
    # default read_idle_timeout / max_buffered_frames rows.
    sig = inspect.signature(mod.UnixSocketAudioInputTransport.__init__)
    assert sig.parameters["read_idle_timeout"].default == 10.0
    assert sig.parameters["max_buffered_frames"].default == 200
