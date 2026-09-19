"""``launchctl kickstart`` self-healing for the STT server + shared cooldown.

Leaf module: no ``runtime``/``status``/``dual`` imports (``onoats.config``,
which itself imports nothing from ``onoats``, is the one exception and is
imported at module scope like any other). Both the preflight
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

- *Cross-instance, process-wide* — this module: :class:`KickstartRegistry`,
  a single object owning both the label-keyed cooldown stamp (via
  ``try_kickstart``/``reset_cooldown``) and the label -> set of
  :class:`InstanceToken` currently failing (via
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

Round 6 quarantined the cross-instance layer as "a human-sized design call,
not a fixer's" because it was two bare module-global dicts keyed by a bare
``str``. Round 10 has that decision and made it: the state is now
:data:`REGISTRY`, one :class:`KickstartRegistry` instance, and the instance
identity is the typed :class:`InstanceToken` rather than a loose string.

**What consolidating did *not* change**, and must not:

- The registry is still **process-wide**, because the thing it guards is
  process-wide: ``dual.py`` builds two ``WebSocketSTTService`` instances
  against one physical server, and both must share one cooldown budget.
  :data:`REGISTRY` being a module singleton is the point, not an accident —
  what changed is that its state is now reachable only through one typed
  surface instead of two dicts anyone could reach into.
- The check-then-stamp atomicity of :meth:`KickstartRegistry.try_kickstart`
  (see its docstring): no ``await`` between the two.
- The preflight path's deliberate non-registration (see
  :meth:`KickstartRegistry.mark_unhealthy`).
- The two status-warning branch schemes (shared ``stt`` for preflight,
  per-instance ``stt-mic``/``stt-system`` for live) stay owned by
  ``onoats.status``; the registry knows nothing about them.
- This module stays a leaf. :class:`InstanceToken` is defined here, not
  imported from the STT service, precisely so that stays true.

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
from dataclasses import dataclass

from loguru import logger

from onoats.config import validate_launchd_label

_KICKSTART_TIMEOUT_SEC = 5

# Absolute path, not a bare ``launchctl`` name resolved through the inherited
# PATH: this shellout has restart/kill effect on a launchd job, so it must not
# be redirectable by a PATH entry an unrelated wrapper script happened to
# prepend. ``/bin/launchctl`` is the shipped location on every supported macOS.
_LAUNCHCTL = "/bin/launchctl"

# Must exceed WebSocketSTTService's whole live reconnect *cycle*, so a
# kickstart's own retry window can never itself trigger a second kickstart.
#
# Round 9: this used to be derived from the backoff sleeps alone (~15.5s,
# `_RECONNECT_BACKOFF_SECONDS`) and set to 30. The sleeps are one term of
# three. Against the case this cooldown exists for — a server that accepts
# the socket and then wedges, which is exactly what a kickstart is a response
# to — each of the six attempts also spends `_CONNECT_TIMEOUT_SECONDS` (5s)
# on the handshake and up to `_closing.CLOSE_TIMEOUT_SEC` (5s) tearing the
# dead client down in `_discard_stale`:
#
#     15.5 (sleeps) + 6 * 5.0 (connect) + 6 * 5.0 (teardown)  =  75.5s
#
# with a floor of 45.5s when every teardown returns instantly. A 30s cooldown
# expires roughly halfway through that cycle, so the cycle's own later
# attempts could arm a second kickstart and SIGKILL a server still warming up
# from the first — the failure mode the cooldown is the only guard against.
#
# Not computed by importing those constants: this module is a leaf about
# launchctl and must not depend on the STT service. The arithmetic is pinned
# instead by `tests/test_stt_launchd.py::
# test_kickstart_cooldown_covers_the_full_reconnect_cycle`, which imports
# both sides and fails if either drifts.
#
# Deep-review finding: this window does NOT uniformly hold for the full 30s
# across the preflight/live handoff. `mark_unhealthy` is deliberately never
# called from the preflight path (see its docstring), so after a *preflight*
# kickstart no instance is ever registered unhealthy for the label — the
# live instances' very first `transcript.*` event then retires the stamp via
# `reset_cooldown` immediately (potentially ~1s in), not after the full
# window. This is accepted as correct, not a bug: a confirmed transcript
# does prove the restarted server works, which is exactly the condition
# `reset_cooldown` exists to detect. Only a *live-path* kickstart (which does
# register the kickstarting instance as unhealthy) is actually held to the
# full window until every registered instance confirms.
KICKSTART_COOLDOWN_SEC = 90

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


@dataclass(frozen=True, slots=True)
class InstanceToken:
    """Typed identity of one participant in the cross-instance registry.

    Replaces the bare ``token: str`` the registry protocol used through
    round 9. It is *not* an enum: the known participants are ``"mic"`` and
    ``"system"`` (:data:`MIC` / :data:`SYSTEM`, the names
    ``runtime._create_stt_service`` passes), but a single-pipeline
    ``WebSocketSTTService`` falls back to Pipecat's own generated
    ``self.name`` (``<Class>#<counter>``), which cannot be enumerated ahead
    of time. A frozen, hashable one-field wrapper gets the type safety an
    enum would without closing the set.

    Deliberately **opaque**: the registry only ever uses it as a set member.
    It is not a status-warning branch key — ``onoats.status`` owns those and
    happens to derive them from the same strings. Do not add formatting or
    branch semantics here.

    Also deliberately **not** ``id(instance)``: CPython reuses a freed
    object's address, so a leaked registration could be inherited wholesale
    by a later instance. See ``WebSocketSTTService._instance_token``.
    """

    name: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


#: The two participants ``onoats.dual`` constructs against one server.
MIC = InstanceToken("mic")
SYSTEM = InstanceToken("system")


class KickstartRegistry:
    """The cross-instance, process-wide half of the kickstart state machine.

    One object owning what were two module-global dicts:

    * **the cooldown stamp** — label -> last-kickstart monotonic time.
      Label-keyed rather than instance-keyed because ``dual.py`` constructs
      two independent instances (mic + system) against the same server and
      the startup preflight path stamps the same registry, so a preflight
      kickstart counts against the live-session budget too.
    * **the unhealthy set** — label -> the :class:`InstanceToken` s
      currently in the "reconnect backoff exhausted, not yet confirmed
      healthy again" state. Guards the EARLY cooldown reset: the cooldown is
      process-wide but confirmation is per-instance, so one instance's
      confirmed transcript is not evidence that its sibling (the other of
      ``dual.py``'s mic/system pair, against the same physical server) is
      healthy. Without it, mic confirming recovery would clear the shared
      cooldown and let system's very next exhaustion SIGKILL + restart the
      server mic is actively using — the exact storm the shared cooldown
      exists to prevent. A lingering token can only *delay* the early reset,
      never block kickstart forever: the cooldown still expires on its own
      after :data:`KICKSTART_COOLDOWN_SEC`.

    Instantiable so tests can build an isolated one, but production uses the
    single module-level :data:`REGISTRY` — see this module's docstring for
    why process-wide is the requirement and not an accident.
    """

    def __init__(self) -> None:
        self._last_kickstart: dict[str, float] = {}
        self._unhealthy: dict[str, set[InstanceToken]] = {}

    # -- observation / isolation (the surface tests use) ----------------

    def is_stamped(self, label: str) -> bool:
        """True while ``label`` carries an un-expired, un-reset stamp."""
        return label in self._last_kickstart

    def stamped_labels(self) -> frozenset[str]:
        """Every currently-stamped label. Empty iff nothing is on cooldown."""
        return frozenset(self._last_kickstart)

    def unhealthy_tokens(self, label: str) -> frozenset[InstanceToken]:
        """The tokens currently registered unhealthy for ``label``."""
        return frozenset(self._unhealthy.get(label, ()))

    def clear(self) -> None:
        """Drop all state. For test isolation only — never called in prod."""
        self._last_kickstart.clear()
        self._unhealthy.clear()

    # -- cooldown ------------------------------------------------------

    def cooldown_elapsed(
        self, label: str, clock: Callable[[], float] = time.monotonic
    ) -> bool:
        """True if ``label`` has never been kickstarted, or its last
        kickstart was more than ``KICKSTART_COOLDOWN_SEC`` ago."""
        last = self._last_kickstart.get(label)
        if last is None:
            return True
        return (clock() - last) >= KICKSTART_COOLDOWN_SEC

    def stamp_cooldown(
        self, label: str, clock: Callable[[], float] = time.monotonic
    ) -> None:
        """Record ``label`` as kickstarted now (per ``clock``)."""
        self._last_kickstart[label] = clock()

    async def try_kickstart(self, label: str) -> bool:
        """Atomic "check cooldown, stamp it, kickstart" primitive. Never raises.

        The single owner of the check-then-stamp-then-kickstart sequence both
        call sites (``runtime._kickstart_and_retry`` for the preflight path,
        ``WebSocketSTTService._maybe_kickstart`` for the live path) previously
        hand-duplicated. Keeping it in one place is what makes the ordering
        guarantee auditable: **the cooldown check and the stamp happen with no
        ``await`` between them**, so two callers racing the same label on one
        event loop can never both pass the check before either stamps. Any
        future edit that introduces an ``await`` between
        :meth:`cooldown_elapsed` and :meth:`stamp_cooldown` re-opens the
        double-kickstart race this primitive exists to close.

        The stamp lands even when ``launchctl`` subsequently fails — a failed
        kickstart still counts against the window so a wedged label isn't
        hammered once per reconnect cycle.

        Returns ``True`` only when launchd accepted the restart request.
        ``False`` covers both "cooldown still active" and "kickstart failed";
        every caller treats those identically (fall through to today's
        behavior), so they are deliberately not distinguished.
        """
        # Validate BEFORE stamping. The stamp deliberately survives a *failed*
        # kickstart (a wedged label must not be hammered every reconnect), but
        # a malformed label can never succeed, so burning the shared cooldown
        # window on it would suppress the next legitimate kickstart for
        # nothing.
        if not _label_is_kickstartable(label):
            return False
        if not self.cooldown_elapsed(label):
            return False
        self.stamp_cooldown(label)
        return await asyncio.to_thread(kickstart_stt_server, label)

    # -- per-instance health -------------------------------------------

    def mark_unhealthy(self, label: str, token: InstanceToken) -> None:
        """Record that instance ``token`` is currently failing to connect.

        Called from ``WebSocketSTTService._ensure_connected`` on that
        instance's **first failed connect attempt** — deliberately *not* at
        full-backoff exhaustion. Registering only at exhaustion left a ~15.5s
        hole: a sibling that had just started failing was not yet registered,
        so the *other* instance's confirmed transcript cleared the shared
        cooldown stamp outright, and the sibling's own exhaustion moments
        later was then free to SIGKILL and restart the server the healthy
        instance was actively using — the exact double-kickstart storm the
        shared cooldown exists to prevent, with the timing merely shifted.
        Registering on first failure only widens the protected window; the
        registration is cleared by that same instance's next confirmed
        transcript (:meth:`reset_cooldown`) or by its ``cleanup()``
        (:meth:`clear_unhealthy`), so nothing is narrowed.

        The **preflight** path (``runtime._kickstart_and_retry``) deliberately
        does *not* register: it runs once at startup against a throwaway probe
        client, before any ``WebSocketSTTService`` exists, and has no lifecycle
        on which to clear a token — a registration from there would linger for
        the whole session and block every early cooldown reset. It is already
        gated by the shared cooldown itself, which is the protection that
        matters for the (startup-only, hence very narrow) preflight-vs-live
        race. That exemption is a property of the *call sites*, not of this
        method: there is deliberately no preflight ``InstanceToken``, so the
        preflight path has nothing to pass and cannot register by accident.

        Paired with :meth:`reset_cooldown`, which only performs the early
        cooldown reset once *every* registered instance for the label has
        confirmed health again.
        """
        self._unhealthy.setdefault(label, set()).add(token)

    def clear_unhealthy(self, label: str, token: InstanceToken) -> None:
        """Drop ``token``'s unhealthy registration without touching the
        cooldown.

        Used on teardown (``WebSocketSTTService.cleanup``) so a stopped
        instance cannot hold its sibling's early cooldown reset hostage.
        """
        pending = self._unhealthy.get(label)
        if pending is None:
            return
        pending.discard(token)
        if not pending:
            self._unhealthy.pop(label, None)

    def reset_cooldown(self, label: str, token: InstanceToken | None = None) -> None:
        """Clear ``label``'s cooldown stamp on confirmed sustained health.

        Called from the live path on the first ``transcript.*`` event
        following a kickstart — never on a bare successful connect. This is
        the one production reset path.

        ``token`` identifies the confirming ``WebSocketSTTService`` instance.
        The cooldown is process-wide but confirmation is per-instance, so the
        stamp is only dropped once no *other* instance sharing ``label`` is
        still in the exhausted-and-unconfirmed state. With ``token`` omitted
        (no instance context) the reset is unconditional, matching the
        pre-guard behaviour.
        """
        if token is not None:
            self.clear_unhealthy(label, token)
            if self._unhealthy.get(label):
                return
        self._last_kickstart.pop(label, None)


#: The process-wide registry. Singleton by requirement, not convenience —
#: see this module's docstring.
REGISTRY = KickstartRegistry()


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
    different job or domain). It would *not* forge a pseudo-branch entry in
    the recovery-message merge: ``status.format_warning_branch`` is the
    choke point for that, and it sanitizes both ``"; "`` and ``": "`` out of
    every branch key regardless of where the key came from. The label
    allowlist is argv defence, and only argv defence — do not rely on it for
    the status grammar's integrity, and do not weaken the status grammar's
    own sanitizer on the strength of it. A rejected
    label is treated exactly like every other failure mode here — logged
    and swallowed, returning ``False`` — matching this function's "never
    raises, callers fall through to today's behavior" contract rather than
    raising, which would break that contract for the one caller that most
    needs it to hold (a wedged/misconfigured label must never itself crash
    the preflight or reconnect path it is meant to heal).
    """
    try:
        # The CALL is inside the `try`, not above it. `validate_launchd_label`
        # is typed for `str` and indexes/regexes its argument, so a non-`str`
        # label from a bypassing caller raised `TypeError` straight out of a
        # function documented as never raising — and, via `try_kickstart`,
        # did so *after* the cooldown stamp had already been consumed. The
        # contract has to cover its own validator. (The import itself is at
        # module scope: `onoats.config` imports nothing from `onoats`, so
        # there is no cycle for a function-local import to break.)
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


async def try_kickstart(label: str) -> bool:
    """Delegate to :meth:`KickstartRegistry.try_kickstart` on :data:`REGISTRY`.

    The module-level name is the documented cross-module entry point (both
    ``runtime._kickstart_and_retry`` and
    ``WebSocketSTTService._maybe_kickstart`` call it); the sequence and its
    atomicity guarantee live on the registry.
    """
    return await REGISTRY.try_kickstart(label)


def _label_is_kickstartable(label: str) -> bool:
    """Never-raises wrapper around ``config.validate_launchd_label``.

    Both this and `kickstart_stt_server`'s own check are deliberate: this
    one protects the *cooldown stamp*, that one protects the ``launchctl``
    argv. Neither may raise — see `kickstart_stt_server`'s contract."""
    try:
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


def mark_unhealthy(label: str, token: InstanceToken) -> None:
    """Delegate to :meth:`KickstartRegistry.mark_unhealthy` on :data:`REGISTRY`."""
    REGISTRY.mark_unhealthy(label, token)


def clear_unhealthy(label: str, token: InstanceToken) -> None:
    """Delegate to :meth:`KickstartRegistry.clear_unhealthy` on :data:`REGISTRY`."""
    REGISTRY.clear_unhealthy(label, token)


def reset_cooldown(label: str, token: InstanceToken | None = None) -> None:
    """Delegate to :meth:`KickstartRegistry.reset_cooldown` on :data:`REGISTRY`."""
    REGISTRY.reset_cooldown(label, token)
