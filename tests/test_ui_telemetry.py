"""Contract tests for ephemeral, display-only recorder telemetry."""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

import onoats._atomic_fs as atomic_fs
from onoats.ui_telemetry import (
    AUDIO_FIELDS,
    AUDIO_OBSERVATION_EXPIRY_SECONDS,
    AUDIO_OBSERVATION_REFRESH_SECONDS,
    DETAIL_LIMIT,
    HEARTBEAT_SECONDS,
    MAX_WRITE_HZ,
    ROOT_OPTIONAL_FIELDS,
    ROOT_REQUIRED_FIELDS,
    SEMINAR_CATEGORY,
    SNAPSHOT_EXPIRY_SECONDS,
    TRANSCRIPT_LIMIT,
    TRANSCRIPT_OPTIONAL_FIELDS,
    TRANSCRIPT_REQUIRED_FIELDS,
    UI_TELEMETRY_FILENAME,
    UI_TELEMETRY_MODE,
    UI_TELEMETRY_SCHEMA_VERSION,
    STT_BRANCH_OPTIONAL_FIELDS,
    STT_BRANCH_REQUIRED_FIELDS,
    STT_FIELDS,
    UITelemetryPublisher,
    UITelemetryState,
    _restore_quarantined,
    aggregate_stt_state,
    cleanup_snapshot,
    read_snapshot,
)

