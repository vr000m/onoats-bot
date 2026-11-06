#!/usr/bin/env python3
"""Bounded signed-app recording proof without printing transcript contents."""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from typing import NamedTuple, TypeVar

T = TypeVar("T")
_PROC_PIDTBSDINFO = 3
_MAXCOMLEN = 16
JSONL_LINE_LIMIT = 1024 * 1024
CONTROL_FILE_LIMIT = 64 * 1024
TIMEOUT_DEFAULTS = {
    "app_timeout": 15.0,
    "recording_timeout": 120.0,
    "activity_timeout": 120.0,
    "stop_timeout": 120.0,
    "transcript_timeout": 30.0,
}


def _load_telemetry_contract():
    """Load constants from this checkout, never from an installed package."""
    path = Path(__file__).resolve().parents[1] / "src/onoats/ui_telemetry_contract.py"
    spec = importlib.util.spec_from_file_location(
        "onoats_smoke_telemetry_contract", path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("UI telemetry contract source is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_TELEMETRY_CONTRACT = _load_telemetry_contract()
UI_TELEMETRY_SCHEMA_VERSION = _TELEMETRY_CONTRACT.UI_TELEMETRY_SCHEMA_VERSION
SNAPSHOT_EXPIRY_SECONDS = _TELEMETRY_CONTRACT.SNAPSHOT_EXPIRY_SECONDS
AUDIO_OBSERVATION_EXPIRY_SECONDS = _TELEMETRY_CONTRACT.AUDIO_OBSERVATION_EXPIRY_SECONDS
LEVEL_MIN = _TELEMETRY_CONTRACT.LEVEL_MIN
LEVEL_MAX = _TELEMETRY_CONTRACT.LEVEL_MAX
_TRANSCRIPT_RUN_ID_RE = re.compile(_TELEMETRY_CONTRACT.TRANSCRIPT_RUN_ID_PATTERN)


class SmokeFailure(RuntimeError):
    """One stage of the installed recording proof failed."""


def _invalidate_receipt(path: Path) -> None:
    from install_staged import invalidate_smoke_receipt

    invalidate_smoke_receipt(path)


def _publish_receipt(path: Path, identity) -> None:
    from install_staged import write_smoke_receipt

    write_smoke_receipt(path, identity)


def _positive_finite_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return value


def _validate_timeout_arguments(args: argparse.Namespace) -> None:
    for field, default in TIMEOUT_DEFAULTS.items():
        value = float(getattr(args, field, default))
        if not math.isfinite(value) or value <= 0:
            option = field.replace("_", "-")
            raise SmokeFailure(f"--{option} must be finite and greater than zero")
        setattr(args, field, value)


def _verify_release_identity(app: Path, identity_path: Path):
    """Re-derive the immutable staged identity before and after recording."""
    try:
        from install_staged import (
            InstallError,
            _read_release_identity,
            observed_cli_git_identity,
            release_identity,
        )

        expected = _read_release_identity(identity_path)
        observed = release_identity(app, cli_git_identity=observed_cli_git_identity())
    except (OSError, ValueError, InstallError) as exc:
        raise SmokeFailure("release identity could not be verified") from exc
    if observed != expected:
        raise SmokeFailure("release identity changed")
    return expected


def _dynamic_code_identity(pid: int) -> tuple[str, str]:
    """Read designated requirement and cdhash for a running PID via Security."""
    if sys.platform != "darwin":
        raise SmokeFailure("dynamic code identity requires macOS Security.framework")
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    ref = ctypes.c_void_p
    core.CFRelease.argtypes = [ref]
    core.CFNumberCreate.argtypes = [ref, ctypes.c_long, ctypes.c_void_p]
    core.CFNumberCreate.restype = ref
    core.CFDictionaryCreate.argtypes = [
        ref,
        ctypes.POINTER(ref),
        ctypes.POINTER(ref),
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    core.CFDictionaryCreate.restype = ref
    core.CFDictionaryGetValue.argtypes = [ref, ref]
    core.CFDictionaryGetValue.restype = ref
    core.CFStringGetLength.argtypes = [ref]
    core.CFStringGetLength.restype = ctypes.c_long
    core.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint]
    core.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    core.CFStringGetCString.argtypes = [
        ref,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint,
    ]
    core.CFStringGetCString.restype = ctypes.c_bool
    core.CFDataGetLength.argtypes = [ref]
    core.CFDataGetLength.restype = ctypes.c_long
    core.CFDataGetBytePtr.argtypes = [ref]
    core.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_ubyte)

    security.SecCodeCopyGuestWithAttributes.argtypes = [
        ref,
        ref,
        ctypes.c_uint,
        ctypes.POINTER(ref),
    ]
    security.SecCodeCopyGuestWithAttributes.restype = ctypes.c_int32
    security.SecCodeCopyDesignatedRequirement.argtypes = [
        ref,
        ctypes.c_uint,
        ctypes.POINTER(ref),
    ]
    security.SecCodeCopyDesignatedRequirement.restype = ctypes.c_int32
    security.SecRequirementCopyString.argtypes = [
        ref,
        ctypes.c_uint,
        ctypes.POINTER(ref),
    ]
    security.SecRequirementCopyString.restype = ctypes.c_int32
    security.SecCodeCopySigningInformation.argtypes = [
        ref,
        ctypes.c_uint,
        ctypes.POINTER(ref),
    ]
    security.SecCodeCopySigningInformation.restype = ctypes.c_int32

    pid_key = ref.in_dll(security, "kSecGuestAttributePid")
    unique_key = ref.in_dll(security, "kSecCodeInfoUnique")
    pid_value = ctypes.c_int32(pid)
    pid_number = core.CFNumberCreate(None, 3, ctypes.byref(pid_value))
    if not pid_number:
        raise SmokeFailure("dynamic code identity could not allocate PID attribute")
    attributes = code = requirement = requirement_text = signing = None
    try:
        keys = (ref * 1)(pid_key)
        values = (ref * 1)(pid_number)
        attributes = core.CFDictionaryCreate(None, keys, values, 1, None, None)
        code = ref()
        if not attributes or security.SecCodeCopyGuestWithAttributes(
            None, attributes, 0, ctypes.byref(code)
        ):
            raise SmokeFailure(
                "running app PID has no Security.framework code identity"
            )
        requirement = ref()
        if security.SecCodeCopyDesignatedRequirement(
            code, 0, ctypes.byref(requirement)
        ):
            raise SmokeFailure("running app has no designated requirement")
        requirement_text = ref()
        if security.SecRequirementCopyString(
            requirement, 0, ctypes.byref(requirement_text)
        ):
            raise SmokeFailure("running app requirement could not be rendered")
        encoding = 0x08000100
        size = (
            core.CFStringGetMaximumSizeForEncoding(
                core.CFStringGetLength(requirement_text), encoding
            )
            + 1
        )
        buffer = ctypes.create_string_buffer(size)
        if not core.CFStringGetCString(requirement_text, buffer, size, encoding):
            raise SmokeFailure("running app requirement is not UTF-8")
        signing = ref()
        if security.SecCodeCopySigningInformation(code, 1 << 1, ctypes.byref(signing)):
            raise SmokeFailure("running app signing information is unavailable")
        unique = core.CFDictionaryGetValue(signing, unique_key)
        if not unique:
            raise SmokeFailure("running app signing information has no cdhash")
        length = core.CFDataGetLength(unique)
        cdhash = bytes(core.CFDataGetBytePtr(unique)[:length]).hex()
        return buffer.value.decode("utf-8"), cdhash
    finally:
        for value in (
            signing,
            requirement_text,
            requirement,
            code,
            attributes,
            pid_number,
        ):
            if value:
                core.CFRelease(value)


def _verify_dynamic_release_identity(pid: int, expected) -> None:
    requirement, cdhash = _dynamic_code_identity(pid)
    if requirement != expected.designated_requirement or cdhash != expected.cdhash:
        raise SmokeFailure("running app dynamic code identity does not match release")


class ProcessBirth(NamedTuple):
    seconds: int
    microseconds: int


class RecorderIdentity(NamedTuple):
    pid: int
    start_epoch: float
    command: str
    process_start: str
    pgid: int
    process_birth: ProcessBirth


class ProcessRow(NamedTuple):
    parent: int
    pgid: int
    start: str
    command: str


class ProcessIdentity(NamedTuple):
    pid: int
    pgid: int
    start: str
    command: str
    process_birth: ProcessBirth


class OwnedProcessGroup(NamedTuple):
    pgid: int
    members: tuple[ProcessIdentity, ...]


class CleanupTargetResult(NamedTuple):
    label: str
    pid: int
    initial_state: str
    term_sent: bool
    kill_sent: bool
    final_state: str


class _ProcBsdInfo(ctypes.Structure):
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * _MAXCOMLEN),
        ("pbi_name", ctypes.c_char * (_MAXCOMLEN * 2)),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


