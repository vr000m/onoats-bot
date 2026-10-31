#!/usr/bin/env python3
"""Bounded signed-app recording proof without printing transcript contents."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class SmokeFailure(RuntimeError):
    """One stage of the installed recording proof failed."""


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


def _json(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _processes() -> dict[int, tuple[int, str]]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    found: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            found[int(fields[0])] = (int(fields[1]), fields[2])
    return found


def _one_app_pid(app: Path) -> int | None:
    executable = str((app / "Contents/MacOS/Onoats").resolve())
    matches = [
        pid
        for pid, (_parent, command) in _processes().items()
        if command.split(None, 1)[0] == executable
    ]
    if len(matches) > 1:
        raise SmokeFailure("app identity failed: multiple matching Onoats processes")
    return matches[0] if matches else None


def _descends_from(
    pid: int, ancestor: int, processes: dict[int, tuple[int, str]]
) -> bool:
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        if current == ancestor:
            return True
        seen.add(current)
        row = processes.get(current)
        if row is None:
            return False
        current = row[0]
    return False


def _session_snapshot(data_dir: Path) -> dict[Path, tuple[int, int]]:
    snapshots: dict[Path, tuple[int, int]] = {}
    for path in data_dir.rglob("*.jsonl") if data_dir.exists() else ():
        try:
            info = path.stat()
        except OSError:
            continue
        if path.is_file() and not path.is_symlink():
            snapshots[path.resolve()] = (info.st_ino, info.st_size)
    return snapshots


def _new_me_utterance(
    data_dir: Path, before: dict[Path, tuple[int, int]]
) -> bool | None:
    for path in data_dir.rglob("*.jsonl") if data_dir.exists() else ():
        try:
            resolved = path.resolve()
            info = path.stat()
            old = before.get(resolved)
            if old == (info.st_ino, info.st_size):
                continue
            offset = old[1] if old and old[0] == info.st_ino else 0
            with path.open("rb") as handle:
                handle.seek(offset)
                payload = handle.read()
        except OSError:
            continue
        for raw in payload.splitlines():
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if (
                isinstance(entry, dict)
                and entry.get("type") == "utterance"
                and entry.get("source") == "me"
                and isinstance(entry.get("text"), str)
                and bool(entry["text"].strip())
            ):
                return True
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
    app = Path(args.app).expanduser().resolve()
    data_dir = Path(args.data_dir).expanduser().resolve()
    _verify_signed_app(app)
    before = _session_snapshot(data_dir)
    subprocess.run(["/usr/bin/open", str(app)], check=True, timeout=10)
    print("Open the Onoats panel, press Start, and speak into the microphone.")

    app_pid = wait_for("signed app launch", args.app_timeout, lambda: _one_app_pid(app))
    status_path = data_dir / ".active/onoats.status.json"
    pid_path = data_dir / ".active/onoats.pid"
    telemetry_path = data_dir / ".active/onoats.ui.json"

    def recording_identity() -> tuple[int, float] | None:
        status = _json(status_path)
        if not status or status.get("running") is not True:
            return None
        try:
            recorder_pid = int(status["pid"])
            epoch = float(status["start_time"])
        except (KeyError, TypeError, ValueError):
            return None
        processes = _processes()
        if not _descends_from(recorder_pid, app_pid, processes):
            raise SmokeFailure("recorder ancestry failed: recorder is not app-owned")
        try:
            pid_fields = pid_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        if len(pid_fields) < 4 or int(pid_fields[0]) != recorder_pid:
            return None
        if float(pid_fields[3]) != epoch:
            return None
        return recorder_pid, epoch

    recorder_pid, epoch = wait_for(
        "app-owned recorder identity", args.recording_timeout, recording_identity
    )

    def nonzero_me() -> bool | None:
        telemetry = _json(telemetry_path)
        if not telemetry:
            return None
        now = time.time()
        if (
            telemetry.get("pid") != recorder_pid
            or telemetry.get("start_epoch") != epoch
        ):
            raise SmokeFailure("telemetry ownership failed: stale or external session")
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
        if now - updated_at > 5 or now - observed_at > 1:
            return None
        return True if level > args.me_floor else None

    wait_for("fresh non-zero Me activity", args.activity_timeout, nonzero_me)
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
    wait_for(
        "durable Me utterance",
        args.transcript_timeout,
        lambda: _new_me_utterance(data_dir, before),
    )
    print(
        "Installed recording smoke passed: app-owned Me activity and durable utterance."
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--app-timeout", type=float, default=15)
    parser.add_argument("--recording-timeout", type=float, default=120)
    parser.add_argument("--activity-timeout", type=float, default=120)
    parser.add_argument("--stop-timeout", type=float, default=120)
    parser.add_argument("--transcript-timeout", type=float, default=30)
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
