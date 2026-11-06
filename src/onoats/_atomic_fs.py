"""Shared stdlib-only atomic filesystem primitives."""

from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
from typing import Any

_AT_FDCWD = -2
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001


def _libc_symbol(name: str) -> Any | None:
    return getattr(ctypes.CDLL(None, use_errno=True), name, None)


def rename_noreplace_available() -> bool:
    return any(
        _libc_symbol(name) is not None
        for name in ("renameatx_np", "renamex_np", "renameat2")
    )


def rename_noreplace(source: Path, destination: Path) -> None:
    """Atomically rename an entry without overwriting ``destination``."""
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    renameatx_np = _libc_symbol("renameatx_np")
    renamex_np = _libc_symbol("renamex_np") if renameatx_np is None else None
    renameat2 = (
        _libc_symbol("renameat2")
        if renameatx_np is None and renamex_np is None
        else None
    )
    if renameatx_np is not None:
        renameatx_np.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            destination_bytes,
            _RENAME_EXCL,
        )
    elif renamex_np is not None:
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(source_bytes, destination_bytes, _RENAME_EXCL)
    elif renameat2 is not None:
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD,
            source_bytes,
            _AT_FDCWD,
            destination_bytes,
            _RENAME_NOREPLACE,
        )
    else:
        raise NotImplementedError("atomic no-replace rename is unavailable")
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.EEXIST:
        raise FileExistsError(error, os.strerror(error), destination)
    raise OSError(error, os.strerror(error), destination)
