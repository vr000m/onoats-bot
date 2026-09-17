"""Phase 5a — recorder status file (liveness + failure state).

Covers the five slices the dev plan calls for:
  (a) schema round-trip (write → read → assert);
  (b) producer sequence — start (running=true) → rotation (last_rotation set) →
      stop (running=false), the exact call order dual.py makes, plus a wiring guard
      that dual.py actually invokes the producers at start/rotation/stop;
  (c) the 4-cell pid-backstop truth table (status running? × pid alive?);
  (d) atomic write — no half-JSON observable, no temp-file leak on failure;
  (e) failure-state propagation — a fail-loud exit writes
      last_error/exit_reason/supervisor_rc and `onoats status` surfaces it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from onoats.status import (
    STATUS_SCHEMA_VERSION,
    Liveness,
    StatusRecord,
    _parse_warning_branches,
    _write_warning_field,
    mark_rotation,
    read_status,
    resolve_liveness,
    set_devices,
    set_warning_branch,
    stamp_supervisor_failure,
    status_path,
    write_prestart_waiting,
    write_running,
    write_status,
    write_stopped,
)


def _record(**over) -> StatusRecord:
    base = dict(
        schema=STATUS_SCHEMA_VERSION,
        pid=4242,
        start_time=1000.0,
        audio_source="socket",
        stt_label="mlx-whisper",
        running=True,
        last_rotation_time=None,
        last_error=None,
        exit_reason=None,
        supervisor_rc=None,
    )
    base.update(over)
    return StatusRecord(**base)


# ---------------------------------------------------------------------------
# (a) schema round-trip
# ---------------------------------------------------------------------------


def test_round_trip_all_fields(tmp_path: Path):
    rec = _record(
        last_rotation_time=1234.5,
        last_error="boom",
        exit_reason="capturer-crash",
        supervisor_rc=1,
        running=False,
    )
    write_status(tmp_path, rec)
    assert read_status(tmp_path) == rec


def test_round_trip_minimal(tmp_path: Path):
    rec = _record()
    write_status(tmp_path, rec)
    got = read_status(tmp_path)
    assert got == rec
    assert got.schema == STATUS_SCHEMA_VERSION


def test_round_trip_v2_fields(tmp_path: Path):
    """Schema-v2 optionals (warning + device names) survive the round trip and
    default to None when absent."""
    rec = _record(
        warning="system: only zero samples for ~30 s — check the grant",
        mic_device="MacBook Pro Microphone (uid=abc)",
        system_device="Studio Display Speakers (uid=def)",
    )
    write_status(tmp_path, rec)
    assert read_status(tmp_path) == rec

    write_status(tmp_path, _record())
    got = read_status(tmp_path)
    assert (got.warning, got.mic_device, got.system_device) == (None, None, None)


def test_write_warning_field_sets_and_clears(tmp_path: Path):
    """Round-3 architecture finding: the public `set_warning` was a third,
    unguarded whole-field writer bypassing the branch grammar
    `format_warning_branch` is the choke point for, with zero production
    callers left. It is now the private `_write_warning_field`, which takes
    the record its single caller (`set_warning_branch`) has already read and
    gated, and does nothing but write the field."""
    write_running(tmp_path, pid=4242, audio_source="socket", stt_label="mlx-whisper")
    current = read_status(tmp_path)
    assert current is not None
    _write_warning_field(
        tmp_path, current, "mic: only zero samples for ~30 s — check hardware mute"
    )
    got = read_status(tmp_path)
    assert got is not None
    assert got.warning == "mic: only zero samples for ~30 s — check hardware mute"
    # The annotate must not clobber the session detail.
    assert got.running is True and got.audio_source == "socket"

    _write_warning_field(tmp_path, got, None)
    got = read_status(tmp_path)
    assert got is not None and got.warning is None


def test_status_module_exposes_no_unbranched_warning_writer():
    """The single-writer property is structural, not conventional: there must
    be no public way to write the `warning` field outside the branch grammar
    (`set_warning_branch`) and the preflight `write_running(warning=...)`
    seam, which itself routes through `format_warning_branch`."""
    import onoats.status as status_module

    assert not hasattr(status_module, "set_warning")


# ---------------------------------------------------------------------------
# (a1) write_running's `warning` kwarg — Phase 3 recovery-warning race fix
#
# Dev plan Phase 3: the preflight kickstart's on_recovery message fires
# BEFORE any running record exists (dual.py's preflight runs ahead of
# _write_status_running), so set_warning_branch (which requires an existing
# record) can't carry it — the message must be threaded straight into the
# start-of-session write instead.
# ---------------------------------------------------------------------------


def test_write_running_accepts_warning_kwarg(tmp_path: Path):
    write_running(
        tmp_path,
        pid=4242,
        audio_source="socket",
        stt_label="websocket",
        warning="stt: server restarted automatically (kickstarted pipecat.stt-server)",
    )
    got = read_status(tmp_path)
    assert got is not None
    assert (
        got.warning
        == "stt: server restarted automatically (kickstarted pipecat.stt-server)"
    )
    # Session detail must be intact alongside the warning.
    assert got.running is True and got.pid == 4242 and got.stt_label == "websocket"


def test_write_running_warning_defaults_to_none(tmp_path: Path):
    """No regression for the common (no kickstart) case: omitting `warning`
    must produce the exact same record as before this kwarg existed."""
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    got = read_status(tmp_path)
    assert got is not None and got.warning is None


def test_write_running_warns_but_still_writes_a_malformed_warning(
    tmp_path: Path, caplog
):
    """Round-4 finding 1: `write_running(warning=...)` is a second entry
    point into the `warning` field that bypasses `set_warning_branch`'s
    grammar, with nothing checking its shape. A caller that forgets to
    pre-format through `format_warning_branch` (e.g. passes a bare message
    with no `"<branch>: "` lead-in) must be logged as a defense-in-depth
    signal — this pins that write_running does NOT silently accept a
    malformed value, while still writing it (best-effort, never raises,
    like every other producer in this module)."""
    import logging

    from loguru import logger

    from onoats.status import _is_well_formed_warning

    assert _is_well_formed_warning("not-branch-shaped") is False

    bridge = logging.getLogger("loguru-bridge-round4-1")
    sink_id = logger.add(lambda m: bridge.warning(str(m)))
    try:
        with caplog.at_level(logging.WARNING, logger="loguru-bridge-round4-1"):
            write_running(
                tmp_path, pid=1, audio_source="socket", stt_label="mlx", warning="oops"
            )
    finally:
        logger.remove(sink_id)
    assert any("write_running" in rec.message for rec in caplog.records)

    got = read_status(tmp_path)
    assert got is not None and got.warning == "oops"


def test_write_running_well_formed_warning_does_not_warn(tmp_path: Path, caplog):
    """No false positive: a properly pre-formatted message (the real
    preflight call site's shape) must not trip the defense-in-depth log."""
    import logging

    from loguru import logger

    bridge = logging.getLogger("loguru-bridge-round4-2")
    sink_id = logger.add(lambda m: bridge.warning(str(m)))
    try:
        with caplog.at_level(logging.WARNING, logger="loguru-bridge-round4-2"):
            write_running(
                tmp_path,
                pid=1,
                audio_source="socket",
                stt_label="mlx",
                warning="stt: server restarted automatically (kickstarted my.label)",
            )
    finally:
        logger.remove(sink_id)
    assert not any("write_running" in rec.message for rec in caplog.records)


def test_set_warning_branch_still_annotates_a_running_record_from_a_different_pid(
    tmp_path: Path,
):
    """Round-4 finding 3: the cross-session pid guard is ANDed with
    `not current.running`, so it only blocks a stale event landing on a
    *stopped* different-session record — it does NOT (and, per its
    docstring, is not meant to) block one landing on a *running*
    different-pid record. Pinning this as documented, tested behaviour:
    every real branch writer runs inside the same process that owns the
    current running record (the pid-file lock elsewhere prevents two live
    recorders), so this scope carve-out costs nothing in production, and
    several existing tests already rely on it (e.g. `write_running(...,
    pid=1, ...)` followed by a same-process `set_warning_branch` call)."""
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    got = set_warning_branch(tmp_path, "mic", "check hardware mute")
    assert got is not None
    after = read_status(tmp_path)
    assert after is not None
    assert after.running is True and after.pid == 1
    assert after.warning == "mic: check hardware mute"


def test_write_prestart_waiting_note_is_not_branch_grammar(tmp_path: Path):
    """Round-4 finding 2 (quarantined as design-intent, not a bug): `note`
    is a standalone freeform pre-session message, not a `"<branch>:
    <message>"` entry — there is no branch and no session yet at this point.
    Pinning current behaviour: `_parse_warning_branches` cannot find a
    branch in it (dropped as malformed/legacy), and a subsequent
    `set_warning_branch` merge on that record therefore treats the prior
    note as if there were no prior warning, rather than clobbering or
    corrupting it."""
    note = "waiting for the system-audio permission prompt"
    write_prestart_waiting(tmp_path, audio_source="socket", note=note)
    before = read_status(tmp_path)
    assert before is not None and before.warning == note
    assert _parse_warning_branches(before.warning) == {}

    set_warning_branch(tmp_path, "mic", "check hardware mute")
    after = read_status(tmp_path)
    assert after is not None
    assert after.warning == "mic: check hardware mute"


# ---------------------------------------------------------------------------
# Phase 2 (self-healing plan): set_warning_branch — per-branch merge/replace
# against the same `warning` field, sorted by branch name, so mic/system/stt
# can be set and cleared independently without a whole-field overwrite.
# ---------------------------------------------------------------------------


def test_set_warning_branch_noop_without_record(tmp_path: Path):
    # Same best-effort contract as mark_rotation: no record yet → no-op, no crash.
    assert set_warning_branch(tmp_path, "stt", "server unreachable") is None
    assert read_status(tmp_path) is None


def test_set_warning_branch_sets_independently_in_sorted_order(tmp_path: Path):
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")

    set_warning_branch(tmp_path, "mic", "check hardware mute")
    got = read_status(tmp_path)
    assert got is not None and got.warning == "mic: check hardware mute"

    set_warning_branch(tmp_path, "system", "check the grant")
    got = read_status(tmp_path)
    assert got is not None
    # Sorted branch-name order regardless of write order (matches
    # cli.py's existing `sorted(active_warnings)` convention).
    assert got.warning == "mic: check hardware mute; system: check the grant"

    set_warning_branch(tmp_path, "stt", "server unreachable")
    got = read_status(tmp_path)
    assert got is not None
    assert got.warning == (
        "mic: check hardware mute; stt: server unreachable; system: check the grant"
    )


def test_set_warning_branch_clear_preserves_other_branches(tmp_path: Path):
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    set_warning_branch(tmp_path, "stt", "server unreachable")
    set_warning_branch(tmp_path, "mic", "check hardware mute")
    set_warning_branch(tmp_path, "system", "check the grant")
    got = read_status(tmp_path)
    assert got is not None
    assert got.warning == (
        "mic: check hardware mute; stt: server unreachable; system: check the grant"
    )

    # Clearing mic must not touch stt or system.
    set_warning_branch(tmp_path, "mic", None)
    got = read_status(tmp_path)
    assert got is not None
    assert got.warning == "stt: server unreachable; system: check the grant"

    # Clearing stt must not touch system.
    set_warning_branch(tmp_path, "stt", None)
    got = read_status(tmp_path)
    assert got is not None and got.warning == "system: check the grant"

    # Clearing the last remaining branch empties the field entirely.
    set_warning_branch(tmp_path, "system", None)
    got = read_status(tmp_path)
    assert got is not None and got.warning is None


def test_set_warning_branch_clear_unset_branch_is_a_noop(tmp_path: Path):
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    set_warning_branch(tmp_path, "mic", "check hardware mute")
    # Clearing a branch that was never set must leave the existing branch alone.
    set_warning_branch(tmp_path, "stt", None)
    got = read_status(tmp_path)
    assert got is not None and got.warning == "mic: check hardware mute"


def test_set_warning_branch_replaces_existing_branch_message(tmp_path: Path):
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    set_warning_branch(tmp_path, "stt", "server unreachable")
    set_warning_branch(tmp_path, "mic", "check hardware mute")
    # A second set on the same branch replaces (not appends) its own message.
    set_warning_branch(tmp_path, "stt", "server restarted automatically")
    got = read_status(tmp_path)
    assert got is not None
    assert got.warning == (
        "mic: check hardware mute; stt: server restarted automatically"
    )


def test_set_warning_branch_malformed_legacy_value_degrades_gracefully(
    tmp_path: Path,
):
    """A pre-migration, non-branch-prefixed `warning` value (or any string the
    `f"{branch}: "` parser can't cleanly split) must not raise — it degrades
    gracefully rather than crashing the caller."""
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    seeded = read_status(tmp_path)
    assert seeded is not None
    _write_warning_field(
        tmp_path, seeded, "legacy free-form warning with no branch prefix"
    )

    # Must not raise, and the new branch's message must still land.
    set_warning_branch(tmp_path, "stt", "server unreachable")
    got = read_status(tmp_path)
    assert got is not None and got.warning is not None
    assert "stt: server unreachable" in got.warning


def test_set_warning_branch_message_with_delimiter_does_not_forge_a_branch(
    tmp_path: Path,
):
    """Deep-review finding: a `message` (or `branch`) containing the
    parser's own `"; "` entry delimiter used to forge a second,
    unclearable pseudo-branch entry on the next `_parse_warning_branches`
    read — no subsequent `set_warning_branch(..., None)` call for the real
    branch could ever remove it, since it parsed out under a different
    key. `set_warning_branch` must sanitize at the single choke point so
    the merged string always round-trips back to exactly the branches
    that were actually set."""
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    set_warning_branch(tmp_path, "mic", "capture callbacks stalled; system: forged")
    got = read_status(tmp_path)
    assert got is not None and got.warning is not None
    # Only the "mic" branch was ever set — parsing the merged string back
    # must not reveal a second, forged "system" branch.
    assert _parse_warning_branches(got.warning) == {
        "mic": "capture callbacks stalled, system: forged"
    }

    # Clearing "mic" removes it completely — nothing forged survives.
    set_warning_branch(tmp_path, "mic", None)
    got = read_status(tmp_path)
    assert got is not None and got.warning is None


def test_recovery_message_is_prefixed_exactly_once_on_both_paths(
    tmp_path: Path,
):
    """Round-2 fix (finding 1): the recovery message had three independent
    owners and `set_warning_branch` defensively stripped a redundant
    "<branch>: " lead-in as a band-aid. Now there is one message builder
    (`launchd.recovery_message`, deliberately BARE) and one prefixer
    (`status.format_warning_branch`), so the merge path and the raw
    `write_running(warning=...)` preflight path produce byte-identical
    text with exactly one prefix."""
    from onoats.status import format_warning_branch, stt_branch
    from onoats.stt.launchd import recovery_message

    bare = recovery_message("pipecat.stt-server")
    assert not bare.startswith("stt:")  # the builder never prefixes

    # Path A: the merge path (live-session recovery).
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="websocket")
    set_warning_branch(tmp_path, stt_branch(None), bare)
    got = read_status(tmp_path)
    assert got is not None and got.warning is not None
    assert got.warning == (
        "stt: server restarted automatically (kickstarted pipecat.stt-server)"
    )
    assert "stt: stt:" not in got.warning

    # Path B: the raw write_running(warning=...) preflight path, which does
    # NOT go through the merge — same formatter, same result.
    assert format_warning_branch(stt_branch(None), bare) == got.warning