RUN_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _inject_atomic_operations(monkeypatch):
    monkeypatch.setattr(
        "onoats.ui_telemetry._rename_noreplace", atomic_fs.rename_noreplace
    )
    monkeypatch.setattr(
        "onoats.ui_telemetry._rename_noreplace_available",
        atomic_fs.rename_noreplace_available,
    )


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ControlledSleep:
    """Sleep seam advanced explicitly by task-level publisher tests."""

    def __init__(self) -> None:
        self.waiters: list[tuple[float, asyncio.Future[None]]] = []

    async def __call__(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((delay, future))
        await future

    async def next_pending(self) -> tuple[float, asyncio.Future[None]]:
        for _ in range(20):
            while self.waiters and self.waiters[0][1].done():
                self.waiters.pop(0)
            if self.waiters:
                return self.waiters.pop(0)
            await asyncio.sleep(0)
        raise AssertionError("publisher did not schedule a sleep")


async def _settle() -> None:
    await asyncio.sleep(0.02)


def _state(
    clock: FakeClock | None = None,
    monotonic_clock: FakeClock | None = None,
) -> UITelemetryState:
    clock = clock or FakeClock()
    monotonic_clock = monotonic_clock or FakeClock(0.0)
    return UITelemetryState(
        pid=4321,
        start_epoch=900.25,
        transcript_run_id=RUN_ID,
        locked_category=SEMINAR_CATEGORY,
        clock=clock,
        monotonic_clock=monotonic_clock,
    )


def _path(data_dir: Path) -> Path:
    return data_dir / ".active" / UI_TELEMETRY_FILENAME


def test_contract_constants_are_independent_and_bounded():
    assert UI_TELEMETRY_SCHEMA_VERSION == 2
    assert UI_TELEMETRY_FILENAME == "onoats.ui.json"
    assert UI_TELEMETRY_MODE == 0o600
    assert MAX_WRITE_HZ == 4
    assert HEARTBEAT_SECONDS == 2
    assert SNAPSHOT_EXPIRY_SECONDS == 5
    assert AUDIO_OBSERVATION_EXPIRY_SECONDS == 1
    assert AUDIO_OBSERVATION_REFRESH_SECONDS == 0.5
    assert SEMINAR_CATEGORY == "seminars"
    assert TRANSCRIPT_LIMIT == 240
    assert DETAIL_LIMIT == 256


def test_producer_emits_every_parity_checked_exact_field_shape():
    state = _state()
    minimal = state.snapshot()
    assert ROOT_REQUIRED_FIELDS.issubset(minimal)
    assert set(minimal).issubset(ROOT_REQUIRED_FIELDS | ROOT_OPTIONAL_FIELDS)
    assert set(minimal["stt"]) == STT_FIELDS
    for branch in ("me", "them"):
        assert STT_BRANCH_REQUIRED_FIELDS.issubset(minimal["stt"][branch])
        assert set(minimal["stt"][branch]).issubset(
            STT_BRANCH_REQUIRED_FIELDS | STT_BRANCH_OPTIONAL_FIELDS
        )

    state.observe_level("me", 0.5)
    state.set_stt_state("me", "recovering", "retrying")
    state.set_transcript("hello", source="them", speaker_label="Them (S1)")
    complete = state.snapshot()
    assert set(complete["audio"]["me"]) == AUDIO_FIELDS
    assert set(complete["transcript"]) == (
        TRANSCRIPT_REQUIRED_FIELDS | TRANSCRIPT_OPTIONAL_FIELDS
    )
    assert set(complete) == ROOT_REQUIRED_FIELDS | ROOT_OPTIONAL_FIELDS


LEVEL_VERDICTS = json.loads(
    (
        Path(__file__).parent / "fixtures" / "ui_telemetry" / "level_verdicts.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("verdict", LEVEL_VERDICTS, ids=lambda row: row["input"])
def test_level_boundary_verdicts_reject_instead_of_clamp(verdict):
    clock = FakeClock()
    state = _state(clock)
    level = float(verdict["input"])
    accepted = verdict["accepted"]
    published = verdict["published"]

    assert state.observe_level("me", level) is accepted
    branch = state.snapshot()["audio"]["me"]
    if accepted:
        assert branch == {"level": published, "observed_at": clock.now}
        assert math.isfinite(branch["level"])
    else:
        assert branch is None


def test_rejected_level_keeps_last_good_value_and_observation_time():
    clock = FakeClock()
    state = _state(clock)
    assert state.observe_level("them", 0.25)
    good = state.snapshot()["audio"]["them"]

    clock.advance(2)
    assert not state.observe_level("them", float("nan"))
    assert state.snapshot()["audio"]["them"] == good


def test_me_and_them_are_independent_and_heartbeat_cannot_refresh_observations():
    clock = FakeClock()
    state = _state(clock)
    state.observe_level("me", 0.125)
    clock.advance(0.4)
    state.observe_level("them", 0.75)
    first = state.snapshot()

    clock.advance(HEARTBEAT_SECONDS)
    heartbeat = state.snapshot()

    assert heartbeat["updated_at"] > first["updated_at"]
    assert heartbeat["audio"] == first["audio"]
    assert (
        heartbeat["audio"]["me"]["observed_at"]
        != heartbeat["audio"]["them"]["observed_at"]
    )


@pytest.mark.parametrize(
    ("me", "them", "expected"),
    [
        ("ready", "ready", "ready"),
        ("ready", "unknown", "unknown"),
        ("unknown", "starting", "starting"),
        ("starting", "recovering", "recovering"),
        ("recovering", "failed", "failed"),
        ("failed", "ready", "failed"),
    ],
)
def test_stt_aggregate_fixed_precedence(me, them, expected):
    assert aggregate_stt_state(me, them) == expected


def test_stt_details_and_transcript_are_unicode_codepoint_bounded():
    state = _state()
    state.set_stt_state("me", "failed", "d" * (DETAIL_LIMIT + 20))
    state.set_transcript(
        "U0001f600" * (TRANSCRIPT_LIMIT + 20), source="them", speaker_label="S12"
    )
    snapshot = state.snapshot()

    assert len(snapshot["stt"]["me"]["detail"]) == DETAIL_LIMIT
    assert len(snapshot["transcript"]["text"]) == TRANSCRIPT_LIMIT
    assert snapshot["transcript"]["source"] == "them"
    assert snapshot["transcript"]["speaker_label"] == "S12"


@pytest.mark.parametrize("scalar_count", [1, DETAIL_LIMIT, DETAIL_LIMIT + 1, 1024])
def test_locked_category_producer_is_consumer_bounded(scalar_count):
    motif = "e\u0301🙂👩\u200d💻"
    value = (motif * ((scalar_count // len(motif)) + 1))[:scalar_count]
    state = UITelemetryState(
        4321, 900.25, transcript_run_id=RUN_ID, locked_category=value
    )

    published = state.snapshot()["locked_category"]

    assert published == value[:DETAIL_LIMIT]
    assert 0 < len(published) <= DETAIL_LIMIT


def test_snapshot_shape_carries_one_owner_epoch_and_consistent_aggregate():
    state = _state()
    state.set_stt_state("me", "ready")
    state.set_stt_state("them", "recovering", "retrying")
    snapshot = state.snapshot(now=1001.0)

    assert snapshot == {
        "schema": 2,
        "pid": 4321,
        "start_epoch": 900.25,
        "transcript_run_id": RUN_ID,
        "updated_at": 1001.0,
        "audio": {"me": None, "them": None},
        "stt": {
            "me": {"state": "ready", "detail": None},
            "them": {"state": "recovering", "detail": "retrying"},
            "aggregate": "recovering",
        },
        "locked_category": "seminars",
    }


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        "0" * 31,
        "0" * 33,
        "G" * 32,
        "ABCDEF0123456789ABCDEF0123456789",
        "01234567-89ab-cdef-0123-456789abcdef",
    ],
)
def test_transcript_run_id_is_required_and_strictly_lower_hex(run_id):
    with pytest.raises((TypeError, ValueError)):
        UITelemetryState(4321, 900.25, transcript_run_id=run_id)


def test_schema_v2_snapshot_requires_recorder_attested_transcript_run_id(tmp_path):
    path = tmp_path / "snapshot.json"
    snapshot = _state().snapshot()
    snapshot.pop("transcript_run_id")
    path.write_text(json.dumps(snapshot), encoding="utf-8")

    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1000.0) is None


def test_write_once_is_atomic_owner_only_and_strict_json(tmp_path, monkeypatch):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    replacements: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        replacements.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr("onoats.ui_telemetry.os.replace", spy_replace)
    assert publisher.write_once(now=1000.0) is True

    path = _path(tmp_path)
    assert replacements and replacements[-1][1] == path
    assert replacements[-1][0].parent == path.parent
    assert path.stat().st_mode & 0o777 == UI_TELEMETRY_MODE
    assert json.loads(path.read_text(encoding="utf-8"))["start_epoch"] == 900.25
    assert not list(path.parent.glob("*.tmp"))


def test_write_failure_is_warning_only_and_later_write_recovers(tmp_path, monkeypatch):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    real_replace = os.replace
    attempts = 0

    def fail_once(src, dst):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("read-only volume")
        return real_replace(src, dst)

    monkeypatch.setattr("onoats.ui_telemetry.os.replace", fail_once)
    assert publisher.write_once(now=1000.0) is False
    assert publisher.write_once(now=1000.25) is True
    assert _path(tmp_path).exists()


def test_real_publisher_task_coalesces_at_four_hz_and_stops_bounded(tmp_path):
    async def _run() -> None:
        wall = FakeClock()
        monotonic = FakeClock(0.0)
        sleep = ControlledSleep()
        state = _state(wall, monotonic)
        publisher = UITelemetryPublisher(
            tmp_path,
            state,
            clock=wall,
            monotonic_clock=monotonic,
            sleep=sleep,
        )
        writes: list[tuple[float, float]] = []
        real_write_once = publisher._write_snapshot

        def record_write(snapshot):
            result = real_write_once(snapshot)
            if result:
                writes.append((snapshot["updated_at"], monotonic()))
            return result

        publisher._write_snapshot = record_write
        task = publisher.start()
        await _settle()
        assert writes == [(1000.0, 0.0)]

        state.set_stt_state("me", "starting")
        state.set_stt_state("me", "recovering")
        state.set_stt_state("me", "ready")
        await _settle()
        assert len(writes) == 1

        delay, future = await sleep.next_pending()
        assert delay == pytest.approx(1.0 / MAX_WRITE_HZ)
        wall.advance(delay)
        monotonic.advance(delay)
        future.set_result(None)
        await _settle()
        assert writes[-1] == (1000.25, 0.25)
        assert len(writes) == 2

        await publisher.stop(timeout=0.01)
        assert task.done()

    asyncio.run(_run())


def test_real_publisher_task_heartbeats_on_monotonic_time_after_wall_jump(tmp_path):
    async def _run() -> None:
        wall = FakeClock()
        monotonic = FakeClock(0.0)
        sleep = ControlledSleep()
        state = _state(wall, monotonic)
        publisher = UITelemetryPublisher(
            tmp_path,
            state,
            clock=wall,
            monotonic_clock=monotonic,
            sleep=sleep,
        )
        publisher.start()
        await _settle()
        first = json.loads(_path(tmp_path).read_text(encoding="utf-8"))

        delay, future = await sleep.next_pending()
        assert delay == pytest.approx(HEARTBEAT_SECONDS)
        wall.now = 100.0
        monotonic.advance(delay)
        future.set_result(None)
        await _settle()
        heartbeat = json.loads(_path(tmp_path).read_text(encoding="utf-8"))

        assert heartbeat["updated_at"] == 100.0
        assert heartbeat["updated_at"] < first["updated_at"]
        assert heartbeat["audio"] == first["audio"]
        await publisher.stop()

    asyncio.run(_run())


def test_real_publisher_task_refreshes_constant_level_after_500ms_wall_jump(
    tmp_path,
):
    async def _run() -> None:
        wall = FakeClock()
        monotonic = FakeClock(0.0)
        sleep = ControlledSleep()
        state = _state(wall, monotonic)
        assert state.observe_level("me", 0.5)
        publisher = UITelemetryPublisher(
            tmp_path,
            state,
            clock=wall,
            monotonic_clock=monotonic,
            sleep=sleep,
        )
        publisher.start()
        await _settle()
        first = json.loads(_path(tmp_path).read_text(encoding="utf-8"))

        wall.now = 50.0
        monotonic.advance(AUDIO_OBSERVATION_REFRESH_SECONDS - 0.001)
        assert state.observe_level("me", 0.5)
        await _settle()
        unchanged = json.loads(_path(tmp_path).read_text(encoding="utf-8"))
        assert unchanged == first

        monotonic.advance(0.001)
        assert state.observe_level("me", 0.5)
        await _settle()
        refreshed = json.loads(_path(tmp_path).read_text(encoding="utf-8"))
        assert refreshed["audio"]["me"] == {
            "level": 0.5,
            "observed_at": 50.0,
        }
        assert refreshed["updated_at"] == 50.0
        await publisher.stop()

    asyncio.run(_run())


def test_real_publisher_task_recovers_after_write_failure(tmp_path, monkeypatch):
    async def _run() -> None:
        wall = FakeClock()
        monotonic = FakeClock(0.0)
        sleep = ControlledSleep()
        state = _state(wall, monotonic)
        publisher = UITelemetryPublisher(
            tmp_path,
            state,
            clock=wall,
            monotonic_clock=monotonic,
            sleep=sleep,
        )
        real_replace = os.replace
        attempts = 0

        def fail_once(src, dst):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OSError("read-only volume")
            return real_replace(src, dst)

        monkeypatch.setattr("onoats.ui_telemetry.os.replace", fail_once)
        publisher.start()
        await _settle()
        assert attempts == 1
        assert not _path(tmp_path).exists()

        delay, future = await sleep.next_pending()
        assert delay == pytest.approx(1.0 / MAX_WRITE_HZ)
        wall.advance(delay)
        monotonic.advance(delay)
        future.set_result(None)
        await _settle()
        assert attempts == 2
        assert _path(tmp_path).exists()
        await publisher.stop()

    asyncio.run(_run())


def test_read_snapshot_rejects_stale_foreign_schema_and_bad_aggregate(
    tmp_path, monkeypatch
):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    assert publisher.write_once(now=1000.0)
    path = _path(tmp_path)

    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1005.0)
    assert read_snapshot(path, pid=9999, start_epoch=900.25, now=1000.0) is None
    assert read_snapshot(path, pid=4321, start_epoch=900.5, now=1000.0) is None
    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1005.001) is None

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema"] = 1
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1000.0) is None

    raw["schema"] = UI_TELEMETRY_SCHEMA_VERSION
    raw["stt"]["aggregate"] = "ready"
    aggregate_checks = []
    real_aggregate = aggregate_stt_state

    def checked_aggregate(me, them):
        aggregate_checks.append((me, them))
        return real_aggregate(me, them)

    monkeypatch.setattr("onoats.ui_telemetry.aggregate_stt_state", checked_aggregate)
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1000.0) is None
    assert aggregate_checks == [("unknown", "unknown")]


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_reader_rejects_non_finite_json_tokens(tmp_path, token):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    assert publisher.write_once(now=1000.0)
    path = _path(tmp_path)
    text = path.read_text(encoding="utf-8").replace(
        '"level": null', f'"level": {token}'
    )
    # If the shape has no null level scalar, inject a malformed observation.
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["audio"]["me"] = {"level": 0.5, "observed_at": 1000.0}
    text = json.dumps(raw).replace("0.5", token, 1)
    path.write_text(text, encoding="utf-8")
    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1000.0) is None


