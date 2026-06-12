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

import re
from pathlib import Path

import pytest

from onoats.status import (
    STATUS_SCHEMA_VERSION,
    Liveness,
    StatusRecord,
    mark_rotation,
    read_status,
    resolve_liveness,
    set_devices,
    set_warning,
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


def test_set_warning_sets_and_clears(tmp_path: Path):
    # No record yet → best-effort no-op (the event raced ahead of the start write).
    assert set_warning(tmp_path, "early") is None
    assert read_status(tmp_path) is None

    write_running(tmp_path, pid=4242, audio_source="socket", stt_label="mlx-whisper")
    set_warning(tmp_path, "mic: only zero samples for ~30 s — check hardware mute")
    got = read_status(tmp_path)
    assert got is not None
    assert got.warning == "mic: only zero samples for ~30 s — check hardware mute"
    # The annotate must not clobber the session detail.
    assert got.running is True and got.audio_source == "socket"

    set_warning(tmp_path, None)
    got = read_status(tmp_path)
    assert got is not None and got.warning is None


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
    device-stamped (unlike set_warning, which only requires existence)."""
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
    pid_rm_idx = src.index("_remove_pid_file(pid_path)")
    assert stop_idx < pid_rm_idx, "status-stopped must be written before pid removal"

    # And the start producer runs AFTER the pid file is written (pid first).
    pid_write_idx = src.index("_write_pid_file(data_dir)")
    start_idx = src.index("_write_status_running(")
    assert pid_write_idx < start_idx, "pid file must be written before status (start)"


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