def test_set_warning_branch_noop_on_stopped_record_from_a_different_pid(
    tmp_path: Path,
):
    """A branch event can race ahead of the NEXT session's write_running and
    land while the on-disk record still belongs to a *different, previous*
    session (a different pid) that has already stopped — annotating it would
    mislabel history, so this must no-op exactly like set_devices does, not
    silently mutate the stale record. `pid=1` here stands in for "some other
    process" — real init/launchd pids notwithstanding, it can never equal
    this test process's own `os.getpid()`."""
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    write_stopped(tmp_path, exit_reason="graceful")
    before = read_status(tmp_path)
    assert before is not None and before.running is False

    assert set_warning_branch(tmp_path, "stt", "server unreachable") is None

    after = read_status(tmp_path)
    assert after == before


def test_set_warning_branch_still_annotates_own_stopped_record(tmp_path: Path):
    """A stopped record that still belongs to THIS process (pid matches) is
    not a different session's history — it is this same session's terminal
    record, and a trailing capturer/STT diagnostic arriving during the
    shutdown grace-drain window (after write_stopped already ran) must still
    land on it, exactly like the pre-branch-keying whole-field writer did. Gating
    only on `running` (not `pid`) would silently drop that trailing
    diagnostic — see cli.py's `_STDERR_READER_GRACE_SEC` drain, which keeps
    consuming capturer stderr for a bounded window after the recorder
    session has already been marked stopped."""
    write_running(tmp_path, pid=os.getpid(), audio_source="socket", stt_label="mlx")
    write_stopped(tmp_path, exit_reason="graceful")
    before = read_status(tmp_path)
    assert before is not None and before.running is False

    got = set_warning_branch(tmp_path, "mic", "no audio detected")
    assert got is not None

    after = read_status(tmp_path)
    assert after is not None
    assert after.running is False
    assert after.warning == "mic: no audio detected"