def _process_birth(pid: int) -> ProcessBirth | None:
    """Read the kernel process generation used as signalling authority."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    except OSError:
        return None
    proc_pidinfo = libproc.proc_pidinfo
    proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    proc_pidinfo.restype = ctypes.c_int
    info = _ProcBsdInfo()
    size = ctypes.sizeof(info)
    if proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), size) != size:
        return None
    return ProcessBirth(int(info.pbi_start_tvsec), int(info.pbi_start_tvusec))


def wait_for(
    stage: str,
    timeout: float,
    probe: Callable[[], T | None],
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    deadline = clock() + timeout
    while True:
        value = probe()
        if value is not None:
            return value
        remaining = deadline - clock()
        if remaining <= 0:
            raise SmokeFailure(f"{stage} timed out after {timeout:.1f}s")
        sleep(min(0.2, remaining))


def _control_text(path: Path) -> str | None:
    """Read one bounded, regular, no-follow control-file descriptor."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            metadata = os.fstat(fd)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > CONTROL_FILE_LIMIT
            ):
                return None
            data = os.read(fd, CONTROL_FILE_LIMIT + 1)
            if len(data) > CONTROL_FILE_LIMIT:
                return None
            return data.decode("utf-8")
        finally:
            os.close(fd)
    except (OSError, UnicodeError):
        return None