def test_cleanup_is_owner_checked_and_fail_open(tmp_path, monkeypatch):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    assert publisher.write_once(now=1000.0)
    path = _path(tmp_path)

    assert not cleanup_snapshot(tmp_path, pid=4321, start_epoch=901.0)
    assert path.exists()
    assert cleanup_snapshot(tmp_path, pid=4321, start_epoch=900.25)
    assert not path.exists()

    assert publisher.write_once(now=1001.0)
    monkeypatch.setattr(
        "onoats.ui_telemetry.os.unlink",
        lambda path: (_ for _ in ()).throw(OSError("busy")),
    )
    assert not cleanup_snapshot(tmp_path, pid=4321, start_epoch=900.25)
    assert path.exists()


@pytest.mark.parametrize("entry_kind", ["fifo", "directory", "symlink"])
def test_cleanup_non_regular_entries_is_bounded_and_preserved(tmp_path, entry_kind):
    """Cleanup must never perform a blocking pathname read.

    Run the probe in a child so the regression fails quickly even when an
    implementation accidentally opens a FIFO without ``O_NONBLOCK``.
    """
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    if entry_kind == "fifo":
        os.mkfifo(path)
    elif entry_kind == "directory":
        path.mkdir()
    else:
        target = tmp_path / "foreign.json"
        target.write_text('{"pid":4321,"start_epoch":900.25}', encoding="utf-8")
        path.symlink_to(target)

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from pathlib import Path; "
                "from onoats.ui_telemetry import cleanup_snapshot; "
                f"assert cleanup_snapshot(Path({str(tmp_path)!r}), "
                "pid=4321, start_epoch=900.25) is False"
            ),
        ],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        timeout=2,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    assert path.lexists() if hasattr(path, "lexists") else os.path.lexists(path)


