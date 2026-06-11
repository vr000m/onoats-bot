"""CLI dispatch: --help, subcommand routing (heavy entrypoints mocked), flush."""

from __future__ import annotations

import signal

import pytest

from onoats import cli


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ONOATS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("KODA_DATA_DIR", raising=False)
    return tmp_path / "data"


# ---------------------------------------------------------------------------
# --help short-circuits before any heavy import
# ---------------------------------------------------------------------------


def test_top_level_help_no_command(capsys):
    rc = cli.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "onoats" in out
    for sub in ("init", "bot", "bot-single", "flush", "convert", "devices", "status"):
        assert sub in out


def test_top_level_help_flag(capsys):
    rc = cli.main(["--help"])
    assert rc == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_unknown_command_errors():
    with pytest.raises(SystemExit) as exc:
        cli.main(["nonsense"])
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# Subcommand routing — each heavy entrypoint is patched
# ---------------------------------------------------------------------------


def test_bot_routes_to_dual(monkeypatch):
    called = {}

    def fake_main(argv=None):
        called["argv"] = argv
        return 0

    monkeypatch.setattr("onoats.dual.main", fake_main)
    rc = cli.main(["bot", "--live-terminal"])
    assert rc == 0
    assert called["argv"] == ["--live-terminal"]


def test_bot_help_under_socket_does_not_enter_supervisor(monkeypatch, capsys):
    """`onoats bot --help` must print bot help even under AUDIO_SOURCE=socket.

    The bot's own parser resolves --help (and arg errors) BEFORE _cmd_bot picks
    a backend, so a help request never enters the socket supervisor — which
    would otherwise require/spawn ONOATS_CAPTURER_BIN just to answer --help,
    regressing the "subcommand help resolves without booting services" contract.
    """
    monkeypatch.setenv("AUDIO_SOURCE", "socket")
    monkeypatch.delenv("ONOATS_CAPTURER_BIN", raising=False)

    def _boom(rest):
        raise AssertionError("socket supervisor entered for a --help request")

    monkeypatch.setattr(cli, "_run_socket_supervisor", _boom)

    with pytest.raises(SystemExit) as exc:
        cli.main(["bot", "--help"])
    assert exc.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_bot_source_socket_flag_enters_supervisor(monkeypatch):
    """`--source socket` selects the supervisor even when env/config say portaudio."""
    monkeypatch.setenv("AUDIO_SOURCE", "portaudio")
    called = {}

    def fake_supervisor(rest):
        called["rest"] = rest
        return 0

    monkeypatch.setattr(cli, "_run_socket_supervisor", fake_supervisor)
    rc = cli.main(["bot", "--source", "socket"])
    assert rc == 0
    assert called["rest"] == ["--source", "socket"]


def test_bot_source_portaudio_flag_overrides_socket_env(monkeypatch):
    """`--source portaudio` wins over AUDIO_SOURCE=socket (flag > env > config)."""
    monkeypatch.setenv("AUDIO_SOURCE", "socket")

    def _boom(rest):
        raise AssertionError("supervisor entered despite --source portaudio")

    monkeypatch.setattr(cli, "_run_socket_supervisor", _boom)
    called = {}
    monkeypatch.setattr("onoats.dual.main", _recorder(called))
    rc = cli.main(["bot", "--source", "portaudio"])
    assert rc == 0
    assert called["argv"] == ["--source", "portaudio"]


def test_bot_source_rejects_unknown_backend(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["bot", "--source", "alsa"])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def _recorder(called):
    def fake_main(argv=None):
        called["argv"] = argv
        return 0

    return fake_main


def test_bot_single_routes_to_main(monkeypatch):
    called = {}
    monkeypatch.setattr("onoats.__main__.main", _recorder(called))
    rc = cli.main(["bot-single", "--category", "work"])
    assert rc == 0
    assert called["argv"] == ["--category", "work"]


def test_convert_routes_and_defaults_to_once(monkeypatch):
    called = {}
    monkeypatch.setattr("onoats.convert.main", _recorder(called))
    # bare `onoats convert` defaults to --once
    assert cli.main(["convert"]) == 0
    assert called["argv"] == ["--once"]


def test_convert_forwards_explicit_args(monkeypatch):
    called = {}
    monkeypatch.setattr("onoats.convert.main", _recorder(called))
    assert cli.main(["convert", "--once", "--data-dir", "/tmp/x"]) == 0
    assert called["argv"] == ["--once", "--data-dir", "/tmp/x"]


def test_init_routes(monkeypatch):
    called = {}
    monkeypatch.setattr("onoats.init.main", _recorder(called))
    assert cli.main(["init", "--categories", "work"]) == 0
    assert called["argv"] == ["--categories", "work"]


