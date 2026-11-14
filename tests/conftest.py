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

import pytest


@pytest.fixture(autouse=True)
def _release_instance_lock_after_each():
    yield
    from onoats.runtime import _release_instance_lock

    _release_instance_lock()
