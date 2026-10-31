#!/usr/bin/env python3
"""Install and roll back one verified Onoats application bundle snapshot."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time


IDENTITY_VERSION = b"onoats-app-identity-v1\0"
RENAME_SWAP = 0x00000002


class InstallError(RuntimeError):
    """A staged install or rollback contract check failed."""


def _field(digest: "hashlib._Hash", value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def app_identity(app: Path) -> str:
    """Hash bundle topology, types, permission modes, bytes, and link targets."""
    if not app.is_dir() or app.is_symlink():
        raise InstallError(f"app bundle is not a real directory: {app}")
    digest = hashlib.sha256(IDENTITY_VERSION)
    entries = [app, *app.rglob("*")]
    entries.sort(
        key=lambda path: os.fsencode("." if path == app else path.relative_to(app))
    )
    for path in entries:
        relative = b"." if path == app else os.fsencode(path.relative_to(app))
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            kind, payload = b"directory", b""
        elif stat.S_ISREG(info.st_mode):
            kind = b"file"
            payload = hashlib.sha256(path.read_bytes()).digest()
        elif stat.S_ISLNK(info.st_mode):
            kind = b"symlink"
            payload = os.fsencode(os.readlink(path))
        else:
            raise InstallError(f"unsupported bundle entry type: {path}")
        for value in (relative, kind, f"{mode:04o}".encode(), payload):
            _field(digest, value)
    return digest.hexdigest()


def _run(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise InstallError(f"command failed ({' '.join(argv)}): {detail}")
    return result


def _designated_requirement(app: Path) -> str:
    result = _run("codesign", "-dr", "-", str(app))
    lines = []
    for line in (result.stdout + result.stderr).splitlines():
        if line.startswith("designated => "):
            lines.append(line.removeprefix("designated => "))
        elif line.startswith("# designated => "):
            lines.append(line.removeprefix("# designated => "))
    if len(lines) != 1:
        raise InstallError(f"expected exactly one designated requirement: {app}")
    return lines[0]


def _cdhash(app: Path) -> str:
    result = _run("codesign", "-dvvv", str(app))
    for line in (result.stdout + result.stderr).splitlines():
        key, separator, value = line.partition("=")
        if separator and key.lower() == "cdhash" and value:
            return value.lower()
    raise InstallError(f"app bundle has no cdhash: {app}")


def _verify_app(
    app: Path,
    *,
    expected_identity: str | None = None,
    makefile_dir: Path | None = None,
) -> tuple[str, str, str]:
    identity = app_identity(app)
    if expected_identity is not None and identity != expected_identity.lower():
        raise InstallError("app differs from the canonical approved identity")
    if makefile_dir is not None:
        _run("make", "-s", "verify-resources", f"APP={app}", cwd=makefile_dir)
    _run("codesign", "--verify", "--deep", "--strict", str(app))
    requirement = _designated_requirement(app)
    return identity, hashlib.sha256(requirement.encode()).hexdigest(), _cdhash(app)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _exchange(left: Path, right: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renamex_np = getattr(libc, "renamex_np", None)
    if renamex_np is None:
        raise InstallError("atomic bundle exchange is unavailable")
    renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    renamex_np.restype = ctypes.c_int
    if renamex_np(os.fsencode(left), os.fsencode(right), RENAME_SWAP) != 0:
        error = ctypes.get_errno()
        raise InstallError(f"atomic bundle exchange failed: {os.strerror(error)}")


def _replace_verified(candidate: Path, installed: Path, verify, commit=None) -> None:
    """Publish candidate and restore the old app unless verification and commit pass."""
    if installed.is_symlink():
        raise InstallError(f"refusing symlink install destination: {installed}")
    if not installed.exists():
        os.rename(candidate, installed)
        try:
            verify(installed)
            if commit is not None:
                commit()
        except Exception:
            os.rename(installed, candidate)
            raise
        return
    _exchange(candidate, installed)
    try:
        verify(installed)
        if commit is not None:
            commit()
    except Exception:
        _exchange(candidate, installed)
        raise
    _remove_path(candidate)


def _copy_snapshot(source: Path, install_dir: Path, app_name: str) -> tuple[Path, Path]:
    parent = Path(tempfile.mkdtemp(prefix=".onoats-install-", dir=install_dir))
    candidate = parent / app_name
    try:
        _run("ditto", str(source), str(candidate))
    except Exception:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    return parent, candidate


def _write_record(record: Path, fields: dict[str, str]) -> None:
    record.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".approved-rollback-", dir=record.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            for key in ("archive", "hash", "tree_hash", "dr_hash", "cdhash"):
                handle.write(f"{key}={fields[key]}\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, record)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _read_record(record: Path, backup_dir: Path) -> dict[str, str]:
    if not record.is_file() or record.is_symlink():
        raise InstallError(f"no approved rollback record in {backup_dir}")
    fields: dict[str, str] = {}
    for line in record.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value
    required = {"archive", "hash", "tree_hash", "dr_hash", "cdhash"}
    if set(fields) != required:
        raise InstallError("approved rollback record is malformed")
    archive = Path(fields["archive"])
    try:
        archive.relative_to(backup_dir)
    except ValueError as exc:
        raise InstallError("invalid recorded archive path") from exc
    if archive.parent != backup_dir or not archive.name.startswith("Onoats-"):
        raise InstallError("invalid recorded archive path")
    return fields


def _archive_baseline(
    installed: Path,
    backup_dir: Path,
    expected_dr_hash: str,
    expected_cdhash: str,
) -> tuple[dict[str, str], Path]:
    tree_hash, dr_hash, cdhash = _verify_app(installed)
    if dr_hash != expected_dr_hash.lower():
        raise InstallError(
            "installed baseline designated requirement differs from approved evidence"
        )
    if cdhash != expected_cdhash.lower():
        raise InstallError("installed baseline cdhash differs from approved evidence")
    backup_dir.mkdir(parents=True, exist_ok=True)
    descriptor, archive_name = tempfile.mkstemp(
        prefix=f"Onoats-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-",
        suffix=".zip",
        dir=backup_dir,
    )
    os.close(descriptor)
    archive = Path(archive_name)
    try:
        _run("ditto", "-c", "-k", "--keepParent", str(installed), str(archive))
        archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="onoats-archive-verify-") as directory:
            _run("ditto", "-x", "-k", str(archive), directory)
            extracted = Path(directory) / installed.name
            extracted_tree, extracted_dr, extracted_cdhash = _verify_app(extracted)
        if (extracted_tree, extracted_dr, extracted_cdhash) != (
            tree_hash,
            dr_hash,
            cdhash,
        ):
            raise InstallError("rollback archive identity mismatch")
    except Exception:
        archive.unlink(missing_ok=True)
        raise
    return {
        "archive": str(archive),
        "hash": archive_hash,
        "tree_hash": tree_hash,
        "dr_hash": dr_hash,
        "cdhash": cdhash,
    }, archive


def install(args: argparse.Namespace) -> None:
    staged = Path(args.app)
    install_dir = Path(args.install_dir)
    installed = install_dir / staged.name
    backup_dir = Path(args.backup_dir)
    record = Path(args.record)
    expected = args.expected_identity.lower()
    if len(expected) != 64 or any(
        character not in "0123456789abcdef" for character in expected
    ):
        raise InstallError(
            "STAGED_APP_TREE_HASH must be a 64-character hexadecimal digest"
        )
    if args.approved not in (0, 1):
        raise InstallError("ROLLBACK_BASELINE_APPROVED must be 0 or 1")

    install_dir.mkdir(parents=True, exist_ok=True)
    parent, candidate = _copy_snapshot(staged, install_dir, staged.name)
    archive: Path | None = None
    try:
        candidate_identity, candidate_dr, _candidate_cdhash = _verify_app(
            candidate, expected_identity=expected, makefile_dir=Path(args.makefile_dir)
        )
        record_fields = None
        if args.approved:
            if not args.baseline_dr_hash or not args.baseline_cdhash:
                raise InstallError(
                    "approved rollback requires ROLLBACK_BASELINE_DR_HASH and "
                    "ROLLBACK_BASELINE_CDHASH"
                )
            record_fields, archive = _archive_baseline(
                installed, backup_dir, args.baseline_dr_hash, args.baseline_cdhash
            )
            if record_fields["dr_hash"] != candidate_dr:
                raise InstallError(
                    "installed baseline designated requirement differs from staged app"
                )

        def verify(path: Path) -> tuple[str, str, str]:
            return _verify_app(path, expected_identity=candidate_identity)

        def commit() -> None:
            nonlocal archive
            if record_fields is not None:
                _write_record(record, record_fields)
                archive = None
            else:
                record.unlink(missing_ok=True)

        _replace_verified(candidate, installed, verify, commit)
        print(f"✓ installed exact staged app at {installed}")
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
        shutil.rmtree(parent, ignore_errors=True)


def rollback(args: argparse.Namespace) -> None:
    install_dir = Path(args.install_dir)
    backup_dir = Path(args.backup_dir)
    fields = _read_record(Path(args.record), backup_dir)
    archive = Path(fields["archive"])
    if not archive.is_file() or archive.is_symlink():
        raise InstallError("approved archive missing")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != fields["hash"]:
        raise InstallError("approved archive hash mismatch")
    install_dir.mkdir(parents=True, exist_ok=True)
    parent = Path(tempfile.mkdtemp(prefix=".onoats-rollback-", dir=install_dir))
    candidate = parent / args.app_name
    try:
        _run("ditto", "-x", "-k", str(archive), str(parent))
        observed = _verify_app(candidate, expected_identity=fields["tree_hash"])
        if observed[1:] != (fields["dr_hash"], fields["cdhash"]):
            raise InstallError("restored app identity mismatch")
        _replace_verified(
            candidate,
            install_dir / args.app_name,
            lambda path: _verify_app(path, expected_identity=fields["tree_hash"]),
        )
        print(f"✓ restored approved app archive {archive}")
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    digest = subparsers.add_parser("identity")
    digest.add_argument("app")

    staged = subparsers.add_parser("install")
    staged.add_argument("--app", required=True)
    staged.add_argument("--install-dir", required=True)
    staged.add_argument("--backup-dir", required=True)
    staged.add_argument("--record", required=True)
    staged.add_argument("--expected-identity", required=True)
    staged.add_argument("--approved", type=int, required=True)
    staged.add_argument("--baseline-dr-hash", default="")
    staged.add_argument("--baseline-cdhash", default="")
    staged.add_argument("--makefile-dir", required=True)

    restore = subparsers.add_parser("rollback")
    restore.add_argument("--app-name", required=True)
    restore.add_argument("--install-dir", required=True)
    restore.add_argument("--backup-dir", required=True)
    restore.add_argument("--record", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "identity":
            print(app_identity(Path(args.app)))
        elif args.command == "install":
            install(args)
        else:
            rollback(args)
    except InstallError as exc:
        print(f"✗ {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
