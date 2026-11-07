"""Shared immutable release-identity and smoke-receipt contracts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import stat
import subprocess
import tempfile
from typing import Callable, NamedTuple
from urllib.parse import unquote, urlparse
from xml.parsers.expat import ExpatError

BUNDLE_ID = "net.varunsingh.onoats"
CHMOD = "/bin/chmod"
CODESIGN = "/usr/bin/codesign"
DITTO = "/usr/bin/ditto"
SCRIPT_DIR = Path(__file__).resolve().parent
SMOKE_RECEIPT_VERSION = 1
# Every installer/identity child process is bounded.  A hung make, codesign,
# ditto, git or lsregister must fail loud (InstallError) so a published but
# unverified bundle always reaches the rollback path instead of hanging there.
COMMAND_TIMEOUT_SECONDS = 300.0


class InstallError(RuntimeError):
    """A release identity or receipt contract check failed."""


# After the group is killed, draining the pipes gets this long; a descendant
# that left the group (its own setsid) and still holds a pipe is abandoned
# rather than waited for.
KILL_DRAIN_SECONDS = 2.0


def _kill_group(process: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group while its pid is still ours.

    ``returncode`` is only set once the leader has been reaped; until then its
    pid (and therefore the group id) cannot be recycled, so the group signal
    cannot reach an unrelated process.
    """
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()
    try:
        process.communicate(timeout=KILL_DRAIN_SECONDS)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def run_bounded(argv, **kwargs) -> subprocess.CompletedProcess[str]:
    """Run *argv* in its own process group under the shared deadline.

    A timeout, or any exception while waiting (including ``KeyboardInterrupt``),
    kills the child's whole group, so helpers it spawned (``make`` recipes,
    ``git`` subprocesses) cannot outlive the failure.  A timeout is an
    ``InstallError``.  Children are session leaders: a terminal Ctrl+C no longer
    reaches them directly, which is why the interrupt path kills the group.

    Scope of the group kill: it covers every failure while the leader is still
    unreaped (timeout, interrupt, a broken pipe). Once the leader has exited and
    been reaped its pid, and so the pgid, may be recycled, so nothing is
    signalled; helpers a leader deliberately detached from its stdio and left
    behind after a normal exit are outside this contract. Output is therefore
    decoded only after ``communicate`` returns, so a decode error can never be
    the exception that arrives with an unkillable group.
    """
    limit = kwargs.pop("timeout", None)
    limit = COMMAND_TIMEOUT_SECONDS if limit is None else limit
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    # Text decoding must NOT happen inside ``communicate``: with ``text=True`` it
    # can reap the leader and then raise ``UnicodeDecodeError``, an exception
    # that would arrive after the group became unkillable (the leader's pid, and
    # so the pgid, may be recycled once reaped). Communicate in bytes and decode
    # only after the group has been settled.
    text = bool(kwargs.pop("text", False) or kwargs.pop("universal_newlines", False))
    encoding = kwargs.pop("encoding", None)
    errors = kwargs.pop("errors", None)
    decode = text or encoding is not None or errors is not None
    stdin_data = kwargs.pop("input", None)
    if stdin_data is not None:
        # ``communicate(input=...)`` writes nothing unless stdin is a pipe.
        kwargs["stdin"] = subprocess.PIPE
        if isinstance(stdin_data, str):
            stdin_data = stdin_data.encode(encoding or "utf-8", errors or "strict")
    check = kwargs.pop("check", False)
    kwargs["start_new_session"] = True
    with subprocess.Popen(argv, **kwargs) as process:
        try:
            raw_stdout, raw_stderr = process.communicate(stdin_data, timeout=limit)
        except BaseException as failure:
            _kill_group(process)
            if isinstance(failure, subprocess.TimeoutExpired):
                raise InstallError(
                    f"command timed out after {limit:g}s ({' '.join(map(str, argv))})"
                ) from failure
            raise
        returncode = process.returncode

    def decoded(value):
        if value is None or not decode:
            return value
        value = value.decode(encoding or "utf-8", errors or "strict")
        return value.replace("\r\n", "\n").replace("\r", "\n") if text else value

    result = subprocess.CompletedProcess(
        process.args, returncode, decoded(raw_stdout), decoded(raw_stderr)
    )
    if check:
        result.check_returncode()
    return result


class ReleaseIdentity(NamedTuple):
    bundle_id: str
    designated_requirement: str
    cdhash: str
    tree_hash: str
    executable_hash: str
    cli_path: str
    cli_executable_hash: str
    cli_interpreter_path: str
    cli_interpreter_hash: str
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
        for field in (
            "tree_hash",
            "executable_hash",
            "cli_executable_hash",
            "cli_interpreter_hash",
        ):
            digest = getattr(identity, field)
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise InstallError(f"release identity {field} is not sha256")
        if identity.bundle_id != BUNDLE_ID:
            raise InstallError("release identity bundle identifier is not canonical")
        if not identity.designated_requirement or not identity.cdhash:
            raise InstallError("release identity signing fields are empty")
        if not Path(identity.cli_path).is_absolute():
            raise InstallError("release identity CLI path is not absolute")
        if not Path(identity.cli_interpreter_path).is_absolute():
            raise InstallError("release identity CLI interpreter path is not absolute")
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
        raise InstallError("output must be a regular non-symlink file")
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
            raise InstallError("output changed before publication")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def write_release_identity(path: Path, identity: ReleaseIdentity) -> None:
    """Publish the identity privately and atomically; never follow a symlink."""
    _atomic_write_private(path, (identity.to_json() + "\n").encode("utf-8"))


