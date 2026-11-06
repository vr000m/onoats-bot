"""Shared immutable release-identity and smoke-receipt contracts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import NamedTuple

BUNDLE_ID = "net.varunsingh.onoats"
SMOKE_RECEIPT_VERSION = 1


class InstallError(RuntimeError):
    """A release identity or receipt contract check failed."""


class ReleaseIdentity(NamedTuple):
    bundle_id: str
    designated_requirement: str
    cdhash: str
    tree_hash: str
    executable_hash: str
    cli_git_identity: str

    def to_json(self) -> str:
        return json.dumps(self._asdict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "ReleaseIdentity":
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InstallError("release identity is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != set(cls._fields):
            raise InstallError("release identity fields do not match v1")
        if not all(isinstance(value[field], str) for field in cls._fields):
            raise InstallError("release identity fields must all be strings")
        identity = cls(**value)
        for field in ("tree_hash", "executable_hash"):
            digest = getattr(identity, field)
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise InstallError(f"release identity {field} is not sha256")
        if identity.bundle_id != BUNDLE_ID:
            raise InstallError("release identity bundle identifier is not canonical")
        if not identity.designated_requirement or not identity.cdhash:
            raise InstallError("release identity signing fields are empty")
        if not identity.cli_git_identity.strip():
            raise InstallError("release identity CLI git identity is empty")
        return identity


class SmokeReceipt(NamedTuple):
    version: int
    release_identity_sha256: str

    def to_json(self) -> str:
        return json.dumps(self._asdict(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "SmokeReceipt":
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise InstallError("smoke receipt is not valid JSON") from exc
        if not isinstance(value, dict) or set(value) != set(cls._fields):
            raise InstallError("smoke receipt fields do not match v1")
        if (
            type(value["version"]) is not int
            or value["version"] != SMOKE_RECEIPT_VERSION
        ):
            raise InstallError("smoke receipt version is unsupported")
        digest = value["release_identity_sha256"]
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise InstallError("smoke receipt identity digest is not sha256")
        return cls(value["version"], digest)


def release_identity_digest(identity: ReleaseIdentity) -> str:
    return hashlib.sha256(identity.to_json().encode("utf-8")).hexdigest()


def _atomic_write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise InstallError("smoke receipt output must be a regular non-symlink file")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise InstallError("smoke receipt output changed before publication")
        os.replace(temporary, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def invalidate_smoke_receipt(path: Path) -> None:
    _atomic_write_private(path, b'{"status":"in-progress"}\n')


def write_smoke_receipt(path: Path, identity: ReleaseIdentity) -> None:
    receipt = SmokeReceipt(SMOKE_RECEIPT_VERSION, release_identity_digest(identity))
    _atomic_write_private(path, (receipt.to_json() + "\n").encode("utf-8"))


def read_smoke_receipt(path: Path) -> SmokeReceipt:
    return SmokeReceipt.from_json(path.read_text(encoding="utf-8"))


def read_release_identity(path: Path) -> ReleaseIdentity:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024:
            raise InstallError("release identity is not a bounded regular file")
        raw = os.read(descriptor, info.st_size + 1)
    finally:
        os.close(descriptor)
    if len(raw) != info.st_size:
        raise InstallError("release identity changed while being read")
    try:
        return ReleaseIdentity.from_json(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise InstallError("release identity is not UTF-8") from exc


def observe_and_derive_release_identity(app: Path) -> ReleaseIdentity:
    """Derive identity using installer-owned bundle inspection, behind one API."""
    from install_staged import observed_cli_git_identity, release_identity

    return release_identity(app, cli_git_identity=observed_cli_git_identity())