def test_devices_routes(monkeypatch):
    """`devices` enumerates via pyaudio; patch PyAudio so no real device needed."""

    class FakePA:
        def get_device_count(self):
            return 1

        def get_device_info_by_index(self, i):
            return {
                "name": "Fake Mic",
                "maxInputChannels": 1,
                "maxOutputChannels": 0,
                "defaultSampleRate": 16000.0,
            }

        def terminate(self):
            pass

    import sys
    import types

    fake_pyaudio = types.ModuleType("pyaudio")
    fake_pyaudio.PyAudio = FakePA
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pyaudio)
    rc = cli.main(["devices"])
    assert rc == 0


def _install_fake_pyaudio(monkeypatch):
    class FakePA:
        def get_device_count(self):
            return 0

        def get_device_info_by_index(self, i):  # pragma: no cover - count is 0
            raise IndexError(i)

        def terminate(self):
            pass

    import sys
    import types

    fake_pyaudio = types.ModuleType("pyaudio")
    fake_pyaudio.PyAudio = FakePA
    monkeypatch.setitem(sys.modules, "pyaudio", fake_pyaudio)


def test_devices_socket_note(monkeypatch, capsys):
    """Under AUDIO_SOURCE=socket the enumeration gets the PortAudio-only note —
    the native capturer binds the default input / default-output tap, never a
    device from this list (release-plan Phase 5)."""
    _install_fake_pyaudio(monkeypatch)
    monkeypatch.setenv("AUDIO_SOURCE", "socket")
    assert cli.main(["devices"]) == 0
    out = capsys.readouterr().out
    assert "PortAudio-only" in out
    assert "onoats status" in out


def test_devices_no_note_on_portaudio(monkeypatch, capsys):
    _install_fake_pyaudio(monkeypatch)
    monkeypatch.delenv("AUDIO_SOURCE", raising=False)
    assert cli.main(["devices"]) == 0
    assert "PortAudio-only" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_not_running(capsys, _isolate_env):
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "not running" in out.lower()
    assert str(_isolate_env) in out


def test_status_running(capsys, _isolate_env, monkeypatch):
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("4242\nonoats-bot\ncmd\n0.0\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_process_alive", lambda pid: True)
    # Live readback matches the stored fingerprint → genuine recorder.
    monkeypatch.setattr("onoats._vendor.pid._live_ps_cmdline", lambda pid: "cmd")
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RUNNING" in out
    assert "4242" in out


def test_status_recycled_pid_reports_stopped(capsys, _isolate_env, monkeypatch):
    """A stale pid file whose pid the kernel reassigned to an unrelated
    program must NOT report RUNNING: kill(0) says alive, but the cmdline
    fingerprint mismatch proves pid recycling."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("4242\nonoats-bot\nonoats bot\n0.0\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_process_alive", lambda pid: True)
    monkeypatch.setattr(
        "onoats._vendor.pid._live_ps_cmdline", lambda pid: "/usr/bin/some-imposter"
    )
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RUNNING" not in out
    assert "not running" in out.lower()


def test_status_indeterminate_ps_probe_stays_running(capsys, _isolate_env, monkeypatch):
    """A failed ``ps`` readback is indeterminate, not proof of recycling —
    the read-only verdict must not flap a live recorder to stopped."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("4242\nonoats-bot\nonoats bot\n0.0\n", encoding="utf-8")
    monkeypatch.setattr(cli, "_process_alive", lambda pid: True)
    monkeypatch.setattr("onoats._vendor.pid._live_ps_cmdline", lambda pid: None)
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RUNNING" in out


def test_status_portaudio_shows_configured_device_names(
    capsys, _isolate_env, monkeypatch, tmp_path
):
    """PortAudio fallback path: `onoats status` surfaces the [devices] names the
    recorder binds by (the wrong-device guard — release-plan Phase 5)."""
    cfg_dir = tmp_path / "config" / "onoats"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.toml").write_text(
        '[devices]\nmic = "Built-in Mic"\nsystem = "BlackHole 2ch"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("AUDIO_SOURCE", raising=False)  # default: portaudio
    monkeypatch.delenv("MIC_INPUT_DEVICE", raising=False)
    monkeypatch.delenv("SYSTEM_INPUT_DEVICE", raising=False)

    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "configured mic (PortAudio): Built-in Mic (from config)" in out
    assert "configured system (PortAudio): BlackHole 2ch (from config)" in out


def test_status_portaudio_defaults_without_devices_config(
    capsys, _isolate_env, monkeypatch
):
    monkeypatch.delenv("AUDIO_SOURCE", raising=False)
    monkeypatch.delenv("MIC_INPUT_DEVICE", raising=False)
    monkeypatch.delenv("SYSTEM_INPUT_DEVICE", raising=False)

    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "configured mic (PortAudio): <system default> (default)" in out
    assert "configured system (PortAudio): <not configured> (default)" in out


