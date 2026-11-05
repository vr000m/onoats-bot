#!/usr/bin/env python3
"""Install and roll back one verified Onoats application bundle snapshot."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlparse
import warnings
from typing import Callable, NamedTuple


IDENTITY_VERSION = b"onoats-app-identity-v1\0"
RENAME_SWAP = 0x00000002
LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
CHMOD = "/bin/chmod"
CODESIGN = "/usr/bin/codesign"
DITTO = "/usr/bin/ditto"
MAKE = "/usr/bin/make"
BUNDLE_ID = "net.varunsingh.onoats"
SCRIPT_DIR = Path(__file__).resolve().parent


class InstallError(RuntimeError):
    """A staged install or rollback contract check failed."""


class ReleaseIdentity(NamedTuple):
    """Immutable identity shared by staged installation and signed-app smoke."""

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


class SourceExecutableEvidence(NamedTuple):
    """Non-portable evidence used only while deriving a portable identity."""

    device: int
    inode: int


def observed_cli_git_identity(
    *,
    checkout: Path | None = None,
    shim: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Return HEAD only after independently binding the installed CLI to it."""
    checkout = (checkout or SCRIPT_DIR.parent).resolve()
    shim = shim or Path.home() / ".local/bin/onoats"
    try:
        root = run(
            ["/usr/bin/git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        head = run(
            ["/usr/bin/git", "-C", str(checkout), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = run(
            ["/usr/bin/git", "-C", str(checkout), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if Path(root).resolve() != checkout or dirty:
            raise InstallError("CLI checkout is not the expected clean worktree")

        if not shim.is_symlink():
            raise InstallError("CLI shim is not the stable ~/.local/bin/onoats link")
        target = shim.resolve(strict=True)
        first_line = target.read_bytes().splitlines()[0]
        if not first_line.startswith(b"#!"):
            raise InstallError("CLI shim target has no interpreter authority")
        interpreter = Path(os.fsdecode(first_line[2:]))
        if not interpreter.is_absolute() or not interpreter.is_file():
            raise InstallError("CLI shim interpreter is unavailable")
        probe = (
            "import importlib.metadata as m,json,onoats,sys;"
            "d=m.distribution('onoats');"
            "print(json.dumps({'python':sys.executable,'module':onoats.__file__,"
            "'direct_url':d.read_text('direct_url.json')}))"
        )
        observed = json.loads(
            run(
                [str(interpreter), "-I", "-c", probe],
                check=True,
                capture_output=True,
                text=True,
                cwd="/",
            ).stdout
        )
        direct_url = json.loads(observed["direct_url"])
        origin = urlparse(direct_url["url"])
        editable_root = Path(unquote(origin.path)).resolve()
        module = Path(observed["module"]).resolve()
        if (
            Path(observed["python"]).resolve() != interpreter.resolve()
            or origin.scheme != "file"
            or direct_url.get("dir_info", {}).get("editable") is not True
            or editable_root != checkout
            or not module.is_relative_to(checkout / "src" / "onoats")
        ):
            raise InstallError("installed CLI is not owned by the verified checkout")
    except InstallError:
        raise
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as exc:
        raise InstallError(
            "CLI git identity could not be independently verified"
        ) from exc
    if len(head) != 40 or any(
        character not in "0123456789abcdef" for character in head
    ):
        raise InstallError("CLI checkout HEAD is not a canonical git identity")
    return head


def _identity_from_private_snapshot(
    app: Path, *, cli_git_identity: str
) -> tuple[ReleaseIdentity, SourceExecutableEvidence]:
    info_path = app / "Contents/Info.plist"
    executable = app / "Contents/MacOS/Onoats"
    try:
        with executable.open("rb") as opened:
            before = os.fstat(opened.fileno())
            executable_hash = hashlib.file_digest(opened, "sha256").hexdigest()
            after = os.fstat(opened.fileno())
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise InstallError("snapshot executable changed during identity derivation")
        with info_path.open("rb") as stream:
            bundle_id = plistlib.load(stream)["CFBundleIdentifier"]
    except (OSError, KeyError, plistlib.InvalidFileException) as exc:
        raise InstallError("staged app has no stable canonical identity") from exc
    if bundle_id != BUNDLE_ID:
        raise InstallError("staged app bundle identifier is not canonical")
    return (
        ReleaseIdentity(
            bundle_id=bundle_id,
            designated_requirement=_designated_requirement(app),
            cdhash=_cdhash(app),
            tree_hash=app_identity(app),
            executable_hash=executable_hash,
            cli_git_identity=cli_git_identity,
        ),
        SourceExecutableEvidence(before.st_dev, before.st_ino),
    )


def release_identity(app: Path, *, cli_git_identity: str) -> ReleaseIdentity:
    """Derive one portable identity from a private bundle snapshot.

    Source inode/device evidence is deliberately not serialized: an installed
    copy must have different inode identity.  It is used only to reject a
    source-path replacement while the private snapshot is being made.
    """
    app = app.resolve()
    executable = app / "Contents/MacOS/Onoats"
    try:
        source_before = executable.stat()
        app_before = app.stat()
    except OSError as exc:
        raise InstallError("staged app identity source is unavailable") from exc
    with tempfile.TemporaryDirectory(prefix="onoats-release-identity-") as directory:
        snapshot = Path(directory) / "snapshot"
        _run(DITTO, str(app), str(snapshot))
        try:
            source_after = executable.stat()
            app_after = app.stat()
        except OSError as exc:
            raise InstallError("staged app changed during identity snapshot") from exc
        if (source_before.st_dev, source_before.st_ino) != (
            source_after.st_dev,
            source_after.st_ino,
        ) or (app_before.st_dev, app_before.st_ino) != (
            app_after.st_dev,
            app_after.st_ino,
        ):
            raise InstallError("staged app path/inode changed during identity snapshot")
        identity, _evidence = _identity_from_private_snapshot(
            snapshot, cli_git_identity=cli_git_identity
        )
        return identity


def _read_release_identity(path: Path) -> ReleaseIdentity:
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


def _normalise_private_tree(app: Path) -> None:
    """Apply the installer's copied-metadata policy to a private snapshot.

    ACLs are not part of the signed bundle identity and are stripped from the
    private copy. Group/other-writable entries and unsupported node types fail
    closed. Extended attributes (including code-signing metadata) are retained.
    """
    entries = [app, *app.rglob("*")]
    for path in entries:
        info = path.lstat()
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            raise InstallError(f"unsupported copied entry type: {path}")
        if stat.S_IMODE(info.st_mode) & (stat.S_IWGRP | stat.S_IWOTH):
            raise InstallError(f"copied entry is writable beyond owner: {path}")
    if os.uname().sysname == "Darwin":
        result = subprocess.run(
            (CHMOD, "-RN", str(app)), capture_output=True, text=True
        )
        if result.returncode:
            raise InstallError(f"could not strip copied ACL metadata: {result.stderr}")


def _run(
    *argv: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise InstallError(f"command failed ({' '.join(argv)}): {detail}")
    return result


def _designated_requirement(app: Path) -> str:
    result = _run(CODESIGN, "-dr", "-", str(app))
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
    result = _run(CODESIGN, "-dvvv", str(app))
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
        env = os.environ.copy()
        env["ONOATS_VERIFY_RESOURCES_APP"] = str(app)
        _run(MAKE, "-s", "verify-resources", cwd=makefile_dir, env=env)
    _run(CODESIGN, "--verify", "--deep", "--strict", str(app))
    requirement = _designated_requirement(app)
    return identity, hashlib.sha256(requirement.encode()).hexdigest(), _cdhash(app)


def _verify_resources(app: Path, makefile_dir: Path | None) -> None:
    if makefile_dir is not None:
        env = os.environ.copy()
        env["ONOATS_VERIFY_RESOURCES_APP"] = str(app)
        _run(MAKE, "-s", "verify-resources", cwd=makefile_dir, env=env)


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
    try:
        _remove_path(candidate)
    except Exception as exc:
        warnings.warn(f"installed app committed; old-tree cleanup failed: {exc}")


def _copy_snapshot(source: Path, install_dir: Path, app_name: str) -> tuple[Path, Path]:
    parent = Path(tempfile.mkdtemp(prefix=".onoats-install-", dir=install_dir))
    # Deliberately not an .app path: an exchanged old tree must not become a
    # second LaunchServices-discoverable bundle while cleanup is pending.
    candidate = parent / "candidate"
    try:
        _run(DITTO, str(source), str(candidate))
        _normalise_private_tree(candidate)
    except Exception:
        shutil.rmtree(parent, ignore_errors=True)
        raise
    return parent, candidate


def _copy_archive_snapshot(archive: Path, parent: Path, expected_hash: str) -> Path:
    """Copy one no-follow regular archive descriptor into a private snapshot."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(archive, flags)
    snapshot = parent / "rollback.zip"
    digest = hashlib.sha256()
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise InstallError("approved archive is not a regular file")
        with snapshot.open("xb") as output:
            os.chmod(snapshot, 0o600)
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                output.write(chunk)
    finally:
        os.close(descriptor)
    if digest.hexdigest() != expected_hash:
        snapshot.unlink(missing_ok=True)
        raise InstallError("approved archive hash mismatch")
    return snapshot


def _launchservices_paths(bundle_id: str) -> set[Path]:
    """Return registered paths for a bundle id when LaunchServices is present."""
    if os.uname().sysname != "Darwin":
        return set()
    if not LSREGISTER.is_file():
        raise InstallError("LaunchServices registration tool is unavailable")
    output = _run(str(LSREGISTER), "-dump").stdout
    paths: set[Path] = set()
    record: dict[str, list[str]] = {}

    def finish_record() -> None:
        identifiers = {
            value.strip().strip('"')
            for key in ("bundleid", "identifier")
            for value in record.get(key, ())
        }
        if bundle_id not in identifiers:
            return
        for key in ("path", "bundlepath"):
            for value in record.get(key, ()):
                raw_path = value.strip().split(" (0x", 1)[0]
                if raw_path:
                    paths.add(Path(raw_path).resolve())

    for raw_line in [*output.splitlines(), ""]:
        line = raw_line.strip()
        if not line or (len(line) >= 8 and set(line) == {"-"}):
            if record:
                finish_record()
                record = {}
            continue
        key, separator, value = line.partition(":")
        if not separator:
            key, separator, value = line.partition("=")
        if not separator:
            continue
        normalised_key = "".join(
            character for character in key.lower() if character.isalnum()
        )
        record.setdefault(normalised_key, []).append(value.strip())
    return paths


def _verify_single_registration(installed: Path, bundle_id: str) -> None:
    registered = _launchservices_paths(bundle_id)
    unexpected = registered - {installed.resolve()}
    if unexpected:
        rendered = ", ".join(str(path) for path in sorted(unexpected, key=str))
        raise InstallError(f"duplicate LaunchServices bundle registration: {rendered}")


def _clear_verified_staged_registration(
    staged: Path,
    installed: Path,
    bundle_id: str,
    expected_identity: str,
    makefile_dir: Path,
) -> None:
    """Remove only the exact approved staged bundle from LaunchServices."""
    staged = staged.resolve()
    installed = installed.resolve()
    if staged == installed:
        raise InstallError("staged app and install destination must be distinct")
    registered = _launchservices_paths(bundle_id)
    if staged not in registered:
        return
    _verify_app(
        staged,
        expected_identity=expected_identity,
        makefile_dir=makefile_dir,
    )
    _run(str(LSREGISTER), "-u", str(staged))
    if staged in _launchservices_paths(bundle_id):
        raise InstallError("staged LaunchServices registration could not be removed")


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
        _run(DITTO, "-c", "-k", "--keepParent", str(installed), str(archive))
        archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory(prefix="onoats-archive-verify-") as directory:
            _run(DITTO, "-x", "-k", str(archive), directory)
            extracted = Path(directory) / installed.name
            _normalise_private_tree(extracted)
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
    if args.bundle_id != BUNDLE_ID:
        raise InstallError(
            "ONOATS_INSTALL_BUNDLE_ID may only redundantly name the canonical bundle"
        )
    staged = Path(args.app)
    release_identity_path = getattr(args, "release_identity", None)
    expected_release = None
    if release_identity_path is not None:
        if not release_identity_path:
            raise InstallError("installation requires an approved release identity")
        expected_release = _read_release_identity(Path(release_identity_path))
        observed_cli_identity = observed_cli_git_identity()
        observed_release = release_identity(
            staged, cli_git_identity=observed_cli_identity
        )
        if observed_release != expected_release:
            raise InstallError(
                "staged app does not match the approved release identity"
            )
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
    if expected_release is not None and expected != expected_release.tree_hash:
        raise InstallError("tree hash and release identity disagree")

    install_dir.mkdir(parents=True, exist_ok=True)
    _clear_verified_staged_registration(
        staged,
        installed,
        args.bundle_id,
        expected,
        Path(args.makefile_dir),
    )
    _verify_single_registration(installed, args.bundle_id)
    parent, candidate = _copy_snapshot(staged, install_dir, staged.name)
    archive: Path | None = None
    committed = False
    try:
        candidate_identity, candidate_dr, _candidate_cdhash = _verify_app(
            candidate, expected_identity=expected
        )
        if expected_release is not None:
            candidate_release = release_identity(
                candidate, cli_git_identity=observed_cli_identity
            )
            if candidate_release != expected_release:
                raise InstallError("installed copy candidate changed release identity")
        _verify_resources(candidate, Path(args.makefile_dir))
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
            verified = _verify_app(
                path,
                expected_identity=candidate_identity,
                makefile_dir=Path(args.makefile_dir),
            )
            if expected_release is not None:
                installed_release = release_identity(
                    path, cli_git_identity=observed_cli_identity
                )
                if installed_release != expected_release:
                    raise InstallError("installed app changed release identity")
            return verified

        def commit() -> None:
            nonlocal archive, committed
            # Installed identity verification has already succeeded inside
            # _replace_verified.  Registration uniqueness and rollback-record
            # publication are part of that same rollback-capable boundary.
            _verify_single_registration(installed, args.bundle_id)
            if record_fields is not None:
                _write_record(record, record_fields)
                archive = None
            else:
                record.unlink(missing_ok=True)
            committed = True

        _replace_verified(candidate, installed, verify, commit)
        print(f"✓ installed exact staged app at {installed}")
    finally:
        if archive is not None:
            try:
                archive.unlink(missing_ok=True)
            except Exception as exc:
                if committed:
                    warnings.warn(
                        f"installed app committed; archive cleanup failed: {exc}"
                    )
                else:
                    raise
        try:
            shutil.rmtree(parent)
        except FileNotFoundError:
            pass
        except Exception as exc:
            if committed:
                warnings.warn(
                    f"installed app committed; temporary cleanup failed: {exc}"
                )
            else:
                raise


def rollback(args: argparse.Namespace) -> None:
    install_dir = Path(args.install_dir)
    backup_dir = Path(args.backup_dir)
    fields = _read_record(Path(args.record), backup_dir)
    archive = Path(fields["archive"])
    install_dir.mkdir(parents=True, exist_ok=True)
    installed = install_dir / args.app_name
    bundle_id = getattr(args, "bundle_id", "net.varunsingh.onoats")
    _verify_single_registration(installed, bundle_id)
    parent = Path(tempfile.mkdtemp(prefix=".onoats-rollback-", dir=install_dir))
    candidate = parent / "candidate"
    committed = False
    try:
        archive = _copy_archive_snapshot(archive, parent, fields["hash"])
        _run(DITTO, "-x", "-k", str(archive), str(parent))
        extracted = parent / args.app_name
        _normalise_private_tree(extracted)
        os.rename(extracted, candidate)
        observed = _verify_app(candidate, expected_identity=fields["tree_hash"])
        if observed[1:] != (fields["dr_hash"], fields["cdhash"]):
            raise InstallError("restored app identity mismatch")

        def commit() -> None:
            nonlocal committed
            _verify_single_registration(installed, bundle_id)
            committed = True

        _replace_verified(
            candidate,
            installed,
            lambda path: _verify_app(
                path,
                expected_identity=fields["tree_hash"],
            ),
            commit,
        )
        print(f"✓ restored approved app archive {archive}")
    finally:
        try:
            shutil.rmtree(parent)
        except FileNotFoundError:
            pass
        except Exception as exc:
            if committed:
                warnings.warn(f"rollback committed; temporary cleanup failed: {exc}")
            else:
                raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    digest = subparsers.add_parser("identity")
    digest.add_argument("app")
    release = subparsers.add_parser("release-identity")
    release.add_argument("app")
    release.add_argument("--cli-git-identity", required=True)
    release.add_argument("--output", required=True)

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
    staged.add_argument("--bundle-id", required=True)
    staged.add_argument("--release-identity", required=True)

    restore = subparsers.add_parser("rollback")
    restore.add_argument("--app-name", required=True)
    restore.add_argument("--install-dir", required=True)
    restore.add_argument("--backup-dir", required=True)
    restore.add_argument("--record", required=True)
    restore.add_argument("--makefile-dir", required=True)
    restore.add_argument("--bundle-id", required=True)
    subparsers.add_parser("env-identity")
    subparsers.add_parser("env-install")
    subparsers.add_parser("env-rollback")
    return parser


def _env_value(name: str, default: str | None = None) -> str:
    value = os.environ.get(name)
    if value is None:
        if default is not None:
            return default
        raise InstallError(f"missing installer environment value: {name}")
    return value


def _env_paths() -> tuple[str, str, str, str]:
    install_dir = _env_value("ONOATS_INSTALL_DIR", str(Path.home() / "Applications"))
    backup_dir = _env_value(
        "ONOATS_INSTALL_BACKUP_DIR",
        str(Path.home() / "Library/Application Support/Onoats/app-backups"),
    )
    record = _env_value(
        "ONOATS_INSTALL_ROLLBACK_RECORD",
        str(Path(backup_dir) / "approved-rollback.txt"),
    )
    app = _env_value("ONOATS_INSTALL_APP", str(SCRIPT_DIR / "Onoats.app"))
    return app, install_dir, backup_dir, record


def _env_install_args() -> argparse.Namespace:
    app, install_dir, backup_dir, record = _env_paths()
    try:
        approved = int(_env_value("ONOATS_INSTALL_ROLLBACK_APPROVED", "0"))
    except ValueError as exc:
        raise InstallError("rollback approval must be an integer") from exc
    return argparse.Namespace(
        app=app,
        install_dir=install_dir,
        backup_dir=backup_dir,
        record=record,
        expected_identity=_env_value("ONOATS_INSTALL_EXPECTED_IDENTITY"),
        approved=approved,
        baseline_dr_hash=_env_value("ONOATS_INSTALL_BASELINE_DR_HASH", ""),
        baseline_cdhash=_env_value("ONOATS_INSTALL_BASELINE_CDHASH", ""),
        makefile_dir=str(SCRIPT_DIR),
        bundle_id=_env_value("ONOATS_INSTALL_BUNDLE_ID", BUNDLE_ID),
        release_identity=_env_value("ONOATS_INSTALL_RELEASE_IDENTITY", ""),
    )


def _env_rollback_args() -> argparse.Namespace:
    app, install_dir, backup_dir, record = _env_paths()
    return argparse.Namespace(
        app_name=Path(app).name,
        install_dir=install_dir,
        backup_dir=backup_dir,
        record=record,
        makefile_dir=str(SCRIPT_DIR),
        bundle_id=_env_value("ONOATS_INSTALL_BUNDLE_ID", BUNDLE_ID),
    )


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "identity":
            print(app_identity(Path(args.app)))
        elif args.command == "release-identity":
            identity = release_identity(
                Path(args.app), cli_git_identity=args.cli_git_identity
            )
            Path(args.output).write_text(identity.to_json() + "\n")
        elif args.command == "env-identity":
            print(
                app_identity(
                    Path(
                        _env_value("ONOATS_INSTALL_APP", str(SCRIPT_DIR / "Onoats.app"))
                    )
                )
            )
        elif args.command == "install":
            install(args)
        elif args.command == "env-install":
            install(_env_install_args())
        elif args.command == "env-rollback":
            rollback(_env_rollback_args())
        else:
            rollback(args)
    except InstallError as exc:
        print(f"✗ {exc}", file=os.sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