def test_cleanup_uses_bounded_descriptor_read_and_nofollow(tmp_path, monkeypatch):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    assert publisher.write_once(now=1000.0)

    real_open = os.open
    observed_flags: list[int] = []

    def checked_open(path, flags, *args, **kwargs):
        observed_flags.append(flags)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr("onoats.ui_telemetry.os.open", checked_open)
    assert cleanup_snapshot(tmp_path, pid=4321, start_epoch=900.25)
    assert observed_flags
    assert observed_flags[-1] & os.O_NONBLOCK
    assert observed_flags[-1] & os.O_NOFOLLOW


def test_publisher_cleanup_delegates_to_one_cleanup_primitive(tmp_path, monkeypatch):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    calls = []

    def cleanup(
        path, *, pid, start_epoch, rename_noreplace, rename_noreplace_available
    ):
        assert rename_noreplace is atomic_fs.rename_noreplace
        assert rename_noreplace_available is atomic_fs.rename_noreplace_available
        calls.append((Path(path), pid, start_epoch))
        return True

    monkeypatch.setattr("onoats.ui_telemetry._cleanup_owned_snapshot", cleanup)
    assert publisher.cleanup() is True
    assert calls == [(_path(tmp_path), 4321, 900.25)]


def test_cleanup_preserves_same_path_inode_replacement(tmp_path, monkeypatch):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    assert publisher.write_once(now=1000.0)
    path = _path(tmp_path)
    replacement = path.with_suffix(".replacement")
    replacement.write_text(
        json.dumps({"pid": 4321, "start_epoch": 900.25}), encoding="utf-8"
    )
    real_lstat = os.lstat
    swapped = False

    def replace_before_compare(candidate, *args, **kwargs):
        nonlocal swapped
        if Path(candidate) == path and not swapped:
            swapped = True
            os.replace(replacement, path)
        return real_lstat(candidate, *args, **kwargs)

    monkeypatch.setattr("onoats.ui_telemetry.os.lstat", replace_before_compare)
    assert cleanup_snapshot(tmp_path, pid=4321, start_epoch=900.25) is False
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "pid": 4321,
        "start_epoch": 900.25,
    }


