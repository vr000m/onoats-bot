"""One quarantine contract, two implementations, one state space.

`onoats._vendor.pid._quarantine_verified_pid_file` (vendored; stdlib + loguru
only) and `onoats.control_socket._quarantine_and_remove` (an application-
import-free leaf) cannot share a helper, so this test pins them to the same
observable policy:

* an entry is removed only after it is moved into a private same-directory
  quarantine and the moved inode is proven to be the caller's pinned identity;
* an entry not so proven — identity mismatch (a successor raced in) OR an
  identity that cannot be established — is restored to the public name with a
  no-replace primitive, or retained in quarantine when that name is occupied;
  it is never deleted and never replaces a newer public entry;
* a quarantine directory is never left behind empty.

Dimensions: mkdtemp {ok, fail} x quarantine rename {ok, fail} x moved
identity {match, mismatch, unprovable} x (match: unlink {ok, fail};
otherwise: public name at restore {free, occupied}), for both implementations.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
from pathlib import Path

import pytest

from onoats import _atomic_fs
from onoats import control_socket as control
from onoats._vendor import pid as pid_module

CELLS = [
    "mkdtemp-fail",
    "rename-fail",
    "match",
    "match-unlink-fail",
    "mismatch-restore",
    "mismatch-occupied",
    "unprovable-restore",
    "unprovable-occupied",
]


def _pid_impl(public: Path, descriptor: int, _opened: os.stat_result) -> bool:
    return pid_module._quarantine_verified_pid_file(public, descriptor)


def _control_impl(public: Path, descriptor: int, opened: os.stat_result) -> bool:
    identity = control._EndpointIdentity(opened.st_dev, opened.st_ino, descriptor)
    return control._quarantine_and_remove(public, identity, _atomic_fs.rename_noreplace)


IMPLS = {"pid": _pid_impl, "control": _control_impl}


def _quarantine_dirs(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if p.is_dir() and "-quarantine-" in p.name]


@pytest.mark.parametrize("cell", CELLS)
@pytest.mark.parametrize("impl", sorted(IMPLS))
def test_quarantine_contract_whole_state_space(tmp_path, monkeypatch, impl, cell):
    public = tmp_path / "entry"
    public.write_text("verified", encoding="utf-8")
    descriptor = os.open(public, os.O_RDONLY)
    real_rename = os.rename
    real_fstat = os.fstat
    real_unlink = pathlib.Path.unlink

    def rename(source, destination):
        if Path(source) != public:
            return real_rename(source, destination)
        if cell == "rename-fail":
            raise OSError("rename")
        if cell.startswith("mismatch"):
            raced = tmp_path / "raced.tmp"
            raced.write_text("raced", encoding="utf-8")
            os.replace(raced, public)
        result = real_rename(source, destination)
        if cell.endswith("occupied"):
            public.write_text("newer", encoding="utf-8")
        return result

    def fstat(fd):
        if cell.startswith("unprovable") and fd == descriptor:
            raise OSError("fstat")
        return real_fstat(fd)

    def unlink(self, *args, **kwargs):
        if cell == "match-unlink-fail" and "-quarantine-" in self.parent.name:
            raise OSError("unlink")
        return real_unlink(self, *args, **kwargs)

    def mkdtemp(*_args, **_kwargs):
        raise OSError("mkdtemp")

    try:
        identity_ok = real_fstat(descriptor)
        with monkeypatch.context() as patch:
            patch.setattr(os, "rename", rename)
            patch.setattr(os, "fstat", fstat)
            patch.setattr(pathlib.Path, "unlink", unlink)
            if cell == "mkdtemp-fail":
                patch.setattr(tempfile, "mkdtemp", mkdtemp)
            removed = IMPLS[impl](public, descriptor, identity_ok)
    finally:
        os.close(descriptor)

    quarantine_dirs = _quarantine_dirs(tmp_path)
    assert all(any(d.iterdir()) for d in quarantine_dirs), "empty quarantine left"
    retained = sorted(
        entry.read_text(encoding="utf-8")
        for d in quarantine_dirs
        for entry in d.iterdir()
    )
    public_text = public.read_text(encoding="utf-8") if public.exists() else None

    assert removed is (cell == "match")
    if cell in ("mkdtemp-fail", "rename-fail"):
        assert public_text == "verified"
        assert public.stat().st_ino == identity_ok.st_ino
        assert retained == []
    elif cell == "match":
        assert public_text is None
        assert retained == []
    elif cell == "match-unlink-fail":
        assert public_text is None
        assert retained == ["verified"]
    elif cell == "mismatch-restore":
        assert public_text == "raced"
        # `os.link` restore leaves the quarantine name as an extra link of the
        # same inode; a no-replace rename leaves none. Neither deletes it.
        assert retained in ([], ["raced"])
    elif cell == "unprovable-restore":
        assert public_text == "verified"
        assert public.stat().st_ino == identity_ok.st_ino
        assert retained in ([], ["verified"])
    elif cell == "mismatch-occupied":
        assert public_text == "newer"
        assert retained == ["raced"]
    elif cell == "unprovable-occupied":
        assert public_text == "newer"
        assert retained == ["verified"]
    else:  # pragma: no cover - every cell is enumerated above
        raise AssertionError(cell)
