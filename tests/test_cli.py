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
    for sub in (
        "init",
        "bot",
        "bot-single",
        "flush",
        "stop",
        "convert",
        "devices",
        "status",
    ):
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


# ---------------------------------------------------------------------------
# stop — behavioural twin of flush, EXCEPT it sends SIGTERM (graceful shutdown)
# instead of SIGUSR1. The identity-checked signalling (resolve_flush_target →
# marker + cmdline fingerprint) is reused verbatim, so a recycled foreign pid is
# never signalled — which matters MORE here because SIGTERM kills by default.
# Every test_flush_* branch is mirrored 1:1 below.
# ---------------------------------------------------------------------------


def test_stop_sends_sigterm(monkeypatch, _isolate_env):
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
    rc = cli.main(["stop"])
    assert rc == 0
    assert sent["pid"] == 9001
    assert sent["sig"] == signal.SIGTERM


@pytest.mark.parametrize(
    ("command", "want_sig", "reject_sig"),
    [
        ("stop", signal.SIGTERM, signal.SIGUSR1),
        ("flush", signal.SIGUSR1, signal.SIGTERM),
    ],
)
def test_stop_and_flush_send_distinct_signals(
    monkeypatch, _isolate_env, command, want_sig, reject_sig
):
    """Differential guard: the ONLY intended divergence between stop and flush is
    the signal number. ``stop`` MUST send SIGTERM and NOT SIGUSR1; ``flush`` MUST
    send SIGUSR1 and NOT SIGTERM. A copy-paste/shared-helper signal swap (or a
    handler accidentally calling the other) fails here."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("9001\nonoats-bot\nonoats bot\n0.0\n", encoding="utf-8")

    sent = []

    def fake_kill(pid, sig):
        if sig != 0:  # ignore the liveness probe
            sent.append(sig)

    monkeypatch.setattr("os.kill", fake_kill)
    monkeypatch.setattr("onoats._vendor.pid._live_ps_cmdline", lambda pid: "onoats bot")

    rc = cli.main([command])
    assert rc == 0
    assert sent == [want_sig], f"{command} must send exactly {want_sig!r}, got {sent!r}"
    assert reject_sig not in sent, f"{command} must NOT send {reject_sig!r}"


def test_stop_no_pid_file(_isolate_env):
    assert cli.main(["stop"]) == 1


def test_stop_stale_dead_pid(monkeypatch, _isolate_env):
    """A pid whose process is gone is stale: no signal, pid file removed."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("12345\nonoats-bot\nonoats bot\n0.0\n", encoding="utf-8")

    def fake_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr("os.kill", fake_kill)
    assert cli.main(["stop"]) == 1
    assert not pid_path.exists()  # stale file cleaned up


def test_stop_ignores_foreign_pid_marker(_isolate_env):
    """A pid file without the onoats-bot marker is not stoppable."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("9001\nsomething-else\ncmd\n0.0\n", encoding="utf-8")
    assert cli.main(["stop"]) == 1


def test_stop_refuses_legacy_pid_file_without_fingerprint(monkeypatch, _isolate_env):
    """A marker-valid but fingerprint-less (legacy) pid file is not signalled."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # Two-line legacy format: pid + marker, no cmdline fingerprint.
    pid_path.write_text("9001\nonoats-bot\n", encoding="utf-8")

    def fake_kill(pid, sig):  # pragma: no cover - must never be called
        raise AssertionError("stop must not signal a pid it cannot verify")

    monkeypatch.setattr("os.kill", fake_kill)
    rc = cli.main(["stop"])
    assert rc == 1
    # No fingerprint to compare against → file is *not* treated as stale.
    assert pid_path.exists()


def test_stop_keeps_pid_file_when_ps_probe_fails(monkeypatch, capsys, _isolate_env):
    """A live recorder whose ``ps`` identity probe fails (ps missing/timeout)
    must NOT be signalled and must NOT have its pid file deleted — a transient
    probe failure is indeterminate, not proof the recorder is gone."""
    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text("9001\nonoats-bot\nonoats bot\n0.0\n", encoding="utf-8")

    def fake_kill(pid, sig):
        if sig == 0:
            return  # liveness probe succeeds → process is alive
        raise AssertionError("stop must not signal an unverifiable pid")

    monkeypatch.setattr("os.kill", fake_kill)
    # Identity probe fails (e.g. ps unavailable / timed out).
    monkeypatch.setattr("onoats._vendor.pid._live_ps_cmdline", lambda pid: None)

    rc = cli.main(["stop"])
    err = capsys.readouterr().err.lower()

    assert rc == 1
    assert "could not verify" in err
    # Live recorder's pid file must survive an indeterminate probe.
    assert pid_path.exists()