def test_restore_quarantined_directory_uses_atomic_noreplace_rename(tmp_path):
    quarantined = tmp_path / "quarantine" / "snapshot"
    quarantined.mkdir(parents=True)
    (quarantined / "sentinel").write_text("foreign", encoding="utf-8")
    public = tmp_path / "onoats.ui.json"
    _restore_quarantined(quarantined, public)
    assert public.is_dir()
    assert (public / "sentinel").read_text(encoding="utf-8") == "foreign"
    assert not quarantined.exists()


def test_linux_renameat2_provides_atomic_noreplace_fallback(monkeypatch):
    # The directory fd for "relative to cwd" is AT_FDCWD, which is -100 on Linux
    # and -2 on Darwin. Pin the platform so the test means the same thing on
    # both CI hosts (it used to compare against the Darwin constant and failed
    # on Linux runners).
    monkeypatch.setattr(atomic_fs.sys, "platform", "linux")
    calls = []

    class RenameAt2:
        argtypes = None
        restype = None

        def __call__(self, *args):
            calls.append(args)
            return 0

    renameat2 = RenameAt2()
    monkeypatch.setattr(
        atomic_fs,
        "_libc_symbol",
        lambda name: renameat2 if name == "renameat2" else None,
    )

    atomic_fs.rename_noreplace(Path("source"), Path("destination"))

    assert calls == [
        (
            atomic_fs._POSIX_AT_FDCWD,
            b"source",
            atomic_fs._POSIX_AT_FDCWD,
            b"destination",
            atomic_fs._RENAME_NOREPLACE,
        )
    ]
    assert atomic_fs._POSIX_AT_FDCWD == -100
    assert atomic_fs.rename_noreplace_available() is True


