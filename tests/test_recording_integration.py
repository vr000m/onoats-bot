"""Full-path recording proofs and installed-smoke contract regressions."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import math
import os
from pathlib import Path
import signal
import shutil
import struct
import tempfile
from types import SimpleNamespace
import wave

import pytest
from pipecat.frames.frames import Frame, InputAudioRawFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from onoats import cli, runtime
from onoats.runtime import SttServiceResult

ROOT = Path(__file__).resolve().parent.parent
SMOKE_PATH = ROOT / "native/installed_recording_smoke.py"
_SMOKE_SPEC = importlib.util.spec_from_file_location(
    "installed_recording_smoke", SMOKE_PATH
)
assert _SMOKE_SPEC is not None and _SMOKE_SPEC.loader is not None
SMOKE = importlib.util.module_from_spec(_SMOKE_SPEC)
_SMOKE_SPEC.loader.exec_module(SMOKE)

RUN_ID = "0123456789abcdef0123456789abcdef"
OLD_RUN_ID = "fedcba9876543210fedcba9876543210"
_FIXTURE_SOCKET_WAIT_SEC = 8.0


def _birth(sec: int, usec: int):
    return SMOKE.ProcessBirth(sec, usec)


def _recorder_identity(**overrides):
    values = {
        "pid": 4321,
        "start_epoch": 900.25,
        "command": "/opt/onoats/bin/onoats bot",
        "process_start": "rec-start",
        "pgid": 4321,
        "process_birth": _birth(100, 200),
    }
    values.update(overrides)
    return SMOKE.RecorderIdentity(**values)


def _app_identity(app: Path, **overrides):
    values = {
        "pid": 1234,
        "pgid": 1234,
        "start": "app-start",
        "command": str((app / "Contents/MacOS/Onoats").resolve()),
        "process_birth": _birth(10, 20),
    }
    values.update(overrides)
    return SMOKE.ProcessIdentity(**values)


class _IdentityVAD(FrameProcessor):
    def __init__(self, **_kwargs):
        super().__init__()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class _DeterministicSpeechSTT(FrameProcessor):
    """Emit one structural transcript only after non-zero PCM reaches this arm."""

    def __init__(self) -> None:
        super().__init__()
        self.emitted = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if (
            isinstance(frame, InputAudioRawFrame)
            and not self.emitted
            and any(frame.audio)
        ):
            self.emitted = True
            await self.push_frame(
                TranscriptionFrame("fixture speech", "test", "fixture-time"), direction
            )
        await self.push_frame(frame, direction)


def _load_fake_capturer_source() -> str:
    spec = importlib.util.spec_from_file_location(
        "socket_supervisor_fixture",
        Path(__file__).with_name("test_socket_supervisor.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._FAKE_CAPTURER_SRC


def _utterances(data_dir: Path) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in data_dir.rglob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("type") == "utterance":
                found.append(entry)
    return found


def _paced_graceful_capturer_source(chunks: list[bytes]) -> str:
    """Feed every real-time chunk, then request the recorder's SIGTERM path."""
    assert chunks and all(len(chunk) == 640 for chunk in chunks)
    source = _load_fake_capturer_source()
    source = source.replace(
        'NONCE = _arg(["ONOATS_CAPTURER_NONCE", "ONOATS_NONCE"], "--nonce")',
        'NONCE = _arg(["ONOATS_CAPTURER_NONCE", "ONOATS_NONCE"], "--nonce")\n'
        "import signal\n"
        'with open(__file__ + ".pcm.json") as _pf:\n'
        "    CHUNKS = [base64.b64decode(value) for value in json.load(_pf)]",
    )
    source = source.replace(
        'pcm = struct.pack("<h", 1234)',
        "pcm = CHUNKS[min(seq, len(CHUNKS) - 1)]",
    )
    source = source.replace(
        "i += 1\n                    await asyncio.sleep(0.01)",
        "i += 1\n"
        "                    await asyncio.sleep(0.02)\n"
        "                    if path == MIC and i == len(CHUNKS):\n"
        "                        os.kill(os.getppid(), signal.SIGTERM)",
    )
    assert "os.kill(os.getppid(), signal.SIGTERM)" in source
    return source


def _write_capturer_fixture(
    tmp_path: Path, chunks: list[bytes], *, name: str = "fake_capturer.py"
) -> Path:
    capturer = tmp_path / name
    capturer.write_text(_paced_graceful_capturer_source(chunks), encoding="utf-8")
    capturer.chmod(0o755)
    Path(str(capturer) + ".pcm.json").write_text(
        json.dumps([base64.b64encode(chunk).decode("ascii") for chunk in chunks]),
        encoding="utf-8",
    )
    Path(str(capturer) + ".control").write_text(
        json.dumps({"behaviour": "stream"}), encoding="utf-8"
    )
    return capturer


