"""Phase 3 — `onoats.stt.launchd`: the `kickstart_stt_server()` leaf helper and
the shared cooldown registry.

Design intent (dev plan Phase 3): kickstart is attempted at most once per
preflight failure, only after handshake-unreachable exhaustion, and only when
the shared cooldown has elapsed — it never raises itself. This module is a
leaf (no `runtime`/`status`/`dual` imports) so both `runtime.py` (preflight,
this phase) and `websocket_stt_service.py` (live reconnect, Phase 4) can
import it without either importing the other.
"""

from __future__ import annotations

import subprocess

import pytest

from onoats.stt import launchd

# ---------------------------------------------------------------------------
# Leaf-module boundary
# ---------------------------------------------------------------------------


def test_launchd_module_imports_no_runtime_status_dual():
    """`launchd.py` must stay a leaf: `runtime.py` (preflight) and
    `websocket_stt_service.py` (Phase 4 live path) both import it, and
    `runtime.py:692` already lazy-imports `websocket_stt_service` — a
    reverse import here would create a cycle."""
    src = launchd.__file__
    text = open(src, encoding="utf-8").read()
    assert "import onoats.runtime" not in text
    assert "from onoats import runtime" not in text
    assert "import onoats.status" not in text
    assert "from onoats import status" not in text
    assert "import onoats.dual" not in text
    assert "from onoats import dual" not in text


# ---------------------------------------------------------------------------
# kickstart_stt_server() — exact argv, never raises
# ---------------------------------------------------------------------------


def test_kickstart_builds_exact_argv(monkeypatch):
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501)

    ok = launchd.kickstart_stt_server("pipecat.stt-server")

    assert ok is True
    assert captured["argv"] == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/pipecat.stt-server",
    ]
    # Mirrors the existing `_own_ps_cmdline` pattern (runtime.py:1021).
    assert captured["kwargs"]["capture_output"] is True
    assert captured["kwargs"]["text"] is True
    assert captured["kwargs"]["timeout"] == 5
    assert captured["kwargs"]["check"] is False


def test_kickstart_resolves_uid_via_os_getuid_when_not_passed(monkeypatch):
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 999)

    launchd.kickstart_stt_server("pipecat.stt-server")

    assert captured["argv"][-1] == "gui/999/pipecat.stt-server"


def test_kickstart_uses_explicit_uid_over_os_getuid(monkeypatch):
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    # If this is consulted, the test should fail loudly rather than pass by
    # accident on a matching uid.
    monkeypatch.setattr(
        launchd.os,
        "getuid",
        lambda: (_ for _ in ()).throw(AssertionError("os.getuid() must not be called")),
    )

    launchd.kickstart_stt_server("pipecat.stt-server", uid=42)

    assert captured["argv"][-1] == "gui/42/pipecat.stt-server"


def test_kickstart_returns_true_on_zero_exit(monkeypatch):
    monkeypatch.setattr(
        launchd.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, returncode=0, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(launchd.os, "getuid", lambda: 1)
    assert launchd.kickstart_stt_server("label") is True


def test_kickstart_returns_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        launchd.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, returncode=1, stdout="", stderr="No such process"
        ),
    )
    monkeypatch.setattr(launchd.os, "getuid", lambda: 1)
    assert launchd.kickstart_stt_server("label") is False


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired(cmd=["launchctl"], timeout=5),
        PermissionError("not permitted"),
        FileNotFoundError("no launchctl"),
        OSError("generic os failure"),
    ],
)
def test_kickstart_never_raises_swallows_subprocess_failures(monkeypatch, exc):
    def fake_run(argv, **kwargs):
        raise exc

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 1)

    # Must not raise — kickstart is best-effort, callers treat any failure
    # as "kickstart did not help" and fall through to their existing error path.
    assert launchd.kickstart_stt_server("label") is False


# ---------------------------------------------------------------------------
# Shared cooldown registry
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cooldown_registry():
    """The cooldown registry is module-level (label -> last-kickstart time),
    shared across every caller (preflight here, Phase 4 live path later) —
    must not leak between tests."""
    launchd._last_kickstart.clear()
    yield
    launchd._last_kickstart.clear()


def _clock(seq):
    it = iter(seq)
    return lambda: next(it)


def test_cooldown_elapsed_true_when_label_never_stamped():
    assert launchd._cooldown_elapsed("never-stamped", clock=lambda: 1000.0) is True


def test_stamp_then_immediately_check_is_not_elapsed():
    clock = _clock([100.0, 100.0])
    launchd._stamp_cooldown("label-a", clock=clock)
    assert launchd._cooldown_elapsed("label-a", clock=clock) is False


def test_cooldown_elapsed_after_window():
    clock = _clock([0.0, 0.0 + launchd.KICKSTART_COOLDOWN_SEC])
    launchd._stamp_cooldown("label-a", clock=clock)
    assert launchd._cooldown_elapsed("label-a", clock=clock) is True


def test_cooldown_not_elapsed_just_under_window():
    clock = _clock([0.0, launchd.KICKSTART_COOLDOWN_SEC - 0.01])
    launchd._stamp_cooldown("label-a", clock=clock)
    assert launchd._cooldown_elapsed("label-a", clock=clock) is False


def test_cooldown_is_keyed_per_label():
    """Stamping one label's cooldown must not gate a different label — the
    registry is shared across two call sites (preflight, Phase 4 live
    reconnect) that may kickstart different labels concurrently."""
    clock = _clock([0.0])
    launchd._stamp_cooldown("label-a", clock=clock)
    assert launchd._cooldown_elapsed("label-b", clock=lambda: 0.0) is True


def test_kickstart_cooldown_sec_constant_is_30():
    assert launchd.KICKSTART_COOLDOWN_SEC == 30