def test_darwin_renameatx_np_uses_the_darwin_cwd_descriptor(monkeypatch):
    monkeypatch.setattr(atomic_fs.sys, "platform", "darwin")
    calls = []

    class RenameAtxNp:
        argtypes = None
        restype = None

        def __call__(self, *args):
            calls.append(args)
            return 0

    stub = RenameAtxNp()
    monkeypatch.setattr(
        atomic_fs,
        "_libc_symbol",
        lambda name: stub if name == "renameatx_np" else None,
    )
    atomic_fs.rename_noreplace(Path("source"), Path("destination"))
    assert calls[0][0] == calls[0][2] == atomic_fs._DARWIN_AT_FDCWD == -2


def test_linux_renameat2_uses_linux_at_fdcwd(monkeypatch):
    calls = []

    class RenameAt2:
        def __call__(self, *args):
            calls.append(args)
            return 0

    renameat2 = RenameAt2()
    monkeypatch.setattr(atomic_fs.sys, "platform", "linux")
    monkeypatch.setattr(
        atomic_fs,
        "_libc_symbol",
        lambda name: renameat2 if name == "renameat2" else None,
    )
    atomic_fs.rename_noreplace(Path("source"), Path("destination"))
    assert calls[0][0] == calls[0][2] == -100


def test_windows_rename_uses_atomic_platform_noreplace(monkeypatch):
    calls = []
    monkeypatch.setattr(atomic_fs.sys, "platform", "win32")
    monkeypatch.setattr(atomic_fs.os, "rename", lambda *args: calls.append(args))
    atomic_fs.rename_noreplace(Path("source"), Path("destination"))
    assert calls == [(Path("source"), Path("destination"))]


