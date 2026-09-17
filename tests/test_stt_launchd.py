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

import asyncio
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
    # Absolute path, not a bare `launchctl` resolved through the inherited
    # PATH: this shellout restarts/kills a launchd job, so it must not be
    # redirectable by a prepended PATH entry.
    assert captured["argv"] == [
        "/bin/launchctl",
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


# ---------------------------------------------------------------------------
# Round-2 review-gauntlet fixes
# ---------------------------------------------------------------------------


def test_kickstart_never_raises_on_embedded_nul_label(monkeypatch):
    """Finding 7: `subprocess` raises ValueError (NOT OSError) for an argv
    element containing an embedded NUL. The label is env/config-sourced, so
    a NUL can reach here — and an uncaught ValueError escapes the documented
    "never raises" contract and aborts preflight instead of producing a
    clean kickstart-failed signal."""

    def fake_run(argv, **kwargs):
        # Mirrors CPython's real behaviour for a NUL in argv.
        raise ValueError("embedded null byte")

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 1)

    assert launchd.kickstart_stt_server("bad\x00label") is False


def test_recovery_message_is_bare_and_names_the_label():
    """Finding 1: one owner for the recovery text, and it never carries the
    `"stt: "` branch prefix — prefixing belongs to
    `status.format_warning_branch` alone."""
    msg = launchd.recovery_message("pipecat.stt-server")
    assert msg == "server restarted automatically (kickstarted pipecat.stt-server)"
    assert not msg.startswith("stt:")


def test_try_kickstart_checks_stamps_and_kickstarts_in_order(monkeypatch):
    """Finding 8: the check-cooldown / stamp-before-await / kickstart
    sequence has ONE owner now. The stamp must land before the `await` —
    that is the whole reason two instances exhausting concurrently on the
    same event loop can't both pass the check."""
    import asyncio

    events: list[str] = []
    monkeypatch.setattr(
        launchd, "_stamp_cooldown", lambda label, **kw: events.append("stamped")
    )
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: events.append("kickstarted") or True,
    )

    async def fake_to_thread(fn, *args, **kwargs):
        events.append("awaited")
        return fn(*args, **kwargs)

    monkeypatch.setattr(launchd.asyncio, "to_thread", fake_to_thread)

    assert asyncio.run(launchd.try_kickstart("label-a")) is True
    assert events == ["stamped", "awaited", "kickstarted"]


def test_try_kickstart_returns_false_without_stamping_when_cooldown_active(monkeypatch):
    import asyncio

    stamped: list[str] = []
    monkeypatch.setattr(launchd, "_cooldown_elapsed", lambda label, **kw: False)
    monkeypatch.setattr(
        launchd, "_stamp_cooldown", lambda label, **kw: stamped.append(label)
    )
    monkeypatch.setattr(
        launchd,
        "kickstart_stt_server",
        lambda label, **kw: pytest.fail("must not kickstart inside the cooldown"),
    )

    assert asyncio.run(launchd.try_kickstart("label-a")) is False
    assert stamped == []


def test_try_kickstart_stamps_even_when_kickstart_fails(monkeypatch):
    """A failed kickstart still counts against the window, so a wedged label
    isn't hammered once per reconnect cycle."""
    import asyncio

    monkeypatch.setattr(launchd, "kickstart_stt_server", lambda label, **kw: False)
    assert asyncio.run(launchd.try_kickstart("label-a")) is False
    assert "label-a" in launchd._last_kickstart


# ---------------------------------------------------------------------------
# Round-2 findings
# ---------------------------------------------------------------------------


def test_kickstart_subprocess_gets_a_minimal_explicit_env(monkeypatch):
    """Round-2 finding 8: `subprocess.run` inherited the FULL process
    environment, handing `launchctl` the STT/Deepgram credentials and any
    `DYLD_*` loader overrides. `launchctl kickstart` needs none of it — the
    env must be an explicit deny-by-default allowlist."""
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(launchd.subprocess, "run", fake_run)
    monkeypatch.setenv("STT_WS_TOKEN", "super-secret-token")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-secret")
    monkeypatch.setenv("DYLD_INSERT_LIBRARIES", "/tmp/evil.dylib")
    monkeypatch.setenv("HOME", "/Users/tester")

    assert launchd.kickstart_stt_server("pipecat.stt-server") is True

    env = captured["env"]
    assert "STT_WS_TOKEN" not in env
    assert "DEEPGRAM_API_KEY" not in env
    assert "DYLD_INSERT_LIBRARIES" not in env
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/Users/tester"