@pytest.mark.parametrize("sample", [1234, 0], ids=["speech", "silence"])
def test_full_supervisor_socket_pipeline_requires_durable_me_speech(
    tmp_path, monkeypatch, request, sample
):
    """Exercise supervisor→socket transports→dual pipeline→tagger→JSONL."""
    speech = struct.pack("<320h", *([sample] * 320))
    chunks = [speech] * 20 + [bytes(640)] * 25
    capturer = _write_capturer_fixture(tmp_path, chunks)
    short_root = Path(tempfile.mkdtemp(prefix="onoats-ri-", dir="/private/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(short_root, ignore_errors=True))
    data_dir = short_root / "data"
    monkeypatch.setenv("AUDIO_SOURCE", "socket")
    monkeypatch.setenv("ONOATS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ONOATS_CAPTURER_BIN", str(capturer))
    monkeypatch.setenv("STT_SERVICE", "websocket")
    monkeypatch.setenv("ONOATS_DIARIZATION", "0")
    socket_dirs_before = set(Path("/tmp").glob("onoats-sock-*"))
    monkeypatch.setattr(tempfile, "tempdir", "/tmp")
    # A freshly-created executable can spend several seconds in macOS's first
    # execution checks before Python binds either socket. Keep this fixture
    # bounded below production's 10-second fail-loud budget without making the
    # result depend on whether this is the first generated child in the run.
    assert _FIXTURE_SOCKET_WAIT_SEC < cli._SOCKET_WAIT_TIMEOUT_SEC
    monkeypatch.setattr(cli, "_SOCKET_WAIT_TIMEOUT_SEC", _FIXTURE_SOCKET_WAIT_SEC)
    monkeypatch.setattr(cli, "_RECORDER_DRAIN_GRACE_SEC", 3.0)

    import onoats.dual as dual
    import pipecat.audio.vad.silero as silero_module
    import pipecat.processors.audio.vad_processor as vad_module

    async def create_stt(**_kwargs):
        return SttServiceResult(_DeterministicSpeechSTT())

    async def no_rss(*_args, **_kwargs):
        return None

    monkeypatch.setattr(dual, "_create_stt_service", create_stt)
    monkeypatch.setattr(dual, "log_stt_server_rss", no_rss)
    monkeypatch.setattr(vad_module, "VADProcessor", _IdentityVAD)
    monkeypatch.setattr(silero_module, "SileroVADAnalyzer", lambda **_kwargs: object())

    try:
        rc = cli._run_socket_supervisor([])
    finally:
        runtime._release_instance_lock()
    assert set(Path("/tmp").glob("onoats-sock-*")) <= socket_dirs_before, (
        "supervisor leaked its short /tmp socket directory"
    )
    assert rc == 0, "fixture must terminate through the explicit graceful-stop path"
    me = [entry for entry in _utterances(data_dir) if entry.get("source") == "me"]
    if sample:
        assert me, "process/socket readiness passed without durable source=me evidence"
    else:
        assert not me, "silence produced false durable speech evidence"


@pytest.mark.parametrize(
    "stage",
    [
        "signed app launch",
        "app-owned recorder identity",
        "fresh non-zero Me activity",
        "graceful app-owned stop",
        "durable Me utterance",
    ],
)
def test_installed_smoke_every_wait_has_stage_specific_timeout(stage):
    ticks = iter((0.0, 0.0, 1.0, 1.0))
    with pytest.raises(SMOKE.SmokeFailure, match=stage):
        SMOKE.wait_for(
            stage,
            0.5,
            lambda: None,
            clock=lambda: next(ticks),
            sleep=lambda _delay: None,
        )


def test_installed_smoke_finds_exact_app_executable_when_path_contains_spaces(
    tmp_path, monkeypatch
):
    app = tmp_path / "Build Output" / "Onoats.app"
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    monkeypatch.setattr(SMOKE, "_process_executable", lambda _pid: executable)
    monkeypatch.setattr(
        SMOKE,
        "_processes",
        lambda: {
            4321: SMOKE.ProcessRow(
                1, 4321, "Sat Nov  1 10:00:00 2026", f"{executable} --launch-argument"
            )
        },
    )
    assert SMOKE._one_app_pid(app) == 4321


def test_installed_smoke_refuses_preexisting_app_before_open(tmp_path, monkeypatch):
    app = tmp_path / "Onoats.app"
    executable = app / "Contents/MacOS/Onoats"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    monkeypatch.setattr(SMOKE, "_verify_signed_app", lambda _app: None)
    monkeypatch.setattr(SMOKE, "_one_app_pid", lambda _app: 777)
    opened = []
    monkeypatch.setattr(
        SMOKE.subprocess, "run", lambda *args, **kwargs: opened.append(args)
    )
    with pytest.raises(SMOKE.SmokeFailure, match="already running"):
        SMOKE.run(SimpleNamespace(app=app, data_dir=tmp_path / "data", me_floor=0.01))
    assert opened == []


def test_installed_smoke_success_still_cleans_exact_launched_app(tmp_path, monkeypatch):
    app = tmp_path / "Onoats.app"
    executable = app / "Contents/MacOS/Onoats"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    recorder = _recorder_identity(command="/onoats bot")
    results = {
        "signed app launch": 1234,
        "app-owned recorder identity": recorder,
        "fresh non-zero Me activity": True,
        "graceful app-owned stop": True,
        "durable Me utterance": True,
    }
    cleaned = []
    monkeypatch.setattr(SMOKE, "_verify_signed_app", lambda _app: None)
    monkeypatch.setattr(SMOKE, "_one_app_pid", lambda _app: None)
    monkeypatch.setattr(SMOKE.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        SMOKE,
        "wait_for",
        lambda stage, _timeout, _probe: results[stage],
    )
    app_command = f"{executable.resolve()} --launch-argument smoke"
    monkeypatch.setattr(
        SMOKE,
        "_processes",
        lambda: {1234: SMOKE.ProcessRow(1, 1234, "app-start", app_command)},
    )
    monkeypatch.setattr(SMOKE, "_process_birth", lambda _pid: _birth(10, 20))
    monkeypatch.setattr(SMOKE, "_owned_descendant_groups", lambda *_args: ())
    monkeypatch.setattr(
        SMOKE, "_cleanup_after_failure", lambda *args, **kwargs: cleaned.append(kwargs)
    )
    SMOKE.run(
        SimpleNamespace(
            app=app,
            data_dir=tmp_path / "data",
            app_timeout=1,
            recording_timeout=1,
            activity_timeout=1,
            stop_timeout=1,
            transcript_timeout=1,
            me_floor=0.01,
        )
    )
    assert cleaned == [
        {
            "app_identity": _app_identity(app, command=app_command),
            "recorder": recorder,
            "status_path": tmp_path / "data/.active/onoats.status.json",
            "pid_path": tmp_path / "data/.active/onoats.pid",
            "groups": (),
        }
    ]


def test_installed_smoke_app_cleanup_preserves_argument_bearing_identity(
    tmp_path, monkeypatch
):
    app = tmp_path / "Onoats.app"
    command = f"{(app / 'Contents/MacOS/Onoats').resolve()} --launch-argument smoke"
    identity = _app_identity(app, command=command)
    seen: list[SMOKE.ProcessIdentity] = []
    live = True

    def signal_app(_app, captured, _sig):
        nonlocal live
        seen.append(captured)
        live = False
        return True

    monkeypatch.setattr(
        SMOKE,
        "_processes",
        lambda: (
            {
                identity.pid: SMOKE.ProcessRow(
                    1, identity.pgid, identity.start, identity.command
                )
            }
            if live
            else {}
        ),
    )
    monkeypatch.setattr(
        SMOKE, "_process_birth", lambda pid: identity.process_birth if live else None
    )
    monkeypatch.setattr(
        SMOKE,
        "_process_executable",
        lambda _pid: str((app / "Contents/MacOS/Onoats").resolve()),
    )
    monkeypatch.setattr(SMOKE, "_signal_app", signal_app)
    SMOKE._cleanup_after_failure(app, app_identity=identity, timeout=0.01)

    assert seen == [identity]
    assert seen[0].command == command


def test_installed_smoke_rejects_birth_collision_around_topology_snapshot(
    tmp_path, monkeypatch
):
    status_path = tmp_path / "onoats.status.json"
    pid_path = tmp_path / "onoats.pid"
    status_path.write_text(
        json.dumps({"running": True, "pid": 4321, "start_time": 900.25}),
        encoding="utf-8",
    )
    pid_path.write_text("4321\nmarker\ncommand\n900.25\n", encoding="utf-8")
    rows = {
        1234: SMOKE.ProcessRow(1, 1234, "same-second", "/app"),
        4321: SMOKE.ProcessRow(1234, 4321, "same-second", "/onoats bot"),
    }
    recorder_births = iter((_birth(20, 100), _birth(20, 101)))
    calls: list[str] = []

    def processes():
        calls.append("ps")
        return rows

    def birth(pid):
        calls.append(f"birth-{pid}")
        if pid == 4321:
            return next(recorder_births)
        return _birth(10, 100)

    monkeypatch.setattr(SMOKE, "_processes", processes)
    monkeypatch.setattr(SMOKE, "_process_birth", birth)

    assert SMOKE._recording_identity(status_path, pid_path, app_pid=1234) is None
    ps_positions = [index for index, value in enumerate(calls) if value == "ps"]
    assert len(ps_positions) == 2
    assert calls.index("birth-4321") < ps_positions[1]
    assert (
        max(index for index, value in enumerate(calls) if value == "birth-4321")
        > ps_positions[1]
    )


@pytest.mark.parametrize(
    ("app_matches", "recorder_matches", "expected"),
    [
        (True, True, [(4321, signal.SIGTERM), (1234, signal.SIGTERM)]),
        (True, False, [(1234, signal.SIGTERM)]),
        (False, True, [(4321, signal.SIGTERM)]),
    ],
    ids=["owned-recorder-and-app", "foreign-recorder", "foreign-app-root"],
)
def test_installed_smoke_failure_cleanup_signals_only_verified_owned_identities(
    tmp_path, monkeypatch, app_matches, recorder_matches, expected
):
    app = tmp_path / "Build Output" / "Onoats.app"
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    monkeypatch.setattr(SMOKE, "_process_executable", lambda _pid: executable)
    app_pid, recorder_pid = 1234, 4321
    expected_recorder_command = "/opt/onoats/bin/onoats bot"
    processes = {
        app_pid: SMOKE.ProcessRow(
            1,
            app_pid,
            "app-start",
            executable if app_matches else "/tmp/foreign-app",
        ),
        recorder_pid: SMOKE.ProcessRow(
            app_pid,
            recorder_pid,
            "recorder-start",
            expected_recorder_command if recorder_matches else "/tmp/foreign-recorder",
        ),
    }
    signalled: list[tuple[int, signal.Signals]] = []

    def kill(pid: int, sig: signal.Signals) -> None:
        signalled.append((pid, sig))
        processes.pop(pid, None)

    monkeypatch.setattr(SMOKE, "_processes", lambda: dict(processes))
    monkeypatch.setattr(SMOKE.os, "kill", kill)
    app_birth = _birth(10, 20)
    recorder_birth = _birth(30, 40)
    monkeypatch.setattr(
        SMOKE,
        "_process_birth",
        lambda pid: {app_pid: app_birth, recorder_pid: recorder_birth}.get(pid),
    )
    identity = _recorder_identity(
        command=expected_recorder_command,
        process_start="recorder-start",
        process_birth=recorder_birth,
    )
    SMOKE._cleanup_after_failure(
        app,
        app_identity=_app_identity(app, pid=app_pid, process_birth=app_birth),
        recorder=identity,
        status_path=tmp_path / "status.json",
        pid_path=tmp_path / "pid",
        timeout=0.01,
    )
    assert signalled == expected


def test_installed_smoke_cleanup_attempts_app_after_recorder_signal_failure(
    tmp_path, monkeypatch
):
    app = tmp_path / "Onoats.app"
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    monkeypatch.setattr(SMOKE, "_process_executable", lambda _pid: executable)
    app_pid, recorder_pid = 1234, 4321
    command = "/opt/onoats/bin/onoats bot"
    processes = {
        app_pid: SMOKE.ProcessRow(1, app_pid, "app-start", executable),
        recorder_pid: SMOKE.ProcessRow(
            app_pid, recorder_pid, "recorder-start", command
        ),
    }
    attempted: list[int] = []

    def kill(pid: int, _sig: signal.Signals) -> None:
        attempted.append(pid)
        if pid == recorder_pid:
            raise PermissionError("injected recorder cleanup failure")
        processes.pop(pid, None)

    monkeypatch.setattr(SMOKE, "_processes", lambda: dict(processes))
    app_birth = _birth(10, 20)
    recorder_birth = _birth(30, 40)
    monkeypatch.setattr(
        SMOKE,
        "_process_birth",
        lambda pid: {app_pid: app_birth, recorder_pid: recorder_birth}.get(pid),
    )
    monkeypatch.setattr(SMOKE.os, "kill", kill)
    with pytest.raises(SMOKE.SmokeFailure, match="injected recorder cleanup failure"):
        SMOKE._cleanup_after_failure(
            app,
            app_identity=_app_identity(app, pid=app_pid, process_birth=app_birth),
            recorder=_recorder_identity(
                command=command,
                process_start="recorder-start",
                process_birth=recorder_birth,
            ),
            status_path=tmp_path / "status.json",
            pid_path=tmp_path / "pid",
            timeout=0.01,
        )
    assert attempted == [recorder_pid, recorder_pid, app_pid]


def test_installed_smoke_group_cleanup_survives_reaped_leader_and_rejects_reuse(
    monkeypatch,
):
    member_birth = _birth(50, 60)
    member = SMOKE.ProcessIdentity(
        pid=5002,
        pgid=5001,
        start="member-start",
        command="/capturer/helper",
        process_birth=member_birth,
    )
    group = SMOKE.OwnedProcessGroup(5001, (member,))
    processes = {5002: SMOKE.ProcessRow(1, 5001, "member-start", "/capturer/helper")}
    signalled = []
    monkeypatch.setattr(SMOKE, "_processes", lambda: dict(processes))
    births = {5002: member_birth}
    monkeypatch.setattr(SMOKE, "_process_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(SMOKE.os, "kill", lambda pid, sig: signalled.append((pid, sig)))
    assert SMOKE._signal_owned_group(group, signal.SIGKILL) is True
    processes[5002] = SMOKE.ProcessRow(1, 5001, "reused-start", "/capturer/helper")
    births[5002] = _birth(50, 61)
    assert SMOKE._signal_owned_group(group, signal.SIGKILL) is False
    assert signalled == [(5002, signal.SIGKILL)]


@pytest.mark.parametrize("app_state", ["live", "gone", "recycled"])
@pytest.mark.parametrize("recorder_state", ["live", "gone", "recycled"])
@pytest.mark.parametrize("group_state", ["live", "reaped", "recycled"])
def test_installed_smoke_captured_identity_cleanup_whole_state_space(
    tmp_path, monkeypatch, app_state, recorder_state, group_state
):
    """Captured ownership survives app exit; kernel generation gates signals."""
    app = tmp_path / "Onoats.app"
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    monkeypatch.setattr(SMOKE, "_process_executable", lambda _pid: executable)
    app_birth = _birth(10, 100)
    recorder_birth = _birth(20, 200)
    member_birth = _birth(30, 300)
    recycled_birth = _birth(30, 301)  # same second, different kernel generation
    rows: dict[int, object] = {}
    births: dict[int, object] = {}
    if app_state != "gone":
        rows[1234] = SMOKE.ProcessRow(1, 1234, "same-second", executable)
        births[1234] = app_birth if app_state == "live" else recycled_birth
    if recorder_state != "gone":
        rows[4321] = SMOKE.ProcessRow(
            1, 4321, "same-second", "/opt/onoats/bin/onoats bot"
        )
        births[4321] = recorder_birth if recorder_state == "live" else recycled_birth
    if group_state != "reaped":
        rows[5002] = SMOKE.ProcessRow(1, 5001, "same-second", "/capturer/helper")
        births[5002] = member_birth if group_state == "live" else recycled_birth

    recorder = _recorder_identity(
        process_start="same-second", process_birth=recorder_birth
    )
    member = SMOKE.ProcessIdentity(
        pid=5002,
        pgid=5001,
        start="same-second",
        command="/capturer/helper",
        process_birth=member_birth,
    )
    groups = (SMOKE.OwnedProcessGroup(5001, (member,)),)
    pid_signals: list[tuple[int, signal.Signals]] = []

    def kill(pid, sig):
        pid_signals.append((pid, sig))
        rows.pop(pid, None)
        births.pop(pid, None)

    monkeypatch.setattr(SMOKE, "_processes", lambda: dict(rows))
    monkeypatch.setattr(SMOKE, "_process_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(SMOKE.os, "kill", kill)
    SMOKE._cleanup_after_failure(
        app,
        app_identity=_app_identity(app, start="same-second", process_birth=app_birth),
        recorder=recorder,
        status_path=tmp_path / "status.json",
        pid_path=tmp_path / "pid",
        groups=groups,
        timeout=0.01,
    )

    assert ((4321, signal.SIGTERM) in pid_signals) is (recorder_state == "live")
    assert ((1234, signal.SIGTERM) in pid_signals) is (app_state == "live")
    assert ((5002, signal.SIGTERM) in pid_signals) is (group_state == "live")
    assert all(pid in {1234, 4321, 5002} for pid, _sig in pid_signals)


@pytest.mark.parametrize(
    ("exit_after", "expected_signals", "expected_final", "raises"),
    [
        ("term", [signal.SIGTERM], "gone", False),
        ("kill", [signal.SIGTERM, signal.SIGKILL], "gone", False),
        ("never", [signal.SIGTERM, signal.SIGKILL], "surviving", True),
    ],
)
def test_installed_smoke_cleanup_target_runs_complete_term_kill_wait_contract(
    monkeypatch, exit_after, expected_signals, expected_final, raises
):
    birth = _birth(90, 900)
    identity = SMOKE.ProcessIdentity(
        pid=6001,
        pgid=6001,
        start="captured-start",
        command="/captured/process",
        process_birth=birth,
    )
    rows = {6001: SMOKE.ProcessRow(1, 6001, "captured-start", "/captured/process")}
    births = {6001: birth}
    signals = []

    def send(target, sig):
        assert target is identity
        signals.append(sig)
        if exit_after == "term" or (exit_after == "kill" and sig == signal.SIGKILL):
            rows.clear()
            births.clear()
        return True

    monkeypatch.setattr(SMOKE, "_processes", lambda: dict(rows))
    monkeypatch.setattr(SMOKE, "_process_birth", lambda pid: births.get(pid))
    result, failures = SMOKE._cleanup_captured_target(
        "fixture",
        identity,
        matches=SMOKE._identity_matches,
        send=send,
        timeout=0,
    )

    assert signals == expected_signals
    assert result.final_state == expected_final
    assert bool(failures) is raises


def test_installed_smoke_cleanup_reports_survivor_after_attempting_every_target(
    tmp_path, monkeypatch
):
    app = tmp_path / "Onoats.app"
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    app_birth = _birth(10, 10)
    recorder_birth = _birth(20, 20)
    member_birth = _birth(30, 30)
    rows = {
        1234: SMOKE.ProcessRow(1, 1234, "app-start", executable),
        4321: SMOKE.ProcessRow(1, 4321, "rec-start", "/opt/onoats/bin/onoats bot"),
        5002: SMOKE.ProcessRow(1, 5001, "member-start", "/capturer/helper"),
    }
    births = {1234: app_birth, 4321: recorder_birth, 5002: member_birth}
    signals = []

    def kill(pid, sig):
        signals.append((pid, sig))
        if pid != 4321:
            rows.pop(pid, None)
            births.pop(pid, None)

    monkeypatch.setattr(SMOKE, "_process_executable", lambda _pid: executable)
    monkeypatch.setattr(SMOKE, "_processes", lambda: dict(rows))
    monkeypatch.setattr(SMOKE, "_process_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(SMOKE.os, "kill", kill)
    group = SMOKE.OwnedProcessGroup(
        5001,
        (
            SMOKE.ProcessIdentity(
                5002, 5001, "member-start", "/capturer/helper", member_birth
            ),
        ),
    )

    with pytest.raises(SMOKE.SmokeFailure, match="recorder pid 4321 survived SIGKILL"):
        SMOKE._cleanup_after_failure(
            app,
            app_identity=_app_identity(
                app, pid=1234, start="app-start", process_birth=app_birth
            ),
            recorder=_recorder_identity(
                pid=4321,
                command="/opt/onoats/bin/onoats bot",
                process_start="rec-start",
                process_birth=recorder_birth,
            ),
            groups=(group,),
            timeout=0,
        )

    assert (4321, signal.SIGTERM) in signals
    assert (4321, signal.SIGKILL) in signals
    assert (5002, signal.SIGTERM) in signals
    assert (1234, signal.SIGTERM) in signals


def test_installed_smoke_rechecks_kernel_birth_before_each_group_signal(monkeypatch):
    captured = _birth(40, 400)
    births = {5002: captured}
    member = SMOKE.ProcessIdentity(
        pid=5002,
        pgid=5001,
        start="same-second",
        command="/capturer/helper",
        process_birth=captured,
    )
    group = SMOKE.OwnedProcessGroup(5001, (member,))
    rows = {5002: SMOKE.ProcessRow(1, 5001, "same-second", "/capturer/helper")}
    signalled = []

    def kill(pid, sig):
        signalled.append((pid, sig))
        births[5002] = _birth(40, 401)

    monkeypatch.setattr(SMOKE, "_processes", lambda: rows)
    monkeypatch.setattr(SMOKE, "_process_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(SMOKE.os, "kill", kill)

    assert SMOKE._signal_owned_group(group, signal.SIGTERM) is True
    assert SMOKE._signal_owned_group(group, signal.SIGKILL) is False
    assert signalled == [(5002, signal.SIGTERM)]


@pytest.mark.parametrize("captured_state", ["live", "gone", "recycled"])
@pytest.mark.parametrize("foreign_member_present", [False, True])
def test_installed_smoke_group_cleanup_signals_only_exact_captured_members(
    monkeypatch, captured_state, foreign_member_present
):
    captured_birth = _birth(70, 700)
    member = SMOKE.ProcessIdentity(
        pid=5002,
        pgid=5001,
        start="captured-start",
        command="/capturer/helper",
        process_birth=captured_birth,
    )
    group = SMOKE.OwnedProcessGroup(5001, (member,))
    rows = {}
    births = {}
    if captured_state != "gone":
        rows[5002] = SMOKE.ProcessRow(1, 5001, "captured-start", "/capturer/helper")
        births[5002] = captured_birth if captured_state == "live" else _birth(70, 701)
    if foreign_member_present:
        rows[5999] = SMOKE.ProcessRow(1, 5001, "foreign-start", "/foreign/helper")
        births[5999] = _birth(80, 800)
    signals = []

    monkeypatch.setattr(SMOKE, "_processes", lambda: dict(rows))
    monkeypatch.setattr(SMOKE, "_process_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(SMOKE.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(
        SMOKE.os,
        "killpg",
        lambda *_args: pytest.fail("group-wide signalling exceeds captured authority"),
    )

    assert SMOKE._signal_owned_group(group, signal.SIGTERM) is (
        captured_state == "live"
    )
    assert signals == ([(5002, signal.SIGTERM)] if captured_state == "live" else [])


def test_installed_smoke_cleanup_attempts_recorder_groups_and_app_independently(
    tmp_path, monkeypatch
):
    app = tmp_path / "Onoats.app"
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    monkeypatch.setattr(SMOKE, "_process_executable", lambda _pid: executable)
    app_birth = _birth(10, 10)
    recorder_birth = _birth(20, 20)
    member_birth = _birth(30, 30)
    rows = {
        1234: SMOKE.ProcessRow(1, 1234, "app-start", executable),
        4321: SMOKE.ProcessRow(1, 4321, "rec-start", "/opt/onoats/bin/onoats bot"),
        5002: SMOKE.ProcessRow(1, 5001, "member-start", "/capturer/helper"),
    }
    births = {1234: app_birth, 4321: recorder_birth, 5002: member_birth}
    attempted: list[tuple[str, int]] = []

    def kill(pid, _sig):
        attempted.append(("pid", pid))
        raise PermissionError(f"pid-{pid}")

    monkeypatch.setattr(SMOKE, "_processes", lambda: rows)
    monkeypatch.setattr(SMOKE, "_process_birth", lambda pid: births.get(pid))
    monkeypatch.setattr(SMOKE.os, "kill", kill)
    group = SMOKE.OwnedProcessGroup(
        5001,
        (
            SMOKE.ProcessIdentity(
                pid=5002,
                pgid=5001,
                start="member-start",
                command="/capturer/helper",
                process_birth=member_birth,
            ),
        ),
    )

    with pytest.raises(SMOKE.SmokeFailure) as raised:
        SMOKE._cleanup_after_failure(
            app,
            app_identity=_app_identity(app, process_birth=app_birth),
            recorder=_recorder_identity(process_birth=recorder_birth),
            status_path=tmp_path / "status.json",
            pid_path=tmp_path / "pid",
            groups=(group,),
            timeout=0.01,
        )
    assert {("pid", 4321), ("pid", 5002), ("pid", 1234)} <= set(attempted)
    message = str(raised.value)
    assert "pid-4321" in message
    assert "pid-5002" in message
    assert "pid-1234" in message


def test_installed_smoke_requires_new_canonical_me_without_exposing_text(
    tmp_path, capsys
):
    session = tmp_path / "pending/session.jsonl"
    session.parent.mkdir(parents=True)
    private_text = "private words must never enter smoke output"
    session.write_text(
        json.dumps({"type": "session_meta", "run_id": RUN_ID})
        + "\n"
        + json.dumps({"type": "utterance", "source": "them", "text": private_text})
        + "\n",
        encoding="utf-8",
    )
    assert SMOKE._new_me_utterance(tmp_path, RUN_ID) is None
    with session.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "utterance", "source": "me", "text": private_text})
            + "\n"
        )
    assert SMOKE._new_me_utterance(tmp_path, RUN_ID) is True
    assert private_text not in SMOKE.__doc__
    captured = capsys.readouterr()
    assert private_text not in captured.out + captured.err


def _write_run_file(
    path: Path,
    run_id: str | None,
    *entries: dict[str, object],
    header_first: bool = True,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "session_meta"}
    if run_id is not None:
        header["run_id"] = run_id
    lines = [json.dumps(header, separators=(",", ":"))]
    lines.extend(json.dumps(entry, separators=(",", ":")) for entry in entries)
    if not header_first:
        lines[0], lines[1] = lines[1], lines[0]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("source", "text", "expected"),
    [
        ("me", "attested speech", True),
        ("them", "attested speech", None),
        ("me", "   ", None),
        (None, "attested speech", None),
    ],
)
def test_installed_smoke_accepts_only_canonical_me_from_attested_run(
    tmp_path, source, text, expected
):
    _write_run_file(
        tmp_path / "pending/session.jsonl",
        RUN_ID,
        {"type": "utterance", "source": source, "text": text},
    )

    assert SMOKE._new_me_utterance(tmp_path, RUN_ID) is expected


def test_installed_smoke_accepts_continuation_file_with_same_attested_id(tmp_path):
    _write_run_file(
        tmp_path / "pending/first.jsonl",
        RUN_ID,
        {"type": "utterance", "source": "them", "text": "other branch"},
    )
    _write_run_file(
        tmp_path / "pending/continuation.jsonl",
        RUN_ID,
        {"type": "utterance", "source": "me", "text": "continuation"},
    )

    assert SMOKE._new_me_utterance(tmp_path, RUN_ID) is True


def test_installed_smoke_rejects_renamed_copied_and_hard_linked_old_run(tmp_path):
    active = tmp_path / ".active/old.jsonl"
    _write_run_file(
        active,
        OLD_RUN_ID,
        {"type": "utterance", "source": "me", "text": "historical secret"},
    )
    pending = tmp_path / "pending"
    pending.mkdir()
    renamed = pending / "renamed.jsonl"
    active.rename(renamed)
    copied = pending / "copied.jsonl"
    copied.write_bytes(renamed.read_bytes())
    os.link(renamed, pending / "hard-linked.jsonl")

    assert SMOKE._new_me_utterance(tmp_path, RUN_ID) is None


@pytest.mark.parametrize("header", ["missing", "malformed", "non-first"])
def test_installed_smoke_rejects_unattested_jsonl_headers(tmp_path, header):
    path = tmp_path / "pending/session.jsonl"
    path.parent.mkdir(parents=True)
    utterance = {"type": "utterance", "source": "me", "text": "private"}
    if header == "missing":
        path.write_text(json.dumps(utterance) + "\n", encoding="utf-8")
    elif header == "malformed":
        path.write_text("{not-json}\n" + json.dumps(utterance) + "\n", encoding="utf-8")
    else:
        _write_run_file(path, RUN_ID, utterance, header_first=False)

    assert SMOKE._new_me_utterance(tmp_path, RUN_ID) is None


def test_installed_smoke_multiple_new_ids_do_not_override_telemetry_attestation(
    tmp_path,
):
    _write_run_file(
        tmp_path / "pending/newer-looking.jsonl",
        OLD_RUN_ID,
        {"type": "utterance", "source": "me", "text": "wrong run"},
    )
    _write_run_file(
        tmp_path / "pending/attested.jsonl",
        RUN_ID,
        {"type": "utterance", "source": "them", "text": "right run, wrong source"},
    )
    assert SMOKE._new_me_utterance(tmp_path, RUN_ID) is None


def test_installed_smoke_header_and_utterances_share_one_descriptor(
    tmp_path, monkeypatch
):
    path = tmp_path / "pending/session.jsonl"
    _write_run_file(
        path,
        RUN_ID,
        {"type": "utterance", "source": "them", "text": "original generation"},
    )
    replacement = tmp_path / "replacement.tmp"
    _write_run_file(
        replacement,
        RUN_ID,
        {"type": "utterance", "source": "me", "text": "replacement generation"},
    )
    original_reader = SMOKE._bounded_jsonl_lines
    replaced = False

    def replacing_reader(candidate):
        nonlocal replaced
        for entry in original_reader(candidate):
            if not replaced:
                replaced = True
                os.replace(replacement, path)
            yield entry

    monkeypatch.setattr(SMOKE, "_bounded_jsonl_lines", replacing_reader)

    assert SMOKE._new_me_utterance(tmp_path, RUN_ID) is None
    assert replaced is True


def _sized_utterance_line(size: int) -> bytes:
    empty = json.dumps(
        {"type": "utterance", "source": "me", "text": ""},
        separators=(",", ":"),
    ).encode()
    assert size >= len(empty)
    line = json.dumps(
        {"type": "utterance", "source": "me", "text": "x" * (size - len(empty))},
        separators=(",", ":"),
    ).encode()
    assert len(line) == size
    return line


def test_installed_smoke_jsonl_reader_enforces_exact_one_mib_line_cap(tmp_path):
    cap = SMOKE.JSONL_LINE_LIMIT
    path = tmp_path / "pending/session.jsonl"
    path.parent.mkdir(parents=True)
    header = json.dumps({"type": "session_meta", "run_id": RUN_ID}).encode()
    path.write_bytes(header + b"\n" + _sized_utterance_line(cap) + b"\n")
    assert SMOKE._new_me_utterance(tmp_path, RUN_ID) is True

    path.write_bytes(header + b"\n" + _sized_utterance_line(cap + 1) + b"\n")
    with pytest.raises(SMOKE.SmokeFailure, match="line limit"):
        SMOKE._new_me_utterance(tmp_path, RUN_ID)


def test_installed_smoke_unterminated_oversized_line_is_secret_safe(tmp_path, capsys):
    secret = "NEVER-PRINT-PRIVATE-TRANSCRIPT"
    path = tmp_path / "pending/session.jsonl"
    path.parent.mkdir(parents=True)
    header = json.dumps({"type": "session_meta", "run_id": RUN_ID}).encode()
    path.write_bytes(header + b"\n" + secret.encode() + b"x" * SMOKE.JSONL_LINE_LIMIT)

    with pytest.raises(SMOKE.SmokeFailure) as raised:
        SMOKE._new_me_utterance(tmp_path, RUN_ID)
    assert secret not in str(raised.value)
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


def test_installed_smoke_prelaunch_scan_rejects_unclassifiable_existing_file(
    tmp_path,
):
    path = tmp_path / "pending/existing.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('{"type":"utterance","source":"me","text":"private"}\n')

    with pytest.raises(SMOKE.SmokeFailure, match="pre-launch"):
        SMOKE._prelaunch_run_ids(tmp_path)


def _exercise_activity_probe(
    tmp_path, monkeypatch, telemetry: dict[str, object]
) -> None:
    app = tmp_path / "Onoats.app"
    executable = app / "Contents/MacOS/Onoats"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    data_dir = tmp_path / "data"
    telemetry_path = data_dir / ".active/onoats.ui.json"
    telemetry_path.parent.mkdir(parents=True)
    telemetry_path.write_text(json.dumps(telemetry), encoding="utf-8")
    recorder = _recorder_identity()

    monkeypatch.setattr(SMOKE, "_verify_signed_app", lambda _app: None)
    monkeypatch.setattr(SMOKE, "_one_app_pid", lambda _app: None)
    monkeypatch.setattr(SMOKE.subprocess, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        SMOKE,
        "_processes",
        lambda: {
            1234: SMOKE.ProcessRow(1, 1234, "app-start", str(executable.resolve()))
        },
    )
    monkeypatch.setattr(SMOKE, "_owned_descendant_groups", lambda *_args: ())
    monkeypatch.setattr(SMOKE, "_cleanup_after_failure", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(SMOKE.time, "time", lambda: 1000.0)
    if hasattr(SMOKE, "_process_birth"):
        monkeypatch.setattr(SMOKE, "_process_birth", lambda _pid: _birth(10, 20))

    def fake_wait(stage, _timeout, probe):
        if stage == "signed app launch":
            return 1234
        if stage == "app-owned recorder identity":
            return recorder
        if stage == "fresh non-zero Me activity":
            value = probe()
            if value is None:
                raise SMOKE.SmokeFailure("fresh non-zero Me activity rejected")
            return value
        return True

    monkeypatch.setattr(SMOKE, "wait_for", fake_wait)
    SMOKE.run(
        SimpleNamespace(
            app=app,
            data_dir=data_dir,
            app_timeout=1,
            recording_timeout=1,
            activity_timeout=1,
            stop_timeout=1,
            transcript_timeout=1,
            me_floor=0.01,
        )
    )


def _telemetry(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": 2,
        "pid": 4321,
        "start_epoch": 900.25,
        "transcript_run_id": RUN_ID,
        "updated_at": 1000.0,
        "audio": {"me": {"level": 0.5, "observed_at": 1000.0}, "them": None},
        "stt": {
            "me": {"state": "ready", "detail": None},
            "them": {"state": "ready", "detail": None},
            "aggregate": "ready",
        },
    }
    value.update(overrides)
    return value


@pytest.mark.parametrize("level", [-0.0, 0.0, 0.01])
def test_installed_smoke_me_floor_is_strictly_greater(tmp_path, monkeypatch, level):
    telemetry = _telemetry(
        audio={"me": {"level": level, "observed_at": 1000.0}, "them": None}
    )
    with pytest.raises(SMOKE.SmokeFailure, match="Me activity rejected"):
        _exercise_activity_probe(tmp_path, monkeypatch, telemetry)


@pytest.mark.parametrize("level", [0.0100001, 1.0])
def test_installed_smoke_accepts_finite_in_range_level_above_floor(
    tmp_path, monkeypatch, level
):
    telemetry = _telemetry(
        audio={"me": {"level": level, "observed_at": 1000.0}, "them": None}
    )
    _exercise_activity_probe(tmp_path, monkeypatch, telemetry)


@pytest.mark.parametrize("level", [-0.000001, 1.0000001, math.nan, math.inf, -math.inf])
def test_installed_smoke_rejects_nonfinite_and_out_of_range_levels(
    tmp_path, monkeypatch, level
):
    telemetry = _telemetry(
        audio={"me": {"level": level, "observed_at": 1000.0}, "them": None}
    )
    with pytest.raises(SMOKE.SmokeFailure, match="Me activity rejected"):
        _exercise_activity_probe(tmp_path, monkeypatch, telemetry)


@pytest.mark.parametrize(
    ("updated_at", "observed_at"),
    [
        (994.999, 1000.0),
        (1000.001, 1000.0),
        (1000.0, 998.999),
        (1000.0, 1000.001),
        (math.nan, 1000.0),
        (1000.0, math.inf),
    ],
)
def test_installed_smoke_rejects_stale_future_and_nonfinite_telemetry_times(
    tmp_path, monkeypatch, updated_at, observed_at
):
    telemetry = _telemetry(
        updated_at=updated_at,
        audio={"me": {"level": 0.5, "observed_at": observed_at}, "them": None},
    )
    with pytest.raises(SMOKE.SmokeFailure, match="Me activity rejected"):
        _exercise_activity_probe(tmp_path, monkeypatch, telemetry)


@pytest.mark.parametrize("field", ["pid", "start_epoch"])
def test_installed_smoke_rejects_telemetry_owner_mismatch(tmp_path, monkeypatch, field):
    telemetry = _telemetry(**{field: 9999})
    with pytest.raises(SMOKE.SmokeFailure, match="ownership"):
        _exercise_activity_probe(tmp_path, monkeypatch, telemetry)


@pytest.mark.parametrize("floor", [math.nan, math.inf, -math.inf, -0.0001, 1.0001])
def test_installed_smoke_rejects_invalid_me_floor_before_any_external_action(
    tmp_path, monkeypatch, floor
):
    touched: list[str] = []
    monkeypatch.setattr(
        SMOKE, "_verify_signed_app", lambda _app: touched.append("verify")
    )
    monkeypatch.setattr(SMOKE, "_one_app_pid", lambda _app: touched.append("discover"))
    monkeypatch.setattr(
        SMOKE.subprocess,
        "run",
        lambda *_args, **_kwargs: touched.append("launch"),
    )
    with pytest.raises(SMOKE.SmokeFailure, match="me-floor"):
        SMOKE.run(
            SimpleNamespace(
                app=tmp_path / "Onoats.app",
                data_dir=tmp_path / "data",
                me_floor=floor,
            )
        )
    assert touched == []


def _write_stopped_status(path: Path, **overrides: object) -> None:
    status: dict[str, object] = {
        "running": False,
        "pid": 4321,
        "start_time": 900.25,
        "exit_reason": "graceful",
        "supervisor_rc": None,
        "last_error": None,
    }
    status.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status), encoding="utf-8")


def test_installed_smoke_accepts_matching_canonical_graceful_stop(tmp_path):
    status_path = tmp_path / ".active/onoats.status.json"
    _write_stopped_status(status_path)
    assert (
        SMOKE._graceful_stop_evidence(
            status_path, recorder_pid=4321, start_epoch=900.25
        )
        is True
    )


def test_installed_smoke_rejects_matching_crash_stop_immediately(tmp_path):
    status_path = tmp_path / ".active/onoats.status.json"
    _write_stopped_status(
        status_path,
        exit_reason="fatal_error_frame",
        supervisor_rc=1,
        last_error="capture ended",
    )
    with pytest.raises(SMOKE.SmokeFailure, match="canonical graceful exit"):
        SMOKE._graceful_stop_evidence(
            status_path, recorder_pid=4321, start_epoch=900.25
        )


@pytest.mark.parametrize(
    "identity",
    [{"pid": 9999}, {"start_time": 899.0}],
    ids=["foreign-pid", "stale-epoch"],
)
def test_installed_smoke_foreign_or_stale_stop_stays_bounded(tmp_path, identity):
    status_path = tmp_path / ".active/onoats.status.json"
    _write_stopped_status(status_path, **identity)
    ticks = iter((0.0, 0.0, 1.0, 1.0))
    with pytest.raises(SMOKE.SmokeFailure, match="graceful app-owned stop"):
        SMOKE.wait_for(
            "graceful app-owned stop",
            0.5,
            lambda: SMOKE._graceful_stop_evidence(
                status_path, recorder_pid=4321, start_epoch=900.25
            ),
            clock=lambda: next(ticks),
            sleep=lambda _delay: None,
        )


@pytest.mark.stt_ws_integration
def test_live_variant_reuses_fixture_through_full_supervisor_path(
    tmp_path, monkeypatch
):
    fixture_raw = os.environ.get("ONOATS_STT_FIXTURE")
    if not fixture_raw:
        pytest.skip("ONOATS_STT_FIXTURE not set (opt-in full-path live STT proof)")
    fixture = Path(fixture_raw).expanduser()
    if not fixture.is_file():
        pytest.skip(f"ONOATS_STT_FIXTURE is not a file: {fixture}")
    with wave.open(str(fixture), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (
            16000,
            1,
            2,
        ):
            pytest.skip("ONOATS_STT_FIXTURE must be 16 kHz mono PCM16")
        pcm = wav.readframes(wav.getnframes())
    chunk_bytes = 320 * 2
    chunks = [pcm[i : i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]
    chunks.extend([bytes(chunk_bytes)] * 150)  # 3 s VAD-closing tail
    if not chunks or not any(pcm):
        pytest.skip("ONOATS_STT_FIXTURE contains no non-zero speech PCM")

    capturer = _write_capturer_fixture(tmp_path, chunks, name="fixture_capturer.py")
    data_dir = tmp_path / "live-data"
    monkeypatch.setenv("AUDIO_SOURCE", "socket")
    monkeypatch.setenv("ONOATS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ONOATS_CAPTURER_BIN", str(capturer))
    monkeypatch.setenv("STT_SERVICE", "websocket")
    monkeypatch.setenv("ONOATS_DIARIZATION", "0")
    monkeypatch.setattr(tempfile, "tempdir", "/tmp")
    monkeypatch.setattr(cli, "_SOCKET_WAIT_TIMEOUT_SEC", _FIXTURE_SOCKET_WAIT_SEC)
    monkeypatch.setattr(cli, "_RECORDER_DRAIN_GRACE_SEC", 120.0)

    from onoats.config import load_config

    endpoint = runtime._resolve_stt_ws_target(runtime._ws_env(load_config()))
    try:
        asyncio.run(
            runtime._preflight_stt_ws(endpoint, runtime._display_target(endpoint))
        )
    except runtime.SttPreflightError as exc:
        from onoats._redact import safe_exc_text

        pytest.skip(f"live websocket STT endpoint unavailable: {safe_exc_text(exc)}")

    try:
        rc = cli._run_socket_supervisor([])
    finally:
        runtime._release_instance_lock()
    assert rc == 0, "live fixture must stop gracefully after its VAD tail"
    assert any(entry.get("source") == "me" for entry in _utterances(data_dir)), (
        "real websocket full path produced no durable source=me utterance"
    )


@pytest.mark.parametrize("reader", ["json", "pid"])
@pytest.mark.parametrize(
    "kind",
    ["regular", "oversized", "fifo", "symlink", "directory", "missing", "invalid_utf8"],
)
def test_smoke_control_descriptor_state_space(tmp_path, reader, kind):
    path = tmp_path / "control"
    content = (
        b'{"ok":true}' if reader == "json" else b"4321\nonoats-bot\ncommand\n900.25\n"
    )
    if kind == "regular":
        path.write_bytes(content)
    elif kind == "oversized":
        path.write_bytes(b"x" * (SMOKE.CONTROL_FILE_LIMIT + 1))
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "symlink":
        target = tmp_path / "target"
        target.write_bytes(content)
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "invalid_utf8":
        path.write_bytes(b"\xff")

    # Alarm makes an unsafe FIFO regression fail rather than hang the suite.
    def expired(*_args):
        raise AssertionError("control read blocked")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 0.5)
    try:
        result = (
            SMOKE._json(path)
            if reader == "json"
            else SMOKE._pid_file_matches(
                path, pid=4321, start_epoch=900.25, command="command"
            )
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert bool(result) is (kind == "regular")


@pytest.mark.parametrize("marker", ["onoats-bot", "foreign", ""])
@pytest.mark.parametrize("command", ["command", "foreign", ""])
@pytest.mark.parametrize("pid", [4321, 9999])
@pytest.mark.parametrize("epoch", [900.25, 901.25])
def test_smoke_pid_identity_state_space(tmp_path, marker, command, pid, epoch):
    path = tmp_path / "pid"
    path.write_text(f"4321\n{marker}\ncommand\n900.25\n")
    assert SMOKE._pid_file_matches(
        path, pid=pid, start_epoch=epoch, command=command
    ) is (
        marker == "onoats-bot"
        and command == "command"
        and pid == 4321
        and epoch == 900.25
    )


@pytest.mark.parametrize("kernel", ["matching", "foreign", "missing"])
@pytest.mark.parametrize("argv", ["matching", "foreign"])
def test_smoke_kernel_app_identity_state_space(tmp_path, monkeypatch, kernel, argv):
    app = tmp_path / "Onoats.app"
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    command = executable if argv == "matching" else "/foreign"
    monkeypatch.setattr(
        SMOKE,
        "_processes",
        lambda: {1234: SMOKE.ProcessRow(1, 1234, "app-start", command)},
    )
    monkeypatch.setattr(SMOKE, "_process_birth", lambda _pid: _birth(10, 20))
    monkeypatch.setattr(
        SMOKE,
        "_process_executable",
        lambda _pid: (
            executable
            if kernel == "matching"
            else "/foreign"
            if kernel == "foreign"
            else None
        ),
    )
    assert SMOKE._one_app_pid(app) == (1234 if kernel == "matching" else None)
    calls = []
    monkeypatch.setattr(SMOKE.os, "kill", lambda *args: calls.append(args))
    assert SMOKE._signal_app(
        app, _app_identity(app, command=command), signal.SIGTERM
    ) is (kernel == "matching")
    assert bool(calls) is (kernel == "matching")