def _process_executable(pid: int) -> str | None:
    """Kernel executable path; argv text is never app authentication."""
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        if proc_pidpath(pid, buffer, len(buffer)) <= 0:
            return None
        return os.fsdecode(buffer.value)
    except OSError:
        return None


def _json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(
            _control_text(path) or "",
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _processes() -> dict[int, ProcessRow]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,pgid=,lstart=,command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    found: dict[int, ProcessRow] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 8)
        if (
            len(fields) == 9
            and fields[0].isdigit()
            and fields[1].isdigit()
            and fields[2].isdigit()
        ):
            found[int(fields[0])] = ProcessRow(
                int(fields[1]), int(fields[2]), " ".join(fields[3:8]), fields[8]
            )
    return found


def _one_app_pid(app: Path) -> int | None:
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    matches = [pid for pid in _processes() if _process_executable(pid) == executable]
    if len(matches) > 1:
        raise SmokeFailure("app identity failed: multiple matching Onoats processes")
    return matches[0] if matches else None


def _pid_file_matches(
    pid_path: Path, *, pid: int, start_epoch: float, command: str
) -> bool:
    try:
        fields = (_control_text(pid_path) or "").splitlines()
        return (
            len(fields) >= 4
            and fields[1].strip() == "onoats-bot"
            and bool(command.strip())
            and fields[2].strip() == command.strip()
            and int(fields[0]) == pid
            and float(fields[3]) == start_epoch
        )
    except (OSError, ValueError):
        return False