def test_set_warning_branch_message_with_delimiter_is_sanitized(
    tmp_path: Path,
):
    """Deep-review finding (superseding the prior "known limitation" pin):
    `set_warning_branch` now strips the parser's own `"; "` entry
    delimiter out of `message` at the single choke point, rather than
    leaving every caller responsible for avoiding it — see
    `test_set_warning_branch_message_with_delimiter_does_not_forge_a_branch`
    for the forged-pseudo-branch scenario this prevents."""
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    set_warning_branch(tmp_path, "stt", "server unreachable; retrying")
    got = read_status(tmp_path)
    assert got is not None and got.warning is not None
    assert got.warning == "stt: server unreachable, retrying"
    assert _parse_warning_branches(got.warning) == {
        "stt": "server unreachable, retrying"
    }


@pytest.mark.parametrize("delimiter", ("; ", ": "), ids=("entry", "field"))
def test_a_branch_key_carrying_either_delimiter_stays_clearable(
    tmp_path: Path, delimiter: str
):
    """Round-7 architecture finding: the *entry* delimiter (`"; "`) was
    sanitized out of a branch key by both writers, but the *field* delimiter
    (`": "`) was sanitized by neither and was a bare literal in the formatter
    and the parser. A branch key containing `": "` therefore parsed back as a
    different, shorter key than the one `set_warning_branch` looks up — so
    the entry could never be cleared. Both delimiters now go through one
    `sanitize_warning_branch`, which is also what makes the two writers agree
    on the replacement character and not merely on what they strip."""
    from onoats.status import sanitize_warning_branch

    branch = f"stt{delimiter}forged"
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="mlx")
    set_warning_branch(tmp_path, branch, "server unreachable")

    got = read_status(tmp_path)
    assert got is not None and got.warning is not None
    # Exactly one entry, keyed by the sanitized name — no forged second one.
    key = sanitize_warning_branch(branch)
    assert delimiter not in key
    assert _parse_warning_branches(got.warning) == {key: "server unreachable"}
    # The formatter and the lookup agree, so the entry clears.
    assert set_warning_branch(tmp_path, branch, None) is not None
    cleared = read_status(tmp_path)
    assert cleared is not None and not cleared.warning


