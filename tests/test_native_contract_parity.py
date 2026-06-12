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
config.toml with) back to the original value. ``_swift_write_value`` below is
a maintained line-by-line mirror of ``ConfigStore.writeValue`` — update it in
the same change whenever the Swift writer changes.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _swift_escape(value: str) -> str:
    """ConfigStore.escape's exact rule: backslash first, then double quote."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


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


def test_tap_preflight_precedes_sockets_and_announces_permission_wait():
    """Phase 7 keystone (inverted startup order): the TCC-prompting tap call
    runs BEFORE the listening sockets exist, announced by
    `ONOATS-EVENT waiting-for-permission` — and the supervisor consumes
    exactly that event type to extend its socket wait. A one-sided rename
    (or a reorder back to sockets-first) silently revives the 10 s
    prompt-pending death this phase removed."""
    main_swift = (
        REPO / "native" / "onoats-capturer" / "Sources" / "main.swift"
    ).read_text(encoding="utf-8")

    emit_pos = main_swift.find('emitEvent(\n    "waiting-for-permission"')
    if emit_pos == -1:
        emit_pos = main_swift.find('emitEvent("waiting-for-permission"')
    assert emit_pos != -1, (
        "main.swift no longer emits the waiting-for-permission event the "
        "supervisor keys its extended socket wait on"
    )
    tap_start_pos = main_swift.find("try systemCapture.start()")
    socket_pos = main_swift.find("makeListeningSocket")
    assert tap_start_pos != -1 and socket_pos != -1
    assert emit_pos < tap_start_pos < socket_pos, (
        "startup order regressed: the waiting-for-permission emit and the "
        "system-tap start (the TCC-prompting call) must both precede socket "
        "creation — see the Phase-7 startup-sequence comment in main.swift"
    )

    supervisor = (REPO / "src" / "onoats" / "cli.py").read_text(encoding="utf-8")
    assert 'event_type == "waiting-for-permission"' in supervisor, (
        "cli._drain_capturer_stderr no longer handles the "
        "waiting-for-permission event type"
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
    escaped = _swift_escape(value)
    doc = f'[storage]\ndata_dir = "{escaped}"\n'
    parsed = tomllib.loads(doc)
    assert parsed["storage"]["data_dir"] == value


def _swift_write_value(text: str, section: str, key: str, value: str) -> str:
    """Line-by-line Python model of ConfigStore.writeValue (Swift).

    Mirrors the Swift writer exactly — same line split (components on "\\n",
    which keeps a CRLF file's "\\r" attached to each line), same
    whitespace-and-newline trim for scanning, same escape rules, same
    replace/insert/append placement, same "\\n" join. Structural drift between
    this model and the Swift source is caught by
    test_configstore_writer_structure_matches_python_model below; behavioural
    drift is caught by the round-trip tests that use this model.
    """
    # Swift: escape() — backslash first, then double-quote.
    escaped = _swift_escape(value)
    # Swift: text.components(separatedBy: "\n") — \r stays on the line.
    lines = text.split("\n")
    new_line = f'{key} = "{escaped}"'

    current = ""
    section_header_idx: int | None = None
    key_idx: int | None = None
    for i, raw_line in enumerate(lines):
        # Swift: trimmingCharacters(in: .whitespacesAndNewlines) — the
        # CRLF-tolerant scan (a bare .whitespaces would leave "\r" on the
        # line and miss every "[section]\r" header).
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()  # tolerate hand-edited [ stt ]
            if current == section:
                section_header_idx = i
            continue
        if current != section or line.startswith("#"):
            continue
        eq = line.find("=")
        if eq != -1 and line[:eq].strip() == key:
            key_idx = i
            break  # Swift breaks on the FIRST in-section match

    if key_idx is not None:
        lines[key_idx] = new_line
    elif section_header_idx is not None:
        lines.insert(section_header_idx + 1, new_line)
    else:
        if text and lines[-1] != "":
            lines.append("")
        lines.append(f"[{section}]")
        lines.append(new_line)
        lines.append("")
    return "\n".join(lines)


def _assert_untouched_lines_byte_identical(
    before: str, after: str, touched_after_idx: int, inserted: bool = False
) -> None:
    """Every output line except the edited/inserted one must be byte-identical
    to its input counterpart — the writer's core promise (comments and
    formatting elsewhere survive verbatim, CRLF terminators included)."""
    before_lines = before.split("\n")
    after_lines = after.split("\n")
    if inserted:
        rest = after_lines[:touched_after_idx] + after_lines[touched_after_idx + 1 :]
        assert rest == before_lines
    else:
        assert len(after_lines) == len(before_lines)
        for i, (b, a) in enumerate(zip(before_lines, after_lines)):
            if i != touched_after_idx:
                assert a == b, f"untouched line {i} changed: {b!r} -> {a!r}"


def test_configstore_writer_structure_matches_python_model():
    """Greps pinning the Swift writer's structural shape to the Python model
    above: split on \\n, CRLF-tolerant trim in BOTH scan loops (readValue and
    writeValue), in-place replace, insert at header+1, join with \\n. A Swift
    refactor that breaks any of these invalidates every round-trip test below."""
    text = (
        REPO / "native" / "onoats-menubar" / "Sources" / "ConfigStore.swift"
    ).read_text(encoding="utf-8")
    assert text.count('components(separatedBy: "\\n")') == 2, (
        "line split changed — model splits on bare \\n with \\r kept on lines"
    )
    assert text.count("trimmingCharacters(in: .whitespacesAndNewlines)") == 2, (
        "both scan loops must trim with .whitespacesAndNewlines — bare "
        ".whitespaces excludes \\r, so a CRLF file's `[section]\\r` header is "
        "never recognised and writeValue appends a duplicate section that "
        "tomllib rejects (the Phase-9 CRLF divergence)"
    )
    assert re.search(r"lines\[\w+\] = \w+", text), "in-place replace gone"
    assert re.search(r"lines\.insert\(\w+, at: \w+ \+ 1\)", text), (
        "insert-at-header+1 gone"
    )
    assert 'joined(separator: "\\n")' in text, "join separator changed"


def test_configstore_write_preserves_comments_on_other_lines():
    """Whole-line comments and trailing comments on UNTOUCHED lines survive
    byte-identically; the edited value updates; the result still parses."""
    doc = (
        "# top-of-file comment\n"
        "[storage]\n"
        "# section comment\n"
        'data_dir = "/old"\n'
        'note = "keep"  # trailing comment on a neighbour\n'
    )
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["storage"]["data_dir"] == "/new"
    assert parsed["storage"]["note"] == "keep"
    _assert_untouched_lines_byte_identical(doc, out, touched_after_idx=3)


def test_configstore_write_drops_trailing_comment_on_edited_line_only():
    """The edited line is rewritten wholesale, so a trailing comment ON THAT
    LINE is dropped — the documented cost of a line editor. Every other line
    (including the other commented neighbour) is byte-identical, and the
    output parses."""
    doc = (
        "[storage]\n"
        'data_dir = "/old"  # chosen in the GUI\n'
        'note = "keep"  # untouched trailing comment\n'
    )
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["storage"]["data_dir"] == "/new"
    out_lines = out.split("\n")
    assert out_lines[1] == 'data_dir = "/new"'  # comment gone, by contract
    _assert_untouched_lines_byte_identical(doc, out, touched_after_idx=1)


def test_configstore_write_tolerates_whitespace_variants():
    """Hand-edited `[ storage ]` headers and padded `key   =   "v"` lines are
    still found; the replacement is canonical `key = "value"`; padded
    neighbours keep their padding byte-for-byte."""
    doc = '  [ storage ]  \n  data_dir   =   "/old"  \n\tnote =\t"keep"\n'
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["storage"]["data_dir"] == "/new"
    assert parsed["storage"]["note"] == "keep"
    assert out.split("\n")[1] == 'data_dir = "/new"'  # canonical form
    _assert_untouched_lines_byte_identical(doc, out, touched_after_idx=1)


def test_configstore_write_absent_section_appends_at_end():
    """Missing section: a blank separator + `[section]` + the key line are
    appended; the original document is a byte-identical prefix; the result
    parses with both tables intact."""
    doc = '[stt]\nservice = "whisper"'  # no trailing newline, exercises padding
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["stt"]["service"] == "whisper"
    assert parsed["storage"]["data_dir"] == "/new"
    assert out.startswith(doc), "original bytes must be an untouched prefix"
    # The writer pads a blank separator line before the appended section.
    assert out == doc + '\n\n[storage]\ndata_dir = "/new"\n'


def test_configstore_write_absent_key_inserts_after_header():
    """Section exists, key doesn't: the new line lands directly under the
    header; every original line is byte-identical; the result parses."""
    doc = "[storage]\n# keep me adjacent to the header\nother = 3\n"
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["storage"]["data_dir"] == "/new"
    assert parsed["storage"]["other"] == 3
    assert out.split("\n")[1] == 'data_dir = "/new"'
    _assert_untouched_lines_byte_identical(doc, out, touched_after_idx=1, inserted=True)


def test_configstore_write_same_key_name_in_another_section_untouched():
    """Key matching is section-scoped: the same key name in a different
    section is not the writer's target and stays byte-identical (this is the
    VALID-toml duplicate-key-name case; in-section duplicates are below)."""
    doc = '[stt]\ndata_dir = "/stt-scratch"\n\n[storage]\ndata_dir = "/old"\n'
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["storage"]["data_dir"] == "/new"
    assert parsed["stt"]["data_dir"] == "/stt-scratch"
    _assert_untouched_lines_byte_identical(doc, out, touched_after_idx=4)


def test_configstore_write_duplicate_key_in_section_replaces_first_only():
    """In-section duplicate keys are INVALID TOML on the way in (tomllib
    rejects the input), so there is no parse contract to keep — the writer's
    actual behaviour, modelled from the Swift `break` on first match, is:
    replace the first occurrence, leave the second byte-identical. The output
    is still duplicate-keyed (the writer never repairs a broken file), so
    tomllib must reject it too — garbage in, the same garbage shape out."""
    doc = '[storage]\ndata_dir = "/first"\ndata_dir = "/second"\n'
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(doc)  # the input was never valid
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    out_lines = out.split("\n")
    assert out_lines[1] == 'data_dir = "/new"'
    assert out_lines[2] == 'data_dir = "/second"'  # second occurrence untouched
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(out)


def test_configstore_write_preserves_non_string_neighbours():
    """Non-string scalars adjacent to the edited key (outside the writer's
    subset, but legal tomllib input) pass through byte-identically with their
    types intact — the writer only ever rewrites its one target line."""
    doc = '[storage]\nmax_files = 5\ndata_dir = "/old"\ncompress = true\nratio = 0.5\n'
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["storage"] == {
        "max_files": 5,
        "data_dir": "/new",
        "compress": True,
        "ratio": 0.5,
    }
    _assert_untouched_lines_byte_identical(doc, out, touched_after_idx=2)


def test_configstore_write_untouched_line_byte_identity_across_sections():
    """Belt-and-braces sweep: a document mixing comments, blank lines,
    padding, and multiple sections — every byte outside the single edited
    line survives, and the result round-trips through tomllib."""
    doc = (
        "# onoats config\n"
        "\n"
        "[stt]\n"
        'service = "whisper"  # picked in the menu\n'
        "\n"
        "  [ storage ]\n"
        "# data lives here\n"
        'data_dir = "/old"\n'
        "max_files = 5\n"
        "\n"
        "[devices]\n"
        'mic = "Built-in"\n'
    )
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["storage"]["data_dir"] == "/new"
    assert parsed["stt"]["service"] == "whisper"
    assert parsed["devices"]["mic"] == "Built-in"
    _assert_untouched_lines_byte_identical(doc, out, touched_after_idx=7)


def test_configstore_write_crlf_untouched_lines_verbatim_edited_line_lf():
    """CRLF contract (pinned in the release plan): untouched lines keep their
    original bytes — CRLF terminators verbatim — while the edited line is
    written with LF, and the mixed-ending result still parses under tomllib.
    Before the Phase-9 fix, the Swift scan trimmed with .whitespaces (which
    excludes \\r), so `[storage]\\r` was never seen as a header and writeValue
    appended a DUPLICATE [storage] section that tomllib rejected."""
    doc = '# saved on Windows\r\n[storage]\r\ndata_dir = "/old"\r\nnote = "keep"\r\n'
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["storage"]["data_dir"] == "/new"
    assert parsed["storage"]["note"] == "keep"
    out_lines = out.split("\n")
    assert out_lines[0] == "# saved on Windows\r"  # CRLF verbatim
    assert out_lines[1] == "[storage]\r"  # CRLF verbatim
    assert out_lines[2] == 'data_dir = "/new"'  # edited line: LF, no \r
    assert out_lines[3] == 'note = "keep"\r'  # CRLF verbatim
    assert "[storage]" in out and out.count("[storage]") == 1, (
        "a second [storage] header means the CRLF header was not recognised "
        "— the pre-fix divergence"
    )


def test_configstore_write_crlf_absent_key_inserts_lf_line():
    """Same CRLF contract on the insert path: the new key lands under the
    CRLF header as an LF-terminated line; all original CRLF lines verbatim."""
    doc = "[storage]\r\nmax_files = 5\r\n"
    out = _swift_write_value(doc, "storage", "data_dir", "/new")
    parsed = tomllib.loads(out)
    assert parsed["storage"]["data_dir"] == "/new"
    assert parsed["storage"]["max_files"] == 5
    out_lines = out.split("\n")
    assert out_lines[0] == "[storage]\r"
    assert out_lines[1] == 'data_dir = "/new"'
    assert out_lines[2] == "max_files = 5\r"
    _assert_untouched_lines_byte_identical(doc, out, touched_after_idx=1, inserted=True)