def _recording_identity(
    status_path: Path, pid_path: Path, *, app_pid: int
) -> RecorderIdentity | None:
    status = _json(status_path)
    if not status or status.get("running") is not True:
        return None
    try:
        recorder_pid = int(status["pid"])
        epoch = float(status["start_time"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(epoch):
        return None
    initial = _processes()
    initial_chain = _ancestry_chain(recorder_pid, app_pid, initial)
    if initial_chain is None:
        raise SmokeFailure("recorder ancestry failed: recorder is not app-owned")
    births_before = {pid: _process_birth(pid) for pid in initial_chain}
    if any(birth is None for birth in births_before.values()):
        return None
    processes = _processes()
    current_chain = _ancestry_chain(recorder_pid, app_pid, processes)
    if current_chain != initial_chain:
        return None
    births_after = {pid: _process_birth(pid) for pid in current_chain}
    if births_after != births_before:
        return None
    if not _descends_from(recorder_pid, app_pid, processes):
        raise SmokeFailure("recorder ancestry failed: recorder is not app-owned")
    if not _pid_file_matches(
        pid_path,
        pid=recorder_pid,
        start_epoch=epoch,
        command=processes[recorder_pid].command,
    ):
        return None
    row = processes[recorder_pid]
    birth = births_after[recorder_pid]
    assert birth is not None
    return RecorderIdentity(
        recorder_pid, epoch, row.command, row.start, row.pgid, birth
    )


def _wait_identity_gone(identity: ProcessIdentity, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _identity_matches(identity):
            return True
        time.sleep(0.1)
    return not _identity_matches(identity)


def _recorder_process_identity(identity: RecorderIdentity) -> ProcessIdentity:
    return ProcessIdentity(
        identity.pid,
        identity.pgid,
        identity.process_start,
        identity.command,
        identity.process_birth,
    )


def _app_process_matches(app: Path, identity: ProcessIdentity) -> bool:
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    return (_process_executable(identity.pid) == executable) and _identity_matches(
        identity
    )


def _capture_process_identity(pid: int) -> ProcessIdentity | None:
    """Capture command/topology between two matching kernel-birth reads."""
    birth_before = _process_birth(pid)
    if birth_before is None:
        return None
    row = _processes().get(pid)
    birth_after = _process_birth(pid)
    if row is None or birth_after != birth_before:
        return None
    return ProcessIdentity(pid, row.pgid, row.start, row.command, birth_after)


def _identity_matches(identity: ProcessIdentity) -> bool:
    row = _processes().get(identity.pid)
    return row is not None and (
        row.pgid == identity.pgid
        and row.start == identity.start
        and row.command == identity.command
        and _process_birth(identity.pid) == identity.process_birth
    )


def _signal_identity(identity: ProcessIdentity, sig: signal.Signals) -> bool:
    """Signal only an exact captured process generation."""
    if not _identity_matches(identity):
        return False
    os.kill(identity.pid, sig)
    return True


def _signal_app(app: Path, identity: ProcessIdentity, sig: signal.Signals) -> bool:
    """Signal only the exact captured app generation at its verified path."""
    if not _app_process_matches(app, identity):
        return False
    os.kill(identity.pid, sig)
    return True


def _owned_descendant_groups(
    app_pid: int, recorder_pid: int
) -> tuple[OwnedProcessGroup, ...]:
    """Snapshot smoke-owned descendant groups while their ancestry is provable."""
    initial = _processes()
    initial_pids = _capture_related_pids(initial, app_pid, recorder_pid)
    births_before = {pid: _process_birth(pid) for pid in initial_pids}
    if any(birth is None for birth in births_before.values()):
        raise SmokeFailure("process capture failed: generation unavailable")
    processes = _processes()
    current_pids = _capture_related_pids(processes, app_pid, recorder_pid)
    births_after = {pid: _process_birth(pid) for pid in current_pids}
    if current_pids != initial_pids or births_after != births_before:
        raise SmokeFailure("process capture failed: generation changed during snapshot")
    app_row = processes.get(app_pid)
    grouped: dict[int, list[ProcessIdentity]] = {}
    for pid, row in processes.items():
        if not _descends_from(pid, recorder_pid, processes):
            continue
        if app_row is not None and row.pgid == app_row.pgid:
            continue
        birth = births_after[pid]
        assert birth is not None
        grouped.setdefault(row.pgid, []).append(
            ProcessIdentity(pid, row.pgid, row.start, row.command, birth)
        )
    return tuple(
        OwnedProcessGroup(pgid, tuple(members))
        for pgid, members in sorted(grouped.items())
    )


def _signal_owned_group(group: OwnedProcessGroup, sig: signal.Signals) -> bool:
    """Signal only exact captured members; the PGID grants no extra authority."""
    signalled = False
    for member in group.members:
        signalled = _signal_identity(member, sig) or signalled
    return signalled


def _captured_state(
    identity: ProcessIdentity, matches: Callable[[ProcessIdentity], bool]
) -> str:
    if matches(identity):
        return "live"
    row = _processes().get(identity.pid)
    birth = _process_birth(identity.pid)
    return "gone" if row is None or birth is None else "recycled"


def _wait_captured_not_live(
    identity: ProcessIdentity,
    matches: Callable[[ProcessIdentity], bool],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not matches(identity):
            return True
        time.sleep(0.1)
    return not matches(identity)


def _cleanup_captured_target(
    label: str,
    identity: ProcessIdentity,
    *,
    matches: Callable[[ProcessIdentity], bool],
    send: Callable[[ProcessIdentity, signal.Signals], bool],
    timeout: float,
) -> tuple[CleanupTargetResult, tuple[BaseException, ...]]:
    """Run the complete bounded cleanup sequence for one captured identity."""
    initial = _captured_state(identity, matches)
    term_sent = False
    kill_sent = False
    failures: list[BaseException] = []
    if initial == "live":
        try:
            term_sent = send(identity, signal.SIGTERM)
        except BaseException as exc:
            failures.append(exc)
        if matches(identity):
            _wait_captured_not_live(identity, matches, timeout)
        if matches(identity):
            try:
                kill_sent = send(identity, signal.SIGKILL)
            except BaseException as exc:
                failures.append(exc)
            if matches(identity):
                _wait_captured_not_live(identity, matches, timeout)
    final = _captured_state(identity, matches)
    if final == "live":
        final = "surviving"
        failures.append(SmokeFailure(f"{label} pid {identity.pid} survived SIGKILL"))
    return (
        CleanupTargetResult(label, identity.pid, initial, term_sent, kill_sent, final),
        tuple(failures),
    )


def _cleanup_after_failure(
    app: Path,
    *,
    app_identity: ProcessIdentity | None,
    recorder: RecorderIdentity | None = None,
    status_path: Path | None = None,
    pid_path: Path | None = None,
    groups: tuple[OwnedProcessGroup, ...] = (),
    timeout: float = 5.0,
) -> tuple[CleanupTargetResult, ...]:
    """Boundedly stop only identities proven to belong to this smoke run."""
    # Ancestry is a discovery gate only. Once captured, each identity remains
    # independently eligible for cleanup even if its ancestor exits first.
    failures: list[BaseException] = []
    results: list[CleanupTargetResult] = []

    def cleanup(
        label: str,
        identity: ProcessIdentity,
        *,
        matches: Callable[[ProcessIdentity], bool] = _identity_matches,
        send: Callable[[ProcessIdentity, signal.Signals], bool] = _signal_identity,
    ) -> None:
        result, target_failures = _cleanup_captured_target(
            label,
            identity,
            matches=matches,
            send=send,
            timeout=timeout,
        )
        results.append(result)
        failures.extend(target_failures)

    if recorder is not None:
        try:
            cleanup("recorder", _recorder_process_identity(recorder))
        except BaseException as exc:
            failures.append(exc)

    for group in groups:
        for member in group.members:
            try:
                cleanup(f"group {group.pgid} member", member)
            except BaseException as exc:
                failures.append(exc)

    if app_identity is not None:
        try:
            cleanup(
                "app",
                app_identity,
                matches=lambda identity: _app_process_matches(app, identity),
                send=lambda identity, sig: _signal_app(app, identity, sig),
            )
        except BaseException as exc:
            failures.append(exc)

    if failures:
        details = "; ".join(f"{type(exc).__name__}: {exc}" for exc in failures)
        raise SmokeFailure(f"bounded cleanup encountered errors: {details}")
    return tuple(results)


def _descends_from(pid: int, ancestor: int, processes: dict[int, ProcessRow]) -> bool:
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        row = processes.get(current)
        if row is None:
            return False
        current = row.parent
    return False


def _ancestry_chain(
    pid: int, ancestor: int, processes: dict[int, ProcessRow]
) -> frozenset[int] | None:
    chain: set[int] = set()
    current = pid
    while current > 1 and current not in chain:
        chain.add(current)
        if current == ancestor:
            return frozenset(chain)
        row = processes.get(current)
        if row is None:
            return None
        current = row.parent
    return None


def _capture_related_pids(
    processes: dict[int, ProcessRow], app_pid: int, recorder_pid: int
) -> frozenset[int]:
    ancestry = _ancestry_chain(recorder_pid, app_pid, processes)
    if ancestry is None:
        raise SmokeFailure("process capture failed: recorder ancestry changed")
    descendants = {
        pid for pid in processes if _descends_from(pid, recorder_pid, processes)
    }
    return ancestry | descendants


def _check_transcript_deadline(
    deadline: float | None, clock: Callable[[], float]
) -> None:
    if deadline is not None and clock() >= deadline:
        raise SmokeFailure("transcript proof timed out while scanning JSONL")


def _bounded_jsonl_lines(
    path: Path,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
):
    """Open once, no-follow, then yield JSON objects from one regular-file fd."""
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SmokeFailure("transcript proof failed: unsafe JSONL candidate")
        handle = os.fdopen(fd, "rb")
        fd = -1
        with handle:
            while True:
                _check_transcript_deadline(deadline, clock)
                raw = handle.readline(JSONL_LINE_LIMIT + 2)
                if not raw:
                    return
                content = raw[:-1] if raw.endswith(b"\n") else raw
                if len(content) > JSONL_LINE_LIMIT:
                    raise SmokeFailure(
                        "transcript proof failed: JSONL line limit exceeded"
                    )
                try:
                    yield json.loads(content)
                except (UnicodeDecodeError, ValueError):
                    yield None
    finally:
        if fd >= 0:
            os.close(fd)


def _header_run_id(path: Path) -> str | None:
    try:
        first = next(_bounded_jsonl_lines(path), None)
    except OSError:
        return None
    if not isinstance(first, dict) or first.get("type") != "session_meta":
        return None
    run_id = first.get("run_id")
    if not isinstance(run_id, str) or _TRANSCRIPT_RUN_ID_RE.fullmatch(run_id) is None:
        return None
    return run_id


def _attested_file_has_me(
    path: Path,
    transcript_run_id: str,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Validate header and utterances through the same opened descriptor."""
    lines = _bounded_jsonl_lines(path, deadline=deadline, clock=clock)
    first = next(lines, None)
    if not isinstance(first, dict) or first.get("type") != "session_meta":
        return False
    if first.get("run_id") != transcript_run_id:
        return False
    for entry in lines:
        if (
            isinstance(entry, dict)
            and entry.get("type") == "utterance"
            and entry.get("source") == "me"
            and isinstance(entry.get("text"), str)
            and bool(entry["text"].strip())
        ):
            return True
    return False


def _prelaunch_run_ids(data_dir: Path) -> set[str]:
    """Classify every existing transcript before launch as defence in depth."""
    run_ids: set[str] = set()
    for path in data_dir.rglob("*.jsonl") if data_dir.exists() else ():
        try:
            run_id = _header_run_id(path)
        except (OSError, SmokeFailure) as exc:
            raise SmokeFailure(
                "transcript pre-launch failed: unsafe JSONL candidate"
            ) from exc
        if run_id is None:
            raise SmokeFailure(
                "transcript pre-launch failed: unclassifiable JSONL candidate"
            )
        run_ids.add(run_id)
    return run_ids


def _new_me_utterance(
    data_dir: Path,
    transcript_run_id: str,
    *,
    deadline: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> bool | None:
    """Accept only a canonical Me utterance from the telemetry-attested run."""
    for path in data_dir.rglob("*.jsonl") if data_dir.exists() else ():
        _check_transcript_deadline(deadline, clock)
        try:
            if _attested_file_has_me(
                path, transcript_run_id, deadline=deadline, clock=clock
            ):
                return True
        except OSError:
            continue
    return None


def _graceful_stop_evidence(
    status_path: Path, *, recorder_pid: int, start_epoch: float
) -> bool | None:
    """Accept only this recorder run's canonical graceful stopped record."""
    status = _json(status_path)
    if not status or status.get("running") is not False:
        return None
    try:
        stopped_pid = int(status["pid"])
        stopped_epoch = float(status["start_time"])
    except (KeyError, TypeError, ValueError):
        return None
    if stopped_pid != recorder_pid or stopped_epoch != start_epoch:
        return None
    if (
        status.get("exit_reason") != "graceful"
        or status.get("supervisor_rc") not in (None, 0)
        or status.get("last_error") is not None
    ):
        raise SmokeFailure(
            "graceful app-owned stop failed: matching recorder did not report "
            "a canonical graceful exit"
        )
    return True


def _verify_signed_app(app: Path) -> None:
    if not app.is_dir() or app.is_symlink():
        raise SmokeFailure(f"app is not a regular bundle directory: {app}")
    subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )


def run(args: argparse.Namespace) -> None:
    _validate_timeout_arguments(args)
    if not math.isfinite(args.me_floor) or not 0.0 <= args.me_floor <= 1.0:
        raise SmokeFailure("--me-floor must be finite and inside [0.0, 1.0]")
    app = Path(args.app).expanduser().resolve()
    data_dir = Path(args.data_dir).expanduser().resolve()
    identity_path = Path(args.release_identity).expanduser().resolve()
    receipt_path = (
        Path(
            getattr(
                args,
                "smoke_receipt",
                identity_path.with_name(f"{identity_path.name}.smoke-receipt.json"),
            )
        )
        .expanduser()
        .resolve()
    )
    try:
        _invalidate_receipt(receipt_path)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SmokeFailure("smoke receipt could not be invalidated") from exc
    expected_release = _verify_release_identity(app, identity_path)
    _verify_signed_app(app)
    executable = (app / "Contents/MacOS/Onoats").resolve()
    verified_executable = executable.stat()
    preexisting = _one_app_pid(app)
    if preexisting is not None:
        raise SmokeFailure(
            f"app preflight failed: Onoats is already running (pid {preexisting})"
        )
    prelaunch_run_ids = _prelaunch_run_ids(data_dir)
    status_path = data_dir / ".active/onoats.status.json"
    pid_path = data_dir / ".active/onoats.pid"
    telemetry_path = data_dir / ".active/onoats.ui.json"
    app_pid: int | None = None
    app_identity: ProcessIdentity | None = None
    recorder: RecorderIdentity | None = None
    groups: tuple[OwnedProcessGroup, ...] = ()
    primary_error: BaseException | None = None
    try:
        subprocess.run(["/usr/bin/open", str(app)], check=True, timeout=10)
        print("Open the Onoats panel, press Start, and speak into the microphone.")

        app_pid = wait_for(
            "signed app launch", args.app_timeout, lambda: _one_app_pid(app)
        )
        app_identity = _capture_process_identity(app_pid)
        if app_identity is None:
            raise SmokeFailure("app launch failed: new process vanished")
        _verify_dynamic_release_identity(app_pid, expected_release)
        current_executable = executable.stat()
        if (verified_executable.st_dev, verified_executable.st_ino) != (
            current_executable.st_dev,
            current_executable.st_ino,
        ):
            raise SmokeFailure("app launch failed: verified executable was replaced")
        recorder = wait_for(
            "app-owned recorder identity",
            args.recording_timeout,
            lambda: _recording_identity(status_path, pid_path, app_pid=app_pid),
        )
        recorder_pid, epoch = recorder.pid, recorder.start_epoch
        groups = _owned_descendant_groups(app_pid, recorder_pid)

        def nonzero_me() -> str | None:
            telemetry = _json(telemetry_path)
            if not telemetry:
                return None
            now = time.time()
            if (
                telemetry.get("pid") != recorder_pid
                or telemetry.get("start_epoch") != epoch
            ):
                raise SmokeFailure(
                    "telemetry ownership failed: stale or external session"
                )
            if telemetry.get("schema") != UI_TELEMETRY_SCHEMA_VERSION:
                return None
            transcript_run_id = telemetry.get("transcript_run_id")
            if (
                not isinstance(transcript_run_id, str)
                or _TRANSCRIPT_RUN_ID_RE.fullmatch(transcript_run_id) is None
            ):
                return None
            if transcript_run_id in prelaunch_run_ids:
                raise SmokeFailure(
                    "transcript attestation failed: recorder reused a pre-launch run id"
                )
            audio = telemetry.get("audio")
            me = audio.get("me") if isinstance(audio, dict) else None
            if not isinstance(me, dict):
                return None
            try:
                level = float(me["level"])
                observed_at = float(me["observed_at"])
                updated_at = float(telemetry["updated_at"])
            except (KeyError, TypeError, ValueError):
                return None
            if (
                not all(
                    math.isfinite(value) for value in (level, observed_at, updated_at)
                )
                or not LEVEL_MIN <= level <= LEVEL_MAX
            ):
                return None
            snapshot_age = now - updated_at
            observation_age = now - observed_at
            if (
                not 0.0 <= snapshot_age <= SNAPSHOT_EXPIRY_SECONDS
                or not 0.0 <= observation_age <= AUDIO_OBSERVATION_EXPIRY_SECONDS
                or observed_at > updated_at
            ):
                return None
            return transcript_run_id if level > args.me_floor else None

        transcript_run_id = wait_for(
            "fresh non-zero Me activity", args.activity_timeout, nonzero_me
        )
        print("Non-zero Me activity confirmed. Press Stop in the Onoats panel.")

        wait_for(
            "graceful app-owned stop",
            args.stop_timeout,
            lambda: (
                _graceful_stop_evidence(
                    status_path, recorder_pid=recorder_pid, start_epoch=epoch
                )
                if not pid_path.exists()
                else None
            ),
        )
        transcript_deadline = time.monotonic() + args.transcript_timeout
        wait_for(
            "durable Me utterance",
            args.transcript_timeout,
            lambda: _new_me_utterance(
                data_dir, transcript_run_id, deadline=transcript_deadline
            ),
        )
        _verify_dynamic_release_identity(app_pid, expected_release)
        _verify_release_identity(app, identity_path)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            cleanup_kwargs = {
                "app_identity": app_identity,
                "recorder": recorder,
                "status_path": status_path,
                "pid_path": pid_path,
                "groups": groups,
            }
            _cleanup_after_failure(app, **cleanup_kwargs)
        except BaseException as cleanup_error:
            print(
                f"installed recording smoke cleanup failed: {cleanup_error}",
                file=sys.stderr,
            )
            if primary_error is None:
                raise
    try:
        _publish_receipt(receipt_path, expected_release)
    except (OSError, ValueError, RuntimeError) as exc:
        raise SmokeFailure("successful smoke receipt could not be published") from exc
    print(
        "Installed recording smoke passed: app-owned Me activity and durable utterance."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--release-identity", required=True)
    parser.add_argument("--smoke-receipt", required=True)
    parser.add_argument("--app-timeout", type=_positive_finite_float, default=15.0)
    parser.add_argument(
        "--recording-timeout", type=_positive_finite_float, default=120.0
    )
    parser.add_argument(
        "--activity-timeout", type=_positive_finite_float, default=120.0
    )
    parser.add_argument("--stop-timeout", type=_positive_finite_float, default=120.0)
    parser.add_argument(
        "--transcript-timeout", type=_positive_finite_float, default=30.0
    )
    parser.add_argument("--me-floor", type=float, default=0.01)
    return parser


def main() -> int:
    try:
        run(_parser().parse_args())
    except (SmokeFailure, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(f"installed recording smoke failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