def test_cleanup_fails_open_before_move_without_atomic_noreplace(tmp_path, monkeypatch):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    assert publisher.write_once(now=1000.0)
    path = _path(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(
        "onoats.ui_telemetry._rename_noreplace_available", lambda: False
    )
    assert cleanup_snapshot(tmp_path, pid=4321, start_epoch=900.25) is False
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".onoats-ui-cleanup-*"))


def test_cleanup_publication_at_quarantine_removal_boundary_survives(
    tmp_path, monkeypatch
):
    """A publication racing the final quarantine unlink must remain public."""
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    assert publisher.write_once(now=1000.0)
    path = _path(tmp_path)
    replacement = path.with_suffix(".next")
    replacement_payload = {"pid": 9876, "start_epoch": 1001.5, "generation": 2}
    replacement.write_text(json.dumps(replacement_payload), encoding="utf-8")

    real_unlink = os.unlink
    removed: list[Path] = []
    published = False

    def publish_at_removal_boundary(candidate, *args, **kwargs):
        nonlocal published
        candidate_path = Path(candidate)
        removed.append(candidate_path)
        if candidate_path.name == "snapshot" and not published:
            published = True
            os.replace(replacement, path)
        return real_unlink(candidate, *args, **kwargs)

    monkeypatch.setattr("onoats.ui_telemetry.os.unlink", publish_at_removal_boundary)
    assert cleanup_snapshot(tmp_path, pid=4321, start_epoch=900.25) is True

    assert published, "test did not reach the quarantined-inode removal seam"
    assert path not in removed, "cleanup unlinked the public publication path"
    assert json.loads(path.read_text(encoding="utf-8")) == replacement_payload
    assert not replacement.exists()
    assert not list(path.parent.glob(".onoats-ui-cleanup-*"))


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"{}", b"x" * (64 * 1024 + 1)],
    ids=["malformed", "missing-owner", "oversized"],
)
def test_cleanup_rejects_bad_or_oversized_regular_files_without_unlinking(
    tmp_path, payload
):
    path = _path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_bytes(payload)
    assert cleanup_snapshot(tmp_path, pid=4321, start_epoch=900.25) is False
    assert path.read_bytes() == payload


def test_status_schema_remains_separate():
    from onoats.status import STATUS_SCHEMA_VERSION

    assert STATUS_SCHEMA_VERSION == 2
    assert UI_TELEMETRY_SCHEMA_VERSION == 2
    repo = Path(__file__).parents[1]
    telemetry_source = (repo / "src/onoats/ui_telemetry.py").read_text()
    status_source = (repo / "src/onoats/status.py").read_text()
    assert "STATUS_SCHEMA_VERSION" not in telemetry_source
    assert "UI_TELEMETRY_SCHEMA_VERSION" not in status_source


