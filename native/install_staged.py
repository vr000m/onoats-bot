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
import warnings

from release_identity import (
    BUNDLE_ID,
    InstallError,
    ReleaseIdentity,
    SmokeReceipt,
    _atomic_write_private,
    _cdhash,
    _designated_requirement,
    app_identity,
    invalidate_smoke_receipt,
    observed_cli_authority,
    read_release_identity as _read_release_identity,
    read_smoke_receipt as _read_smoke_receipt,
    release_identity,
    release_identity_digest,
    require_canonical_bundle,
    run_bounded,
    write_release_identity,
    write_smoke_receipt,
)

__all__ = ("invalidate_smoke_receipt", "write_smoke_receipt")


RENAME_SWAP = 0x00000002
LSREGISTER = Path(
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
CHMOD = "/bin/chmod"
CODESIGN = "/usr/bin/codesign"
DITTO = "/usr/bin/ditto"
MAKE = "/usr/bin/make"
SCRIPT_DIR = Path(__file__).resolve().parent
ROLLBACK_RECORD_MAX_BYTES = 64 * 1024


def _require_smoke_receipt(path: Path, identity: ReleaseIdentity) -> SmokeReceipt:
    receipt = _read_smoke_receipt(path)
    if receipt.release_identity_sha256 != release_identity_digest(identity):
        raise InstallError("smoke receipt does not match the approved release identity")
    return receipt


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
        result = run_bounded((CHMOD, "-RN", str(app)), capture_output=True, text=True)
        if result.returncode:
            raise InstallError(f"could not strip copied ACL metadata: {result.stderr}")


def _run(
    *argv: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = run_bounded(argv, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stdout + result.stderr).strip()
        raise InstallError(f"command failed ({' '.join(argv)}): {detail}")
    return result


def _make_verifier_env(
    app: Path, *, environ: dict[str, str] | None = None
) -> dict[str, str]:
    """Return the complete deny-by-default environment for resource Make."""
    source = os.environ if environ is None else environ
    child = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LC_ALL": "C",
        "ONOATS_VERIFY_RESOURCES_APP": str(app),
    }
    # Preserve an operator-selected temporary filesystem location without
    # forwarding unrelated application credentials or Make control variables.
    if source.get("TMPDIR"):
        child["TMPDIR"] = source["TMPDIR"]
    return child


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
        env = _make_verifier_env(app)
        _run(MAKE, "-s", "verify-resources", cwd=makefile_dir, env=env)
    _run(CODESIGN, "--verify", "--deep", "--strict", str(app))
    requirement = _designated_requirement(app)
    return identity, hashlib.sha256(requirement.encode()).hexdigest(), _cdhash(app)


def _verify_resources(app: Path, makefile_dir: Path | None) -> None:
    if makefile_dir is not None:
        env = _make_verifier_env(app)
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


class SwapBackError(InstallError):
    """The swap-back did not complete and the previous app is retained.

    Raised only when a previous app existed (``SwapState.previous_at_candidate``
    is set, so the caller keeps the temporary parent that holds it).
    """


class WithdrawError(InstallError):
    """A fresh install failed verification and the unverified app is still live.

    There was no previous app, so nothing is retained; the operator must remove
    the published bundle.
    """


class SwapState:
    """Where the previous app's only copy lives while a swap is in flight.

    ``previous_at_candidate`` is the single input to the caller's cleanup: when
    it is set, the candidate's temporary parent holds the sole copy of the
    previous app and must not be deleted.  It is driven by *state*, never by
    which exception escaped, so an interrupt in any phase cannot bypass it.
    """

    def __init__(self) -> None:
        self.previous_at_candidate = False


def _replace_verified(
    candidate: Path,
    installed: Path,
    verify,
    commit=None,
    state: SwapState | None = None,
) -> None:
    """Publish candidate and restore the old app unless verification and commit pass.

    Transaction contract (``state.previous_at_candidate`` is the previous app's
    only-copy flag; ``installed`` is the published path):

    ====================================  ===========  ====================
    phase / outcome                       previous at  flag, then result
    ====================================  ===========  ====================
    before the exchange                   installed    False
    exchange begun (an interrupt here     unknown      True (conservative)
      keeps the flag: retain, never lose)
    exchange refused (InstallError)       installed    False, error raised
    published, verify+commit pass         candidate    False, old tree
                                                       removed (warn-only)
    failure (any BaseException) +         installed    False, original
      swap-back ok                                     exception re-raised
    failure (any BaseException) +         candidate    True, SwapBackError
      swap-back fails or is interrupted
    fresh install, failure + withdraw ok  none         False, re-raised
    fresh install, failure + withdraw     none         False, WithdrawError
      fails                                            (unverified app live)
    ====================================  ===========  ====================

    A swap-back interrupted *after* the exchange completed leaves the flag set
    and the retained message says to inspect both trees: retaining a stale
    unverified tree is recoverable, deleting the previous app is not.
    """
    if state is None:
        state = SwapState()
    if installed.is_symlink():
        raise InstallError(f"refusing symlink install destination: {installed}")
    if not installed.exists():
        os.rename(candidate, installed)
        try:
            verify(installed)
            if commit is not None:
                commit()
        except BaseException as failure:
            try:
                os.rename(installed, candidate)
            except BaseException as restore_error:
                raise WithdrawError(
                    f"unverified app is still published at {installed} and could "
                    f"not be withdrawn ({restore_error}); remove it manually"
                ) from failure
            raise
        return
    state.previous_at_candidate = True
    try:
        _exchange(candidate, installed)
    except InstallError:
        state.previous_at_candidate = False
        raise
    try:
        verify(installed)
        if commit is not None:
            commit()
    except BaseException as failure:
        try:
            _exchange(candidate, installed)
        except BaseException as restore_error:
            raise SwapBackError(
                f"could not confirm the previous app was restored ({restore_error}); "
                f"it is retained at {candidate} unless the swap-back completed, and "
                f"the unverified app may be at {installed} — inspect both trees and "
                "move the retained tree back manually"
            ) from failure
        state.previous_at_candidate = False
        raise
    state.previous_at_candidate = False
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
    """Publish the approval record through the one private atomic writer."""
    payload = "".join(
        f"{key}={fields[key]}\n"
        for key in ("archive", "hash", "tree_hash", "dr_hash", "cdhash")
    )
    _atomic_write_private(record, payload.encode("utf-8"))


def _record_names_archive(record: Path, archive: Path) -> bool:
    """True when the published record may name *archive* (unknown counts as yes).

    The record is the only reference to its baseline archive.  An atomic write
    can fail *after* its rename (the directory fsync), so a failed commit does
    not prove the record is absent; deleting an archive a record still names
    would leave an approval that rollback can never satisfy.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(record, flags)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        # Anything that is not a plain regular file (FIFO, device, symlink
        # target) cannot be proven not to name the archive: retain, never read.
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return True
        raw = os.read(descriptor, ROLLBACK_RECORD_MAX_BYTES + 1)
    except OSError:
        return True
    finally:
        os.close(descriptor)
    return f"archive={archive}\n".encode() in raw


def _read_record(record: Path, backup_dir: Path) -> dict[str, str]:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(record, flags)
    except OSError as exc:
        raise InstallError(f"no approved rollback record in {backup_dir}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size > ROLLBACK_RECORD_MAX_BYTES
        ):
            raise InstallError("approved rollback record is not a safe bounded file")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        current = record.lstat()
    except OSError as exc:
        raise InstallError("approved rollback record could not be validated") from exc
    finally:
        os.close(descriptor)
    if (
        len(raw) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise InstallError("approved rollback record changed while being read")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError("approved rollback record is not UTF-8") from exc
    fields: dict[str, str] = {}
    for line in text.splitlines():
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
    approved_cli_authority = None
    if release_identity_path is not None:
        if not release_identity_path:
            raise InstallError("installation requires an approved release identity")
        expected_release = _read_release_identity(Path(release_identity_path))
        receipt_path = getattr(args, "smoke_receipt", None)
        if not receipt_path:
            raise InstallError("installation requires a successful smoke receipt")
        _require_smoke_receipt(Path(receipt_path), expected_release)
        approved_cli_authority = observed_cli_authority()
        observed_release = release_identity(
            staged, cli_authority=approved_cli_authority
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
    swap = SwapState()
    try:
        candidate_identity, candidate_dr, _candidate_cdhash = _verify_app(
            candidate, expected_identity=expected
        )
        if expected_release is not None:
            assert approved_cli_authority is not None
            candidate_release = release_identity(
                candidate, cli_authority=approved_cli_authority
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
                assert approved_cli_authority is not None
                installed_release = release_identity(
                    path, cli_authority=approved_cli_authority
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

        _replace_verified(candidate, installed, verify, commit, swap)
        print(f"✓ installed exact staged app at {installed}")
    finally:
        # The previous app lives only inside ``parent`` while the flag is set
        # (and in the baseline archive, when approved); never clean up the
        # sole copy, whatever exception got us here.
        retain_parent = swap.previous_at_candidate
        if retain_parent:
            print(
                f"✗ retained previous app tree under {parent}"
                + (f" and baseline archive {archive}" if archive else ""),
                file=os.sys.stderr,
            )
        elif archive is not None and _record_names_archive(record, archive):
            print(
                f"✗ retained baseline archive {archive}: the rollback record names it",
                file=os.sys.stderr,
            )
        elif archive is not None:
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
            if not retain_parent:
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
    bundle_id = getattr(args, "bundle_id", BUNDLE_ID)
    if bundle_id != BUNDLE_ID:
        # Same gate as install(): the registration-uniqueness check is only
        # meaningful for the canonical LaunchServices namespace.
        raise InstallError(
            "ONOATS_INSTALL_BUNDLE_ID may only redundantly name the canonical bundle"
        )
    _verify_single_registration(installed, bundle_id)
    parent = Path(tempfile.mkdtemp(prefix=".onoats-rollback-", dir=install_dir))
    candidate = parent / "candidate"
    committed = False
    swap = SwapState()
    try:
        archive = _copy_archive_snapshot(archive, parent, fields["hash"])
        _run(DITTO, "-x", "-k", str(archive), str(parent))
        extracted = parent / args.app_name
        _normalise_private_tree(extracted)
        os.rename(extracted, candidate)
        # The record's fields are attestations; the archived bundle itself must
        # independently carry the canonical identifier before it is published.
        require_canonical_bundle(candidate)
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
            swap,
        )
        print(f"✓ restored approved app archive {archive}")
    finally:
        retain_parent = swap.previous_at_candidate
        if retain_parent:
            print(f"✗ retained previous app tree under {parent}", file=os.sys.stderr)
        try:
            if not retain_parent:
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
    staged.add_argument("--smoke-receipt", required=True)

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
        smoke_receipt=_env_value("ONOATS_INSTALL_SMOKE_RECEIPT", ""),
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
            write_release_identity(Path(args.output), identity)
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