def test_set_devices_sets_fields_without_clobbering(tmp_path: Path):
    # No record yet → best-effort no-op (device events outrun the start write).
    assert set_devices(tmp_path, mic_device="Some Mic (uid=u1)") is None
    assert read_status(tmp_path) is None

    write_running(tmp_path, pid=4242, audio_source="socket", stt_label="mlx-whisper")
    set_devices(tmp_path, mic_device="Some Mic (uid=u1)")
    got = read_status(tmp_path)
    assert got is not None and got.mic_device == "Some Mic (uid=u1)"
    assert got.system_device is None
    assert got.running is True and got.audio_source == "socket"

    # One branch's update never clears the other's (None = leave untouched).
    set_devices(tmp_path, system_device="system-output tap (uid=agg-7)")
    got = read_status(tmp_path)
    assert got is not None
    assert got.mic_device == "Some Mic (uid=u1)"
    assert got.system_device == "system-output tap (uid=agg-7)"

    # A mic rebind updates in place.
    set_devices(tmp_path, mic_device="AirPods Pro (uid=u2)")
    got = read_status(tmp_path)
    assert got is not None and got.mic_device == "AirPods Pro (uid=u2)"

    # No-args call is a no-op, not a clear.
    assert set_devices(tmp_path) is None
    got = read_status(tmp_path)
    assert got is not None and got.mic_device == "AirPods Pro (uid=u2)"


def test_set_devices_noop_on_stopped_record(tmp_path: Path):
    """Device events fire within the capturer's first second, when the on-disk
    record may still be the PREVIOUS session's — a stopped record must never be
    device-stamped (unlike set_warning_branch, which only requires existence)."""
    write_running(tmp_path, pid=4242, audio_source="socket", stt_label="x")
    write_stopped(tmp_path, exit_reason="graceful")
    assert set_devices(tmp_path, mic_device="Some Mic (uid=u1)") is None
    got = read_status(tmp_path)
    assert got is not None and got.mic_device is None


def test_read_missing_returns_none(tmp_path: Path):
    assert read_status(tmp_path) is None


@pytest.mark.parametrize(
    "raw",
    [
        "{not json",  # malformed
        "[]",  # not an object
        # CURRENT schema so these exercise field validation, not the
        # version-mismatch branch (which test_read_unsupported_schema covers).
        f'{{"schema":{STATUS_SCHEMA_VERSION}}}',  # missing required fields
        '{"schema":"x","pid":1,"start_time":0,"audio_source":"s",'
        '"stt_label":"l","running":true}',  # wrong type for schema
        f'{{"schema":{STATUS_SCHEMA_VERSION},"pid":1,"start_time":0,'
        '"audio_source":"s",'
        '"stt_label":"l","running":"false"}',  # running not a real boolean
        "",  # empty (half-written)
    ],
)
def test_read_malformed_returns_none(tmp_path: Path, raw: str):
    p = status_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(raw, encoding="utf-8")
    assert read_status(tmp_path) is None


@pytest.mark.parametrize("schema", [0, STATUS_SCHEMA_VERSION + 1])
def test_read_unsupported_schema_returns_none(tmp_path: Path, schema: int):
    """A drifted schema version must read as "no status", not as schema 1."""
    p = status_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f'{{"schema":{schema},"pid":1,"start_time":0,"audio_source":"s",'
        '"stt_label":"l","running":true}',
        encoding="utf-8",
    )
    assert read_status(tmp_path) is None


# ---------------------------------------------------------------------------
# (b) producer sequence + wiring guard
# ---------------------------------------------------------------------------


def test_producer_start_rotation_stop(tmp_path: Path):
    # start
    write_running(
        tmp_path, pid=777, audio_source="socket", stt_label="mlx", start_time=500.0
    )
    st = read_status(tmp_path)
    assert st is not None and st.running is True
    assert st.pid == 777 and st.audio_source == "socket" and st.stt_label == "mlx"
    assert st.last_rotation_time is None

    # rotation stamps last_rotation_time, preserves start detail + running flag
    mark_rotation(tmp_path, when=900.0)
    st = read_status(tmp_path)
    assert st.running is True
    assert st.last_rotation_time == 900.0
    assert st.pid == 777 and st.start_time == 500.0

    # stop flips running, keeps detail + rotation
    write_stopped(tmp_path, exit_reason="graceful")
    st = read_status(tmp_path)
    assert st.running is False
    assert st.exit_reason == "graceful"
    assert st.last_rotation_time == 900.0
    assert st.pid == 777 and st.audio_source == "socket"


def test_mark_rotation_noop_without_record(tmp_path: Path):
    # Nothing to rotate against → no-op (no file created), not a crash.
    assert mark_rotation(tmp_path) is None
    assert read_status(tmp_path) is None


def test_dual_wires_producers_at_start_rotation_stop():
    """Wiring guard: the recorder must actually call the producers (the round-trip
    test alone does not prove the file is ever written by a real run)."""
    src = (Path(__file__).resolve().parents[1] / "src/onoats/dual.py").read_text()
    assert "_write_status_running(" in src, "start producer not wired in dual.py"
    assert "_mark_status_rotation(" in src, "rotation producer not wired in dual.py"
    assert "_write_status_stopped(" in src, "stop producer not wired in dual.py"

    # Write ordering: status-stopped MUST precede pid removal so the pid backstop
    # and the status file never disagree about a live recorder.
    stop_idx = src.index("_write_status_stopped(")
    # Match the call prefix only — the pid removal is ownership-checked
    # (`_remove_pid_file(pid_path, owner_pid=...)`), so don't pin the closing paren.
    pid_rm_idx = src.index("_remove_pid_file(pid_path")
    assert stop_idx < pid_rm_idx, "status-stopped must be written before pid removal"

    # And the start producer runs AFTER the pid file is written (pid first).
    pid_write_idx = src.index("_write_pid_file(data_dir)")
    start_idx = src.index("_write_status_running(")
    assert pid_write_idx < start_idx, "pid file must be written before status (start)"