def test_status_socket_hides_portaudio_config_lines(capsys, _isolate_env, monkeypatch):
    monkeypatch.setenv("AUDIO_SOURCE", "socket")
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "configured mic (PortAudio)" not in out


# ---------------------------------------------------------------------------
# flush — verifies live process identity before sending SIGUSR1
# ---------------------------------------------------------------------------


def test_flush_sends_sigusr1(monkeypatch, _isolate_env):
    """Happy path: live process cmdline matches the stored fingerprint."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("9001\nonoats-bot\nonoats bot\n0.0\n", encoding="utf-8")

    sent = {}

    def fake_kill(pid, sig):
        # sig 0 is the liveness probe; record only the real signal.
        if sig != 0:
            sent["pid"] = pid
            sent["sig"] = sig

    monkeypatch.setattr("os.kill", fake_kill)
    # Live readback matches the stored 3rd-line fingerprint → identity confirmed.
    monkeypatch.setattr("onoats._vendor.pid._live_ps_cmdline", lambda pid: "onoats bot")
    rc = cli.main(["flush"])
    assert rc == 0
    assert sent["pid"] == 9001
    assert sent["sig"] == signal.SIGUSR1


def test_flush_no_pid_file(_isolate_env):
    assert cli.main(["flush"]) == 1


def test_flush_stale_dead_pid(monkeypatch, _isolate_env):
    """A pid whose process is gone is stale: no signal, pid file removed."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345\nonoats-bot\nonoats bot\n0.0\n", encoding="utf-8")

    def fake_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr("os.kill", fake_kill)
    assert cli.main(["flush"]) == 1
    assert not pid_path.exists()  # stale file cleaned up


def test_flush_ignores_foreign_pid_marker(_isolate_env):
    """A pid file without the onoats-bot marker is not flushable."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("9001\nsomething-else\ncmd\n0.0\n", encoding="utf-8")
    assert cli.main(["flush"]) == 1


def test_flush_refuses_legacy_pid_file_without_fingerprint(monkeypatch, _isolate_env):
    """A marker-valid but fingerprint-less (legacy) pid file is not signalled."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # Two-line legacy format: pid + marker, no cmdline fingerprint.
    pid_path.write_text("9001\nonoats-bot\n", encoding="utf-8")

    def fake_kill(pid, sig):  # pragma: no cover - must never be called
        raise AssertionError("flush must not signal a pid it cannot verify")

    monkeypatch.setattr("os.kill", fake_kill)
    rc = cli.main(["flush"])
    assert rc == 1
    # No fingerprint to compare against → file is *not* treated as stale.
    assert pid_path.exists()


def test_flush_keeps_pid_file_when_ps_probe_fails(monkeypatch, capsys, _isolate_env):
    """A live recorder whose ``ps`` identity probe fails (ps missing/timeout)
    must NOT be signalled and must NOT have its pid file deleted — a transient
    probe failure is indeterminate, not proof the recorder is gone."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("9001\nonoats-bot\nonoats bot\n0.0\n", encoding="utf-8")

    def fake_kill(pid, sig):
        if sig == 0:
            return  # liveness probe succeeds → process is alive
        raise AssertionError("flush must not signal an unverifiable pid")

    monkeypatch.setattr("os.kill", fake_kill)
    # Identity probe fails (e.g. ps unavailable / timed out).
    monkeypatch.setattr("onoats._vendor.pid._live_ps_cmdline", lambda pid: None)

    rc = cli.main(["flush"])
    err = capsys.readouterr().err.lower()

    assert rc == 1
    assert "could not verify" in err
    # Live recorder's pid file must survive an indeterminate probe.
    assert pid_path.exists()


def test_flush_refuses_recycled_pid_identity_mismatch(capsys, _isolate_env):
    """Regression: a recycled pid pointing at an unrelated *live* process must
    not be signalled. Mirrors koda's shell-guard regression.

    Integration-flavoured: exercises the real ``ps`` readback path. Skips on
    the (rare) host without ``ps``/``sleep`` rather than misreporting the
    mismatch as a "not running" stale path.
    """
    import shutil
    import subprocess

    if not shutil.which("ps") or not shutil.which("sleep"):
        pytest.skip("requires ps and sleep for the live identity readback")

    # A real, unrelated live process whose cmdline will not match the stored
    # fingerprint. SIGUSR1 would terminate it if flush signalled blindly.
    proc = subprocess.Popen(["sleep", "30"])
    try:
        pid_path = cli._pid_path(_isolate_env)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(
            f"{proc.pid}\nonoats-bot\nonoats bot (this is not sleep)\n0.0\n",
            encoding="utf-8",
        )

        rc = cli.main(["flush"])
        err = capsys.readouterr().err.lower()

        assert rc == 1
        assert "identity mismatch" in err
        # The unrelated process must still be alive — no signal was sent.
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)
