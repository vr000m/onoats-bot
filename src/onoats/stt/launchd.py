"""``launchctl kickstart`` self-healing for the STT server + shared cooldown.

Leaf module: no ``runtime``/``status``/``dual`` imports. Both the preflight
path (``onoats.runtime._preflight_stt_ws``) and the live reconnect path
(``onoats.stt.websocket_stt_service.WebSocketSTTService``) import from here
without either importing the other — this module is the shared meeting
point for the process-wide, label-keyed kickstart cooldown.

``kickstart_stt_server`` never raises — it follows the same
subprocess-shellout convention as ``runtime._own_ps_cmdline``:
``subprocess.run(argv, capture_output=True, text=True, timeout=N,
check=False)`` wrapped in a broad, non-raising ``except``. Every async call
site invokes it via ``asyncio.to_thread`` so a wedged ``launchd`` job never
blocks the event loop.

**Where the kickstart recovery state machine lives** (it is deliberately
split across three layers; this is the map, since no single module owns it):

- *Cross-instance, process-wide* — this module: ``_last_kickstart`` (the
  label-keyed cooldown stamp, via ``try_kickstart``/``reset_cooldown``) and
  ``_unhealthy`` (label -> instance tokens currently failing, via
  ``mark_unhealthy``/``clear_unhealthy``), plus the one recovery message
  text (``recovery_message``).
- *Per-instance* — ``onoats.stt.websocket_stt_service.WebSocketSTTService``:
  ``_instance_token``, ``_kickstart_awaiting_connect`` (+ its
  ``KICKSTART_CONFIRM_WINDOW_SEC`` deadline), ``_kickstart_awaiting_transcript``
  and ``_preflight_confirm_pending`` — which gate *when* this instance fires
  its own ``on_recovery``/``on_preflight_confirmed``.
- *Presentation* — ``onoats.runtime._create_stt_service`` builds the
  swallow-and-log callbacks, and ``onoats.status`` owns the branch keys
  (``stt`` shared / ``stt-mic``/``stt-system`` per-instance) and the
  ``"<branch>: "`` prefix. ``AGENTS.md``'s "STT self-healing invariants"
  section states the load-bearing rules across all three.

Consolidating these into one owner is a worthwhile refactor but a
human-sized design call (it crosses the leaf-module boundary this module
exists to preserve), not a fixer's — see the dev plan's Findings.

**GUI-domain caveat**: ``gui/$UID/<label>`` assumes ``onoats bot`` runs
inside a GUI (Aqua) session, matching how the shipped LaunchAgents are
registered today. A non-GUI invocation (ssh session, headless daemon with no
Aqua session) gets a domain-not-found error from ``launchctl``, not the
"label not loaded" case this module's fall-through is written for —
the broad non-raising exception handling still absorbs it (returns
``False``), but the caller's log text may say "kickstart failed" when the
real cause is "no such domain." This is a pre-existing constraint of
launchd's GUI-domain model, not something this module can fix.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Callable

from loguru import logger

_KICKSTART_TIMEOUT_SEC = 5

# Absolute path, not a bare ``launchctl`` name resolved through the inherited
# PATH: this shellout has restart/kill effect on a launchd job, so it must not
# be redirectable by a PATH entry an unrelated wrapper script happened to
# prepend. ``/bin/launchctl`` is the shipped location on every supported macOS.
_LAUNCHCTL = "/bin/launchctl"

# Must exceed WebSocketSTTService's live reconnect backoff total (~15.5s,
# see websocket_stt_service.py's `_RECONNECT_BACKOFF_SECONDS` comment) plus
# margin, so a kickstart's own retry window can never itself trigger a
# second kickstart.
#
# Deep-review finding: this window does NOT uniformly hold for the full 30s
# across the preflight/live handoff. `mark_unhealthy` is deliberately never
# called from the preflight path (see its docstring), so after a *preflight*
# kickstart no instance is ever registered `_unhealthy` for the label — the
# live instances' very first `transcript.*` event then retires the stamp via
# `reset_cooldown` immediately (potentially ~1s in), not after the full
# window. This is accepted as correct, not a bug: a confirmed transcript
# does prove the restarted server works, which is exactly the condition
# `reset_cooldown` exists to detect. Only a *live-path* kickstart (which does
# register the kickstarting instance as unhealthy) is actually held to the
# full window until every registered instance confirms.
KICKSTART_COOLDOWN_SEC = 30

# How long after a kickstart a subsequent successful connect may still be
# attributed to it ("server restarted automatically" + arm the transcript
# gate). Deliberately NOT ``KICKSTART_COOLDOWN_SEC``: the confirming reconnect
# is demand-driven (it only happens on the next VAD-triggered segment) and can
# itself burn ~15.5s of reconnect backoff before succeeding, so a 30s window
# silently dropped genuine recoveries — and with them the arming of
# ``_kickstart_awaiting_transcript``, so ``reset_cooldown`` never fired
# either. Sized to cover the full backoff schedule plus a realistic gap of
# silence before the user next speaks, while still expiring a kickstart that
# launchd accepted but that never restored service (the stale-claim case this
# deadline exists for).
KICKSTART_CONFIRM_WINDOW_SEC = 120

# Minimal, deny-by-default environment for the ``launchctl`` shellout. The
# parent process environment carries STT/Deepgram credentials (``STT_WS_TOKEN``,
# ``DEEPGRAM_API_KEY``) and can carry loader-control variables
# (``DYLD_INSERT_LIBRARIES``); ``launchctl kickstart`` needs none of it — it
# only talks to launchd over XPC. Passing an explicit allowlist keeps secrets
# out of the child and keeps the child's loader behaviour non-overridable.
_ENV_PASSTHROUGH_KEYS = ("HOME", "TMPDIR", "USER", "LOGNAME")


def _kickstart_env() -> dict[str, str]:
    """Build the explicit, minimal env for the ``launchctl`` subprocess."""
    env = {"PATH": "/usr/bin:/bin"}
    for key in _ENV_PASSTHROUGH_KEYS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    return env


# label -> last-kickstart monotonic time. Process-wide and label-keyed
# (not per-WebSocketSTTService-instance): dual.py constructs two
# independent instances (mic + system) against the same server, and the
# startup preflight path stamps the same registry, so a preflight kickstart
# counts against the live-session budget too.
_last_kickstart: dict[str, float] = {}

# label -> set of instance tokens currently in the "reconnect backoff
# exhausted, not yet confirmed healthy again" state. Guards the EARLY cooldown
# reset: the cooldown is process-wide, but confirmation is per-instance, so one
# instance's confirmed transcript is not evidence that its sibling (the other
# of dual.py's mic/system pair, against the same physical server) is healthy.
# Without this, mic confirming recovery would clear the shared cooldown and let
# system's very next exhaustion SIGKILL + restart the server mic is actively
# using — the exact storm the shared cooldown exists to prevent. A lingering
# token can only delay the early reset, never block kickstart forever: the
# cooldown still expires on its own after ``KICKSTART_COOLDOWN_SEC``.
_unhealthy: dict[str, set[str]] = {}


def kickstart_stt_server(label: str, uid: int | None = None) -> bool:
    """Ask launchd to restart the managed job ``label``. Never raises.

    Runs ``launchctl kickstart -k gui/<uid>/<label>`` synchronously
    (best-effort shellout, mirrors ``runtime._own_ps_cmdline``). ``uid``
    defaults to ``os.getuid()`` when not passed. Returns ``True`` only on
    rc 0 — a non-zero exit (label not loaded, permission denied, no such
    domain) or any exception is swallowed and returns ``False``, so callers
    can treat every failure mode identically: fall through to today's
    behavior.

    A ``True`` return means only that launchd accepted the restart
    request, not that the server is answering again — callers must confirm
    recovery with their own post-kickstart handshake/reconnect.

    Defense in depth: ``label`` is re-validated here against the exact same
    allowlist ``onoats.config.validate_launchd_label`` enforces at
    config-resolution time, even though every shipped caller
    (``runtime._create_stt_service``) already resolves its label through
    that gate before this function ever sees it. This is a public entry
    point interpolated straight into the ``launchctl`` argv with no
    validation of its own otherwise — a future or test caller that bypasses
    the config resolver would forge the ``gui/<uid>/<label>`` service target
    verbatim (a ``label`` containing ``/`` redirects the kickstart at a
    different job or domain) or corrupt the recovery-message merge (a
    ``label`` containing ``"; "``/``": "`` forges a pseudo-branch entry on
    the next :func:`onoats.status._parse_warning_branches` read). A rejected
    label is treated exactly like every other failure mode here — logged
    and swallowed, returning ``False`` — matching this function's "never
    raises, callers fall through to today's behavior" contract rather than
    raising, which would break that contract for the one caller that most
    needs it to hold (a wedged/misconfigured label must never itself crash
    the preflight or reconnect path it is meant to heal).
    """
    try:
        # Inside the `try`, not above it. `validate_launchd_label` is typed
        # for `str` and indexes/regexes its argument, so a non-`str` label
        # from a bypassing caller raised `TypeError` straight out of a
        # function documented as never raising — and, via `try_kickstart`,
        # did so *after* the cooldown stamp had already been consumed. The
        # contract has to cover its own validator.
        from onoats.config import validate_launchd_label

        if validate_launchd_label(label) is None:
            logger.warning(
                f"STT: kickstart of {label!r} refused — not a well-formed "
                "launchd label (re-validated inside kickstart_stt_server, "
                "defense in depth)"
            )
            return False
        # `os.getuid` is POSIX-only and absent on Windows. onoats is a
        # macOS-only app, so this is defensive rather than a live platform
        # gap — but the whole contract of this function is "never raises",
        # and a bare `AttributeError` escaping from the *first* line would
        # break it before the handled block is ever entered.
        resolved_uid = uid if uid is not None else os.getuid()
        argv = [_LAUNCHCTL, "kickstart", "-k", f"gui/{resolved_uid}/{label}"]
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_KICKSTART_TIMEOUT_SEC,
            check=False,
            env=_kickstart_env(),
        )
        if result.returncode == 0:
            return True
        logger.warning(
            f"STT: kickstart of {label!r} failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
        return False
    except (
        FileNotFoundError,
        subprocess.SubprocessError,
        OSError,
        # ``os.getuid`` is absent on non-POSIX platforms; see the comment at
        # the top of the block. Keeps the "never raises" contract honest even
        # off the supported platform.
        AttributeError,
        # ``subprocess`` raises ValueError (not OSError) for an argv element
        # containing an embedded NUL. ``label`` is env/config-sourced, so a
        # NUL can reach here whenever a caller bypasses
        # ``onoats.config.validate_launchd_label``. Without this clause the "never
        # raises" contract breaks and preflight aborts instead of falling
        # through to its normal unreachable-server error.
        ValueError,
        # ``validate_launchd_label`` (now inside this ``try``) is typed for
        # ``str`` and regexes its argument, so a non-``str`` label from a
        # caller that bypassed the config resolver raises ``TypeError``
        # before any of the shapes above can fire.
        TypeError,
    ) as exc:
        logger.warning(f"STT: kickstart of {label!r} failed: {exc}")
        return False


def _cooldown_elapsed(label: str, clock: Callable[[], float] = time.monotonic) -> bool:
    """True if ``label`` has never been kickstarted, or its last kickstart
    was more than ``KICKSTART_COOLDOWN_SEC`` ago."""
    last = _last_kickstart.get(label)
    if last is None:
        return True
    return (clock() - last) >= KICKSTART_COOLDOWN_SEC


def _stamp_cooldown(label: str, clock: Callable[[], float] = time.monotonic) -> None:
    """Record ``label`` as kickstarted now (per ``clock``)."""
    _last_kickstart[label] = clock()


async def try_kickstart(label: str) -> bool:
    """Atomic "check cooldown, stamp it, kickstart" primitive. Never raises.

    The single owner of the check-then-stamp-then-kickstart sequence both
    call sites (``runtime._kickstart_and_retry`` for the preflight path,
    ``WebSocketSTTService._maybe_kickstart`` for the live path) previously
    hand-duplicated. Keeping it in one place is what makes the ordering
    guarantee auditable: the cooldown check and the stamp happen with **no**
    ``await`` between them, so two callers racing the same label on one event
    loop can never both pass the check before either stamps.

    The stamp lands even when ``launchctl`` subsequently fails — a failed
    kickstart still counts against the window so a wedged label isn't
    hammered once per reconnect cycle.

    Returns ``True`` only when launchd accepted the restart request. ``False``
    covers both "cooldown still active" and "kickstart failed"; every caller
    treats those identically (fall through to today's behavior), so they are
    deliberately not distinguished.
    """
    # Validate BEFORE stamping. The stamp deliberately survives a *failed*
    # kickstart (a wedged label must not be hammered every reconnect), but a
    # malformed label can never succeed, so burning the shared cooldown
    # window on it would suppress the next legitimate kickstart for nothing.
    if not _label_is_kickstartable(label):
        return False
    if not _cooldown_elapsed(label):
        return False
    _stamp_cooldown(label)
    return await asyncio.to_thread(kickstart_stt_server, label)


def _label_is_kickstartable(label: str) -> bool:
    """Never-raises wrapper around ``config.validate_launchd_label``.

    Both this and `kickstart_stt_server`'s own check are deliberate: this
    one protects the *cooldown stamp*, that one protects the ``launchctl``
    argv. Neither may raise — see `kickstart_stt_server`'s contract."""
    try:
        from onoats.config import validate_launchd_label

        if validate_launchd_label(label) is not None:
            return True
    except Exception:
        pass
    logger.warning(
        f"STT: kickstart of {label!r} refused — not a well-formed launchd label"
    )
    return False


def recovery_message(label: str) -> str:
    """The one recovery warning message, for every path that reports one.

    Deliberately **bare** — no ``"stt: "`` branch prefix. Prefixing is
    ``status``'s job (``format_warning_branch`` / ``set_warning_branch``), so
    there is exactly one owner of the text and exactly one owner of the
    branch-key lead-in. Both the preflight path
    (``runtime._kickstart_and_retry``) and the live path
    (``WebSocketSTTService._ensure_connected``) call this rather than
    formatting their own copy.
    """
    return f"server restarted automatically (kickstarted {label})"


def mark_unhealthy(label: str, token: str) -> None:
    """Record that instance ``token`` is currently failing to connect.

    Called from ``WebSocketSTTService._ensure_connected`` on that instance's
    **first failed connect attempt** — deliberately *not* at full-backoff
    exhaustion. Registering only at exhaustion left a ~15.5s hole: a sibling
    that had just started failing was not yet registered, so the *other*
    instance's confirmed transcript cleared the shared cooldown stamp
    outright, and the sibling's own exhaustion moments later was then free to
    SIGKILL and restart the server the healthy instance was actively using —
    the exact double-kickstart storm the shared cooldown exists to prevent,
    with the timing merely shifted. Registering on first failure only widens
    the protected window; the registration is cleared by that same instance's
    next confirmed transcript (:func:`reset_cooldown`) or by its
    ``cleanup()`` (:func:`clear_unhealthy`), so nothing is narrowed.

    The **preflight** path (``runtime._kickstart_and_retry``) deliberately
    does *not* register: it runs once at startup against a throwaway probe
    client, before any ``WebSocketSTTService`` exists, and has no lifecycle
    on which to clear a token — a registration from there would linger for
    the whole session and block every early cooldown reset. It is already
    gated by the shared cooldown itself, which is the protection that matters
    for the (startup-only, hence very narrow) preflight-vs-live race.

    Paired with :func:`reset_cooldown`, which only performs the early
    cooldown reset once *every* registered instance for the label has
    confirmed health again.
    """
    _unhealthy.setdefault(label, set()).add(token)


def clear_unhealthy(label: str, token: str) -> None:
    """Drop ``token``'s unhealthy registration without touching the cooldown.

    Used on teardown (``WebSocketSTTService.cleanup``) so a stopped instance
    cannot hold its sibling's early cooldown reset hostage.
    """
    pending = _unhealthy.get(label)
    if pending is None:
        return
    pending.discard(token)
    if not pending:
        _unhealthy.pop(label, None)


def reset_cooldown(label: str, token: str | None = None) -> None:
    """Clear ``label``'s cooldown stamp on confirmed sustained health.

    Called from the live path on the first ``transcript.*`` event following a
    kickstart — never on a bare successful connect. This is the one production
    reset path (Phase 4); public (not ``_reset_cooldown``) because it is a
    documented cross-module entry point like :func:`try_kickstart`.

    ``token`` identifies the confirming ``WebSocketSTTService`` instance. The
    cooldown is process-wide but confirmation is per-instance, so the stamp is
    only dropped once no *other* instance sharing ``label`` is still in the
    exhausted-and-unconfirmed state (see :data:`_unhealthy`). With ``token``
    omitted (no instance context) the reset is unconditional, matching the
    pre-guard behaviour.
    """
    if token is not None:
        clear_unhealthy(label, token)
        if _unhealthy.get(label):
            return
    _last_kickstart.pop(label, None)