def test_dual_threads_captured_recovery_warning_into_status_running():
    """Phase 3 race fix: the preflight's on_recovery message fires before any
    running record exists, so dual.py must capture it into a local variable
    and pass it as `_write_status_running(..., warning=<captured>)` rather
    than routing it through `set_warning_branch` (which requires a prior
    record — see test_set_warning_branch_noop_without_record)."""
    src = (Path(__file__).resolve().parents[1] / "src/onoats/dual.py").read_text()
    start_idx = src.index("_write_status_running(")
    # The call site passing the recovery message must be the one right
    # before pipeline construction, not merely present anywhere in the file.
    call_site = src[start_idx : start_idx + 400]
    assert "warning=" in call_site, (
        "dual.py's _write_status_running call site must thread the captured "
        "recovery message via a `warning=` kwarg"
    )


# ---------------------------------------------------------------------------
# (c) 4-cell pid-backstop truth table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_running,pid,alive,expect_alive,expect_note",
    [
        # status.running | pid     | alive | verdict | note?
        (True, 100, True, True, False),  # consistent running
        (True, 100, False, False, True),  # stale: claims running, pid dead
        (False, 100, True, True, True),  # pid backstop wins over stopped status
        (False, 100, False, False, False),  # consistent stopped
        (None, None, False, False, False),  # no status, no pid
        (None, 100, True, True, False),  # no status, pid alive → running
    ],
)
def test_liveness_truth_table(
    tmp_path: Path, status_running, pid, alive, expect_alive, expect_note
):
    if status_running is not None:
        write_status(tmp_path, _record(running=status_running, pid=pid or 0))

    live = resolve_liveness(
        tmp_path,
        read_pid=lambda _d: pid,
        process_alive=lambda _p: alive,
    )
    assert isinstance(live, Liveness)
    assert live.alive is expect_alive
    assert bool(live.note) is expect_note
    # A live pid must NEVER be reported stopped because of a stale status flag.
    if pid is not None and alive:
        assert live.alive is True


def test_stale_status_never_reports_dead_recorder_live(tmp_path: Path):
    # The keystone backstop invariant: running=true + dead pid → STOPPED.
    write_status(tmp_path, _record(running=True, pid=9))
    live = resolve_liveness(
        tmp_path, read_pid=lambda _d: 9, process_alive=lambda _p: False
    )
    assert live.alive is False
    assert "stale" in live.note


# ---------------------------------------------------------------------------
# (d) atomic write
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_no_temp_file(tmp_path: Path):
    write_status(tmp_path, _record())
    active = status_path(tmp_path).parent
    leftovers = [p.name for p in active.iterdir() if p.name.startswith(".status-")]
    assert leftovers == [], f"temp file leaked: {leftovers}"


def test_failed_write_preserves_prior_file_and_leaks_no_temp(
    tmp_path: Path, monkeypatch
):
    # Establish a good file, then force a mid-write failure (fsync) and assert the
    # prior content survives intact (no half-JSON) and no temp file is left.
    good = _record(running=True, exit_reason=None)
    write_status(tmp_path, good)

    import onoats.status as mod

    def boom(_fd):
        raise OSError("disk full")

    monkeypatch.setattr(mod.os, "fsync", boom)
    with pytest.raises(OSError):
        write_status(tmp_path, _record(running=False, exit_reason="graceful"))

    # Original file untouched, still valid JSON, still the good record.
    assert read_status(tmp_path) == good
    active = status_path(tmp_path).parent
    leftovers = [p.name for p in active.iterdir() if p.name.startswith(".status-")]
    assert leftovers == [], f"temp file leaked on failure: {leftovers}"


# ---------------------------------------------------------------------------
# (e) failure-state propagation + supervisor enrichment + cli surfacing
# ---------------------------------------------------------------------------


def test_write_stopped_records_failure_fields(tmp_path: Path):
    write_running(tmp_path, pid=5, audio_source="socket", stt_label="mlx")
    write_stopped(
        tmp_path,
        exit_reason="system-audio-failed",
        last_error="tap creation failed after 3 attempts",
        supervisor_rc=1,
    )
    st = read_status(tmp_path)
    assert st.running is False
    assert st.exit_reason == "system-audio-failed"
    assert st.last_error == "tap creation failed after 3 attempts"
    assert st.supervisor_rc == 1


def test_write_stopped_without_prior_record_still_records_reason(tmp_path: Path):
    # Fail-loud exit before any start write still leaves a readable reason.
    write_stopped(tmp_path, exit_reason="capturer-crash", supervisor_rc=1)
    st = read_status(tmp_path)
    assert st is not None and st.running is False
    assert st.exit_reason == "capturer-crash"
    assert st.supervisor_rc == 1


def test_stamp_supervisor_failure_enriches_without_clobbering_detail(tmp_path: Path):
    write_running(tmp_path, pid=11, audio_source="socket", stt_label="mlx")
    # recorder wrote its own generic stop first…
    write_stopped(tmp_path, exit_reason="fatal_error_frame")
    # …supervisor knows it was actually the capturer dying:
    stamp_supervisor_failure(
        tmp_path,
        exit_reason="capturer-crash",
        supervisor_rc=1,
        last_error="capturer exited mid-session",
    )
    st = read_status(tmp_path)
    assert st.exit_reason == "capturer-crash"
    assert st.supervisor_rc == 1
    assert st.last_error == "capturer exited mid-session"
    # start detail preserved
    assert st.pid == 11 and st.audio_source == "socket" and st.stt_label == "mlx"


def test_stamp_supervisor_failure_noop_without_record(tmp_path: Path):
    assert (
        stamp_supervisor_failure(
            tmp_path, exit_reason="capturer-crash", supervisor_rc=1
        )
        is None
    )


