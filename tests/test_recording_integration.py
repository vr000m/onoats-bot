"""Full-path recording proofs and installed-smoke contract regressions."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import signal
import struct
import tempfile
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
    tmp_path, monkeypatch, sample
):
    """Exercise supervisor→socket transports→dual pipeline→tagger→JSONL."""
    speech = struct.pack("<320h", *([sample] * 320))
    chunks = [speech] * 20 + [bytes(640)] * 25
    capturer = _write_capturer_fixture(tmp_path, chunks)
    data_dir = tmp_path / "data"
    monkeypatch.setenv("AUDIO_SOURCE", "socket")
    monkeypatch.setenv("ONOATS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("ONOATS_CAPTURER_BIN", str(capturer))
    monkeypatch.setenv("STT_SERVICE", "websocket")
    monkeypatch.setenv("ONOATS_DIARIZATION", "0")
    socket_dirs_before = set(Path("/tmp").glob("onoats-sock-*"))
    monkeypatch.setattr(tempfile, "tempdir", "/tmp")
    monkeypatch.setattr(cli, "_SOCKET_WAIT_TIMEOUT_SEC", 2.0)
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
    monkeypatch.setattr(
        SMOKE,
        "_processes",
        lambda: {4321: (1, f"{executable} --launch-argument")},
    )
    assert SMOKE._one_app_pid(app) == 4321


@pytest.mark.parametrize(
    ("app_matches", "recorder_matches", "expected"),
    [
        (True, True, [(4321, signal.SIGTERM), (1234, signal.SIGTERM)]),
        (True, False, [(1234, signal.SIGTERM)]),
        (False, True, []),
    ],
    ids=["owned-recorder-and-app", "foreign-recorder", "foreign-app-root"],
)
def test_installed_smoke_failure_cleanup_signals_only_verified_owned_identities(
    tmp_path, monkeypatch, app_matches, recorder_matches, expected
):
    app = tmp_path / "Build Output" / "Onoats.app"
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    app_pid, recorder_pid = 1234, 4321
    expected_recorder_command = "/opt/onoats/bin/onoats bot"
    processes = {
        app_pid: (1, executable if app_matches else "/tmp/foreign-app"),
        recorder_pid: (
            app_pid,
            expected_recorder_command if recorder_matches else "/tmp/foreign-recorder",
        ),
    }
    signalled: list[tuple[int, signal.Signals]] = []

    def kill(pid: int, sig: signal.Signals) -> None:
        signalled.append((pid, sig))
        processes.pop(pid, None)

    monkeypatch.setattr(SMOKE, "_processes", lambda: dict(processes))
    monkeypatch.setattr(SMOKE.os, "kill", kill)
    identity = SMOKE.RecorderIdentity(recorder_pid, 900.25, expected_recorder_command)
    SMOKE._cleanup_after_failure(
        app,
        app_pid=app_pid,
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
    app_pid, recorder_pid = 1234, 4321
    command = "/opt/onoats/bin/onoats bot"
    processes = {app_pid: (1, executable), recorder_pid: (app_pid, command)}
    attempted: list[int] = []

    def kill(pid: int, _sig: signal.Signals) -> None:
        attempted.append(pid)
        if pid == recorder_pid:
            raise PermissionError("injected recorder cleanup failure")
        processes.pop(pid, None)

    monkeypatch.setattr(SMOKE, "_processes", lambda: dict(processes))
    monkeypatch.setattr(SMOKE.os, "kill", kill)
    with pytest.raises(SMOKE.SmokeFailure, match="injected recorder cleanup failure"):
        SMOKE._cleanup_after_failure(
            app,
            app_pid=app_pid,
            recorder=SMOKE.RecorderIdentity(recorder_pid, 900.25, command),
            status_path=tmp_path / "status.json",
            pid_path=tmp_path / "pid",
            timeout=0.01,
        )
    assert attempted == [recorder_pid, app_pid]


def test_installed_smoke_requires_new_canonical_me_without_exposing_text(
    tmp_path, capsys
):
    before = SMOKE._session_snapshot(tmp_path)
    session = tmp_path / "pending/session.jsonl"
    session.parent.mkdir(parents=True)
    private_text = "private words must never enter smoke output"
    session.write_text(
        json.dumps({"type": "utterance", "source": "them", "text": private_text})
        + "\n",
        encoding="utf-8",
    )
    assert SMOKE._new_me_utterance(tmp_path, before) is None
    with session.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"type": "utterance", "source": "me", "text": private_text})
            + "\n"
        )
    assert SMOKE._new_me_utterance(tmp_path, before) is True
    assert private_text not in SMOKE.__doc__
    captured = capsys.readouterr()
    assert private_text not in captured.out + captured.err


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
    monkeypatch.setattr(cli, "_SOCKET_WAIT_TIMEOUT_SEC", 3.0)
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
