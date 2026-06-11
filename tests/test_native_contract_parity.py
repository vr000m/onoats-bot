"""Cross-language contract parity: Swift literals vs Python constants.

The native side re-states several Python contracts as Swift literals — the
wire version (capturer handshake), the status-file schema/path (menu-bar
reader), the pid-file marker (menu-bar liveness), the XDG data-dir default,
and the STT service list (menu-bar picker). None of those literals are
compiled against the Python source, so a one-sided edit passes every other
test and only fails at runtime (handshake rejected, menu bar mis-rendering,
liveness silently broken).

Same approach as ``test_audio_socket_contract_parity.py`` takes with the
markdown contract doc: grep the Swift source for each literal and assert it
equals the Python constant it mirrors. Editing either side without the other
fails here, with the file/constant named.

Also pins the ConfigStore writer contract: the Swift TOML line editor emits
``key = "value"`` with ``\\``/``\"`` escaping; a sample written in exactly
that format must round-trip through ``tomllib`` (what the Python CLI parses
config.toml with) back to the original value.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RECORDER_MODEL = REPO / "native" / "onoats-menubar" / "Sources" / "RecorderModel.swift"
FRAME_WRITER = REPO / "native" / "onoats-capturer" / "Sources" / "FrameWriter.swift"


def _extract(pattern: str, path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    m = re.search(pattern, text)
    assert m, f"pattern {pattern!r} not found in {path.relative_to(REPO)}"
    return m.group(1)


def test_wire_version_matches_swift():
    from onoats.transports.socket_audio import WIRE_VERSION

    swift = int(_extract(r"let WIRE_VERSION\s*=\s*(\d+)", FRAME_WRITER))
    assert swift == WIRE_VERSION


def test_status_schema_version_matches_swift():
    from onoats.status import STATUS_SCHEMA_VERSION

    swift = int(_extract(r"statusSchemaVersion\s*=\s*(\d+)", RECORDER_MODEL))
    assert swift == STATUS_SCHEMA_VERSION


def test_status_file_relative_path_matches_swift():
    from onoats.status import status_path

    python_rel = status_path(Path("X")).relative_to("X").as_posix()
    swift = _extract(
        r'appendingPathComponent\("(\.active/onoats\.status\.json)"\)', RECORDER_MODEL
    )
    assert swift == python_rel


def test_pid_marker_and_path_match_swift():
    from onoats.cli import PID_FILENAME
    from onoats._vendor.pid import PID_MARKER

    swift_marker = _extract(r'lines\[1\]\s*==\s*"([^"]+)"', RECORDER_MODEL)
    assert swift_marker == PID_MARKER

    swift_path = _extract(
        r'appendingPathComponent\("(\.active/onoats\.pid)"\)', RECORDER_MODEL
    )
    assert swift_path == f".active/{PID_FILENAME}"


def test_xdg_default_data_dir_matches_swift(monkeypatch: pytest.MonkeyPatch):
    from onoats._vendor.store import onoats_data_dir

    monkeypatch.delenv("ONOATS_DATA_DIR", raising=False)
    monkeypatch.delenv("KODA_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    python_default = onoats_data_dir()
    assert python_default == Path.home() / ".local" / "share" / "onoats"

    # Swift: NSHomeDirectory() + this literal.
    swift_suffix = _extract(
        r'NSHomeDirectory\(\)\s*\+\s*"(/\.local/share/onoats)"', RECORDER_MODEL
    )
    assert Path.home().as_posix() + swift_suffix == python_default.as_posix()


def test_stt_service_list_matches_swift():
    from onoats.runtime import VALID_STT_SERVICES

    raw = _extract(r"sttServices\s*=\s*\[([^\]]+)\]", RECORDER_MODEL)
    swift = tuple(re.findall(r'"([^"]+)"', raw))
    assert swift == tuple(VALID_STT_SERVICES)


@pytest.mark.parametrize(
    "value",
    [
        "/Users/x/plain",
        '/Users/x/my"dir',  # double-quote — legal in macOS dir names
        "/Users/x/back\\slash",
        '/Users/x/we\\"ird',
    ],
)
def test_configstore_escaping_round_trips_through_tomllib(value: str):
    """Emulate ConfigStore.writeValue's exact output format and parse it with
    tomllib (what every Python entrypoint uses on config.toml). If the Swift
    escaping rules drift from TOML basic-string rules, this fails before a
    user's chosen data dir can corrupt their config."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    doc = f'[storage]\ndata_dir = "{escaped}"\n'
    parsed = tomllib.loads(doc)
    assert parsed["storage"]["data_dir"] == value