@pytest.mark.parametrize("operation", ["mkdir", "replace", "cleanup"])
def test_publisher_blocked_filesystem_never_blocks_loop_or_shutdown(
    tmp_path, monkeypatch, operation
):
    import threading
    import time

    async def run():
        state = _state()
        publisher = UITelemetryPublisher(tmp_path, state)
        entered = threading.Event()
        release = threading.Event()
        calls = []
        worker_ids = []
        original = (
            publisher._cleanup_sync
            if operation == "cleanup"
            else Path.mkdir
            if operation == "mkdir"
            else os.replace
        )

        def blocked(*args, **kwargs):
            worker_ids.append(threading.get_ident())
            calls.append(1)
            entered.set()
            release.wait(0.3)  # bounded even when the off-loop mutation fails
            return original(*args, **kwargs)

        if operation == "cleanup":
            monkeypatch.setattr(publisher, "_cleanup_sync", blocked)
        elif operation == "mkdir":
            monkeypatch.setattr(Path, "mkdir", blocked)
        else:
            monkeypatch.setattr("onoats.ui_telemetry.os.replace", blocked)
        main_thread = threading.get_ident()
        publisher.start()
        begin = time.monotonic()
        if operation == "cleanup":
            await _settle()
            publisher.request_stop()
            publisher.cleanup()
        while not entered.is_set():
            await asyncio.sleep(0.001)
        assert time.monotonic() - begin < 0.2
        assert worker_ids == [worker_ids[0]] and worker_ids[0] != main_thread
        for index in range(200):
            state.set_transcript(str(index), "me")
        assert publisher._jobs.qsize() <= 1
        begin = time.monotonic()
        await publisher.stop(timeout=0.01)
        publisher.cleanup()
        assert time.monotonic() - begin < 0.1
        release.set()
        for _ in range(100):
            if not publisher._worker.is_alive():
                break
            await asyncio.sleep(0.005)
        assert not publisher._worker.is_alive()
        assert not _path(tmp_path).exists()  # no late snapshot resurrected
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("source", ["me", "them"])
@pytest.mark.parametrize("producer", ["audio", "stt", "transcript"])
def test_worker_coalesces_inflight_updates_to_latest_snapshot(
    tmp_path, monkeypatch, source, producer
):
    import threading

    async def run():
        state = _state()
        publisher = UITelemetryPublisher(tmp_path, state, clock=lambda: 1000.0)
        entered = threading.Event()
        release = threading.Event()
        snapshots = []
        original = publisher._write_snapshot

        def write(snapshot):
            snapshots.append(snapshot)
            if len(snapshots) == 1:
                entered.set()
                release.wait(1)
            return original(snapshot)

        monkeypatch.setattr(publisher, "_write_snapshot", write)
        publisher.start()
        while not entered.is_set():
            await asyncio.sleep(0.001)
        for index in range(100):
            if producer == "audio":
                state.observe_level(source, index / 100)
            elif producer == "stt":
                state.set_stt_state(source, "ready" if index == 99 else "recovering")
            else:
                state.set_transcript(str(index), source)
        latest = state.snapshot(1000.0)
        assert len(snapshots) == 1
        release.set()
        for _ in range(200):
            if len(snapshots) >= 2:
                break
            await asyncio.sleep(0.005)
        assert len(snapshots) == 2
        assert snapshots[-1] == latest
        assert snapshots[0] != latest
        await publisher.stop()

    asyncio.run(run())


def test_cleanup_before_first_publisher_turn_is_off_loop(tmp_path, monkeypatch):
    import threading
    import time

    async def run():
        publisher = UITelemetryPublisher(tmp_path, _state())
        task = publisher.start()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        threads = []
        release = threading.Event()

        def cleanup():
            threads.append(threading.get_ident())
            release.wait(0.3)
            return False

        monkeypatch.setattr(publisher, "_cleanup_sync", cleanup)
        before = time.monotonic()
        publisher.cleanup()
        assert time.monotonic() - before < 0.1
        while not threads:
            await asyncio.sleep(0.001)
        assert threads[0] != threading.get_ident()
        release.set()

    asyncio.run(run())
