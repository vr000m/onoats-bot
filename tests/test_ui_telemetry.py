"""Contract tests for ephemeral, display-only recorder telemetry."""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path

import pytest

from onoats.ui_telemetry import (
    AUDIO_OBSERVATION_EXPIRY_SECONDS,
    AUDIO_OBSERVATION_REFRESH_SECONDS,
    DETAIL_LIMIT,
    HEARTBEAT_SECONDS,
    MAX_WRITE_HZ,
    SEMINAR_CATEGORY,
    SNAPSHOT_EXPIRY_SECONDS,
    TRANSCRIPT_LIMIT,
    UI_TELEMETRY_FILENAME,
    UI_TELEMETRY_MODE,
    UI_TELEMETRY_SCHEMA_VERSION,
    UITelemetryPublisher,
    UITelemetryState,
    aggregate_stt_state,
    cleanup_snapshot,
    read_snapshot,
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
    for _ in range(6):
        await asyncio.sleep(0)


def _state(
    clock: FakeClock | None = None,
    monotonic_clock: FakeClock | None = None,
) -> UITelemetryState:
    clock = clock or FakeClock()
    monotonic_clock = monotonic_clock or FakeClock(0.0)
    return UITelemetryState(
        pid=4321,
        start_epoch=900.25,
        locked_category=SEMINAR_CATEGORY,
        clock=clock,
        monotonic_clock=monotonic_clock,
    )


def _path(data_dir: Path) -> Path:
    return data_dir / ".active" / UI_TELEMETRY_FILENAME


def test_contract_constants_are_independent_and_bounded():
    assert UI_TELEMETRY_SCHEMA_VERSION == 1
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
    state = UITelemetryState(4321, 900.25, locked_category=value)

    published = state.snapshot()["locked_category"]

    assert published == value[:DETAIL_LIMIT]
    assert 0 < len(published) <= DETAIL_LIMIT


def test_snapshot_shape_carries_one_owner_epoch_and_consistent_aggregate():
    state = _state()
    state.set_stt_state("me", "ready")
    state.set_stt_state("them", "recovering", "retrying")
    snapshot = state.snapshot(now=1001.0)

    assert snapshot == {
        "schema": 1,
        "pid": 4321,
        "start_epoch": 900.25,
        "updated_at": 1001.0,
        "audio": {"me": None, "them": None},
        "stt": {
            "me": {"state": "ready", "detail": None},
            "them": {"state": "recovering", "detail": "retrying"},
            "aggregate": "recovering",
        },
        "locked_category": "seminars",
    }


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
        real_write_once = publisher.write_once

        def record_write(now=None, *, monotonic_now=None):
            result = real_write_once(now, monotonic_now=monotonic_now)
            if result:
                writes.append((float(now), float(monotonic_now)))
            return result

        publisher.write_once = record_write
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


def test_read_snapshot_rejects_stale_foreign_schema_and_bad_aggregate(tmp_path):
    state = _state()
    publisher = UITelemetryPublisher(tmp_path, state)
    assert publisher.write_once(now=1000.0)
    path = _path(tmp_path)

    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1005.0)
    assert read_snapshot(path, pid=9999, start_epoch=900.25, now=1000.0) is None
    assert read_snapshot(path, pid=4321, start_epoch=900.5, now=1000.0) is None
    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1005.001) is None

    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["schema"] = 2
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1000.0) is None

    raw["schema"] = 1
    raw["stt"]["aggregate"] = "ready"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert read_snapshot(path, pid=4321, start_epoch=900.25, now=1000.0) is None


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
        Path, "unlink", lambda self: (_ for _ in ()).throw(OSError("busy"))
    )
    assert not cleanup_snapshot(tmp_path, pid=4321, start_epoch=900.25)
    assert path.exists()


def test_status_schema_remains_separate():
    from onoats.status import STATUS_SCHEMA_VERSION

    assert STATUS_SCHEMA_VERSION == 2
    assert STATUS_SCHEMA_VERSION != UI_TELEMETRY_SCHEMA_VERSION