def test_confirm_window_is_decoupled_from_the_cooldown():
    """Round-2 finding 17: the post-kickstart confirmation window used the
    30s cooldown constant, but the confirming reconnect is demand-driven and
    can itself burn ~15.5s of reconnect backoff, so genuine recoveries
    silently dropped their on_recovery message AND never armed the transcript
    gate that resets the cooldown."""
    assert launchd.KICKSTART_CONFIRM_WINDOW_SEC > launchd.KICKSTART_COOLDOWN_SEC
    # Must clear the full live reconnect backoff schedule (~15.5s) on top of
    # the cooldown, plus a realistic gap of silence before the next segment.
    assert launchd.KICKSTART_CONFIRM_WINDOW_SEC >= launchd.KICKSTART_COOLDOWN_SEC + 16


def test_reset_cooldown_is_public():
    """Round-2 finding 11: `_reset_cooldown` was underscore-private but is the
    documented cross-module production API called from
    `websocket_stt_service`."""
    assert callable(launchd.reset_cooldown)
    assert not hasattr(launchd, "_reset_cooldown")


def test_reset_cooldown_untokened_is_unconditional():
    launchd._stamp_cooldown("label-a")
    launchd.reset_cooldown("label-a")
    assert "label-a" not in launchd._last_kickstart


def test_sibling_instance_still_failing_blocks_the_early_cooldown_reset():
    """Round-2 finding 18: the cooldown is PROCESS-WIDE but confirmation is
    per-instance. mic confirming a transcript used to clear the shared stamp
    outright, re-arming system's very next exhaustion to SIGKILL + restart the
    server mic was actively, successfully using."""
    launchd._unhealthy.clear()
    launchd._stamp_cooldown("shared")
    launchd.mark_unhealthy("shared", "mic")
    launchd.mark_unhealthy("shared", "system")

    # mic confirms health first — system is still exhausted and unconfirmed,
    # so the shared stamp must survive.
    launchd.reset_cooldown("shared", "mic")
    assert "shared" in launchd._last_kickstart

    # Once system confirms too, the stamp drops.
    launchd.reset_cooldown("shared", "system")
    assert "shared" not in launchd._last_kickstart
    launchd._unhealthy.clear()


def test_clear_unhealthy_releases_a_stopped_instances_hold():
    """A torn-down instance (`cleanup()`) must not hold its sibling's early
    cooldown reset hostage for the rest of the process's life."""
    launchd._unhealthy.clear()
    launchd._stamp_cooldown("shared")
    launchd.mark_unhealthy("shared", "mic")
    launchd.mark_unhealthy("shared", "system")

    launchd.clear_unhealthy("shared", "system")  # system's pipeline stopped
    launchd.reset_cooldown("shared", "mic")
    assert "shared" not in launchd._last_kickstart
    launchd._unhealthy.clear()


# ---------------------------------------------------------------------------
# Round-3 review-gauntlet fixes
# ---------------------------------------------------------------------------


