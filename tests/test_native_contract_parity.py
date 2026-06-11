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
SUPPORT = REPO / "native" / "onoats-capturer" / "Sources" / "Support.swift"


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


def test_status_record_fields_match_swift():
    """The Swift StatusRecord mirror must carry exactly the Python dataclass's
    fields, in the same order — a one-sided field addition (e.g. a schema-v2
    optional) otherwise decodes fine and silently never renders."""
    from dataclasses import fields as dc_fields

    from onoats.status import StatusRecord

    text = RECORDER_MODEL.read_text(encoding="utf-8")
    m = re.search(r"struct StatusRecord: Decodable \{(.*?)\n\}", text, re.DOTALL)
    assert m, "struct StatusRecord not found in RecorderModel.swift"
    swift_fields = re.findall(r"let (\w+):", m.group(1))
    python_fields = [f.name for f in dc_fields(StatusRecord)]
    assert swift_fields == python_fields


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

    # The [^\]]+ capture assumes a SINGLE-LINE, comment-free Swift array
    # literal (re.DOTALL is off, so a multi-line reformat truncates the
    # match). The count assertion below turns a partial parse into a loud
    # failure instead of a garbled comparison.
    raw = _extract(r"sttServices\s*=\s*\[([^\]]+)\]", RECORDER_MODEL)
    swift = tuple(re.findall(r'"([^"]+)"', raw))
    assert len(swift) == len(VALID_STT_SERVICES), (
        f"parsed {len(swift)} services from Swift, expected "
        f"{len(VALID_STT_SERVICES)} — if the Swift array went multi-line, "
        f"keep it on one line or update this test's regex"
    )
    assert swift == tuple(VALID_STT_SERVICES)


def test_event_line_prefix_matches_swift():
    """The capturer's emitEvent prefix and the supervisor's parser prefix are
    the same string-with-trailing-space, restated in two languages — a
    one-sided edit silently turns every event into an ignored log line."""
    from onoats.cli import _ONOATS_EVENT_PREFIX

    swift = _extract(r'let line = "(ONOATS-EVENT )" \+ type', SUPPORT)
    assert swift == _ONOATS_EVENT_PREFIX


def test_device_event_emission_matches_supervisor_parser():
    """Both capture branches emit `ONOATS-EVENT device branch=<b> hint=<desc>`
    (hint is the trailing free-text field BY CONTRACT — device names contain
    spaces) and the supervisor's stderr reader consumes exactly that event
    type into the schema-v2 mic_device/system_device fields. A one-sided
    rename silently turns every device event into an ignored log line."""
    mic = REPO / "native" / "onoats-capturer" / "Sources" / "MicCapture.swift"
    system = REPO / "native" / "onoats-capturer" / "Sources" / "SystemCapture.swift"
    assert re.search(
        r'emitEvent\("device", "branch=mic hint=', mic.read_text(encoding="utf-8")
    ), "MicCapture.swift no longer emits the device event the supervisor parses"
    assert re.search(
        r'emitEvent\("device", "branch=system hint=',
        system.read_text(encoding="utf-8"),
    ), "SystemCapture.swift no longer emits the device event the supervisor parses"

    supervisor = (REPO / "src" / "onoats" / "cli.py").read_text(encoding="utf-8")
    assert 'event_type == "device"' in supervisor, (
        "cli._drain_capturer_stderr no longer handles the `device` event type"
    )


def test_capturer_exit_codes_are_all_accounted_for():
    """Every non-ok ExitCode in Support.swift must be either mapped to a
    specific exit_reason in cli._CAPTURER_RC_REASONS or deliberately listed
    as generic below. Adding a new ExitCode fails here until someone decides
    which bucket it belongs in."""
    from onoats.cli import _CAPTURER_RC_REASONS

    support = (
        REPO / "native" / "onoats-capturer" / "Sources" / "Support.swift"
    ).read_text(encoding="utf-8")
    swift_codes = {
        name: int(value)
        for name, value in re.findall(r"static let (\w+): Int32 = (\d+)", support)
    }
    assert swift_codes, "no ExitCode constants found in Support.swift"

    # Deliberately generic: these stamp as plain "capturer-crash" because the
    # menu bar has no more-useful label for them. ok/usage never reach the
    # mid-session stamping path.
    generic = {"ok", "usage", "socketFailed", "captureFailed"}

    assert swift_codes["micDenied"] in _CAPTURER_RC_REASONS
    assert swift_codes["systemAudioFailed"] in _CAPTURER_RC_REASONS
    mapped_values = set(_CAPTURER_RC_REASONS)
    unaccounted = {
        name
        for name, value in swift_codes.items()
        if value not in mapped_values and name not in generic
    }
    assert not unaccounted, (
        f"new ExitCode(s) {sorted(unaccounted)} are neither mapped in "
        f"cli._CAPTURER_RC_REASONS nor listed as deliberately generic here"
    )


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
