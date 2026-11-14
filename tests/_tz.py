"""Process time-zone override shared by test modules.

A plain module, not conftest: pytest says not to import conftest directly.
``tests`` is on ``pythonpath`` (pyproject), so ``from _tz import with_tz``
resolves in both the default and ``--import-mode=importlib`` modes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest


@contextmanager
def with_tz(tz: str) -> Iterator[None]:
    """Run the block under process time zone *tz*, then restore the old one.

    Skips the calling test where ``time.tzset`` is missing (it is POSIX-only).
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