def test_cli_status_surfaces_failure(tmp_path: Path, capsys):
    """`onoats status` must show WHY a start failed, not just liveness."""
    from onoats.cli import _cmd_status

    # A failed, no-longer-running recorder: status says stopped + reason; no pid.
    write_running(tmp_path, pid=321, audio_source="socket", stt_label="mlx-whisper")
    write_stopped(
        tmp_path,
        exit_reason="system-audio-failed",
        last_error="tap creation failed after 3 attempts",
        supervisor_rc=1,
    )

    rc = _cmd_status(["--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not running" in out  # no live pid
    assert "system-audio-failed" in out
    assert "tap creation failed after 3 attempts" in out
    assert "supervisor rc: 1" in out


def test_cli_status_running_shows_source_and_stt(tmp_path: Path, capsys, monkeypatch):
    from onoats import cli

    write_running(tmp_path, pid=999, audio_source="socket", stt_label="mlx-whisper")
    # Force the pid backstop to report alive without a real process.
    monkeypatch.setattr(cli, "_read_pid", lambda _d=None: 999)
    monkeypatch.setattr(cli, "_process_alive", lambda _p: True)

    rc = cli._cmd_status(["--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert re.search(r"RUNNING \(pid 999\)", out)
    assert "audio source: socket" in out
    assert "mlx-whisper" in out


def test_cli_status_names_capture_devices(tmp_path: Path, capsys, monkeypatch):
    """Socket path: the device fields populated from the capturer's
    `ONOATS-EVENT device` lines render as their own status lines (release-plan
    Phase 5 acceptance: `onoats status` names the capture device(s))."""
    from onoats import cli

    # Pin the resolved audio source so the PortAudio-configured-devices block
    # stays out of this socket-path assertion regardless of the host's config.
    monkeypatch.setenv("AUDIO_SOURCE", "socket")
    write_running(tmp_path, pid=999, audio_source="socket", stt_label="mlx-whisper")
    set_devices(
        tmp_path,
        mic_device="MacBook Pro Microphone (uid=BuiltIn)",
        system_device="system-output tap (uid=agg-9)",
    )
    monkeypatch.setattr(cli, "_read_pid", lambda _d=None: 999)
    monkeypatch.setattr(cli, "_process_alive", lambda _p: True)

    rc = cli._cmd_status(["--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mic device: MacBook Pro Microphone (uid=BuiltIn)" in out
    assert "system device: system-output tap (uid=agg-9)" in out
    assert "configured mic (PortAudio)" not in out


def test_cli_status_live_socket_session_suppresses_portaudio_config(
    tmp_path: Path, capsys, monkeypatch
):
    """A LIVE socket session hides the configured-(PortAudio) lines even when
    THIS shell's config resolves portaudio (e.g. a menu-bar-launched session
    whose AUDIO_SOURCE env never reached this shell) — showing both device
    blocks at once would mislead."""
    from onoats import cli

    monkeypatch.delenv("AUDIO_SOURCE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))  # default: portaudio
    write_running(tmp_path, pid=999, audio_source="socket", stt_label="mlx-whisper")
    monkeypatch.setattr(cli, "_read_pid", lambda _d=None: 999)
    monkeypatch.setattr(cli, "_process_alive", lambda _p: True)

    rc = cli._cmd_status(["--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "audio source: socket" in out
    assert "configured mic (PortAudio)" not in out


def test_write_prestart_waiting_is_fresh_running_with_warning(tmp_path: Path):
    """Phase 7: the prompt-pending record is FRESH (not the previous session's
    record annotated), running=True, with the note in the v2 `warning` field —
    and the recorder's own start write replaces it wholesale once the prompt
    is answered."""
    # Stale stopped record from a previous session.
    write_running(tmp_path, pid=1, audio_source="socket", stt_label="old")
    write_stopped(tmp_path, exit_reason="graceful")
    stale = read_status(tmp_path)

    write_prestart_waiting(
        tmp_path,
        audio_source="socket",
        note="waiting for the system-audio permission prompt",
    )
    st = read_status(tmp_path)
    assert st is not None and st.running is True
    assert st.warning == "waiting for the system-audio permission prompt"
    assert st.audio_source == "socket"
    assert st.start_time > stale.start_time, "must be a fresh record"
    assert st.exit_reason is None and st.last_error is None

    # The recorder's start write builds a fresh record — warning cleared.
    write_running(tmp_path, pid=2, audio_source="socket", stt_label="mlx")
    st = read_status(tmp_path)
    assert st.warning is None and st.pid == 2


# ---------------------------------------------------------------------------
# (g) stop-then-immediate-start race: pid-file single-instance guard +
# ownership-checked removal. `onoats stop` returns on signal delivery, not exit,
# so a new `onoats bot` can launch while the old recorder is still draining.
# Without these guards the new start would overwrite the draining recorder's pid
# file, and the drainer would later unlink the NEW recorder's file (leaving it
# invisible to status/stop/flush). See runtime._write_pid_file / _remove_pid_file.
# ---------------------------------------------------------------------------

import shutil  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

from onoats._vendor.pid import PID_FILENAME  # noqa: E402
from onoats.runtime import (  # noqa: E402
    LOCK_FILENAME,
    RecorderAlreadyRunningError,
    _acquire_instance_lock,
    _release_instance_lock,
    _remove_pid_file,
    _write_pid_file,
)

# The single-instance lock is released after every test by an autouse fixture in
# tests/conftest.py (the lock is process-lifetime in production; pytest shares one
# process, so tests must reset it). `_release_instance_lock` is imported above for
# the explicit acquire/release-cycle test below.


def _seed_pid_file(data_dir: Path, pid: int, *, cmdline: str = "onoats bot") -> Path:
    pid_path = data_dir / ".active" / PID_FILENAME
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{pid}\nonoats-bot\n{cmdline}\n0.0\n", encoding="utf-8")
    return pid_path


def test_write_pid_file_refuses_verified_live_recorder(tmp_path, monkeypatch):
    """Start guard: a second start over an identity-verified LIVE recorder is
    refused (RecorderAlreadyRunningError), and the existing pid file is left
    intact — never overwritten."""
    if not shutil.which("sleep"):
        pytest.skip("requires a real live process for the liveness check")
    # A real, unrelated live process stands in for the still-draining recorder.
    proc = subprocess.Popen(["sleep", "30"])
    try:
        pid_path = _seed_pid_file(tmp_path, proc.pid)
        # Make the identity readback match the stored fingerprint → verified live.
        monkeypatch.setattr(
            "onoats._vendor.pid._live_ps_cmdline", lambda pid: "onoats bot"
        )
        with pytest.raises(RecorderAlreadyRunningError):
            _write_pid_file(tmp_path)
        # The draining recorder's pid file MUST survive the refused start.
        assert pid_path.read_text(encoding="utf-8").startswith(f"{proc.pid}\n")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_write_pid_file_overwrites_stale_dead_recorder(tmp_path):
    """A stale pid file for a DEAD recorder does not block a legitimate start —
    it is overwritten with the current process's pid (no false single-instance
    refusal on a crashed predecessor)."""
    import os as _os

    if not shutil.which("sleep"):
        pytest.skip("requires spawning a process to obtain a real dead pid")
    # Spawn then reap a process so its pid is genuinely dead (kill(0) raises
    # ProcessLookupError) — a realistic crashed-predecessor pid, no mocking.
    proc = subprocess.Popen(["sleep", "30"])
    proc.terminate()
    proc.wait(timeout=5)
    _seed_pid_file(tmp_path, proc.pid)

    pid_path = _write_pid_file(tmp_path)
    # Overwritten with OUR pid — start proceeds.
    assert pid_path.read_text(encoding="utf-8").startswith(f"{_os.getpid()}\n")


def test_remove_pid_file_skips_when_owned_by_newer_recorder(tmp_path):
    """Ownership-checked removal: a draining recorder must NOT delete a pid file a
    newer recorder has since overwritten with its own pid."""
    pid_path = _seed_pid_file(tmp_path, 55555)  # the NEW recorder owns it now
    # The OLD (draining) recorder, pid 12345, tears down and tries to remove.
    _remove_pid_file(pid_path, owner_pid=12345)
    assert pid_path.exists(), "new recorder's pid file must survive the old drainer"
    # The rightful owner can remove it.
    _remove_pid_file(pid_path, owner_pid=55555)
    assert not pid_path.exists()


def test_remove_pid_file_unconditional_without_owner(tmp_path):
    """Back-compat: with no owner_pid the removal is unconditional (the prior
    best-effort behaviour for callers that don't pass ownership)."""
    pid_path = _seed_pid_file(tmp_path, 999)
    _remove_pid_file(pid_path)
    assert not pid_path.exists()


def test_write_pid_file_refuses_live_recorder_with_indeterminate_probe(
    tmp_path, monkeypatch
):
    """[high] regression (Codex re-review): a marker-valid pid file naming a LIVE
    process whose identity can't be verified (ps probe returns None) must REFUSE
    startup, not overwrite — the same indeterminate state flush/stop refuse to act
    on. Without the guard a transient `ps` failure spawns a second recorder over a
    live one."""
    if not shutil.which("sleep"):
        pytest.skip("requires a real live process for the liveness check")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        pid_path = _seed_pid_file(tmp_path, proc.pid)
        # kill(0) succeeds (process alive) but the identity readback fails.
        monkeypatch.setattr("onoats._vendor.pid._live_ps_cmdline", lambda pid: None)
        with pytest.raises(RecorderAlreadyRunningError):
            _write_pid_file(tmp_path)
        # The live recorder's pid file MUST survive — never overwritten.
        assert pid_path.read_text(encoding="utf-8").startswith(f"{proc.pid}\n")
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_write_pid_file_refuses_live_legacy_fingerprintless_pid(tmp_path):
    """A legacy (2-line, fingerprint-less) pid file naming a LIVE process is
    unverifiable → refuse startup rather than overwrite a possibly-live recorder."""
    if not shutil.which("sleep"):
        pytest.skip("requires a real live process for the liveness check")
    proc = subprocess.Popen(["sleep", "30"])
    try:
        pid_path = tmp_path / ".active" / PID_FILENAME
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(f"{proc.pid}\nonoats-bot\n", encoding="utf-8")  # no line 3
        with pytest.raises(RecorderAlreadyRunningError):
            _write_pid_file(tmp_path)
        assert pid_path.exists()
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.parametrize(
    "corrupt",
    [
        "",  # empty — a newer recorder mid-write (truncated)
        "garbage-not-an-int\n",  # unparseable first line
        "12345\nWRONG-MARKER\nonoats bot\n0.0\n",  # foreign / invalid marker
    ],
)
def test_remove_pid_file_fail_closed_when_unreadable(tmp_path, corrupt):
    """[high] regression (Codex adversarial review): owner-checked removal must
    fail CLOSED. If the pid file reads back as None — an empty/partial file (a
    newer recorder mid-write) or a foreign/invalid record — a draining recorder
    must NOT unlink it. Pre-fix this fell through to ``pid_path.unlink()``, which
    (paired with the old in-place truncating writer) could delete a newer
    recorder's in-progress pid file, orphaning it (invisible to
    status/stop/flush). Even with the atomic writer closing the truncation window,
    removal stays fail-closed: a None read is never our own benign mid-write."""
    pid_path = tmp_path / ".active" / PID_FILENAME
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(corrupt, encoding="utf-8")
    _remove_pid_file(pid_path, owner_pid=12345)
    assert pid_path.exists(), (
        "fail-closed: a draining recorder must not delete an unreadable/foreign "
        "pid file (it may be a newer recorder's in-progress record)"
    )


def test_write_pid_file_is_atomic_via_os_replace(tmp_path, monkeypatch):
    """[high] regression (Codex adversarial review): the pid file is written via
    temp + ``os.replace`` (atomic rename), never truncated in place. An in-place
    ``write_text`` exposes an empty/partial file mid-write; a concurrent
    owner-checked ``_remove_pid_file`` would then read None and delete the newer
    recorder's file. Pin the atomic-replace contract and assert no temp residue."""
    import os as _os

    from onoats import runtime as _runtime

    replace_dests = []
    real_replace = _os.replace

    def _spy_replace(src, dst):
        replace_dests.append(Path(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(_runtime.os, "replace", _spy_replace)
    pid_path = _write_pid_file(tmp_path)

    # Final record landed via os.replace into the real path — not written in place.
    assert pid_path in replace_dests, "pid file must be published via os.replace"
    assert pid_path.read_text(encoding="utf-8").startswith(f"{_os.getpid()}\n")
    # No leaked temp files in the active dir.
    leftovers = list((tmp_path / ".active").glob("*.tmp"))
    assert not leftovers, f"atomic writer leaked temp residue: {leftovers}"


def test_write_pid_file_refuses_concurrent_start_holding_instance_lock(tmp_path):
    """[high] regression (Codex adversarial review round 4): the single-instance
    guard must be ATOMIC, not check-then-replace. Two `onoats bot` starts racing
    with no valid pid file both pass the best-effort identity check; the flock is
    the gate that lets exactly ONE proceed. Simulate the race winner by holding
    the flock (a separate open file description — POSIX flock conflicts even
    within one process), then assert a second `_write_pid_file` refuses and does
    NOT publish a pid file (so the loser can't run unrepresented)."""
    import os as _os

    if sys.platform == "win32":
        pytest.skip("flock single-instance lock is POSIX-only")
    import fcntl as _fcntl

    active = tmp_path / ".active"
    active.mkdir(parents=True, exist_ok=True)
    lock_path = active / LOCK_FILENAME
    holder = _os.open(str(lock_path), _os.O_RDWR | _os.O_CREAT, 0o644)
    _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)  # instance 1 holds the slot
    try:
        with pytest.raises(RecorderAlreadyRunningError):
            _write_pid_file(tmp_path)  # instance 2 loses the race
        assert not (active / PID_FILENAME).exists(), (
            "the start that lost the instance-lock race must not publish a pid file"
        )
    finally:
        _fcntl.flock(holder, _fcntl.LOCK_UN)
        _os.close(holder)


def test_instance_lock_blocks_then_frees_on_release(tmp_path):
    """The lock is exclusive while held and freed on release — so a post-drain
    start can re-acquire the slot (the stop→start handoff)."""
    import os as _os

    if sys.platform == "win32":
        pytest.skip("flock single-instance lock is POSIX-only")
    import fcntl as _fcntl

    active = tmp_path / ".active"
    active.mkdir(parents=True, exist_ok=True)
    _acquire_instance_lock(active)  # we now hold the slot
    # A concurrent acquirer (separate fd) is blocked while we hold it.
    other = _os.open(str(active / LOCK_FILENAME), _os.O_RDWR)
    try:
        with pytest.raises(OSError):
            _fcntl.flock(other, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        # Release; the slot is now free for the next start.
        _release_instance_lock()
        _fcntl.flock(other, _fcntl.LOCK_EX | _fcntl.LOCK_NB)  # must succeed now
        _fcntl.flock(other, _fcntl.LOCK_UN)
    finally:
        _os.close(other)


def test_stt_mic_and_system_branches_do_not_clobber_each_other(tmp_path: Path):
    """Finding 6: `dual.py` constructs two independent WebSocketSTTService
    instances against one server. Sharing a single "stt" branch key let
    system's kickstart-recovery clear erase mic's still-unconfirmed warning
    even though mic's own health was never confirmed — the same hazard the
    "mic"/"system" capture branches already avoid by being instance-scoped."""
    from onoats.status import stt_branch
    from onoats.stt.launchd import recovery_message

    write_running(tmp_path, pid=1, audio_source="socket", stt_label="websocket")

    set_warning_branch(tmp_path, stt_branch("mic"), recovery_message("label-a"))
    set_warning_branch(tmp_path, stt_branch("system"), recovery_message("label-a"))
    got = read_status(tmp_path)
    assert got is not None and got.warning is not None
    assert "stt-mic: " in got.warning
    assert "stt-system: " in got.warning

    # system confirms first; mic's warning must survive.
    set_warning_branch(tmp_path, stt_branch("system"), None)
    got = read_status(tmp_path)
    assert got is not None and got.warning is not None
    assert "stt-mic: " in got.warning
    assert "stt-system: " not in got.warning

    # And they coexist with the shared preflight branch and the capture
    # branches, in sorted order.
    set_warning_branch(tmp_path, stt_branch(None), recovery_message("label-a"))
    set_warning_branch(tmp_path, "mic", "no input")
    got = read_status(tmp_path)
    assert got is not None and got.warning is not None
    branches = [part.split(": ", 1)[0] for part in got.warning.split("; ")]
    assert branches == sorted(branches)
    assert set(branches) == {"mic", "stt", "stt-mic"}


def test_stt_branch_keys_are_instance_scoped():
    """Finding 6: mic and system are independent instances against one
    server; a shared branch key let either one's clear erase the other's
    still-unconfirmed warning."""
    from onoats.status import stt_branch

    assert stt_branch(None) == "stt"
    assert stt_branch("mic") == "stt-mic"
    assert stt_branch("system") == "stt-system"
    assert stt_branch("mic") != stt_branch("system")


def test_swift_menu_bar_splits_on_the_documented_warning_delimiter():
    """Round-6 architecture finding: `status.py`'s header says the Swift
    menu bar's outer split must change "in lockstep" with this module's
    `warning` join delimiter, but nothing checked it — the delimiter was a
    bare `"; "` literal in five places here and one `components(separatedBy:)`
    call in Swift, and a change on either side would have silently produced a
    menu bar that renders several branches as one unbroken line (or splits
    mid-message).

    This is the lockstep mechanism the convention lacked. It reads the Swift
    source rather than importing it — the two languages cannot share a
    constant, which is the whole reason the convention exists — and fails if
    the Swift split string stops matching
    `status.WARNING_ENTRY_DELIMITER`."""
    from onoats.status import WARNING_ENTRY_DELIMITER

    swift = (
        Path(__file__).resolve().parents[1]
        / "native"
        / "onoats-menubar"
        / "Sources"
        / "OnoatsMenuBarApp.swift"
    )
    assert swift.is_file(), swift
    source = swift.read_text(encoding="utf-8")

    # The warning renderer's outer split. Matched structurally (the
    # `components(separatedBy:)` call applied to `warning`), not by grepping
    # for the literal anywhere in the file, so an unrelated `"; "` elsewhere
    # in the Swift source cannot satisfy it.
    m = re.search(
        r"\bwarning\s*\n?\s*\.components\(separatedBy:\s*\"([^\"]*)\"\)", source
    )
    assert m is not None, (
        "OnoatsMenuBarApp.swift no longer splits `warning` with "
        "components(separatedBy:) — the status.py warning-grammar lockstep "
        "convention has no reader to stay in step with."
    )
    assert m.group(1) == WARNING_ENTRY_DELIMITER, (
        f"Swift splits `warning` on {m.group(1)!r} but "
        f"status.WARNING_ENTRY_DELIMITER is {WARNING_ENTRY_DELIMITER!r}. "
        "Change both in lockstep (see the status.py header)."
    )
