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

STATUS_FILENAME = "onoats.status.json"
# v2 (release-plan Phase 4): adds the OPTIONAL `warning`, `mic_device`, and
# `system_device` fields (all flat string scalars, default None). One bump
# defines all three; Phase 5 populates the device fields without another bump.
# Both readers (this module and the menu bar's RecorderModel.swift) hard-reject
# any other version, so app + CLI must be reinstalled together
# (`make -C native install`) — a mixed-version window shows schema drift, not data.
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
) -> Path:
    """Write the start-of-session record (``running=true``)."""
    return write_status(
        data_dir,
        StatusRecord(
            schema=STATUS_SCHEMA_VERSION,
            pid=pid,
            start_time=start_time if start_time is not None else time.time(),
            audio_source=audio_source,
            stt_label=stt_label,
            running=True,
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


def set_warning(data_dir: Path, warning: str | None) -> Path | None:
    """Set (or clear, with ``None``) the live capture warning on the current record.

    Called by the socket supervisor when the capturer reports a non-fatal
    capture anomaly (the all-zero-input detector) and again when real audio
    re-arms it. Best-effort like :func:`mark_rotation`: returns ``None`` when
    there is no readable record to annotate (e.g. the event raced ahead of the
    recorder's start write — the detector needs ~30 s of session, so in
    practice the record exists). Same last-writer-wins concurrency contract as
    :func:`stamp_supervisor_failure`.
    """
    current = read_status(data_dir)
    if current is None:
        return None
    return write_status(data_dir, replace(current, warning=warning))


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

    Unlike :func:`set_warning` this is a no-op on a NON-running record too:
    device events fire within the capturer's first second, when the record on
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
