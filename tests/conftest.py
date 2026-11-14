"""Shared pytest fixtures.

The single-instance lock (`onoats.runtime._instance_lock_fd`) is held for the
recorder's whole process lifetime — in production the kernel releases it on exit,
so there is no teardown release call. But pytest runs every test in ONE process,
so a test that acquires the lock (directly, via `_write_pid_file`, or by driving
the socket supervisor) would otherwise leak the held fd into the next test and,
because acquisition is idempotent (already-held → no-op), silently suppress the
next test's acquire. Release it after every test so each starts from a clean slot.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest


@pytest.fixture(autouse=True)
def _release_instance_lock_after_each():
    yield
    from onoats.runtime import _release_instance_lock

    _release_instance_lock()


@contextmanager
def with_tz(tz: str) -> Iterator[None]:
    """Run the block under process time zone *tz*, then restore the old one.

    Skips the calling test where ``time.tzset`` is missing (it is POSIX-only).
    Test modules import it with ``from conftest import with_tz``.
    """

    if not hasattr(time, "tzset"):
        pytest.skip("tzset is POSIX-only")
    orig = os.environ.get("TZ")
    os.environ["TZ"] = tz
    time.tzset()
    try:
        yield
    finally:
        if orig is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = orig
        time.tzset()
