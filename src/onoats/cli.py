"""``onoats`` console entrypoint — subcommand dispatch.

Subcommands::

    onoats init          # guided first-run setup (config.toml + secrets.env)
    onoats bot           # dual-input recorder (mic + system loopback)
    onoats bot-single    # legacy single-input (mic-only) recorder
    onoats flush         # signal the running recorder to rotate its buffer
    onoats convert       # render pending/*.jsonl -> markdown transcripts
    onoats devices       # list audio input/output devices
    onoats status        # report recorder pid / running state + data dir

Heavy modules (the recorder runtime, pyaudio, the STT stack) are imported
**lazily inside each handler** — never at module top — so ``onoats --help`` and
each subcommand's ``--help`` resolve via argparse without booting any service
or importing pyaudio / pipecat / MLX. This keeps the import-guard clean: a bare
``import onoats.cli`` pulls in nothing heavy.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import NamedTuple

PID_FILENAME = "onoats.pid"


# ---------------------------------------------------------------------------
# pid / data-dir resolution (lightweight — no pyaudio / runtime import)
# ---------------------------------------------------------------------------


def _apply_config_data_dir() -> None:
    """Export ``ONOATS_DATA_DIR`` from config.toml ``[storage].data_dir`` so every
    downstream resolver (store, queue, recorder, flush, status) sees the
    configured location via the existing env path — keeping ``_vendor/store.py``
    free of any config import.

    Precedence is preserved: if ``ONOATS_DATA_DIR`` is already in the env
    (koda-driven / CI injection), it wins and this is a no-op.
    """
    if os.environ.get("ONOATS_DATA_DIR", "").strip():
        return
    from onoats.config import load_config

    configured = load_config().data_dir
    if configured:
        os.environ["ONOATS_DATA_DIR"] = os.path.expanduser(str(configured))


def _resolve_data_dir() -> Path:
    """Resolve the recorder data dir without importing the heavy runtime."""
    from onoats._vendor.store import onoats_data_dir

    return onoats_data_dir()


def _pid_path(data_dir: Path | None = None) -> Path:
    """Path to the recorder pid file (``<data_dir>/.active/onoats.pid``)."""
    base = data_dir if data_dir is not None else _resolve_data_dir()
    return base / ".active" / PID_FILENAME


def _read_pid(data_dir: Path | None = None) -> int | None:
    """Read + validate the recorder pid (marker-checked). None if absent."""
    from onoats._vendor.pid import read_pid_file

    return read_pid_file(_pid_path(data_dir))


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# Subcommand handlers (heavy imports stay inside each handler)
# ---------------------------------------------------------------------------


def _cmd_init(rest: list[str]) -> int:
    from onoats.init import main as init_main

    return init_main(rest)


def _cmd_bot(rest: list[str]) -> int:
    """Run the dual-input recorder.

    Two capture backends, selected by ``AUDIO_SOURCE`` (env / config.toml
    ``[audio].source``):

    * ``portaudio`` (default) — today's path, byte-for-byte unchanged: defer
      straight to ``onoats.dual.main`` which builds ``LocalAudioTransport`` s.
    * ``socket`` — the capturer/recorder supervisor (Phase 3 of the socket-audio
      plan). It owns the native capturer's process lifecycle: it mints a private
      socket directory + a fresh generation nonce, spawns the capturer
      (``ONOATS_CAPTURER_BIN``), waits for both branch sockets to appear, then
      runs the recorder against them. See :func:`_run_socket_supervisor`.

    The branch is read here (and only here) so the PortAudio path never imports
    or touches the supervisor / socket machinery.
    """
    from onoats.config import load_config
    from onoats.dual import _parse_args

    # Resolve --help and argument errors via the bot's own parser BEFORE
    # choosing a backend. argparse prints help / usage and raises SystemExit
    # here, so `onoats bot --help` (or a bad flag) never enters the socket
    # supervisor — it must not require or spawn ONOATS_CAPTURER_BIN just to
    # answer a help request. (dual.py's module top is import-light — no pipecat
    # / pyaudio / MLX — so this preserves the no-boot-on-help guarantee.)
    args = _parse_args(rest)

    # --source overrides via the env channel (top of the existing precedence:
    # env > config.toml > default), so the supervisor, the spawned recorder,
    # and the status file all see one consistent value with no new plumbing.
    # Note: `rest` still contains --source and dual.main/_parse_args will
    # parse it again downstream — that re-parse is deliberately ignored (the
    # env var set here is the single effective channel), kept so both launch
    # paths share one parser.
    if args.source:
        os.environ["AUDIO_SOURCE"] = args.source

    if load_config().audio_source == "socket":
        return _run_socket_supervisor(rest)

    from onoats.dual import main as dual_main

    return dual_main(rest)


# ---------------------------------------------------------------------------
# AUDIO_SOURCE=socket supervisor: own the capturer↔recorder lifecycle.
#
# Phase 3 of docs/dev_plans/20260607-feature-menubar-coreaudio-socket-transport.md.
# The wire contract this supervisor + the capturer speak is pinned in
# docs/audio-socket-contract.md.
#
# Lifecycle (single owner — the supervisor owns the capturer process; the
# transport never self-reconnects, it surfaces an ErrorFrame and the session
# rotates):
#   1. mint a PRIVATE 0700 socket dir (NOT a shared /tmp path) + a fresh
#      generation nonce. The fresh per-generation dir is the primary stale-socket
#      defense: a leftover socket from a prior generation lives at a path the new
#      recorder never references, so it is structurally unreachable.
#   2. spawn the capturer (ONOATS_CAPTURER_BIN) pointed at the two sockets, with
#      the nonce + socket paths passed via env AND argv (documented in the
#      contract doc).
#   3. point ONOATS_MIC_SOCKET / ONOATS_SYSTEM_SOCKET at the private-dir sockets
#      so the recorder's dual._build_socket_transports(cfg) connects to them.
#   4. wait (bounded) for both sockets to appear, then run the recorder.
#   5. teardown: on recorder exit stop the capturer; on capturer death tear down
#      cleanly — the recorder's own ErrorFrame path flushes+rotates, and the
#      supervisor exits NON-ZERO.
#
# "Fail loud" is a testable observable: every failure path (capturer crash,
# permission denied, slow/silent reader) yields an ErrorFrame on the affected
# branch (the transport already does this), a non-zero supervisor exit code, AND
# a WARNING/ERROR log line — and the partial session still rotates (no hang).
# ---------------------------------------------------------------------------

# How long to wait for BOTH branch sockets to be created by the capturer before
# declaring the launch failed. Intentionally shorter than the capturer's
# --accept-timeout-s default (30 s): the socket FILES appear almost immediately
# once the capturer is listening, whereas the 30 s accept window covers the
# slower step of the recorder actually connecting. The asymmetry is deliberate,
# not a conflict.
_SOCKET_WAIT_TIMEOUT_SEC = 10.0
_SOCKET_WAIT_POLL_SEC = 0.05

# Extra socket wait granted ONCE when the capturer announced
# `ONOATS-EVENT waiting-for-permission` (release-plan Phase 7): its tap
# preflight runs the TCC-prompting AudioHardwareCreateProcessTap call BEFORE
# the sockets exist, and a pending Screen & System Audio Recording prompt
# blocks that call until the user answers. The event is emitted on EVERY start
# (there is no TCC preflight API, so the capturer cannot know whether the call
# will block), but the extension only applies if the base wait actually
# expires — the granted/fast path never pays it. Generous on purpose: a human
# reading a first-run permission prompt is the thing being waited on.
_PERMISSION_WAIT_EXTRA_SEC = 120.0

# Map the capturer's exit-code contract (native/onoats-capturer/Sources/
# Support.swift ``ExitCode``) to the ``exit_reason`` vocabulary documented in
# status.py, so a TCC denial shows as itself in `onoats status` / the menu bar
# instead of a generic "capturer-crash".
_CAPTURER_RC_REASONS = {
    10: "mic-denied",  # ExitCode.micDenied
    # ExitCode.systemAudioFailed: a GENUINE AudioHardwareCreateProcessTap API
    # failure (retry exhaustion). NOT a TCC denial — a denied tap succeeds and
    # delivers zeros (verified 2026-06-11), so denial never exits the capturer;
    # its only observable is the zero-run WARNING.
    11: "system-audio-failed",
}

# Grace for the recorder to drain its own ErrorFrame-driven shutdown (flush +
# rotate) after the capturer dies, before the supervisor force-cancels it.
_RECORDER_DRAIN_GRACE_SEC = 30.0

# Stable prefix for machine-parseable capturer stderr lines (mirrored by
# Support.swift emitEvent; parity-pinned by tests/test_native_contract_parity.py
# and documented in docs/audio-socket-contract.md "Capturer event lines").
_ONOATS_EVENT_PREFIX = "ONOATS-EVENT "

# After the capturer is stopped, how long the stderr reader gets to consume the
# pipe's EOF before being cancelled. EOF normally arrives with process exit;
# the bound exists so a still-open pipe fd (e.g. inherited by a straggling
# child the group-kill is sweeping) can never extend supervisor teardown.
_STDERR_READER_GRACE_SEC = 2.0

# Grace for the capturer to exit on SIGTERM before SIGKILL during teardown.
_CAPTURER_TERM_GRACE_SEC = 5.0

# Poll cadence for the deferred device-field apply (see
# _apply_device_fields_when_recorded): the capturer's `device` events fire
# within its first second, before the recorder's write_running, so the
# supervisor re-applies them once this session's record exists.
_DEVICE_FLUSH_POLL_SEC = 0.25

MIC_SOCKET_NAME = "mic.sock"
SYSTEM_SOCKET_NAME = "system.sock"


# Deny-by-default allowlist for the capturer's environment. The capturer is a
# native macOS/Linux child process that needs ONLY the socket paths + nonce (set
# explicitly) plus a minimal runtime/OS environment to launch. We build its env
# from THIS allowlist instead of copying os.environ wholesale, so STT /
# application secrets in the recorder env (DEEPGRAM_API_KEY, any *_API_KEY /
# *_TOKEN / *_SECRET, STT_*) are NEVER forwarded. New secrets can't leak by
# omission: anything not listed here is excluded by construction.
#
# Exact names pulled individually; prefix families (LC_*, __CF*) matched by
# iterating os.environ so locale + CoreFoundation vars pass through only when
# actually present. `exact`, `prefixes`, and `deny` are ONE policy — kept in a
# single object so a future edit can't add a rule to the wrong tuple and silently
# change what reaches the capturer.
#
# DYLD_* is deliberately NOT forwarded. The whole family is a dynamic-loader
# injection surface: DYLD_INSERT_LIBRARIES (dylib injection), DYLD_LIBRARY_PATH /
# DYLD_FRAMEWORK_PATH / DYLD_FALLBACK_* (planted-dylib search-path redirection),
# DYLD_PRINT_TO_FILE (arbitrary file write), etc. A native capturer that genuinely
# needs a specific DYLD_* var (framework resolution) must add it explicitly in
# Phase 4 — see docs/audio-socket-contract.md. `deny` is a defense-in-depth
# backstop: even if a future edit re-adds a DYLD_/library prefix, the classic
# injection pair never forwards.
class _CapturerEnvPolicy(NamedTuple):
    exact: tuple[str, ...]  # forwarded verbatim when present in the recorder env
    prefixes: tuple[str, ...]  # var-name prefixes whose whole family is forwarded
    deny: frozenset[str]  # names NEVER forwarded, even if a prefix would match


_CAPTURER_ENV_POLICY = _CapturerEnvPolicy(
    exact=(
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "USER",
        "LOGNAME",
        "LANG",
        "SHELL",
    ),
    prefixes=("LC_", "__CF"),
    deny=frozenset({"DYLD_INSERT_LIBRARIES", "DYLD_FORCE_FLAT_NAMESPACE"}),
)


def _build_capturer_env(
    base_env: "os._Environ[str] | dict[str, str]",
    *,
    mic_sock: str,
    system_sock: str,
    nonce: str,
) -> dict[str, str]:
    """Build the capturer's env from the deny-by-default allowlist + the three
    socket/nonce vars (which must always be present).

    Only allowlisted keys actually present in ``base_env`` are forwarded (minus
    the ``deny`` set); the socket paths + nonce are then set explicitly so they're
    guaranteed present regardless of the inbound env.
    """
    policy = _CAPTURER_ENV_POLICY
    env: dict[str, str] = {}
    for key in policy.exact:
        if key in policy.deny:
            continue
        value = base_env.get(key)
        if value is not None:
            env[key] = value
    for key, value in base_env.items():
        if key in policy.deny:
            continue
        if key.startswith(policy.prefixes):
            env[key] = value
    env["ONOATS_MIC_SOCKET"] = mic_sock
    env["ONOATS_SYSTEM_SOCKET"] = system_sock
    env["ONOATS_CAPTURER_NONCE"] = nonce
    return env


def _parse_capturer_event(line: str) -> tuple[str, dict[str, str]] | None:
    """Parse one ``ONOATS-EVENT <type> k=v …`` capturer stderr line.

    Returns ``(type, fields)`` or ``None`` for a non-event line. Field values
    are single space-delimited tokens, EXCEPT ``hint=``, which by contract is
    the trailing field and consumes the rest of the line (free text). Unknown
    keys are carried through — the caller decides what it understands, so a
    newer capturer emitting extra fields never breaks an older supervisor.
    """
    if not line.startswith(_ONOATS_EVENT_PREFIX):
        return None
    rest = line[len(_ONOATS_EVENT_PREFIX) :].strip()
    if not rest:
        return None
    event_type, _, remainder = rest.partition(" ")
    fields: dict[str, str] = {}
    while remainder:
        key, sep, after = remainder.partition("=")
        if not sep:
            break  # trailing non-k=v junk: ignore, keep what parsed
        key = key.strip()
        if key == "hint":
            fields["hint"] = after.strip()
            break
        value, _, remainder = after.partition(" ")
        fields[key] = value
    return event_type, fields


async def _drain_capturer_stderr(
    stderr, data_dir, logger, device_state=None, permission_event=None
) -> None:
    """Always-drain reader for the capturer's piped stderr.

    Three jobs, in priority order:

    1. **Drain.** Runs from spawn to EOF so the capturer can never block on a
       full stderr pipe — even before the sockets exist, even if every line is
       noise. An overlong line (> the stream's 64 KiB limit) is dropped by
       ``readline``'s documented ValueError path (the StreamReader clears its
       buffer and resumes the transport), so the reader survives and the
       capturer stays unblocked.
    2. **Tee.** Every line is forwarded verbatim to the supervisor's own
       stderr, preserving the pre-Phase-4 inherited-fd behaviour (the menu
       bar's log redirect sees exactly what it used to).
    3. **Parse.** ``ONOATS-EVENT`` lines update the status file: a
       ``zero-run-warning`` sets the v2 ``warning`` field (per-branch messages
       merged, deterministically ordered); ``zero-run-clear`` removes that
       branch's message and clears the field when none remain. A ``device``
       event records the branch's bound device into ``device_state`` (the
       dict shared with _apply_device_fields_when_recorded — device events
       fire before the recorder's start write, so the live set_devices below
       is a no-op then and the deferred task applies them) and best-effort
       updates the running record (covers mid-session mic rebinds). A
       ``waiting-for-permission`` event (Phase 7's tap preflight, emitted
       before the TCC-prompting tap call) sets ``permission_event`` so
       _wait_for_sockets can extend its budget while the prompt is pending.
       Unknown event types are tee'd and otherwise ignored
       (forward-compatible).

    Returns on EOF. Never raises out (a status write failing must not kill the
    drain — job 1 outranks job 3).
    """
    from onoats import status as status_file

    active_warnings: dict[str, str] = {}
    while True:
        try:
            line = await stderr.readline()
        except ValueError:
            # Overlong line: StreamReader dropped it and resumed; keep draining.
            logger.warning(
                "Socket supervisor: capturer wrote an overlong stderr line "
                "(>64 KiB) — dropped from the log tee, continuing to drain."
            )
            continue
        if not line:
            return  # EOF: capturer (and any child holding the fd) is gone
        try:
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()
        except (OSError, ValueError, AttributeError):
            # A broken/replaced stderr must not stop the drain.
            pass
        parsed = _parse_capturer_event(
            line.decode("utf-8", errors="replace").rstrip("\n")
        )
        if parsed is None:
            continue
        event_type, fields = parsed
        branch = fields.get("branch", "?")
        try:
            if event_type == "zero-run-warning":
                hint = fields.get("hint", "no detail provided")
                active_warnings[branch] = f"{branch}: {hint}"
                status_file.set_warning(
                    data_dir,
                    "; ".join(active_warnings[b] for b in sorted(active_warnings)),
                )
            elif event_type == "zero-run-clear":
                if active_warnings.pop(branch, None) is not None:
                    merged = "; ".join(
                        active_warnings[b] for b in sorted(active_warnings)
                    )
                    status_file.set_warning(data_dir, merged or None)
            elif event_type == "device":
                desc = fields.get("hint", "")
                if branch in ("mic", "system") and desc:
                    if device_state is not None:
                        device_state[branch] = desc
                    status_file.set_devices(
                        data_dir,
                        mic_device=desc if branch == "mic" else None,
                        system_device=desc if branch == "system" else None,
                    )
            elif event_type == "waiting-for-permission":
                # The capturer is about to make the TCC-prompting tap call; no
                # status write here — _wait_for_sockets surfaces the pending
                # prompt only if its base budget actually expires.
                if permission_event is not None:
                    permission_event.set()
        except OSError as exc:
            logger.warning(
                f"Socket supervisor: could not update status fields for a "
                f"{event_type} event ({exc}); continuing to drain capturer stderr."
            )


async def _apply_device_fields_when_recorded(
    data_dir, device_state, session_floor, logger
) -> None:
    """Deferred apply: stamp the capturer's device fields once THIS session's
    record exists.

    The capturer emits its ``device`` events within its first second — before
    the recorder (STT preflight, model load) gets to ``write_running``, which
    builds a FRESH record. So the live apply in the stderr reader either finds
    no record / the previous session's record (no-op by design, see
    status.set_devices) or gets clobbered by the start write. This task polls
    until a running record stamped at/after ``session_floor`` appears, applies
    whatever ``device_state`` holds by then, and exits; later rebind events
    find a running record and apply live through the reader.

    Lifecycle: spawned and cancelled by _run_recorder_with_capturer — it never
    outlives the recorder/capturer race and never extends any bounded wait.
    """
    import asyncio

    from onoats import status as status_file

    while True:
        st = status_file.read_status(data_dir)
        if st is not None and st.running and st.start_time >= session_floor:
            if device_state:
                try:
                    status_file.set_devices(
                        data_dir,
                        mic_device=device_state.get("mic"),
                        system_device=device_state.get("system"),
                    )
                except OSError as exc:
                    logger.warning(
                        f"Socket supervisor: could not stamp device fields ({exc})"
                    )
            return
        await asyncio.sleep(_DEVICE_FLUSH_POLL_SEC)


def _run_socket_supervisor(rest: list[str]) -> int:
    """Synchronous entry: drive the async socket session, mirror dual.main's rc.

    Returns 0 on a clean recorder shutdown, non-zero on any fail-loud path
    (missing/failed capturer, sockets that never appeared, capturer death
    mid-session, or an STT preflight failure).
    """
    import asyncio as _asyncio

    from loguru import logger

    from onoats.runtime import SttPreflightError
    from onoats.transports import SocketHandshakeError

    try:
        return _asyncio.run(_supervise_socket_session(rest))
    except SttPreflightError as exc:
        # Mirror dual.main: actionable hint, not a traceback.
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except (SocketHandshakeError, OSError, ValueError) as exc:
        # A controlled socket-mode launch failure (bad/stale capturer handshake,
        # socket connect error, or a same-socket / config guard rejection) must
        # be a clean non-zero exit per the fail-loud contract — not a traceback.
        # Unexpected errors (programming bugs) still propagate so they are not
        # silently swallowed.
        logger.error(
            f"Socket supervisor: recorder failed to start ({exc!r}); exiting non-zero."
        )
        return 1


async def _supervise_socket_session(rest: list[str]) -> int:
    """Mint sockets + nonce, spawn the capturer, run the recorder, tear down.

    Runs the recorder and a capturer-exit watcher concurrently in one event
    loop. Whichever finishes first decides teardown:

    * recorder finishes first  → normal shutdown / EndFrame: stop the capturer,
      return the recorder's rc (0 on clean exit).
    * capturer finishes first  → the capturer died: the recorder's socket reader
      already saw EOF / read-idle, surfaced an ``ErrorFrame``, and is rotating
      the partial session. Give it a bounded grace to drain, force-cancel if it
      overruns, log loudly, and return non-zero.
    """
    import asyncio
    import secrets
    import shutil
    import tempfile

    from loguru import logger

    # 0. One canonical data-dir resolution per session. Every status write in
    # this supervisor — the early-failure records below AND the recorder's own
    # stamps (passed down through _run_recorder_with_capturer → run_onoats_dual)
    # — uses this single value, so an env-configured non-default dir can never
    # land the early-exit status in a different place than the session records.
    data_dir = _resolve_data_dir()

    capturer_bin = os.environ.get("ONOATS_CAPTURER_BIN", "").strip()
    if not capturer_bin:
        logger.error(
            "AUDIO_SOURCE=socket requires ONOATS_CAPTURER_BIN (path to the native "
            "capturer that writes framed PCM16 to the two branch sockets). It is "
            "unset; refusing to start. See docs/audio-socket-contract.md."
        )
        return 1

    # 1. Private, supervisor-owned socket dir (0700). mkdtemp already creates the
    # dir 0700 and owner-only; a fresh per-generation dir means any stale socket
    # from a prior generation lives at a path the new recorder never references
    # (structurally unreachable — the primary stale-socket defense). The system
    # temp dir keeps the AF_UNIX path well under the macOS ~104-byte sun_path
    # limit (a data-dir-nested path can blow it).
    sock_dir = tempfile.mkdtemp(prefix="onoats-sock-")
    os.chmod(sock_dir, 0o700)
    mic_sock = os.path.join(sock_dir, MIC_SOCKET_NAME)
    system_sock = os.path.join(sock_dir, SYSTEM_SOCKET_NAME)

    # 2. Fresh generation nonce. A restarted capturer presents a new nonce in its
    # handshake; combined with the fresh dir this invalidates stale fds.
    nonce = secrets.token_hex(16)

    # 3. Point the recorder at the private-dir sockets AND the generation nonce.
    # The recorder resolves these through OnoatsConfig (ONOATS_MIC_SOCKET /
    # ONOATS_SYSTEM_SOCKET / ONOATS_CAPTURER_NONCE env > [audio] toml), so
    # exporting them here is all dual._build_socket_transports needs to (a)
    # connect to the right sockets and (b) gate on the nonce — rejecting a
    # capturer that handshakes with a missing/stale nonce. Capture any prior
    # values so the `finally` can restore them — these are process-global, and
    # leaving them set would leak our private-dir paths / nonce into any
    # in-process caller (e.g. tests) that runs after us.
    _prior_socket_env = {
        k: os.environ.get(k)
        for k in ("ONOATS_MIC_SOCKET", "ONOATS_SYSTEM_SOCKET", "ONOATS_CAPTURER_NONCE")
    }
    os.environ["ONOATS_MIC_SOCKET"] = mic_sock
    os.environ["ONOATS_SYSTEM_SOCKET"] = system_sock
    os.environ["ONOATS_CAPTURER_NONCE"] = nonce

    capturer_proc: asyncio.subprocess.Process | None = None
    stderr_task: "asyncio.Task[None] | None" = None
    # Latest device description per branch ("mic"/"system"), written by the
    # stderr reader and applied to this session's status record by the
    # deferred task in _run_recorder_with_capturer (the events outrun the
    # recorder's start write — see _apply_device_fields_when_recorded).
    device_state: dict[str, str] = {}
    # Set by the stderr reader when the capturer announces its tap preflight
    # (`ONOATS-EVENT waiting-for-permission`) — lets _wait_for_sockets extend
    # its budget while a TCC prompt is pending (release-plan Phase 7).
    permission_event = asyncio.Event()
    rc = 0
    try:
        # 3b. Spawn the capturer pointed at both sockets. Pass the socket paths +
        # nonce via BOTH env and argv (documented in the contract doc) so a
        # capturer can read whichever it prefers.
        # Deny-by-default: the capturer gets ONLY the allowlisted runtime/OS vars
        # plus the socket paths + nonce — NOT the full recorder env. This keeps
        # STT/application secrets (DEEPGRAM_API_KEY, *_API_KEY/*_TOKEN/*_SECRET,
        # STT_*) out of a native child that never needs them. See
        # _CAPTURER_ENV_POLICY and docs/audio-socket-contract.md.
        capturer_env = _build_capturer_env(
            os.environ, mic_sock=mic_sock, system_sock=system_sock, nonce=nonce
        )
        logger.info(
            f"Socket supervisor: spawning capturer {capturer_bin!r} "
            f"(mic={mic_sock}, system={system_sock}, nonce={nonce[:8]}…)"
        )
        try:
            capturer_proc = await asyncio.create_subprocess_exec(
                capturer_bin,
                "--mic-socket",
                mic_sock,
                "--system-socket",
                system_sock,
                "--nonce",
                nonce,
                env=capturer_env,
                # Phase 4: pipe stderr through the supervisor instead of
                # inheriting the fd. The always-drain reader task below tees
                # every line to our own stderr (so the menu bar's log redirect
                # is unchanged) and parses ONOATS-EVENT lines into the status
                # file. Process.wait() is deadlock-prone with a PIPE only if
                # nothing drains it — the reader starts before any wait.
                stderr=asyncio.subprocess.PIPE,
                # Spawn the capturer in its OWN session/process group
                # (start_new_session=True is the portable subprocess spelling of
                # setsid). On POSIX a terminal Ctrl+C/SIGTERM is delivered to the
                # whole foreground process group, so without this it would hit
                # BOTH onoats and the capturer. Terminal signals must NOT reach
                # the capturer: the supervisor owns capturer teardown explicitly
                # via _stop_capturer (SIGTERM → bounded wait → SIGKILL on the
                # whole process group) AFTER the recorder finishes. This
                # isolation is what makes the
                # recorder-finishes-first branch in _run_recorder_with_capturer
                # correct under Ctrl+C — without it, the capturer could win the
                # race and a graceful shutdown would be mis-classified as
                # capturer-death (rc=1).
                start_new_session=True,
            )
        except OSError as exc:
            # Missing binary / permission denied launching it — fail loud.
            logger.error(
                f"Socket supervisor: could not spawn capturer {capturer_bin!r}: {exc}. "
                "AUDIO_SOURCE=socket cannot capture without it. "
                "See docs/audio-socket-contract.md."
            )
            return 1

        # 3c. Start the always-drain stderr reader IMMEDIATELY — before any
        # bounded wait — so a chatty capturer can never block on a full pipe
        # while we wait for its sockets, and prestart stderr still reaches the
        # log via the tee. Lifecycle: runs to pipe EOF (capturer death); the
        # `finally` below bounds its retirement so it can never extend
        # teardown. It must NOT be awaited alongside the recorder/capturer
        # race in _run_recorder_with_capturer — it is deliberately not a
        # participant there (EOF on stderr is not a lifecycle signal; process
        # exit is).
        stderr_task = asyncio.create_task(
            _drain_capturer_stderr(
                capturer_proc.stderr, data_dir, logger, device_state, permission_event
            ),
            name="socket_supervisor_capturer_stderr",
        )

        # 4. Wait (bounded) for BOTH sockets to appear. If the capturer dies or
        # is too slow, fail loud rather than hang the recorder on a connect that
        # never succeeds.
        ready = await _wait_for_sockets(
            capturer_proc,
            (mic_sock, system_sock),
            logger,
            data_dir=data_dir,
            permission_event=permission_event,
        )
        if not ready:
            # _wait_for_sockets already logged the cause (capturer death / timeout).
            # Read the exit code BEFORE stopping: _stop_capturer always reaps,
            # so reading afterwards would mis-stamp a hung-but-alive capturer
            # (a start-timeout) with the SIGTERM exit code as
            # "capturer-start-failed" instead of "capturer-start-timeout".
            rc_cap = capturer_proc.returncode
            await _stop_capturer(capturer_proc, logger)
            # The recorder never started, so nothing else will write the status
            # file — without this, a stale record from the PREVIOUS session is
            # what `onoats status` / the menu bar would read (observed live:
            # a mic-denial start rendered as "failed: graceful").
            from onoats import status as status_file

            if rc_cap is not None:
                exit_reason = _CAPTURER_RC_REASONS.get(rc_cap, "capturer-start-failed")
                last_error = (
                    f"capturer exited (rc={rc_cap}) before creating its sockets"
                )
            else:
                exit_reason = "capturer-start-timeout"
                last_error = "capturer did not create its sockets in time"
                if permission_event.is_set():
                    # The wait was already extended once for the pending TCC
                    # prompt — name it, so a never-answered prompt is
                    # diagnosable from the status file.
                    last_error += (
                        " (the system-audio permission prompt may still be "
                        "unanswered — see System Settings ▸ Privacy & Security "
                        "▸ Screen & System Audio Recording)"
                    )
            status_file.write_prestart_failure(
                data_dir,
                audio_source="socket",
                exit_reason=exit_reason,
                last_error=last_error,
            )
            return 1

        # 5. Run the recorder against the sockets, watching the capturer
        # concurrently so its death tears the session down.
        rc = await _run_recorder_with_capturer(
            rest, capturer_proc, logger, data_dir, device_state
        )
    finally:
        if capturer_proc is not None:
            await _stop_capturer(capturer_proc, logger)
        if stderr_task is not None:
            # The capturer group is gone, so the pipe's EOF is imminent — give
            # the reader a bounded grace to consume it (flushing any final
            # lines to the log), then cancel. Bounded so a leaked fd can never
            # hang teardown; cancellation is safe (readline is the only await).
            try:
                # wait_for cancels (and awaits) the task itself on timeout.
                await asyncio.wait_for(stderr_task, timeout=_STDERR_READER_GRACE_SEC)
            except asyncio.TimeoutError:
                pass
        # Remove the private socket dir. Best-effort: a leftover here is harmless
        # (next generation mints a new one), but tidy up so private dirs don't
        # accumulate under the system temp root across restarts.
        try:
            shutil.rmtree(sock_dir, ignore_errors=True)
        except OSError:
            pass
        # Restore the socket env vars to their prior state so we don't leak our
        # private-dir paths into a subsequent in-process caller.
        for key, prior in _prior_socket_env.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior

    return rc


async def _wait_for_sockets(
    capturer_proc, socket_paths, logger, *, data_dir=None, permission_event=None
) -> bool:
    """Wait (bounded) for every path in ``socket_paths`` to exist.

    Returns ``True`` once all sockets exist. Returns ``False`` — having logged
    the cause loudly — if the capturer exits before the sockets appear or the
    bounded timeout elapses. A short poll loop is used rather than inotify so the
    behaviour is identical on macOS / Linux and trivially testable.

    Two modes, selected by the keyword args: with ``data_dir`` and
    ``permission_event`` both ``None`` (the default) this is a pure,
    side-effect-free poller; with both supplied it additionally coordinates the
    Phase 7 permission-wait below (reads the event, may extend the deadline
    once, and writes a status record). The ``is not None`` guards are this
    interface seam, not defensive checks.

    Phase 7 (tap preflight): the capturer makes the TCC-prompting tap call
    BEFORE binding its sockets, announced by ``ONOATS-EVENT
    waiting-for-permission`` (relayed here via ``permission_event``, set by the
    stderr reader). A pending Screen & System Audio Recording prompt therefore
    looks exactly like a pre-socket hang — so when the base budget expires with
    that event seen, the wait is extended ONCE by ``_PERMISSION_WAIT_EXTRA_SEC``
    and the pending prompt is surfaced in the status file (a fresh record the
    recorder's own start write later replaces). Without the event, the base
    ``capturer-start-timeout`` behaviour is unchanged.
    """
    import asyncio
    import time

    deadline = time.monotonic() + _SOCKET_WAIT_TIMEOUT_SEC
    extended = False
    while True:
        if all(os.path.exists(p) for p in socket_paths):
            logger.info("Socket supervisor: both branch sockets are present")
            return True

        # Capturer died before binding both sockets → permission denied / crash
        # at startup. Fail loud.
        if capturer_proc.returncode is not None:
            logger.error(
                "Socket supervisor: capturer exited (rc="
                f"{capturer_proc.returncode}) before both sockets appeared — it "
                "likely failed to start capture (permission denied / no device). "
                "AUDIO_SOURCE=socket cannot record. See docs/audio-socket-contract.md."
            )
            return False

        if time.monotonic() >= deadline:
            if (
                not extended
                and permission_event is not None
                and permission_event.is_set()
            ):
                extended = True
                deadline = time.monotonic() + _PERMISSION_WAIT_EXTRA_SEC
                logger.info(
                    "Socket supervisor: capturer announced waiting-for-permission "
                    "and its sockets have not appeared — the system-audio "
                    f"permission prompt is likely pending; extending the wait by "
                    f"{_PERMISSION_WAIT_EXTRA_SEC:.0f}s."
                )
                if data_dir is not None:
                    from onoats import status as status_file

                    try:
                        status_file.write_prestart_waiting(
                            data_dir,
                            audio_source="socket",
                            note=(
                                "waiting for the system-audio permission prompt — "
                                "answer the Screen & System Audio Recording dialog "
                                "to start the session"
                            ),
                        )
                    except OSError as exc:
                        logger.warning(
                            f"Socket supervisor: could not write the "
                            f"waiting-for-permission status record ({exc})"
                        )
                continue
            missing = [p for p in socket_paths if not os.path.exists(p)]
            budget = _SOCKET_WAIT_TIMEOUT_SEC + (
                _PERMISSION_WAIT_EXTRA_SEC if extended else 0.0
            )
            logger.error(
                "Socket supervisor: capturer did not create "
                f"{missing} within {budget}s — refusing to start "
                "the recorder rather than hang on a connect that never succeeds."
            )
            return False

        await asyncio.sleep(_SOCKET_WAIT_POLL_SEC)


async def _run_recorder_with_capturer(
    rest, capturer_proc, logger, data_dir, device_state=None
) -> int:
    """Run the recorder + a capturer-death watcher; return the supervisor rc.

    The recorder (``run_onoats_dual``) installs its own signal handlers and runs
    the pipeline; on capturer death the socket transport surfaces an
    ``ErrorFrame`` which terminates the pipeline (fatal-error → pipeline cancel),
    so the recorder coroutine returns on its own and flushes+rotates via its
    ``finally`` shutdown path. The watcher is the backstop that turns that into a
    non-zero supervisor exit and force-cancels the recorder if it overruns the
    drain grace.
    """
    import asyncio
    import time

    from onoats import status as status_file
    from onoats.dual import _apply_recorder_args, _parse_args, run_onoats_dual

    # Parse + validate the same args dual.main would, through the shared helper,
    # so `onoats bot --live-terminal --category X` behaves identically in socket
    # mode (interactive-mode warning + category validation can never drift).
    args = _parse_args(rest)
    rc = _apply_recorder_args(args)
    if rc is not None:
        return rc

    # Any write_running at/after this instant belongs to THIS session — the
    # deferred device-apply task keys on it so it never stamps a stale record.
    session_floor = time.time()
    recorder_task = asyncio.create_task(
        run_onoats_dual(
            live_terminal=args.live_terminal,
            locked_category=args.category,
            # Same resolution the supervisor's own status stamps use — one
            # canonical data dir per session (see run_onoats_dual).
            data_dir=data_dir,
        ),
        name="socket_supervisor_recorder",
    )
    capturer_task = asyncio.create_task(
        capturer_proc.wait(), name="socket_supervisor_capturer_wait"
    )
    # Deferred device-field apply (see _apply_device_fields_when_recorded). It
    # is NOT a participant in the recorder/capturer race below — the finally
    # retires it so it can never extend a bounded wait or outlive the session.
    device_flush_task = (
        asyncio.create_task(
            _apply_device_fields_when_recorded(
                data_dir, device_state, session_floor, logger
            ),
            name="socket_supervisor_device_flush",
        )
        if device_state is not None
        else None
    )

    try:
        done, _pending = await asyncio.wait(
            {recorder_task, capturer_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if recorder_task in done:
            # The recorder coroutine finished. Re-await to surface any exception
            # (the caller maps SttPreflightError to rc=1), then honour its return
            # code: run_onoats_dual returns non-zero when the pipeline ended via a
            # fatal ErrorFrame (e.g. a silent/dead capturer tripped the read-idle
            # watchdog) rather than a clean shutdown. That must propagate as a
            # non-zero supervisor exit per the fail-loud contract — the capturer
            # process is still alive (capturer_task pending), so we cannot infer
            # success from "recorder finished first".
            capturer_task.cancel()
            try:
                await capturer_task
            except (asyncio.CancelledError, ProcessLookupError):
                pass
            rc = recorder_task.result()  # re-raise if the recorder failed
            rc = rc if rc is not None else 0
            if rc != 0:
                logger.error(
                    f"Socket supervisor: recorder exited non-zero (rc={rc}) — a "
                    "capture branch surfaced a fatal ErrorFrame (silent/failed "
                    "capturer); stopping capturer and exiting non-zero."
                )
                # The recorder already wrote running=false + a fatal_error_frame
                # reason; enrich it with the supervisor's final rc so `onoats status`
                # / the menu bar can show the exit code.
                status_file.stamp_supervisor_failure(
                    data_dir, exit_reason="fatal_error_frame", supervisor_rc=rc
                )
            else:
                logger.info("Socket supervisor: recorder exited; stopping capturer")
            return rc

        # Capturer finished first → it died mid-session. The recorder's own
        # ErrorFrame path is draining (flush + rotate). Give it a bounded grace, then
        # force-cancel so the supervisor never hangs.
        #
        # Contract (Interpretation A): a capturer that exits BEFORE the recorder is
        # ALWAYS a fail-loud event, even on a clean rc=0. The supervisor outlives the
        # capturer by design — it stops the capturer when the recorder ends, never the
        # reverse — so any capturer-initiated exit means the audio stream ended
        # mid-session and the recording is truncated regardless of exit code. We do
        # not branch on rc==0 here. A future, deliberate "clean stop" signal and its
        # supervisor semantics are reserved for the Phase-4 capturer exit-code
        # contract (see docs/audio-socket-contract.md); honouring it would also mean
        # redefining the transport's EOF-is-fatal rule, which is out of scope.
        rc = capturer_task.result()
        logger.error(
            f"Socket supervisor: capturer exited mid-session (rc={rc}); the recorder "
            "branch surfaced an ErrorFrame and is rotating the partial session. "
            "Supervisor will exit non-zero (capturer-exit-before-recorder is always "
            "fail-loud, even on rc=0)."
        )
        try:
            await asyncio.wait_for(recorder_task, timeout=_RECORDER_DRAIN_GRACE_SEC)
        except asyncio.TimeoutError:
            logger.warning(
                "Socket supervisor: recorder did not finish draining within "
                f"{_RECORDER_DRAIN_GRACE_SEC}s after capturer death — force-cancelling."
            )
            recorder_task.cancel()
            try:
                await recorder_task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The recorder may surface its own teardown error; we already log + exit
            # non-zero for the capturer death, so just record it.
            logger.warning(f"Socket supervisor: recorder drain raised: {exc}")
        # The supervisor alone knows this was a capturer-death (not the recorder's own
        # ErrorFrame). Stamp the specific reason: the capturer's exit-code contract
        # (Support.swift ExitCode: 10=mic denied, 11=system-audio failed) is the only
        # way the menu bar / `onoats status` can show WHY a start failed, not just
        # that it crashed. Anything else — including rc=0 — is "capturer-crash".
        # If the recorder was force-cancelled before writing its stopped record,
        # this also flips the lingering running=true start record to stopped.
        exit_reason = _CAPTURER_RC_REASONS.get(rc, "capturer-crash")
        status_file.stamp_supervisor_failure(
            data_dir,
            exit_reason=exit_reason,
            supervisor_rc=1,
            last_error="capturer exited mid-session; partial recording rotated to pending/",
        )
        return 1
    finally:
        if device_flush_task is not None:
            device_flush_task.cancel()
            try:
                await device_flush_task
            except asyncio.CancelledError:
                pass


def _signal_capturer_group(capturer_proc, sig, logger) -> bool:
    """Send ``sig`` to the capturer's entire process group.

    The capturer is spawned with ``start_new_session=True`` (see
    ``_supervise_socket_session``), which makes it a session/process-group
    leader, so its **PGID is equal to its PID by construction**. We target the
    group by that PID rather than resolving it with ``os.getpgid``: once the
    leader has been reaped (the crash path — asyncio sets ``returncode`` only
    after reaping), ``os.getpgid`` raises ``ProcessLookupError`` even though the
    group can still hold living children. The kernel keeps the PGID reserved
    while the group is non-empty, so ``os.killpg(pid, …)`` still reaches those
    children and we can sweep them.

    Signalling only the lone PID would orphan any helper/child the capturer
    spawned (a wrapper script, a CoreAudio helper) — leaving it holding the
    audio device after the supervisor reports success and removes the socket
    dir.

    Returns True if a signal was delivered (group/process existed), False if it
    was already gone. ``sig`` may be ``0`` to probe existence without delivering
    a signal. Falls back to a single-PID signal on platforms without process
    groups (``os.killpg`` unavailable, e.g. Windows).
    """
    pid = capturer_proc.pid
    try:
        os.killpg(pid, sig)
        return True
    except ProcessLookupError:
        return False
    except AttributeError:
        # No process-group support (e.g. Windows) — fall through to per-PID.
        pass
    except OSError as exc:
        logger.warning(
            f"Socket supervisor: killpg({pid}, {sig}) failed ({exc}); "
            "falling back to single-process signal."
        )
    try:
        os.kill(pid, sig)
        return True
    except ProcessLookupError:
        return False


async def _stop_capturer(capturer_proc, logger) -> None:
    """Stop the capturer's whole process group: SIGTERM, bounded grace, then
    SIGKILL. Idempotent.

    Signals the process group rather than the lone capturer PID, so a
    helper/child the capturer spawned cannot survive teardown holding the audio
    device — including on the **crash path**, where the capturer *leader* has
    already exited (``returncode`` set) but its children may still be alive.
    See ``_signal_capturer_group``.
    """
    import asyncio

    leader_alive = capturer_proc.returncode is None
    # SIGTERM the whole group. If nothing in the group is left, we're done.
    if not _signal_capturer_group(capturer_proc, signal.SIGTERM, logger):
        return

    # Give the group a bounded grace to exit on SIGTERM. When the leader is
    # still alive we await it directly. When it has already been reaped (crash
    # path) we cannot await it, so we poll the group with signal 0 until it
    # drains or the grace elapses.
    drained = False
    try:
        if leader_alive:
            await asyncio.wait_for(
                capturer_proc.wait(), timeout=_CAPTURER_TERM_GRACE_SEC
            )
            drained = True
        else:
            loop = asyncio.get_event_loop()
            deadline = loop.time() + _CAPTURER_TERM_GRACE_SEC
            while loop.time() < deadline:
                if not _signal_capturer_group(capturer_proc, 0, logger):
                    drained = True
                    break
                await asyncio.sleep(0.05)
    except asyncio.TimeoutError:
        pass
    if drained:
        return

    # Stragglers remain (a live leader that ignored SIGTERM, or orphaned
    # children of a dead leader) — SIGKILL the whole group.
    if _signal_capturer_group(capturer_proc, signal.SIGKILL, logger):
        logger.warning(
            "Socket supervisor: capturer process group did not exit on SIGTERM "
            f"within {_CAPTURER_TERM_GRACE_SEC}s — sent SIGKILL to the group."
        )
    if leader_alive:
        try:
            await capturer_proc.wait()
        except ProcessLookupError:
            pass


def _cmd_bot_single(rest: list[str]) -> int:
    from onoats.__main__ import main as single_main

    return single_main(rest)


def _cmd_convert(rest: list[str]) -> int:
    """Render pending sessions. ``onoats convert --once`` supersedes the P3
    ``python -m onoats.convert --once`` entry (which still works)."""
    from onoats.convert import main as convert_main

    # The converter's own parser requires --once; default to it so a bare
    # `onoats convert` does the obvious thing rather than erroring.
    if not rest:
        rest = ["--once"]
    return convert_main(rest)


def _cmd_flush(rest: list[str]) -> int:
    """Send SIGUSR1 to the running recorder so it rotates its buffer.

    This is the seam an integrating consumer's ``flush`` pass-through execs. It
    resolves the pid from ``<data_dir>/.active/onoats.pid`` (marker
    ``onoats-bot``) under the forwarded ``ONOATS_DATA_DIR`` root.
    """
    parser = argparse.ArgumentParser(prog="onoats flush")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Data dir override (else $ONOATS_DATA_DIR / XDG default).",
    )
    args = parser.parse_args(rest)
    data_dir = Path(args.data_dir) if args.data_dir else None

    from onoats._vendor.pid import resolve_flush_target

    pid_path = _pid_path(data_dir)
    target = resolve_flush_target(pid_path)
    if target.pid is None:
        # Identity could not be confirmed. Drop a now-untrustworthy pid file so
        # the next run starts clean, but never signal an unverified pid.
        if target.stale:
            try:
                pid_path.unlink()
            except OSError:
                pass
        print(f"onoats flush: {target.reason} (pid file {pid_path})", file=sys.stderr)
        return 1
    pid = target.pid
    try:
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        # Raced: the verified recorder exited between the identity check and
        # the signal. Treat as stale rather than signalling a recycled pid.
        try:
            pid_path.unlink()
        except OSError:
            pass
        print(
            f"onoats flush: recorder pid {pid} is not running (stale pid file)",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"onoats flush: could not signal pid {pid}: {exc}", file=sys.stderr)
        return 1
    print(f"onoats flush: sent SIGUSR1 to recorder pid {pid}")
    return 0


def _cmd_stop(rest: list[str]) -> int:
    """Send SIGTERM to the running recorder so it shuts down gracefully.

    Near-clone of ``_cmd_flush``: the safe identity-checked signalling
    (``resolve_flush_target`` → marker + cmdline fingerprint) is reused verbatim,
    so a recycled foreign pid is never signalled. The PID-recycling guard matters
    *more* here than for flush — SIGTERM's default disposition kills, so
    signalling an unrelated pid would terminate it. The only behavioural change
    from flush is the signal: SIGTERM (the graceful-shutdown trigger,
    ``runtime.py`` — same as a single Ctrl-C / the GUI's owned
    ``Process.terminate()``) instead of SIGUSR1. The recorder drains and writes a
    final flush before exiting; the command returns on signal delivery, NOT on
    confirmed exit.
    """
    parser = argparse.ArgumentParser(prog="onoats stop")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Data dir override (else $ONOATS_DATA_DIR / XDG default).",
    )
    args = parser.parse_args(rest)
    data_dir = Path(args.data_dir) if args.data_dir else None

    from onoats._vendor.pid import resolve_flush_target

    pid_path = _pid_path(data_dir)
    target = resolve_flush_target(pid_path)
    if target.pid is None:
        # Identity could not be confirmed. Drop a now-untrustworthy pid file so
        # the next run starts clean, but never signal an unverified pid.
        if target.stale:
            try:
                pid_path.unlink()
            except OSError:
                pass
        print(f"onoats stop: {target.reason} (pid file {pid_path})", file=sys.stderr)
        return 1
    pid = target.pid
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Raced: the verified recorder exited between the identity check and
        # the signal. Treat as stale rather than signalling a recycled pid.
        try:
            pid_path.unlink()
        except OSError:
            pass
        print(
            f"onoats stop: recorder pid {pid} is not running (stale pid file)",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"onoats stop: could not signal pid {pid}: {exc}", file=sys.stderr)
        return 1
    print(f"onoats stop: sent SIGTERM to recorder pid {pid} (graceful shutdown)")
    return 0


def _cmd_devices(rest: list[str]) -> int:
    """List audio input/output devices (reuses the device picker's enumeration)."""
    argparse.ArgumentParser(prog="onoats devices").parse_args(rest)

    from onoats.config import load_config

    if load_config().audio_source == "socket":
        # This enumeration is PortAudio's view; the socket path never picks
        # from it — the native capturer binds the system default input and a
        # global system-output tap. `onoats status` shows what a running
        # session actually bound.
        print(
            "note: AUDIO_SOURCE=socket — this list is PortAudio-only and not "
            "what the recorder uses. The native capturer captures the system "
            "default input (mic) and the default-output tap (system audio); "
            "see `onoats status` for the devices bound by a running session.\n"
        )

    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        print("Input devices:")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxInputChannels"] > 0:
                print(
                    f"  [{i}] {info['name']} "
                    f"({int(info['defaultSampleRate'])} Hz, "
                    f"{info['maxInputChannels']} ch)"
                )
        print("\nOutput devices:")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info["maxOutputChannels"] > 0:
                print(
                    f"  [{i}] {info['name']} "
                    f"({int(info['defaultSampleRate'])} Hz, "
                    f"{info['maxOutputChannels']} ch)"
                )
    finally:
        pa.terminate()
    return 0


def _cmd_status(rest: list[str]) -> int:
    """Report recorder pid / running state + the resolved data dir."""
    parser = argparse.ArgumentParser(prog="onoats status")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Data dir override (else $ONOATS_DATA_DIR / XDG default).",
    )
    args = parser.parse_args(rest)
    data_dir = Path(args.data_dir) if args.data_dir else _resolve_data_dir()

    from onoats import status as status_file

    print(f"Data dir: {data_dir}")
    print(f"PID file: {_pid_path(data_dir)}")
    print(f"Status file: {status_file.status_path(data_dir)}")

    # pid-authoritative verdict; the status file supplies the rich detail and the
    # off-diagonal staleness note (see status.resolve_liveness — the 4-cell table).
    # The injected aliveness is identity-checked (cmdline fingerprint) so a
    # recycled pid behind a stale pid file is never reported as RUNNING.
    from onoats._vendor.pid import fingerprint_matches, read_pid_record

    rec = read_pid_record(_pid_path(data_dir))
    live = status_file.resolve_liveness(
        data_dir,
        read_pid=_read_pid,
        process_alive=lambda pid: (
            _process_alive(pid)
            and (rec is None or rec.pid != pid or fingerprint_matches(rec))
        ),
    )
    st = live.status

    if live.alive:
        print(f"Recorder: RUNNING (pid {live.pid})")
    elif live.pid is not None:
        print(f"Recorder: stale pid file (pid {live.pid} not running)")
    else:
        print("Recorder: not running (no valid pid file)")
    if live.note:
        print(f"  note: {live.note}")

    if st is not None:
        if st.audio_source:
            print(f"  audio source: {st.audio_source}")
        if st.stt_label:
            print(f"  STT: {st.stt_label}")
        # Socket path: the devices the capturer actually bound (ONOATS-EVENT
        # device → status schema v2), updated on mid-session mic rebinds.
        if st.mic_device:
            print(f"  mic device: {st.mic_device}")
        if st.system_device:
            print(f"  system device: {st.system_device}")
        if st.warning:
            print(f"  warning: {st.warning}")
        if st.last_rotation_time is not None:
            print(f"  last rotation: {st.last_rotation_time}")
        # Surface WHY a start failed, not just liveness — the menu bar reads the
        # same fields.
        if st.exit_reason and st.exit_reason != "graceful":
            print(f"  exit reason: {st.exit_reason}")
        if st.last_error:
            print(f"  last error: {st.last_error}")
        if st.supervisor_rc is not None:
            print(f"  supervisor rc: {st.supervisor_rc}")

    # PortAudio fallback path: there are no capturer device events, so show the
    # configured [devices] names the recorder binds by — the wrong-device guard
    # for that path (an A/B finding: a stale name silently records the wrong
    # input). Printed even without a record, so it helps before first start —
    # but suppressed while a LIVE socket session is displayed (this shell's
    # config may resolve portaudio even though the running session, e.g.
    # menu-bar-launched, is socket; showing both blocks would mislead).
    from onoats.config import load_config

    cfg = load_config()
    socket_session_live = live.alive and st is not None and st.audio_source == "socket"
    if cfg.audio_source != "socket" and not socket_session_live:
        mic = cfg.mic_device or "<system default>"
        system = cfg.system_device or "<not configured>"
        print(f"  configured mic (PortAudio): {mic} ({cfg.mic_device_source})")
        print(f"  configured system (PortAudio): {system} ({cfg.system_device_source})")
    return 0


_HANDLERS = {
    "init": _cmd_init,
    "bot": _cmd_bot,
    "bot-single": _cmd_bot_single,
    "flush": _cmd_flush,
    "stop": _cmd_stop,
    "convert": _cmd_convert,
    "devices": _cmd_devices,
    "status": _cmd_status,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onoats",
        description=(
            "onoats — Always-on Organized Audio Transcript System. "
            "A standalone voice recorder + self-contained markdown converter."
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.add_parser("init", help="Guided first-run setup (config.toml + secrets.env).")
    sub.add_parser("bot", help="Dual-input recorder (mic + system loopback).")
    sub.add_parser("bot-single", help="Legacy single-input (mic-only) recorder.")
    sub.add_parser("flush", help="Signal the running recorder to rotate its buffer.")
    sub.add_parser(
        "stop",
        help="Signal the running recorder to stop gracefully (drain + final flush).",
    )
    sub.add_parser("convert", help="Render pending/*.jsonl into markdown transcripts.")
    sub.add_parser("devices", help="List audio input/output devices.")
    sub.add_parser("status", help="Report recorder pid / running state + data dir.")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch ``onoats <command> [args...]``.

    Unknown options after the subcommand are forwarded verbatim to the
    subcommand's own parser (so e.g. ``onoats bot --help`` reaches the dual
    recorder's parser, and ``onoats convert --once`` reaches the converter).
    """
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser()

    # No command (or a bare -h/--help) → top-level help. Print + return 0
    # rather than letting argparse raise SystemExit, so callers get a clean rc.
    if not raw or raw[0] in ("-h", "--help"):
        parser.print_help()
        return 0

    command = raw[0]
    rest = raw[1:]
    handler = _HANDLERS.get(command)
    if handler is None:
        # Let argparse render the standard "invalid choice" error + usage.
        parser.parse_args(raw)
        return 2
    # Honor config.toml [storage].data_dir for every command (env still wins).
    # `init` writes config to XDG_CONFIG_HOME, unaffected by the data dir.
    if command != "init":
        _apply_config_data_dir()
    return handler(rest)


if __name__ == "__main__":
    raise SystemExit(main())