def test_stop_refuses_recycled_pid_identity_mismatch(capsys, _isolate_env):
    """Highest-value regression (given SIGTERM's lethality): a recycled pid
    pointing at an unrelated *live* process must not be signalled. A blind
    SIGTERM would terminate that foreign process; the identity check stops it.

    Integration-flavoured: exercises the real ``ps`` readback path. Skips on the
    (rare) host without ``ps``/``sleep`` rather than misreporting the mismatch as
    a "not running" stale path.
    """
    import shutil
    import subprocess

    if not shutil.which("ps") or not shutil.which("sleep"):
        pytest.skip("requires ps and sleep for the live identity readback")

    # A real, unrelated live process whose cmdline will not match the stored
    # fingerprint. SIGTERM would terminate it if stop signalled blindly.
    proc = subprocess.Popen(["sleep", "30"])
    try:
        pid_path = cli._pid_path(_isolate_env)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(
            f"{proc.pid}\nonoats-bot\nonoats bot (this is not sleep)\n0.0\n",
            encoding="utf-8",
        )

        rc = cli.main(["stop"])
        err = capsys.readouterr().err.lower()

        assert rc == 1
        assert "identity mismatch" in err
        # The unrelated process must still be ALIVE — no SIGTERM was sent.
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_stop_in_dispatch_table():
    """`stop` is wired into the subcommand dispatch table."""
    assert cli._HANDLERS["stop"] is cli._cmd_stop


def test_stop_help_resolves_without_booting(capsys):
    """`onoats stop --help` resolves via _cmd_stop's own local argparse (lazy
    resolver import), so it never boots a service."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["stop", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "usage" in out
    assert "onoats stop" in out


def test_stop_stale_cleanup_preserves_freshly_written_pid(monkeypatch, _isolate_env):
    """[high] regression (Codex adversarial review round 6): stale cleanup must
    COMPARE-and-unlink. A blind unlink would delete a NEWER recorder's pid file
    written in the window between stale resolution and cleanup (the new recorder
    won the single-instance lock and published its pid). Simulate that racing
    write inside resolve_flush_target; the stale cleanup must NOT delete it."""
    import os as _os

    from onoats._vendor.pid import FlushTarget

    pid_path = cli._pid_path(_isolate_env)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    # The old, dead pid `stop` resolves first (valid marker → parseable).
    pid_path.write_text("999999\nonoats-bot\nonoats bot\n0.0\n", encoding="utf-8")

    fresh_pid = _os.getpid()  # a live pid standing in for the new recorder

    def _racing_resolve(p):
        # The new recorder won the lock and published its pid in the window
        # between our prior-read and the stale cleanup.
        p.write_text(f"{fresh_pid}\nonoats-bot\nonoats bot\n123.0\n", encoding="utf-8")
        return FlushTarget(pid=None, reason="stale (dead pid)", stale=True)

    monkeypatch.setattr("onoats._vendor.pid.resolve_flush_target", _racing_resolve)
    rc = cli.main(["stop"])
    assert rc == 1
    # The freshly-written pid file MUST survive — never deleted by stale cleanup.
    assert pid_path.exists(), "stale cleanup must not delete a newer recorder's file"
    assert pid_path.read_text(encoding="utf-8").startswith(f"{fresh_pid}\n")


def test_bot_single_held_lock_fails_before_device_selection(monkeypatch, _isolate_env):
    """[high] regression (Codex adversarial review round 6): `bot-single` must
    claim the single-instance lock BEFORE any capture setup. With the slot already
    held, run_onoats must fail (rc=1) without ever calling select_input_device —
    a losing start must not enumerate/touch audio before discovering it lost."""
    import fcntl as _fcntl
    import os as _os
    import sys as _sys

    if _sys.platform == "win32":
        pytest.skip("flock single-instance lock is POSIX-only")

    from onoats.runtime import LOCK_FILENAME

    active = _isolate_env / ".active"
    active.mkdir(parents=True, exist_ok=True)
    holder = _os.open(str(active / LOCK_FILENAME), _os.O_RDWR | _os.O_CREAT, 0o644)
    _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)

    selected = {"n": 0}
    monkeypatch.setattr(
        "onoats.config.audio_devices.select_input_device",
        lambda **k: selected.__setitem__("n", selected["n"] + 1),
    )
    try:
        from onoats.__main__ import main as single_main

        rc = single_main([])
        assert rc == 1, "a losing bot-single start must exit rc=1"
        assert selected["n"] == 0, (
            "device selection must not run when the instance lock is already held"
        )
    finally:
        _fcntl.flock(holder, _fcntl.LOCK_UN)
        _os.close(holder)
