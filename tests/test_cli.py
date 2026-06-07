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
    rc = cli.main(["status"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "RUNNING" in out
    assert "4242" in out


# ---------------------------------------------------------------------------
# flush — sends SIGUSR1 to the resolved pid
# ---------------------------------------------------------------------------


def test_flush_sends_sigusr1(monkeypatch, _isolate_env):
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("9001\nonoats-bot\ncmd\n0.0\n", encoding="utf-8")

    sent = {}

    def fake_kill(pid, sig):
        sent["pid"] = pid
        sent["sig"] = sig

    monkeypatch.setattr("os.kill", fake_kill)
    rc = cli.main(["flush"])
    assert rc == 0
    assert sent["pid"] == 9001
    assert sent["sig"] == signal.SIGUSR1


def test_flush_no_pid_file(_isolate_env):
    assert cli.main(["flush"]) == 1


def test_flush_stale_pid(monkeypatch, _isolate_env):
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345\nonoats-bot\ncmd\n0.0\n", encoding="utf-8")

    def fake_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr("os.kill", fake_kill)
    assert cli.main(["flush"]) == 1


def test_flush_ignores_foreign_pid_marker(monkeypatch, _isolate_env):
    """A pid file without the onoats-bot marker is not flushable."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("9001\nsomething-else\ncmd\n0.0\n", encoding="utf-8")
    assert cli.main(["flush"]) == 1
