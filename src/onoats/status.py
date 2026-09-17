"""Recorder status file — liveness + failure-state for the menu bar and ``onoats status``.

This is the **third versioned contract** in the system, alongside the audio-socket
wire contract (``transports/socket_audio.py`` ``WIRE_VERSION``) and the JSONL queue
``source`` enum. The recorder writes ``<data_dir>/.active/onoats.status.json`` on
start, rotation, and stop; ``onoats status`` reads it with the pid file kept as the
authoritative **liveness backstop**; the SwiftUI menu bar (Phase 5b) consumes the
same file. The ``schema`` integer lets the Swift consumer reject a drifted file
loudly rather than silently mis-render it — same independent-versioning argument as
the audio handshake ``v``.

**pid is the source of truth for the live/stopped verdict; the status file is the
source of truth for the detail** (audio source, STT label, start time, and *why* a
start failed). A stale status file must never report a dead recorder as live — so
the verdict is keyed on pid liveness, and the status ``running`` flag is used only
to *detect and label* staleness, never to override the pid (see ``resolve_liveness``).

**Atomic writes.** Every write goes through a temp file + ``os.replace`` so a reader
(or a crash mid-write) never observes half-JSON. A malformed/partial file reads back
as ``None`` (treated as "no status"), never as an exception.

**Write ordering (producer's contract — see runtime/dual):** on start, the pid file
is written *first*, then the status file; on stop, the status-stopped file is written
*first*, then the pid file is removed. That keeps the pid backstop consistent with
whatever ``onoats status`` reads at any instant.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from loguru import logger

STATUS_FILENAME = "onoats.status.json"
# v2 (release-plan Phase 4): adds the OPTIONAL `warning`, `mic_device`, and
# `system_device` fields (all flat string scalars, default None). One bump
# defines all three; Phase 5 populates the device fields without another bump.
# Both readers (this module and the menu bar's RecorderModel.swift) hard-reject
# any other version, so app + CLI must be reinstalled together
# (`make -C native install`) — a mixed-version window shows schema drift, not data.
#
# **`warning`'s grammar is frozen under v2, deliberately, not versioned
# separately.** `warning` is still a single string field at the schema level —
# no bump — but its CONTENTS are a small merged-branch grammar owned by
# `format_warning_branch`/`set_warning_branch`/`_parse_warning_branches` below:
# zero or more `"<branch>: <message>"` entries, sorted by branch name, joined
# by `"; "`. Both `branch` and `message` are sanitized against that same `"; "`
# delimiter at the one production choke point (`format_warning_branch`).
#
# **Two entry points emit grammar-shaped content, one is exempt by design.**
# `set_warning_branch` (via the private `_write_warning_field`) is the only
# read-modify-write UPDATE path against an EXISTING record, and is what
# actually prevents concurrent branches from clobbering each other.
# `write_running(warning=...)` is a second, narrower entry point used only by
# the startup-preflight kickstart-recovery seam, where no running record
# exists yet for `set_warning_branch` to annotate — it validates its `warning`
# argument (when not `None`) against this same grammar at write time (see its
# docstring), so a caller that forgets to pre-format through
# `format_warning_branch` is caught rather than silently corrupting the next
# `set_warning_branch` merge. `write_prestart_waiting`'s `note` argument is
# NOT part of this grammar at all, by design: it is a standalone, freeform
# pre-session message written before any branch or session exists (see its
# docstring) — a later `set_warning_branch` call landing on that record simply
# fails to find a parseable entry in it and treats it as the documented
# "malformed/legacy, drop it" case, which is expected, not a bug.
# `write_status` remains the fully general, ungated atomic primitive every
# producer in this module (including all of the above) uses to persist a
# whole `StatusRecord` — it is not warning-specific, and gating it would gate
# every field this module writes, not just `warning`.
#
# The Swift menu bar (`native/onoats-menubar/Sources/OnoatsMenuBarApp.swift`,
# `RecorderModel.swift`) is a cosmetic line-splitter, NOT a decoder of this
# grammar: it cannot import this module, so it re-implements only the OUTER
# split (entries on `"; "`, matching the join above) to know where one
# branch's text ends and the next begins for its own multi-line rendering.
# The inner split it then applies to each entry's text, on `" — "` (em dash),
# is unrelated to this grammar's `": "` branch/message separator — it exists
# purely to keep a long single-line hint from stretching the whole menu, by
# breaking on any em-dash clause the message text happens to contain. Swift
# never extracts a branch key from `warning` at all. Change the outer `"; "`
# join delimiter here only in lockstep with the Swift split, and bump
# `STATUS_SCHEMA_VERSION` only if the *field's shape* changes (e.g. `warning`
# stops being a string) — not for a change confined to this string's internal
# grammar.
STATUS_SCHEMA_VERSION = 2

# Active dir name mirrors the pid file's location (``<data_dir>/.active``).
_ACTIVE_DIR = ".active"


@dataclass(frozen=True)
class StatusRecord:
    """One snapshot of the recorder's state.

    ``schema`` guards consumer drift. ``running`` is the recorder's *self-reported*
    flag (informational — the pid file is authoritative for the live/stopped
    verdict). ``exit_reason``/``last_error``/``supervisor_rc`` are populated on a
    fail-loud exit so the menu bar can show *why* a start failed, not just that it
    is no longer running.
    """

    schema: int
    pid: int
    start_time: float
    audio_source: str
    # "" is the documented pre-recorder sentinel: records written before the
    # recorder has started (write_prestart_waiting / write_prestart_failure /
    # the write_stopped fallback) have no STT label yet. Readers must guard on
    # truthiness, not presence (`onoats status` does).
    stt_label: str
    running: bool
    last_rotation_time: float | None = None
    last_error: str | None = None
    # e.g. "graceful", "fatal_error_frame", "capturer-crash", "mic-denied",
    # "system-audio-failed" (genuine tap API failure — a TCC denial never
    # exits the capturer; denied taps deliver zeros and surface as `warning`).
    # Free-form but stable across producers.
    exit_reason: str | None = None
    supervisor_rc: int | None = None
    # Schema v2. `warning` is a live, non-fatal capture anomaly (today: the
    # capturer's all-zero-input detector) — set/cleared by the supervisor while
    # the session runs, so the menu bar can surface it without tailing logs.
    # `mic_device`/`system_device` are "<name> (uid=<uid>)" strings populated
    # from the capturer's `ONOATS-EVENT device` lines (release-plan Phase 5);
    # None on the PortAudio path, where `onoats status` falls back to the
    # configured [devices] names instead.
    warning: str | None = None
    mic_device: str | None = None
    system_device: str | None = None


@dataclass(frozen=True)
class Liveness:
    """Resolved verdict for ``onoats status`` / the menu bar.

    ``alive`` is the **verdict** and is keyed on pid liveness, never on the status
    file's ``running`` flag. ``note`` explains any staleness (the off-diagonal cells
    of the truth table) so the discrepancy is visible rather than silently resolved.
    """

    alive: bool
    pid: int | None
    status: StatusRecord | None
    note: str = ""


def status_path(data_dir: Path) -> Path:
    """``<data_dir>/.active/onoats.status.json``."""
    return data_dir / _ACTIVE_DIR / STATUS_FILENAME


# ---------------------------------------------------------------------------
# Atomic write / tolerant read
# ---------------------------------------------------------------------------


def write_status(data_dir: Path, record: StatusRecord) -> Path:
    """Atomically write ``record`` to the status file (temp + ``os.replace``)."""
    path = status_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(record), separators=(",", ":"), sort_keys=True)
    # NamedTemporaryFile in the SAME dir so os.replace is an atomic rename (no
    # cross-filesystem copy). delete=False because we hand the path to os.replace.
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".status-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Never leak a temp file on failure.
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


def read_status(data_dir: Path) -> StatusRecord | None:
    """Read the status file. Returns ``None`` if absent, half-written, malformed,
    or written under a different ``schema`` version.

    Tolerant by design: a partial/corrupt/drifted file is "no status", never an
    exception — the pid backstop still yields a correct liveness verdict on its own.
    """
    path = status_path(data_dir)
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    try:
        # The whole point of the schema integer is rejecting drift: a file
        # written under any other version must read as "no status", never be
        # rendered as if it were ours.
        if int(obj["schema"]) != STATUS_SCHEMA_VERSION:
            return None
        # `running` must be a real JSON boolean — truthy coercion would let a
        # drifted producer (e.g. "running": "false") silently mis-render.
        if not isinstance(obj["running"], bool):
            return None
        return StatusRecord(
            schema=int(obj["schema"]),
            pid=int(obj["pid"]),
            start_time=float(obj["start_time"]),
            audio_source=str(obj["audio_source"]),
            stt_label=str(obj["stt_label"]),
            running=obj["running"],
            last_rotation_time=(
                float(obj["last_rotation_time"])
                if obj.get("last_rotation_time") is not None
                else None
            ),
            last_error=(
                str(obj["last_error"]) if obj.get("last_error") is not None else None
            ),
            exit_reason=(
                str(obj["exit_reason"]) if obj.get("exit_reason") is not None else None
            ),
            supervisor_rc=(
                int(obj["supervisor_rc"])
                if obj.get("supervisor_rc") is not None
                else None
            ),
            warning=(str(obj["warning"]) if obj.get("warning") is not None else None),
            mic_device=(
                str(obj["mic_device"]) if obj.get("mic_device") is not None else None
            ),
            system_device=(
                str(obj["system_device"])
                if obj.get("system_device") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError):
        # Missing/typewrong required field → treat as no status, not a crash.
        return None


# ---------------------------------------------------------------------------
# Producer helpers (called by runtime/dual at start, rotation, stop)
# ---------------------------------------------------------------------------


def write_running(
    data_dir: Path,
    *,
    pid: int,
    audio_source: str,
    stt_label: str,
    start_time: float | None = None,
    warning: str | None = None,
) -> Path:
    """Write the start-of-session record (``running=true``).

    ``warning`` threads a message straight into this freshly-built record —
    used by the preflight-path kickstart-recovery seam (``dual.py``), where
    no running record exists yet for ``set_warning_branch()`` to annotate.
    A callback fired *during* preflight would otherwise be silently
    overwritten the moment this function next runs; passing the captured
    message here avoids that race. ``None`` (the default) preserves today's
    behavior exactly.

    Defense in depth: when ``warning`` is not ``None``, it is validated
    against the same ``"<branch>: <message>"`` grammar
    :func:`format_warning_branch`/:func:`set_warning_branch` produce and
    consume. This function is a second, unguarded entry point into the
    ``warning`` field (the module docstring above documents why it exists
    alongside :func:`set_warning_branch`) — a caller that passes a raw,
    un-prefixed message here would silently write a value the next
    :func:`set_warning_branch` merge cannot parse back apart (it degrades to
    "malformed/legacy, drop it" — see :func:`_parse_warning_branches`),
    losing the message rather than corrupting anything, but silently. A
    malformed value is logged and written anyway (this producer never
    raises, matching every other write helper in this module) so the
    misuse is visible instead of merely lossy on the next read.
    """
    if warning is not None and not _is_well_formed_warning(warning):
        logger.warning(
            f"status.write_running: warning={warning!r} does not match the "
            '"<branch>: <message>" grammar (expected output from '
            "format_warning_branch) — writing it anyway, but a later "
            "set_warning_branch() call will not be able to merge it"
        )
    return write_status(
        data_dir,
        StatusRecord(
            schema=STATUS_SCHEMA_VERSION,
            pid=pid,
            start_time=start_time if start_time is not None else time.time(),
            audio_source=audio_source,
            stt_label=stt_label,
            running=True,
            warning=warning,
        ),
    )


def mark_rotation(data_dir: Path, *, when: float | None = None) -> Path | None:
    """Stamp ``last_rotation_time`` on the current record (best-effort).

    Returns ``None`` if there is no readable record to update (nothing to rotate
    against) — the caller treats that as a no-op, not a failure.
    """
    current = read_status(data_dir)
    if current is None:
        return None
    return write_status(
        data_dir,
        replace(current, last_rotation_time=when if when is not None else time.time()),
    )


def _write_warning_field(
    data_dir: Path, current: StatusRecord, warning: str | None
) -> Path:
    """Write the merged ``warning`` field onto an already-read record.

    **Private on purpose.** This is the only whole-field writer used for a
    read-modify-write UPDATE of an EXISTING record's ``warning`` — it takes
    the record the caller has already read and gated, and neither reads nor
    gates on its own. Its sole production caller is :func:`set_warning_branch`,
    which owns both the branch grammar (via :func:`format_warning_branch`) and
    the same-pid/running gate. (See the module docstring above for how this
    relates to :func:`write_running`'s narrower, validated ``warning=``
    entry point, :func:`write_prestart_waiting`'s deliberately-exempt
    freeform ``note``, and :func:`write_status`, which stays the fully
    general, ungated primitive underneath all of these.)

    It used to be a public ``set_warning(data_dir, warning)``: an unguarded
    third whole-field writer that bypassed the branch grammar entirely and
    carried a weaker gate than :func:`set_warning_branch` (existence only, no
    same-pid check). Its last production callers — ``cli.py``'s capturer
    zero-run warning/clear handlers — moved to :func:`set_warning_branch`,
    leaving a public escape hatch around the choke point with nothing using
    it. Folding it in here keeps the "no clobbering between concurrent
    branches" property structural rather than conventional for this update
    path: there is no longer a supported way to merge a branch change into
    an existing record's ``warning`` without going through the grammar.
    """
    return write_status(data_dir, replace(current, warning=warning))


# Status-warning branch key for the SHARED startup-preflight STT recovery (one
# probe against one server, before any WebSocketSTTService instance exists).
# Live-session recoveries are per-instance and use ``stt_branch(name)`` below,
# so mic's and system's independent warnings cannot clobber each other.
STT_WARNING_BRANCH = "stt"


def stt_branch(instance: str | None = None) -> str:
    """Status-warning branch key for an STT warning.

    ``None`` -> the shared ``"stt"`` branch, used for the startup preflight
    recovery (one probe, one server, no instance exists yet).
    ``"mic"``/``"system"`` -> ``"stt-mic"``/``"stt-system"``, the per-instance
    live-session branches. ``dual.py`` constructs two independent
    ``WebSocketSTTService`` instances against the same server; sharing one
    branch key let either instance's clear erase the other's still-unconfirmed
    warning, exactly the way the ``mic``/``system`` capture branches already
    avoid by being instance-scoped.

    Lives here, not in ``onoats.stt.launchd``: a branch key is a status-layer
    naming concept, owned alongside :func:`format_warning_branch` /
    :func:`set_warning_branch`, and ``launchd.py`` is meant to stay a leaf
    module about ``launchctl`` and the kickstart cooldown. Its only caller
    (``runtime._create_stt_service``) already imports ``status`` directly.
    """
    return STT_WARNING_BRANCH if not instance else f"{STT_WARNING_BRANCH}-{instance}"


def format_warning_branch(branch: str, message: str) -> str:
    """Render one branch's entry exactly as :func:`set_warning_branch` merges it.

    The single owner of the ``"<branch>: <message>"`` lead-in. The preflight
    kickstart-recovery path writes its message through
    ``write_running(warning=...)`` (a raw whole-field write — no running record
    exists yet for :func:`set_warning_branch` to annotate), so without a shared
    formatter the two paths each own a copy of the prefixing rule and drift.
    Callers pass a **bare** message; nobody pre-prefixes.

    Also the single sanitization choke point for the ``"; "`` entry delimiter:
    stripping it here, rather than only in :func:`set_warning_branch`, covers
    BOTH writers of the merged ``warning`` grammar (``set_warning_branch``'s
    read-modify-write path and the preflight path's direct
    ``write_running(warning=format_warning_branch(...))`` call) — a stray
    ``"; "`` in either ``branch`` or ``message`` would otherwise forge an
    unclearable pseudo-branch entry on the next :func:`_parse_warning_branches`
    read regardless of which writer produced it.
    """
    return f"{branch.replace('; ', ',')}: {message.replace('; ', ', ')}"


def _parse_warning_branches(warning: str | None) -> dict[str, str]:
    """Parse a merged ``warning`` string back into ``{branch: message}``.

    Mirrors the join convention in :func:`set_warning_branch` /
    ``cli.py``'s (former) ``active_warnings`` rebuild: entries are separated
    by ``"; "`` and each entry is ``f"{branch}: {message}"``. A malformed or
    legacy entry (no ``": "`` separator — e.g. a warning written before
    branch-keying existed) is dropped rather than raising: it can't be
    attributed to a branch, so it degrades to "no prior warning for that
    slot" instead of corrupting the merge.
    """
    if not warning:
        return {}
    branches: dict[str, str] = {}
    for part in warning.split("; "):
        branch, sep, message = part.partition(": ")
        if not sep:
            continue
        branches[branch] = message
    return branches


def _is_well_formed_warning(warning: str) -> bool:
    """True if ``warning`` round-trips exactly through the branch grammar.

    Parses ``warning`` with :func:`_parse_warning_branches` and re-renders it
    with :func:`format_warning_branch`, in sorted branch order — the exact
    merge :func:`set_warning_branch` performs. A non-empty string that is not
    itself grammar-shaped (no branch at all, or extra text the parser drops)
    fails to round-trip and is reported malformed. Used by
    :func:`write_running` as a defense-in-depth check on its ``warning``
    keyword-argument, the one entry point into this field that does not go
    through :func:`set_warning_branch` itself.
    """
    parsed = _parse_warning_branches(warning)
    if not parsed:
        return False
    rebuilt = "; ".join(format_warning_branch(b, parsed[b]) for b in sorted(parsed))
    return rebuilt == warning


def set_warning_branch(data_dir: Path, branch: str, message: str | None) -> Path | None:
    """Set (or clear, with ``None``) one branch's slice of the ``warning`` field.

    The single public writer of the ``warning`` field: it reads the current
    merged ``warning``, parses it into per-branch entries (:func:`_parse_warning_branches`), replaces or removes only
    ``branch``'s entry, and rewrites the merge in **sorted branch-name
    order** (matching ``cli.py``'s pre-existing ``sorted(active_warnings)``
    convention) — so concurrent branches (``mic``/``system``/``stt``) never
    clobber each other's entries the way a whole-field overwrite would.

    ``"; "`` is the split-based parser's entry delimiter, so a ``branch`` or
    ``message`` containing it would otherwise forge a second, unclearable
    pseudo-branch entry on the next read (a stray delimiter in
    caller-controlled text used to be merely cosmetic, back when ``cli.py``
    kept its own in-process ``active_warnings`` dict as the authority — it
    is not cosmetic now that this helper round-trips through the on-disk
    string). :func:`format_warning_branch` is the single sanitization choke
    point for this delimiter (it strips any ``"; "`` occurring inside
    ``branch`` or ``message`` before rendering the entry) — this helper only
    pre-sanitizes ``branch`` for its own dict lookup/pop, since that key never
    passes through :func:`format_warning_branch` itself. Moved there rather
    than kept only here so the preflight path's direct
    ``write_running(warning=format_warning_branch(...))`` call — which never
    goes through this function — gets the same protection, not just
    :func:`set_warning_branch`'s read-modify-write path. Changing the on-disk
    ``warning`` grammar itself (e.g. escaping) would also change what the
    Swift menu-bar reader shares under
    ``STATUS_SCHEMA_VERSION`` and is out of scope here. Best-effort like
    :func:`mark_rotation`: returns ``None`` when there is no readable record
    to annotate.

    ``message`` must be **bare** — never pre-prefixed with ``"<branch>: "``.
    :func:`format_warning_branch` is the sole owner of that lead-in and is
    applied here, so a caller that prefixes too would double it.

    Like :func:`set_devices`, this is a no-op on a **stopped** record that
    belongs to a *different* process: a branch event (an stt kickstart-
    recovery confirm/clear, a capturer zero-run event) can race ahead of the
    next session's :func:`write_running` and land while the record on disk
    still belongs to an earlier, now-stopped, different session — annotating
    that record would mislabel history (the same reasoning :func:`set_devices`
    documents for device fields).

    That race is about a *different* session's record, not this one's: a
    stopped record whose ``pid`` still matches the caller's own process is
    still this same session's terminal record (e.g. ``cli.py``'s bounded
    stderr-drain grace period, which keeps consuming trailing capturer
    diagnostics — including zero-run-warning/-clear — for a short window
    *after* :func:`write_stopped` has already run for this same session).
    Gating on ``running`` alone would silently drop those trailing
    diagnostics, which the unbranched whole-field writer this replaced (the
    former public ``set_warning``) did not do. Gating on ``pid`` **in
    addition to** ``running`` (not instead of it) distinguishes "a new
    session already started" (block) from "my own session, already marked
    stopped, still draining" (allow) — PID reuse within the same shutdown
    window is not a realistic concern on any platform this runs on.

    **Scope this guard does NOT cover, by design, not by oversight:** a
    *running* record whose ``pid`` differs from the caller's own process is
    NOT blocked here. That is not the "stale event vs. a new session"
    race above — it would mean two `onoats bot` recorder processes have a
    live, ``running=true`` record for the same ``data_dir`` at once, which
    the pid-file lock elsewhere in this system (see ``resolve_liveness`` /
    the cli's start-up lock) is what actually prevents; every real branch
    writer (the socket supervisor's mic/system events, the STT preflight and
    live-reconnect recoveries) runs inside the same single process that owns
    the current running record, so ``current.pid`` when ``current.running``
    is true is always this caller's own pid in practice. A second pid check
    on the running branch would therefore never fire against a real race —
    only against test fixtures that plant an arbitrary ``pid=`` on a running
    record as a stand-in for "some other process" (several of this module's
    own tests do exactly that, e.g. ``write_running(..., pid=1, ...)``
    followed by a same-process ``set_warning_branch`` call that must still
    succeed) — so adding it would reject legitimate same-process calls in
    every test that uses that shorthand without closing any real gap.
    """
    current = read_status(data_dir)
    if current is None:
        return None
    if not current.running and current.pid != os.getpid():
        return None
    # `"; "`-strip `branch` the same way `format_warning_branch` will below,
    # so the dict key used for pop()/lookup matches the sanitized key that
    # ends up on disk (format_warning_branch is the sole sanitization choke
    # point — see its docstring — but its output isn't parsed back through
    # this dict, so the key here must be pre-sanitized to stay consistent).
    branch = branch.replace("; ", ",")
    branches = _parse_warning_branches(current.warning)
    if message is None:
        branches.pop(branch, None)
    else:
        branches[branch] = message
    merged = (
        "; ".join(format_warning_branch(b, branches[b]) for b in sorted(branches))
        or None
    )
    return _write_warning_field(data_dir, current, merged)


def set_devices(
    data_dir: Path,
    *,
    mic_device: str | None = None,
    system_device: str | None = None,
) -> Path | None:
    """Set the capture-device fields on the current RUNNING record.

    Called by the socket supervisor when the capturer reports the device it
    bound (``ONOATS-EVENT device``) — at session start (via the deferred-apply
    task, since the events outrun the recorder's start write) and again on a
    mid-session mic rebind. ``None`` arguments leave that field untouched, so
    one branch's update never clears the other's.

    Unlike :func:`set_warning_branch` this is a no-op on a NON-running
    record too: device events fire within the capturer's first second, when the record on
    disk (if any) still belongs to the *previous* session — annotating that
    stopped record would mislabel history. Same last-writer-wins concurrency
    contract as :func:`stamp_supervisor_failure`.
    """
    current = read_status(data_dir)
    if current is None or not current.running:
        return None
    updates: dict[str, str] = {}
    if mic_device is not None:
        updates["mic_device"] = mic_device
    if system_device is not None:
        updates["system_device"] = system_device
    if not updates:
        return None
    return write_status(data_dir, replace(current, **updates))


def write_stopped(
    data_dir: Path,
    *,
    exit_reason: str = "graceful",
    last_error: str | None = None,
    supervisor_rc: int | None = None,
) -> Path:
    """Write the end-of-session record (``running=false``) + any failure detail.

    Preserves the start-of-session detail (pid, source, STT label, rotation time)
    by reading the current record when present; falls back to a minimal stopped
    record if none exists (so a fail-loud exit before any start write still leaves
    a readable failure reason).
    """
    current = read_status(data_dir)
    if current is not None:
        record = replace(
            current,
            running=False,
            exit_reason=exit_reason,
            last_error=last_error,
            supervisor_rc=supervisor_rc,
        )
    else:
        record = StatusRecord(
            schema=STATUS_SCHEMA_VERSION,
            pid=os.getpid(),
            start_time=time.time(),
            audio_source="",
            stt_label="",
            running=False,
            exit_reason=exit_reason,
            last_error=last_error,
            supervisor_rc=supervisor_rc,
        )
    return write_status(data_dir, record)


def write_prestart_failure(
    data_dir: Path,
    *,
    audio_source: str,
    exit_reason: str,
    last_error: str,
    supervisor_rc: int = 1,
) -> Path:
    """Write a FRESH stopped record for a session that died before the recorder ran.

    Unlike :func:`write_stopped`, this never preserves an existing record's
    pid/start_time: the recorder never started, so whatever is on disk belongs
    to a PREVIOUS session — preserving it would defeat readers' freshness
    checks (the menu bar rejects records whose ``start_time`` predates the
    session it spawned, falling back to the raw exit code).
    """
    return write_status(
        data_dir,
        StatusRecord(
            schema=STATUS_SCHEMA_VERSION,
            pid=os.getpid(),
            start_time=time.time(),
            audio_source=audio_source,
            stt_label="",
            running=False,
            exit_reason=exit_reason,
            last_error=last_error,
            supervisor_rc=supervisor_rc,
        ),
    )


def write_prestart_waiting(data_dir: Path, *, audio_source: str, note: str) -> Path:
    """Write a FRESH record for the prompt-pending window before the recorder runs.

    Release-plan Phase 7: the capturer's tap preflight makes the TCC-prompting
    call before its sockets exist, so a first start can legitimately sit for
    tens of seconds waiting on the Screen & System Audio Recording dialog. The
    supervisor calls this (once, when it extends its socket wait) so
    ``onoats status`` / the menu bar show *why* nothing is recording yet
    instead of a stale previous-session record.

    The record is ``running=True`` with ``note`` in the v2 ``warning`` field —
    the session is genuinely in progress (the supervisor pid is live), just not
    capturing yet. ``stt_label`` is the pre-recorder ``""`` sentinel (see
    :class:`StatusRecord`): the recorder, which owns STT resolution, has not
    started. Every successor overwrites it: the recorder's
    :func:`write_running` builds a fresh record once the prompt is answered,
    and :func:`write_prestart_failure` replaces it if the wait times out.

    **`note` is deliberately NOT branch-grammar content** — see the module
    docstring's "two entry points ... one is exempt" note. There is no
    branch and no session yet at this point (``stt_label`` is still the
    pre-recorder sentinel), so there is nothing for :func:`set_warning_branch`
    to merge against and no concurrent-branch clobbering to protect; `note`
    is a plain, full sentence for the human reading ``onoats status``/the
    menu bar (e.g. "waiting for the system-audio permission prompt — answer
    the Screen & System Audio Recording dialog to start the session"),
    exactly as it appears in ``cli.py``'s single caller. Routing it through
    :func:`format_warning_branch` would fabricate a branch key for a
    message that has none, changing what ships to the Swift menu bar for no
    behavioral gain. If a branch write lands on this record before its
    successor overwrites it (see above), :func:`_parse_warning_branches`
    finds no ``": "``-separated entry in `note` and drops it as
    malformed/legacy — expected, not a bug.
    """
    return write_status(
        data_dir,
        StatusRecord(
            schema=STATUS_SCHEMA_VERSION,
            pid=os.getpid(),
            start_time=time.time(),
            audio_source=audio_source,
            stt_label="",
            running=True,
            warning=note,
        ),
    )


def stamp_supervisor_failure(
    data_dir: Path,
    *,
    exit_reason: str,
    supervisor_rc: int,
    last_error: str | None = None,
) -> Path | None:
    """Enrich an existing stopped record with the supervisor's verdict.

    The in-process recorder writes ``running=false`` on its own teardown; the
    socket supervisor then knows the *specific* cause (capturer-crash vs the
    recorder's own fatal ErrorFrame) and the final rc. This stamps those without
    clobbering the recorder's start detail. No-op (returns ``None``) if there is no
    record to enrich.

    Concurrency contract: this is a read-modify-write and is **deliberately
    last-writer-wins**. The supervisor calls it after waiting for recorder drain,
    but a force-cancelled recorder may still race its own stopped-write against
    this one. ``os.replace`` keeps every individual write atomic (a reader never
    sees torn JSON); the guarantee is "one complete record wins", NOT "updates
    are serialized". Both racers write ``running=false``, so the liveness verdict
    is unaffected either way — only the failure detail differs.
    """
    current = read_status(data_dir)
    if current is None:
        return None
    return write_status(
        data_dir,
        replace(
            current,
            running=False,
            exit_reason=exit_reason,
            supervisor_rc=supervisor_rc,
            last_error=last_error if last_error is not None else current.last_error,
        ),
    )


# ---------------------------------------------------------------------------
# Reader: pid-authoritative liveness with status as detail (the 4-cell table)
# ---------------------------------------------------------------------------


def resolve_liveness(
    data_dir: Path,
    *,
    read_pid,
    process_alive,
) -> Liveness:
    """Resolve the live/stopped verdict from {status running?, pid alive?}.

    The verdict is **pid-authoritative**; ``read_pid``/``process_alive`` are injected
    (the cli's marker-validated pid helpers) so this module stays dependency-light
    and unit-testable. The four cells:

    | status.running | pid alive | verdict  | note                                  |
    |----------------|-----------|----------|---------------------------------------|
    | true           | true      | RUNNING  | (consistent)                          |
    | true           | dead      | STOPPED  | stale status — pid dead wins          |
    | false/absent   | true      | RUNNING  | pid backstop wins (status not-yet/stale) |
    | false/absent   | dead      | STOPPED  | (consistent)                          |

    The pid file existing-but-dead and the pid file being absent both mean "not
    alive"; only a *live* pid yields RUNNING. The status ``running`` flag never
    flips the verdict — it only labels the off-diagonal staleness.
    """
    status = read_status(data_dir)
    pid = read_pid(data_dir)
    alive = pid is not None and process_alive(pid)

    note = ""
    if status is not None:
        if status.running and not alive:
            note = (
                "stale status file (claims running, pid not alive) — reporting stopped"
            )
        elif not status.running and alive:
            note = "status file claims stopped but pid is alive — reporting running (pid backstop)"
    # pid is returned even when dead — `onoats status` prints "stale pid file
    # (pid X not running)" and needs the number.
    return Liveness(alive=alive, pid=pid, status=status, note=note)