def test_kickstart_returns_false_when_getuid_is_unavailable(monkeypatch):
    """Round-3 finding 11: `os.getuid()` was resolved OUTSIDE the handled
    block, so on a platform without it (`os.getuid` is POSIX-only) the very
    first line raised `AttributeError` — breaking the function's "never
    raises" contract before the broad `except` was ever entered. Defensive
    for a macOS-only app, but the contract is what every call site relies on
    to fall through to today's behaviour."""
    monkeypatch.delattr(launchd.os, "getuid")

    def _boom(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("subprocess.run must not be reached")

    monkeypatch.setattr(launchd.subprocess, "run", _boom)

    assert launchd.kickstart_stt_server("pipecat.stt-server") is False


# ---------------------------------------------------------------------------
# Round-4 finding 6: defense-in-depth label re-validation
# ---------------------------------------------------------------------------


def test_kickstart_rejects_a_label_containing_a_slash(monkeypatch):
    """A `/` in `label` would redirect the `gui/<uid>/<label>` kickstart
    target at a different job or domain. Every shipped caller already
    resolves its label through `onoats.config.validate_launchd_label`
    before calling this function, but the function itself had no validation
    of its own — re-check here so a caller that bypasses the config
    resolver (a bug, a future caller, a test) can't forge the target."""

    def _boom(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("subprocess.run must not be reached for a bad label")

    monkeypatch.setattr(launchd.subprocess, "run", _boom)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 1)

    assert launchd.kickstart_stt_server("evil/../other-domain") is False


def test_kickstart_rejects_a_label_containing_the_warning_delimiter(monkeypatch):
    """A `label` containing `"; "` or `": "` would forge a pseudo-branch
    entry the next time `status._parse_warning_branches` reads the merged
    recovery message that embeds this label (`recovery_message`)."""

    def _boom(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("subprocess.run must not be reached for a bad label")

    monkeypatch.setattr(launchd.subprocess, "run", _boom)
    monkeypatch.setattr(launchd.os, "getuid", lambda: 1)

    assert launchd.kickstart_stt_server("a; b: c") is False


def test_kickstart_accepts_a_well_formed_label(monkeypatch):
    """No false positive: the re-validation must not reject the shipped
    label shapes (`pipecat.stt-server`, `pipecat.stt-server.nemotron`)."""
    monkeypatch.setattr(
        launchd.subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, returncode=0, stdout="", stderr=""
        ),
    )
    monkeypatch.setattr(launchd.os, "getuid", lambda: 1)

    assert launchd.kickstart_stt_server("pipecat.stt-server.nemotron") is True


def test_mark_unhealthy_documents_the_first_failure_registration_point():
    """Round-3 finding 2's contract, pinned at the registry layer: a token
    registered before any kickstart still blocks a sibling's early cooldown
    reset — the guard must not depend on the registering instance having
    exhausted its full backoff first."""
    launchd._last_kickstart.clear()
    launchd._unhealthy.clear()

    launchd._stamp_cooldown("shared")
    # "mic" exhausted and won the kickstart; "system" has only just started
    # failing (one refused connect, no exhaustion yet).
    launchd.mark_unhealthy("shared", "mic")
    launchd.mark_unhealthy("shared", "system")

    launchd.reset_cooldown("shared", "mic")
    assert "shared" in launchd._last_kickstart  # system still unconfirmed

    launchd.reset_cooldown("shared", "system")
    assert "shared" not in launchd._last_kickstart

    launchd._unhealthy.clear()


# ---------------------------------------------------------------------------
# Round-5 finding 15: the "never raises" contract has to cover its own
# validator, and a label that can never succeed must not burn the cooldown.
# ---------------------------------------------------------------------------


def test_kickstart_stt_server_never_raises_on_a_non_str_label():
    """`validate_launchd_label` indexes/regexes its argument, and its call
    sat ABOVE the `try` — so a non-`str` label raised `TypeError` straight
    out of a function documented as never raising."""
    assert launchd.kickstart_stt_server(None) is False  # type: ignore[arg-type]
    assert launchd.kickstart_stt_server(object()) is False  # type: ignore[arg-type]


def test_try_kickstart_rejects_a_bad_label_without_consuming_the_cooldown():
    """The stamp deliberately survives a *failed* kickstart, but a malformed
    label can never succeed — burning the shared window on it would suppress
    the next legitimate kickstart. Validation therefore runs before the
    stamp, and `try_kickstart` still never raises."""
    launchd._last_kickstart.clear()
    assert asyncio.run(launchd.try_kickstart("has/slash")) is False
    assert asyncio.run(launchd.try_kickstart(None)) is False  # type: ignore[arg-type]
    assert launchd._last_kickstart == {}