def invalidate_smoke_receipt(path: Path) -> None:
    _atomic_write_private(path, b'{"status":"in-progress"}\n')


def write_smoke_receipt(path: Path, identity: ReleaseIdentity) -> None:
    receipt = SmokeReceipt(SMOKE_RECEIPT_VERSION, release_identity_digest(identity))
    _atomic_write_private(path, (receipt.to_json() + "\n").encode("utf-8"))


def read_smoke_receipt(path: Path) -> SmokeReceipt:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > 64 * 1024:
            raise InstallError("smoke receipt is not a bounded regular file")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        current = path.lstat()
    finally:
        os.close(descriptor)
    if (
        len(raw) != before.st_size
        or (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
        or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
    ):
        raise InstallError("smoke receipt changed while being read")
    try:
        return SmokeReceipt.from_json(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise InstallError("smoke receipt is not UTF-8") from exc


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


def preserve_final_component(path: Path) -> Path:
    """Resolve parent authority without following the caller-controlled leaf."""
    expanded = path.expanduser()
    return expanded.parent.resolve() / expanded.name


_SHEBANG_MAX_BYTES = 4096


def read_interpreter_line(path: Path) -> bytes:
    """First line of a CLI shim target, read without ever blocking or following.

    The target lives in the user's checkout, so it can be swapped for a FIFO
    between resolution and read; a plain ``read_bytes()`` would then hang the
    installer or smoke. Open non-blocking, require a regular file, read a
    bounded prefix, and fail closed otherwise.
    """
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise InstallError(f"CLI shim target is not a regular file: {path}")
        head = os.read(descriptor, _SHEBANG_MAX_BYTES)
    finally:
        os.close(descriptor)
    lines = head.splitlines()
    if not lines:
        raise InstallError(f"CLI shim target is empty: {path}")
    return lines[0]


def _sha256_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise InstallError(f"identity input is not a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
    ):
        raise InstallError(f"identity input changed while hashing: {path}")
    return digest


def observed_cli_authority(
    *,
    checkout: Path | None = None,
    shim: Path | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = run_bounded,
) -> tuple[str, str, str, str, str]:
    """Bind canonical CLI path, entrypoint, interpreter, and clean git HEAD."""
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
        first_line = read_interpreter_line(target)
        if not first_line.startswith(b"#!"):
            raise InstallError("CLI shim target has no interpreter authority")
        interpreter = Path(os.fsdecode(first_line[2:])).resolve(strict=True)
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
            Path(observed["python"]).resolve() != interpreter
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
        subprocess.TimeoutExpired,
    ) as exc:
        raise InstallError(
            "CLI git identity could not be independently verified"
        ) from exc
    if len(head) != 40 or any(
        character not in "0123456789abcdef" for character in head
    ):
        raise InstallError("CLI checkout HEAD is not a canonical git identity")
    return (
        str(shim.absolute()),
        _sha256_file(target),
        str(interpreter),
        _sha256_file(interpreter),
        head,
    )


def observed_cli_git_identity(**kwargs) -> str:
    return observed_cli_authority(**kwargs)[-1]


def verify_cli_authority(identity: ReleaseIdentity, effective_path: Path) -> Path:
    """Verify the app-selected CLI and return its authenticated interpreter."""
    selected = preserve_final_component(effective_path)
    expected = preserve_final_component(Path(identity.cli_path))
    if selected != expected:
        raise InstallError("effective app CLI path is not the approved release CLI")
    if not selected.is_symlink():
        raise InstallError("effective app CLI is not the approved stable symlink")
    try:
        target = selected.resolve(strict=True)
        first_line = read_interpreter_line(target)
        if not first_line.startswith(b"#!"):
            raise InstallError("effective app CLI has no interpreter authority")
        interpreter = Path(os.fsdecode(first_line[2:])).resolve(strict=True)
    except (OSError, IndexError) as exc:
        raise InstallError("effective app CLI authority is unavailable") from exc
    if _sha256_file(target) != identity.cli_executable_hash:
        raise InstallError("effective app CLI executable hash changed")
    if str(interpreter) != identity.cli_interpreter_path:
        raise InstallError("effective app CLI interpreter path changed")
    if _sha256_file(interpreter) != identity.cli_interpreter_hash:
        raise InstallError("effective app CLI interpreter hash changed")
    return interpreter


class SourceExecutableEvidence(NamedTuple):
    device: int
    inode: int


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    result = run_bounded(argv, capture_output=True, text=True)
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


def _field(digest: "hashlib._Hash", value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def app_identity(app: Path) -> str:
    if not app.is_dir() or app.is_symlink():
        raise InstallError(f"app bundle is not a real directory: {app}")
    digest = hashlib.sha256(b"onoats-app-identity-v1\0")
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
            kind, payload = b"file", hashlib.sha256(path.read_bytes()).digest()
        elif stat.S_ISLNK(info.st_mode):
            kind, payload = b"symlink", os.fsencode(os.readlink(path))
        else:
            raise InstallError(f"unsupported bundle entry type: {path}")
        for value in (relative, kind, f"{mode:04o}".encode(), payload):
            _field(digest, value)
    return digest.hexdigest()


def read_bundle_identifier(app: Path) -> str:
    """The app's ``CFBundleIdentifier``; any unreadable plist fails closed."""
    try:
        with (app / "Contents/Info.plist").open("rb") as stream:
            bundle_id = plistlib.load(stream)["CFBundleIdentifier"]
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        ExpatError,
        plistlib.InvalidFileException,
    ) as exc:
        # plistlib surfaces malformed XML as expat's ExpatError, a non-dict
        # root as TypeError, and bad binary/XML values as ValueError; all are
        # "no stable canonical identity", never an escaping traceback.
        raise InstallError("staged app has no stable canonical identity") from exc
    if not isinstance(bundle_id, str):
        raise InstallError("staged app has no stable canonical identity")
    return bundle_id


def require_canonical_bundle(app: Path) -> str:
    """Return the canonical bundle id, or fail unless *app* carries exactly it."""
    bundle_id = read_bundle_identifier(app)
    if bundle_id != BUNDLE_ID:
        raise InstallError("staged app bundle identifier is not canonical")
    return bundle_id


def _identity_from_private_snapshot(
    app: Path, *, cli_authority: tuple[str, str, str, str, str]
) -> tuple[ReleaseIdentity, SourceExecutableEvidence]:
    executable = app / "Contents/MacOS/Onoats"
    try:
        with executable.open("rb") as opened:
            before = os.fstat(opened.fileno())
            executable_hash = hashlib.file_digest(opened, "sha256").hexdigest()
            after = os.fstat(opened.fileno())
    except OSError as exc:
        raise InstallError("staged app has no stable canonical identity") from exc
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise InstallError("snapshot executable changed during identity derivation")
    bundle_id = require_canonical_bundle(app)
    cli_path, cli_hash, interpreter_path, interpreter_hash, git_identity = cli_authority
    return (
        ReleaseIdentity(
            bundle_id=bundle_id,
            designated_requirement=_designated_requirement(app),
            cdhash=_cdhash(app),
            tree_hash=app_identity(app),
            executable_hash=executable_hash,
            cli_path=cli_path,
            cli_executable_hash=cli_hash,
            cli_interpreter_path=interpreter_path,
            cli_interpreter_hash=interpreter_hash,
            cli_git_identity=git_identity,
        ),
        SourceExecutableEvidence(before.st_dev, before.st_ino),
    )


def release_identity(
    app: Path,
    *,
    cli_git_identity: str | None = None,
    cli_authority: tuple[str, str, str, str, str] | None = None,
) -> ReleaseIdentity:
    """Derive one portable identity from a private bundle snapshot."""
    authority = cli_authority or observed_cli_authority()
    if cli_git_identity is not None:
        authority = (*authority[:-1], cli_git_identity)
    app = app.resolve()
    executable = app / "Contents/MacOS/Onoats"
    try:
        source_before, app_before = executable.stat(), app.stat()
    except OSError as exc:
        raise InstallError("staged app identity source is unavailable") from exc
    with tempfile.TemporaryDirectory(prefix="onoats-release-identity-") as directory:
        snapshot = Path(directory) / "snapshot"
        result = run_bounded(
            [DITTO, str(app), str(snapshot)], capture_output=True, text=True
        )
        if result.returncode:
            raise InstallError(
                f"command failed ({DITTO}): {(result.stdout + result.stderr).strip()}"
            )
        try:
            source_after, app_after = executable.stat(), app.stat()
        except OSError as exc:
            raise InstallError("staged app changed during identity snapshot") from exc
        if (source_before.st_dev, source_before.st_ino) != (
            source_after.st_dev,
            source_after.st_ino,
        ) or (
            app_before.st_dev,
            app_before.st_ino,
        ) != (app_after.st_dev, app_after.st_ino):
            raise InstallError("staged app path/inode changed during identity snapshot")
        identity, _evidence = _identity_from_private_snapshot(
            snapshot, cli_authority=authority
        )
        return identity


def observe_and_derive_release_identity(app: Path) -> ReleaseIdentity:
    return release_identity(app, cli_authority=observed_cli_authority())
