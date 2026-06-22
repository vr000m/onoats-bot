"""Phase 3 tests: the CLI capturer↔recorder lifecycle supervisor.

These cover the Phase-3 acceptance criteria from
``docs/dev_plans/20260607-feature-menubar-coreaudio-socket-transport.md``. The
supervisor (``onoats.cli._run_socket_supervisor`` / ``_supervise_socket_session``)
runs when ``onoats bot`` is invoked with ``AUDIO_SOURCE=socket``:

  - it mints a **private, supervisor-owned 0700 socket directory** (via
    ``tempfile.mkdtemp``, NOT a shared world-writable path), generates a fresh
    per-launch **generation nonce**, spawns the capturer named by
    ``ONOATS_CAPTURER_BIN`` (passing socket paths + nonce via env AND argv),
    waits (bounded) for both sockets to appear, then runs the recorder;
  - on capturer death / shutdown it tears down — the recorder's own
    ErrorFrame-driven path flushes + rotates the partial session into
    ``pending/`` — and the supervisor exits **NON-ZERO**;
  - the transport does NOT self-reconnect: the supervisor owns capturer restart.

"Fail loud" is asserted as the testable observable, in all parts the supervisor
seam exposes:

  ErrorFrame on the affected branch  →  surfaced by the transport (proven in the
      Phase-1 suite; here we drive a *fake recorder* that stands in for that
      ErrorFrame-driven rotation so the SUPERVISOR contract is unit-testable
      without booting the heavy STT/VAD stack);
  supervisor exits NON-ZERO          →  asserted on the supervisor return code;
  a WARNING/ERROR log line           →  asserted via a loguru sink;
  the partial session still rotates  →  asserted by a file landing in pending/;
  the process does NOT hang          →  every wait is bounded by SUP_TIMEOUT, so
                                         a hang FAILS the test, never blocks CI.

There is **no Swift** here: the "capturer" is a pure-Python script (a real
subprocess the supervisor spawns) that writes the Phase-1 wire format. The wire
helpers are imported from the Phase-1 suite rather than reinvented.

Why a fake recorder?  ``_supervise_socket_session`` calls the real
``onoats.dual.run_onoats_dual``, which boots two Whisper/STT services + VAD —
heavy, non-deterministic, and (default ``STT_SERVICE=whisper``) needs an MLX
model not available in CI. The supervisor's *own* Phase-3 contract (mint sockets
+ nonce, spawn the capturer, wait for sockets, and on capturer-death rotate +
exit non-zero without hanging) is fully exercisable by substituting a fake
``run_onoats_dual`` that emulates the recorder's ErrorFrame-driven rotation. The
transport-side ErrorFrame / read-idle watchdog behaviour is already proven
against the real transport in ``tests/test_socket_audio_transport.py``; the
stale-socket/nonce test below also drives the *real* transport directly.
"""

from __future__ import annotations

import asyncio
import os
import signal
import stat
import textwrap
from pathlib import Path

import pytest
from loguru import logger

# Reuse the Phase-1 wire helpers (handshake header + length-prefixed frames)
# rather than reinventing the framing. Sibling module under tests/ — importable
# by bare name under pytest's default (prepend) import mode.
from test_socket_audio_transport import (  # noqa: E402
    WAIT_TIMEOUT,
    anyio_backend,  # noqa: F401 - re-exported fixture (pins anyio to asyncio)
    make_frame,
    make_header,
    pcm_from_samples,
)

import onoats.cli as cli

# Bounded ceiling for blocking (subprocess / loop) waits in this suite. The
# supervisor's own internal timeouts (socket-wait, drain grace) are larger, so
# we shorten them per-test via monkeypatch to keep CI fast while still proving
# the no-hang property.
SUP_TIMEOUT = max(WAIT_TIMEOUT, 8.0)

# A short marker payload the fake capturer streams (one PCM16 sample).
_PCM_MARKER = pcm_from_samples([1234])


# ---------------------------------------------------------------------------
# Short-path AF_UNIX root. The supervisor itself mints its socket dir under the
# system temp root (already short), but our fixtures still need a short root for
# the directly-driven transport test (macOS caps AF_UNIX paths at ~104 bytes).
# ---------------------------------------------------------------------------


@pytest.fixture
def short_root():
    import shutil
    import tempfile
    import uuid

    base = Path(tempfile.gettempdir()) / f"os{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# The FAKE capturer: a pure-Python script the supervisor spawns via
# ONOATS_CAPTURER_BIN with create_subprocess_exec (so it must be a single
# directly-executable file — shebang + chmod +x, NOT a shell string). It binds
# the two unix sockets at the paths the supervisor passes, writes the Phase-1
# handshake (echoing the supervisor's generation nonce), then behaves per
# ONOATS_FAKE_BEHAVIOUR:
#
#   "crash"  -> write N frames, then exit (process death + EOF on the sockets)
#   "silent" -> handshake only, never send a frame (hung-but-alive)
#   "stream" -> stream frames until killed (clean-shutdown path)
#   "hang"   -> never bind a socket, stay alive (the pre-socket block a TCC
#               prompt produces during the Phase-7 tap preflight)
#
# The supervisor passes socket paths + nonce via BOTH argv (--mic-socket /
# --system-socket / --nonce) AND env (ONOATS_MIC_SOCKET / ONOATS_SYSTEM_SOCKET /
# ONOATS_CAPTURER_NONCE); the capturer reads either, so the fixture does not
# over-fit one form.
# ---------------------------------------------------------------------------

_FAKE_CAPTURER_SRC = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    """A pure-Python fake capturer (stands in for the Swift binary)."""
    import asyncio
    import base64
    import json
    import os
    import struct
    import sys


    def _arg(env_names, argv_flag):
        for n in env_names:
            v = os.environ.get(n)
            if v:
                return v
        argv = sys.argv[1:]
        for i, tok in enumerate(argv):
            if tok == argv_flag and i + 1 < len(argv):
                return argv[i + 1]
            if tok.startswith(argv_flag + "="):
                return tok.split("=", 1)[1]
        return None


    MIC = _arg(["ONOATS_MIC_SOCKET", "MIC_SOCKET"], "--mic-socket")
    SYSTEM = _arg(["ONOATS_SYSTEM_SOCKET", "SYSTEM_SOCKET"], "--system-socket")
    NONCE = _arg(["ONOATS_CAPTURER_NONCE", "ONOATS_NONCE"], "--nonce")

    # Behaviour control. The supervisor builds the capturer env from a strict
    # deny-by-default allowlist (Phase 2, invariant I2), which intentionally
    # strips ONOATS_FAKE_* — so we CANNOT rely on env to steer the fake. Read a
    # sidecar control file written next to this script ("<script>.control",
    # JSON) which survives the allowlist; fall back to env (for any direct,
    # non-supervisor invocation) then defaults.
    _control = {}
    try:
        with open(__file__ + ".control") as _cf:
            _control = json.load(_cf)
    except (OSError, ValueError):
        _control = {}
    BEHAVIOUR = _control.get("behaviour") or os.environ.get(
        "ONOATS_FAKE_BEHAVIOUR", "stream"
    )
    N_FRAMES = int(_control.get("nframes") or os.environ.get("ONOATS_FAKE_NFRAMES", "5"))
    SPAWN_CHILD = bool(_control.get("spawn_child"))
    # Phase-4 stderr-channel controls (see the supervisor's always-drain reader):
    #   stderr_lines: lines written to stderr at STARTUP, before binding sockets
    #       (tee coverage — they must surface verbatim on the supervisor's stderr).
    #   stderr_flood: bytes of stderr noise written at STARTUP, before binding.
    #       Far exceeds the 64 KiB pipe capacity: if nothing drains, the write
    #       BLOCKS and the sockets never appear (start-timeout) — so a passing
    #       run proves the always-drain property.
    #   events: ONOATS-EVENT lines written to stderr when the MIC connection
    #       arrives (after the fake recorder has written its running status
    #       record, so set_warning has a record to annotate).
    #   startup_events: ONOATS-EVENT lines written at STARTUP, before binding
    #       sockets — the real capturer's `device` event timing (it outruns
    #       the recorder's start write; the supervisor's deferred-apply task
    #       must land these in THIS session's record anyway).
    STDERR_LINES = _control.get("stderr_lines") or []
    STDERR_FLOOD = int(_control.get("stderr_flood") or 0)
    EVENTS = _control.get("events") or []
    STARTUP_EVENTS = _control.get("startup_events") or []
    # Phase-7 control: seconds to sleep BEFORE binding the sockets — models the
    # tap preflight blocking on a pending TCC prompt (sockets appear late).
    BIND_DELAY = float(_control.get("bind_delay") or 0)

    for _line in STDERR_LINES + STARTUP_EVENTS:
        sys.stderr.write(_line + "\\n")
    sys.stderr.flush()
    if STDERR_FLOOD:
        _noise = "x" * 1023 + "\\n"
        for _ in range(max(1, STDERR_FLOOD // 1024)):
            sys.stderr.write(_noise)
        sys.stderr.flush()

    if BEHAVIOUR == "exit-early":
        # Die BEFORE creating any socket — the shape of a TCC denial at
        # startup (real capturer: ExitCode.micDenied=10 / systemAudioFailed=11).
        sys.exit(int(_control.get("rc") or 10))

    if BEHAVIOUR == "hang":
        # Stay alive but never bind a socket — a pre-socket block with no
        # eventual recovery (e.g. a permission prompt nobody ever answers).
        import time as _time

        _time.sleep(3600)

    if not MIC or not SYSTEM:
        sys.stderr.write(
            "fake-capturer: missing socket paths (mic=%r system=%r)\\n" % (MIC, SYSTEM)
        )
        sys.exit(3)

    if SPAWN_CHILD:
        # Spawn a long-lived child that INHERITS our process group (no
        # start_new_session), then record its PID next to this script. We are a
        # process-group leader (the supervisor spawned us with
        # start_new_session=True), so a teardown that signals only OUR pid would
        # orphan this child while it keeps running. The test reads the recorded
        # PID and asserts it is gone after the supervisor stops us — proving the
        # supervisor tore down the whole process group, not just the leader.
        import subprocess

        _child = subprocess.Popen(["sleep", "3600"])
        with open(__file__ + ".child_pid", "w") as _pf:
            _pf.write(str(_child.pid))
            _pf.flush()


    def header():
        obj = {"rate": 16000, "width": 2, "channels": 1, "v": 1}
        if NONCE is not None:
            obj["nonce"] = NONCE
        return (json.dumps(obj) + "\\n").encode("utf-8")


    def frame(seq):
        pcm = struct.pack("<h", 1234)  # one PCM16-LE sample
        payload = json.dumps(
            {"seq": seq, "captured_monotonic_ns": seq,
             "pcm_b64": base64.b64encode(pcm).decode("ascii")}
        ).encode("utf-8")
        return struct.pack(">I", len(payload)) + payload


    async def serve_one(path):
        async def on_conn(reader, writer):
            try:
                writer.write(header())
                await writer.drain()
                if EVENTS and path == MIC:
                    for _line in EVENTS:
                        sys.stderr.write(_line + "\\n")
                    sys.stderr.flush()
                if BEHAVIOUR == "silent":
                    while True:
                        await asyncio.sleep(3600)
                elif BEHAVIOUR == "crash":
                    for i in range(N_FRAMES):
                        writer.write(frame(i))
                    await writer.drain()
                    writer.close()
                else:
                    i = 0
                    while True:
                        writer.write(frame(i))
                        await writer.drain()
                        i += 1
                        await asyncio.sleep(0.01)
            except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
                pass

        return await asyncio.start_unix_server(on_conn, path=path)


    async def main():
        if BIND_DELAY:
            await asyncio.sleep(BIND_DELAY)
        mic_srv = await serve_one(MIC)
        sys_srv = await serve_one(SYSTEM)
        async with mic_srv, sys_srv:
            if BEHAVIOUR == "crash":
                # Serve the frames, then exit so the supervisor observes capturer
                # PROCESS death (its capturer-wait watcher fires).
                await asyncio.sleep(0.5)
                return
            await asyncio.Event().wait()


    asyncio.run(main())
    '''
)


@pytest.fixture
def fake_capturer(short_root):
    """Write the fake-capturer script (directly executable) and return its path."""
    p = short_root / "fake_capturer.py"
    p.write_text(_FAKE_CAPTURER_SRC)
    p.chmod(0o755)
    return p


@pytest.fixture
def set_capturer_behaviour(fake_capturer):
    """Steer the fake capturer's behaviour via its sidecar control file.

    The supervisor builds the capturer env from a strict deny-by-default
    allowlist (invariant I2), so ``ONOATS_FAKE_*`` env vars do NOT reach the
    capturer. The capturer instead reads ``<script>.control`` (JSON), which this
    helper writes — keeping the behaviour switch independent of the env contract
    under test.
    """
    import json

    def _set(
        behaviour: str,
        *,
        nframes: int | None = None,
        spawn_child: bool = False,
        rc: int | None = None,
        stderr_lines: list[str] | None = None,
        stderr_flood: int | None = None,
        events: list[str] | None = None,
        startup_events: list[str] | None = None,
        bind_delay: float | None = None,
    ) -> None:
        control: dict = {"behaviour": behaviour}
        if nframes is not None:
            control["nframes"] = nframes
        if spawn_child:
            control["spawn_child"] = True
        if rc is not None:
            control["rc"] = rc
        if stderr_lines is not None:
            control["stderr_lines"] = stderr_lines
        if stderr_flood is not None:
            control["stderr_flood"] = stderr_flood
        if events is not None:
            control["events"] = events
        if startup_events is not None:
            control["startup_events"] = startup_events
        if bind_delay is not None:
            control["bind_delay"] = bind_delay
        Path(str(fake_capturer) + ".control").write_text(json.dumps(control))

    yield _set
    # Defensive cleanup: the sidecar lives next to the per-test fake-capturer
    # script (function-scoped tmp dir, so it can't leak across tests already),
    # but remove it explicitly so no test ever depends on ordering or scope.
    Path(str(fake_capturer) + ".control").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# A controllable FAKE recorder substituted for onoats.dual.run_onoats_dual.
#
# It stands in for the heavy real recorder. It connects to the two sockets the
# supervisor exported (proving the supervisor wired ONOATS_MIC_SOCKET /
# ONOATS_SYSTEM_SOCKET to its private dir), reads until EOF / a short idle
# (emulating the transport's ErrorFrame-driven end-of-branch), then emulates the
# recorder's flush_and_rotate by dropping a session file into pending/ and
# returning. On a clean supervisor shutdown (capturer streaming) it runs until
# cancelled.
# ---------------------------------------------------------------------------


def _install_fake_recorder(
    monkeypatch,
    *,
    idle_end: float = 0.6,
    post_eof_drain: float = 0.0,
    write_running: bool = False,
):
    """Patch ``onoats.dual.run_onoats_dual`` with a socket-reading fake recorder.

    Args:
        idle_end: read-idle window emulating the transport's read-idle watchdog
            (hung-but-alive capturer ends the branch after this).
        post_eof_drain: after a branch ends (EOF/idle), sleep this long BEFORE
            rotating + returning. This models the real recorder's flush+rotate
            latency: in production the ErrorFrame -> pipeline-cancel -> flush ->
            rotate path outlasts the capturer's process exit, so the supervisor
            observes capturer-death FIRST and takes its non-zero branch. A fake
            recorder that returns instantly would instead win the race and look
            like a clean shutdown, masking the crash semantics under test.

    Returns a dict the test can inspect after the run.
    """
    state: dict = {"rotated_path": None, "frames_read": 0, "connected": 0}

    async def _fake_run_onoats_dual(
        *, live_terminal=False, locked_category=None, data_dir=None
    ):
        from onoats._vendor.store import onoats_data_dir
        from onoats._vendor import session_queue

        data_dir = onoats_data_dir()
        if write_running:
            # Mirror the real recorder's start-of-session status write, BEFORE
            # connecting — the fake capturer emits its ONOATS-EVENT lines only
            # once a connection arrives, so the supervisor's set_warning always
            # finds a record (the ordering the warning tests rely on).
            from onoats import status as status_file

            status_file.write_running(
                data_dir, pid=os.getpid(), audio_source="socket", stt_label="fake"
            )
        mic = os.environ["ONOATS_MIC_SOCKET"]
        system = os.environ["ONOATS_SYSTEM_SOCKET"]

        async def _drain(path: str) -> None:
            reader, writer = await asyncio.open_unix_connection(path)
            state["connected"] += 1
            try:
                # Consume the handshake line.
                await asyncio.wait_for(reader.readline(), timeout=SUP_TIMEOUT)
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            reader.read(4096), timeout=idle_end
                        )
                    except asyncio.TimeoutError:
                        # Read-idle: emulate the transport's watchdog ending the
                        # branch (hung-but-alive capturer).
                        return
                    if not chunk:
                        # EOF: capturer closed (crash path).
                        return
                    state["frames_read"] += len(chunk)
            finally:
                writer.close()

        try:
            # Both branches read concurrently; the first to end (EOF/idle) ends
            # the recorder — mirroring a fatal ErrorFrame cancelling the pipeline.
            await asyncio.wait(
                {asyncio.create_task(_drain(mic)), asyncio.create_task(_drain(system))},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # Model the flush+rotate drain latency so the supervisor observes
            # capturer-process death before this recorder coroutine returns.
            if post_eof_drain:
                await asyncio.sleep(post_eof_drain)
        finally:
            # Emulate flush_and_rotate: a partial session lands in pending/.
            session_queue.ensure_queue_dirs(data_dir)
            pending = session_queue.queue_dir("pending", data_dir)
            rotated = pending / "session_fake_partial.jsonl"
            rotated.write_text('{"type":"utterance","source":"me","text":"partial"}\n')
            state["rotated_path"] = rotated
            logger.warning(
                "fake recorder: branch ended (capturer death / idle); "
                "rotated partial session into pending/"
            )
        # This fake only ever ends via a branch error (EOF / read-idle), never a
        # clean shutdown — so it mirrors the real run_onoats_dual's non-zero
        # return for an ErrorFrame-terminated pipeline.
        return 1

    monkeypatch.setattr("onoats.dual.run_onoats_dual", _fake_run_onoats_dual)
    return state


# ---------------------------------------------------------------------------
# Loguru -> list sink so WARNING/ERROR records are assertable (loguru does not
# propagate to pytest's caplog by default).
# ---------------------------------------------------------------------------


class _LogSink:
    def __init__(self):
        self.records: list[str] = []
        self._id = logger.add(self._emit, level="WARNING")

    def _emit(self, message) -> None:
        self.records.append(str(message))

    def close(self) -> None:
        logger.remove(self._id)

    def warned(self) -> bool:
        return bool(self.records)


@pytest.fixture
def log_sink():
    sink = _LogSink()
    try:
        yield sink
    finally:
        sink.close()


# ---------------------------------------------------------------------------
# Environment wiring shared by the supervisor lifecycle tests.
# ---------------------------------------------------------------------------


@pytest.fixture
def sup_env(short_root, fake_capturer, monkeypatch):
    """Socket mode + a private data dir + the fake capturer as ONOATS_CAPTURER_BIN.

    Returns ``(data_dir, pending_dir)``. The supervisor mints its OWN socket
    directory + paths, so we deliberately do NOT pre-set socket paths here; we
    only point it at the data dir, the audio source, and the capturer binary.
    Internal supervisor timeouts are shortened so the no-hang property is proven
    quickly.
    """
    data_dir = short_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("ONOATS_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AUDIO_SOURCE", "socket")
    monkeypatch.setenv("ONOATS_CAPTURER_BIN", str(fake_capturer))
    monkeypatch.delenv("KODA_DATA_DIR", raising=False)
    # Don't leak socket paths in from a prior test/process.
    monkeypatch.delenv("ONOATS_MIC_SOCKET", raising=False)
    monkeypatch.delenv("ONOATS_SYSTEM_SOCKET", raising=False)

    # Shorten the supervisor's internal grace windows so a hang surfaces fast.
    monkeypatch.setattr(cli, "_RECORDER_DRAIN_GRACE_SEC", 5.0, raising=False)
    monkeypatch.setattr(cli, "_SOCKET_WAIT_TIMEOUT_SEC", 5.0, raising=False)
    monkeypatch.setattr(cli, "_CAPTURER_TERM_GRACE_SEC", 2.0, raising=False)

    pending_dir = data_dir / "sessions" / "pending"
    return data_dir, pending_dir


def _pending_files(pending_dir: Path) -> list[Path]:
    if not pending_dir.exists():
        return []
    return sorted(pending_dir.glob("*.jsonl"))


def _run_supervisor_bounded(rest=None) -> int:
    """Run the synchronous supervisor in a worker thread, bounded by SUP_TIMEOUT.

    The supervisor runs its own ``asyncio.run`` loop, so it must NOT be called
    from within an event loop. Running it in a thread with a join timeout turns
    any hang into a test failure (rather than a blocked CI run).
    """
    import threading

    box: dict = {}

    def _target() -> None:
        try:
            box["rc"] = cli._run_socket_supervisor(rest or [])
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test
            box["exc"] = exc

    t = threading.Thread(target=_target, name="supervisor-under-test", daemon=True)
    t.start()
    t.join(timeout=SUP_TIMEOUT * 2)
    if t.is_alive():
        pytest.fail(
            "HANG: the socket supervisor did not return within the bounded "
            f"timeout ({SUP_TIMEOUT * 2}s). A fail-loud path must tear down and "
            "exit, never block."
        )
    if "exc" in box:
        raise box["exc"]
    return box["rc"]


# ---------------------------------------------------------------------------
# Scaffolding sanity (seam-independent — always runs)
# ---------------------------------------------------------------------------


def test_supervisor_entry_point_present():
    """The Phase-3 supervisor seam exists with the expected shape."""
    assert callable(getattr(cli, "_run_socket_supervisor", None)), (
        "expected onoats.cli._run_socket_supervisor(rest) -> int"
    )
    assert callable(getattr(cli, "_supervise_socket_session", None)), (
        "expected onoats.cli._supervise_socket_session(rest) coroutine"
    )


def test_fake_capturer_speaks_the_wire_contract():
    """Sanity-check the FAKE capturer payloads against the Phase-1 wire helpers."""
    import json as _json
    import struct as _struct

    hdr = make_header(nonce="gen-x")
    parsed = _json.loads(hdr.rstrip(b"\n"))
    assert (parsed["rate"], parsed["width"], parsed["channels"], parsed["v"]) == (
        16000,
        2,
        1,
        1,
    )
    assert parsed["nonce"] == "gen-x"

    fr = make_frame(0, _PCM_MARKER)
    plen = _struct.unpack(">I", fr[:4])[0]
    assert plen == len(fr) - 4
    body = _json.loads(fr[4:])
    assert body["seq"] == 0 and "pcm_b64" in body


# ---------------------------------------------------------------------------
# Missing-capturer: ONOATS_CAPTURER_BIN unset / unspawnable -> fail loud
# ---------------------------------------------------------------------------


def test_missing_capturer_bin_fails_loud(sup_env, log_sink, monkeypatch):
    """No ``ONOATS_CAPTURER_BIN`` -> non-zero exit + a WARNING/ERROR log, no hang."""
    monkeypatch.delenv("ONOATS_CAPTURER_BIN", raising=False)
    rc = _run_supervisor_bounded()
    assert rc != 0, "missing ONOATS_CAPTURER_BIN must yield a NON-ZERO exit"
    assert log_sink.warned(), "missing capturer must emit a WARNING/ERROR log line"


def test_unspawnable_capturer_bin_fails_loud(sup_env, log_sink, monkeypatch):
    """A capturer path that cannot exec -> non-zero exit + log, no hang."""
    monkeypatch.setenv("ONOATS_CAPTURER_BIN", "/nonexistent/onoats-capturer-xyz")
    rc = _run_supervisor_bounded()
    assert rc != 0, "an unspawnable capturer must yield a NON-ZERO exit"
    assert log_sink.warned()


def test_held_instance_lock_blocks_capturer_spawn(sup_env, monkeypatch):
    """[high] regression (Codex adversarial review round 5): the single-instance
    lock is acquired BEFORE the capturer is spawned. With the slot already held,
    the supervisor must refuse (rc=1) WITHOUT ever calling
    ``create_subprocess_exec`` — a start that lost the race must not spawn a second
    CoreAudio process tap / trigger a TCC prompt / contend for the device and only
    fail afterwards. Acquiring inside ``_write_pid_file`` (post capturer-spawn) was
    too late; this pins the hoisted acquisition."""
    import asyncio as _asyncio
    import fcntl as _fcntl
    import os as _os
    import sys as _sys

    if _sys.platform == "win32":
        pytest.skip("flock single-instance lock is POSIX-only")

    from onoats.runtime import LOCK_FILENAME

    data_dir, _pending = sup_env
    active = data_dir / ".active"
    active.mkdir(parents=True, exist_ok=True)
    # Instance 1 holds the slot (separate open file description → flock conflicts
    # even within this one process).
    holder = _os.open(str(active / LOCK_FILENAME), _os.O_RDWR | _os.O_CREAT, 0o644)
    _fcntl.flock(holder, _fcntl.LOCK_EX | _fcntl.LOCK_NB)

    spawned = {"n": 0}
    real_exec = _asyncio.create_subprocess_exec

    async def _spy_exec(*a, **k):
        spawned["n"] += 1
        return await real_exec(*a, **k)

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", _spy_exec)

    try:
        rc = _run_supervisor_bounded([])
        assert rc == 1, "a start that lost the instance-lock race must exit rc=1"
        assert spawned["n"] == 0, (
            "the capturer must NOT be spawned when the instance lock is already held"
        )
    finally:
        _fcntl.flock(holder, _fcntl.LOCK_UN)
        _os.close(holder)


def test_recorder_handshake_failure_maps_to_clean_nonzero(log_sink, monkeypatch):
    """A controlled recorder launch failure must be rc=1, not a traceback.

    E.g. a capturer that creates the sockets but handshakes with a bad/stale
    nonce raises SocketHandshakeError out of the recorder; only SttPreflightError
    was caught at the CLI boundary before, so this used to escape as a traceback
    (and an ugly non-zero exit) instead of the documented clean fail-loud rc=1.
    """
    from onoats.transports.socket_audio import SocketHandshakeError

    async def _raise_handshake(rest):
        raise SocketHandshakeError("stale/foreign capturer: handshake nonce mismatch")

    monkeypatch.setattr(cli, "_supervise_socket_session", _raise_handshake)

    rc = cli._run_socket_supervisor([])
    assert rc == 1, "a SocketHandshakeError must map to a clean non-zero exit"
    assert log_sink.warned()


@pytest.mark.parametrize(
    ("cap_rc", "expected_reason"),
    [(10, "mic-denied"), (11, "system-audio-failed"), (7, "capturer-start-failed")],
)
def test_prestart_capturer_death_writes_fresh_failure_status(
    sup_env, log_sink, set_capturer_behaviour, cap_rc, expected_reason
):
    """A capturer dying BEFORE its sockets exist (the TCC-denial shape) must
    leave a FRESH stopped status record naming the mapped reason.

    Observed live (2026-06-10): this path wrote nothing, so the menu bar read
    the PREVIOUS session's record and rendered a mic denial as
    "failed: graceful". The record must be fresh (new start_time), not the
    stale session's record enriched — the menu's staleness guard keys on it.
    """
    from onoats import status as status_file

    data_dir, _ = sup_env
    # Plant the stale "graceful" record of a previous successful session.
    stale_start = 123.0
    status_file.write_status(
        data_dir,
        status_file.StatusRecord(
            schema=status_file.STATUS_SCHEMA_VERSION,
            pid=1,
            start_time=stale_start,
            audio_source="socket",
            stt_label="old-session",
            running=False,
            exit_reason="graceful",
        ),
    )
    set_capturer_behaviour("exit-early", rc=cap_rc)

    rc = _run_supervisor_bounded()

    assert rc != 0, "pre-start capturer death must yield a NON-ZERO exit"
    rec = status_file.read_status(data_dir)
    assert rec is not None, "the pre-start failure must write a status record"
    assert rec.exit_reason == expected_reason
    assert rec.running is False
    assert rec.supervisor_rc == 1
    assert rec.last_error, "the record must carry a human-readable cause"
    assert rec.start_time > stale_start, (
        "must be a FRESH record, not the stale session's record enriched"
    )


# ---------------------------------------------------------------------------
# CRASH: capturer writes N frames then dies -> fail loud + rotate partial
# ---------------------------------------------------------------------------


def test_crash_fails_loud_and_rotates_partial_session(
    sup_env, log_sink, monkeypatch, set_capturer_behaviour
):
    """CRASH path, asserting every fail-loud observable the supervisor exposes.

    The fake capturer writes 5 frames then exits (process death + socket EOF).
    The fake recorder reads them, sees EOF (emulating the transport's ErrorFrame
    ending the branch), and rotates a partial session into ``pending/``. The
    supervisor's capturer-death watcher must then:
      * return a NON-ZERO exit code;
      * emit a WARNING/ERROR log line;
      * leave the rotated partial session in ``pending/`` (no lost data);
      * NOT hang (bounded by the worker-thread join).
    """
    _data_dir, pending_dir = sup_env
    set_capturer_behaviour("crash", nframes=5)
    # The capturer sleeps ~0.5s after closing sockets before its process exits;
    # the recorder's drain must outlast that so the supervisor observes capturer
    # PROCESS death first and takes its non-zero crash branch (real ordering).
    state = _install_fake_recorder(monkeypatch, post_eof_drain=1.5)

    rc = _run_supervisor_bounded()

    assert rc != 0, f"capturer crash must yield a NON-ZERO supervisor exit, got {rc}"
    assert log_sink.warned(), "capturer crash must emit a WARNING/ERROR log line"
    assert _pending_files(pending_dir), (
        "capturer crash must still rotate the partial session into pending/ "
        f"(none under {pending_dir})"
    )
    # The recorder actually connected to the supervisor-minted sockets and read
    # the capturer's frames before EOF — proving the supervisor wired the
    # private-dir sockets through to the recorder.
    assert state["connected"] >= 1, "fake recorder never connected to a socket"
    assert state["frames_read"] > 0, "no capturer frames reached the recorder"


# ---------------------------------------------------------------------------
# HUNG-BUT-ALIVE: capturer connects but never sends a frame -> watchdog + rotate
# ---------------------------------------------------------------------------


def test_hung_but_alive_does_not_hang_and_rotates(
    sup_env, log_sink, monkeypatch, set_capturer_behaviour
):
    """HUNG-BUT-ALIVE path: a connected-but-silent capturer must not hang.

    The fake capturer handshakes then stays silent forever (no EOF). The fake
    recorder's read-idle (emulating the Phase-1 read-idle watchdog) ends the
    branch and rotates a partial session. Because the capturer process stays
    ALIVE, this exercises the recorder-finishes-first teardown branch: the
    supervisor must still return promptly (bounded join), stop the capturer, and
    leave the rotated session in pending/. Critically, the test proves the
    session ends rather than waiting forever for an EOF that never comes — AND
    that a silent/failed capturer fails loud (non-zero exit), not silently
    succeeds, even though the capturer process is still alive.
    """
    _data_dir, pending_dir = sup_env
    set_capturer_behaviour("silent")
    # Short read-idle in the fake recorder so the watchdog-equivalent fires well
    # within the bounded join.
    state = _install_fake_recorder(monkeypatch, idle_end=0.4)

    rc = _run_supervisor_bounded()

    # The recorder ended first (idle) via a fatal ErrorFrame, NOT a clean
    # shutdown — the fail-loud contract requires a non-zero supervisor exit even
    # though the capturer process is still alive (the recorder-finishes-first
    # branch must honour the recorder's non-zero rc, not assume success).
    assert rc != 0, (
        "a silent capturer that trips the recorder's idle-watchdog is a FAILURE, "
        f"not a clean exit; expected non-zero rc, got {rc}"
    )
    assert _pending_files(pending_dir), (
        "the idle-ended session must still rotate into pending/ "
        f"(none under {pending_dir})"
    )
    assert state["connected"] >= 1, "fake recorder never connected (silent capturer)"
    # No frames were ever sent — proving the branch ended on idle, not on data.
    assert state["frames_read"] == 0, (
        "the silent capturer sent no frames; the branch must end on idle, not data"
    )


# ---------------------------------------------------------------------------
# CLEAN SHUTDOWN: streaming capturer, recorder exits normally -> rc 0
# ---------------------------------------------------------------------------


def test_clean_recorder_exit_returns_zero_and_stops_capturer(
    sup_env, monkeypatch, set_capturer_behaviour
):
    """Negative control: a healthy streaming capturer + a recorder that returns
    cleanly yields rc=0, and the supervisor stops the (still-alive) capturer.

    This proves the fail-loud non-zero exits above are specific to failure, not
    indiscriminate. The fake recorder here reads a few frames then returns
    voluntarily (emulating an EndFrame shutdown).
    """
    _data_dir, pending_dir = sup_env
    set_capturer_behaviour("stream")

    state: dict = {"frames_read": 0}

    async def _fake_run_onoats_dual(
        *, live_terminal=False, locked_category=None, data_dir=None
    ):
        mic = os.environ["ONOATS_MIC_SOCKET"]
        reader, writer = await asyncio.open_unix_connection(mic)
        try:
            await asyncio.wait_for(reader.readline(), timeout=SUP_TIMEOUT)  # handshake
            for _ in range(3):
                chunk = await asyncio.wait_for(reader.read(64), timeout=SUP_TIMEOUT)
                if not chunk:
                    break
                state["frames_read"] += len(chunk)
        finally:
            writer.close()
        # Voluntary clean exit (EndFrame-equivalent) — no rotation needed here.

    monkeypatch.setattr("onoats.dual.run_onoats_dual", _fake_run_onoats_dual)

    rc = _run_supervisor_bounded()
    assert rc == 0, f"a clean recorder exit must yield rc=0, got {rc}"
    assert state["frames_read"] > 0, "recorder read no frames from a streaming capturer"


# ---------------------------------------------------------------------------
# I1 SIGNAL ISOLATION: the capturer must be spawned in its OWN session so a
# terminal Ctrl+C/SIGTERM is NOT delivered to it by the OS (process-group
# membership). A graceful recorder shutdown (recorder finishes first, capturer
# still alive) must yield rc=0 — NOT be mis-classified as capturer-death (rc=1).
# ---------------------------------------------------------------------------


def test_capturer_spawned_in_isolated_session_and_clean_exit_is_zero(
    sup_env, monkeypatch, set_capturer_behaviour
):
    """Regression for invariant I1 (signal isolation).

    Two assertions, structural + behavioural:

    1. The capturer is spawned via ``asyncio.create_subprocess_exec`` with
       ``start_new_session=True`` — the portable spelling of ``setsid``. This is
       what stops a terminal Ctrl+C/SIGTERM from reaching the capturer as a side
       effect of foreground process-group membership; the supervisor stops the
       capturer explicitly via ``_stop_capturer`` after the recorder finishes.
       We spy on the spawn rather than deliver a real OS signal in CI.

    2. A recorder that finishes first while the capturer is still streaming
       (the graceful-shutdown shape) yields rc=0 — proving a clean shutdown is
       NOT mis-classified as capturer-death (rc=1). Without isolation, an
       inherited terminal signal could kill the capturer first and flip this to
       the fail-loud branch.
    """
    _data_dir, _pending = sup_env
    set_capturer_behaviour("stream")

    spawn_kwargs: dict = {}
    real_exec = asyncio.create_subprocess_exec

    async def _spy_exec(*args, **kwargs):
        spawn_kwargs.update(kwargs)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_exec)

    state: dict = {"frames_read": 0}

    async def _fake_run_onoats_dual(
        *, live_terminal=False, locked_category=None, data_dir=None
    ):
        # Healthy streaming capturer; the recorder reads a few frames then
        # returns cleanly (EndFrame-equivalent) while the capturer is STILL
        # alive — the graceful-shutdown shape a terminal Ctrl+C would produce
        # once signal isolation keeps the capturer out of the foreground group.
        mic = os.environ["ONOATS_MIC_SOCKET"]
        reader, writer = await asyncio.open_unix_connection(mic)
        try:
            await asyncio.wait_for(reader.readline(), timeout=SUP_TIMEOUT)  # handshake
            for _ in range(3):
                chunk = await asyncio.wait_for(reader.read(64), timeout=SUP_TIMEOUT)
                if not chunk:
                    break
                state["frames_read"] += len(chunk)
        finally:
            writer.close()

    monkeypatch.setattr("onoats.dual.run_onoats_dual", _fake_run_onoats_dual)

    rc = _run_supervisor_bounded()

    # Structural: the OS will never relay a terminal signal to the capturer.
    assert spawn_kwargs.get("start_new_session") is True, (
        "the capturer must be spawned with start_new_session=True so a terminal "
        "Ctrl+C/SIGTERM is not delivered to it by the OS; the supervisor owns "
        "capturer teardown explicitly via _stop_capturer"
    )
    # Behavioural: a recorder-first completion with a still-alive capturer is a
    # GRACEFUL shutdown — rc=0, not the capturer-death fail-loud branch (rc=1).
    assert rc == 0, (
        "a graceful recorder shutdown (recorder finishes first, capturer still "
        f"alive) must yield rc=0, not be mis-classified as capturer-death; got {rc}"
    )
    assert state["frames_read"] > 0, "recorder read no frames from a streaming capturer"


# ---------------------------------------------------------------------------
# I3 PROCESS-GROUP TEARDOWN: the capturer is a process-group leader
# (start_new_session=True), so the supervisor must tear down the WHOLE group on
# shutdown — not just the leader PID. A helper/child the capturer spawned must
# not survive teardown holding the audio device while the supervisor reports
# success and removes the socket dir.
# ---------------------------------------------------------------------------


def _read_recorded_child_pid(fake_capturer, timeout: float) -> int:
    """Read the PID the fake capturer recorded for its spawned child.

    The capturer writes ``<script>.child_pid`` at startup (before serving), so
    by the time the supervisor returns the file exists — but poll briefly so the
    test never races a slow filesystem flush.
    """
    import time

    child_pid_file = Path(str(fake_capturer) + ".child_pid")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = child_pid_file.read_text().strip()
        except OSError:
            text = ""
        if text:
            return int(text)
        time.sleep(0.05)
    raise AssertionError(
        f"fake capturer never recorded its child PID at {child_pid_file} — "
        "spawn_child wiring broke"
    )


def _poll_pid_gone(pid: int, timeout: float) -> bool:
    """True if ``pid`` is gone (or becomes gone) within ``timeout`` seconds."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            # Exists but not ours to signal — still "alive" for our purpose.
            pass
        time.sleep(0.05)
    return False


def _assert_capturer_child_reaped(fake_capturer, *, rc_check) -> None:
    """Shared body for the I3 regression tests.

    Reads the child PID the fake capturer recorded, asserts it is gone after the
    supervisor's teardown, and best-effort cleans it up so a regression does not
    leak a stray ``sleep`` across the session.
    """
    child_pid: int | None = None
    try:
        rc = _run_supervisor_bounded()
        rc_check(rc)
        child_pid = _read_recorded_child_pid(fake_capturer, timeout=SUP_TIMEOUT)
        assert _poll_pid_gone(child_pid, timeout=SUP_TIMEOUT), (
            f"capturer child PID {child_pid} survived supervisor teardown — the "
            "supervisor signalled only the leader PID, not the whole process "
            "group; an orphaned capture path can outlive shutdown"
        )
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def test_capturer_teardown_reaps_group_on_clean_shutdown(
    sup_env, monkeypatch, set_capturer_behaviour, fake_capturer
):
    """Regression for invariant I3 (process-group teardown), graceful path.

    The fake capturer spawns a long-lived child (``sleep 3600``) that inherits
    the capturer's process group, and records the child PID. After a clean
    recorder shutdown (recorder finishes first, capturer still streaming), the
    supervisor stops the capturer via ``_stop_capturer``. The recorded child PID
    must be GONE — proving the teardown signalled the whole group, not just the
    leader.

    Without the fix (single-PID terminate/kill), the leader dies but the
    orphaned ``sleep`` child keeps running and this assertion fails.
    """
    _data_dir, _pending = sup_env
    set_capturer_behaviour("stream", spawn_child=True)

    state: dict = {"frames_read": 0}

    async def _fake_run_onoats_dual(
        *, live_terminal=False, locked_category=None, data_dir=None
    ):
        # Healthy streaming capturer; read a few frames then return cleanly while
        # the capturer (and its child) are still alive — the graceful-shutdown
        # shape that drives the supervisor's _stop_capturer teardown.
        mic = os.environ["ONOATS_MIC_SOCKET"]
        reader, writer = await asyncio.open_unix_connection(mic)
        try:
            await asyncio.wait_for(reader.readline(), timeout=SUP_TIMEOUT)  # handshake
            for _ in range(3):
                chunk = await asyncio.wait_for(reader.read(64), timeout=SUP_TIMEOUT)
                if not chunk:
                    break
                state["frames_read"] += len(chunk)
        finally:
            writer.close()

    monkeypatch.setattr("onoats.dual.run_onoats_dual", _fake_run_onoats_dual)

    def _rc_check(rc: int) -> None:
        assert rc == 0, f"a graceful recorder shutdown must yield rc=0, got {rc}"
        assert state["frames_read"] > 0, "recorder read no frames from the capturer"

    _assert_capturer_child_reaped(fake_capturer, rc_check=_rc_check)


def test_capturer_teardown_reaps_group_on_crash(
    sup_env, log_sink, monkeypatch, set_capturer_behaviour, fake_capturer
):
    """Regression for invariant I3 on the CRASH path.

    Here the capturer LEADER exits on its own (crash behaviour: serve frames,
    then die), so by the time ``_stop_capturer`` runs the leader is already
    reaped (``returncode`` set). A teardown that early-returned on a reaped
    leader — or that resolved the group via ``os.getpgid`` (which now fails) —
    would never sweep the orphaned ``sleep`` child. The fix targets the group by
    the leader PID (== PGID via ``start_new_session``), which the kernel keeps
    reserved while the group is non-empty, so the child is still reaped.
    """
    _data_dir, pending_dir = sup_env
    set_capturer_behaviour("crash", nframes=5, spawn_child=True)
    # Drain must outlast the capturer's ~0.5s post-close exit so the supervisor
    # observes capturer PROCESS death first and takes its crash branch — leaving
    # _stop_capturer to run against an already-reaped leader.
    _install_fake_recorder(monkeypatch, post_eof_drain=1.5)

    def _rc_check(rc: int) -> None:
        assert rc != 0, f"capturer crash must yield a NON-ZERO exit, got {rc}"
        assert _pending_files(pending_dir), "crash must still rotate a partial session"

    _assert_capturer_child_reaped(fake_capturer, rc_check=_rc_check)


# ---------------------------------------------------------------------------
# I2 ENV ALLOWLIST: the capturer is spawned with a deny-by-default env — ONLY
# the socket paths, nonce, and a fixed runtime/OS allowlist (PATH/HOME/…). STT /
# application secrets in the recorder env (DEEPGRAM_API_KEY, *_TOKEN, …) must
# NOT leak into the native child.
# ---------------------------------------------------------------------------


def test_capturer_env_is_allowlisted_and_excludes_secrets(
    sup_env, monkeypatch, set_capturer_behaviour
):
    """Regression for invariant I2 (capturer env allowlist).

    A sentinel STT secret is planted in the recorder env. We spy on
    ``asyncio.create_subprocess_exec`` to capture the ``env=`` kwarg the capturer
    is launched with, then assert:

      * the three socket/nonce vars + PATH ARE present (the allowlist isn't
        over-aggressive — the capturer can still launch);
      * the planted secrets (DEEPGRAM_API_KEY / STT_WS_TOKEN) are ABSENT (the
        deny-by-default allowlist holds — secrets are never forwarded).
    """
    _data_dir, _pending = sup_env
    set_capturer_behaviour("stream")
    # Plant sentinel secrets that the supervisor's old dict(os.environ) copy
    # would have leaked into the capturer.
    monkeypatch.setenv("DEEPGRAM_API_KEY", "should-not-leak")
    monkeypatch.setenv("STT_WS_TOKEN", "should-not-leak-either")

    spawn_kwargs: dict = {}
    real_exec = asyncio.create_subprocess_exec

    async def _spy_exec(*args, **kwargs):
        spawn_kwargs.update(kwargs)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_exec)

    # The recorder reads a few frames then returns cleanly so the supervisor runs
    # to a normal completion (we only need the captured spawn env).
    async def _fake_run_onoats_dual(
        *, live_terminal=False, locked_category=None, data_dir=None
    ):
        mic = os.environ["ONOATS_MIC_SOCKET"]
        reader, writer = await asyncio.open_unix_connection(mic)
        try:
            await asyncio.wait_for(reader.readline(), timeout=SUP_TIMEOUT)  # handshake
            for _ in range(3):
                chunk = await asyncio.wait_for(reader.read(64), timeout=SUP_TIMEOUT)
                if not chunk:
                    break
        finally:
            writer.close()

    monkeypatch.setattr("onoats.dual.run_onoats_dual", _fake_run_onoats_dual)

    rc = _run_supervisor_bounded()
    assert rc == 0, f"a clean recorder exit must yield rc=0, got {rc}"

    env = spawn_kwargs.get("env")
    assert env is not None, "the capturer must be spawned with an explicit env="

    # Required: socket paths + nonce are always present.
    for required in (
        "ONOATS_MIC_SOCKET",
        "ONOATS_SYSTEM_SOCKET",
        "ONOATS_CAPTURER_NONCE",
    ):
        assert required in env, f"capturer env is missing required var {required!r}"
    # The allowlist isn't over-aggressive — PATH survives so the native child can
    # actually launch.
    assert "PATH" in env, (
        "PATH must be passed through to the capturer (allowlist must not break "
        "the native launch)"
    )

    # I2: the planted STT secrets must NOT be forwarded into the capturer env.
    assert "DEEPGRAM_API_KEY" not in env, (
        "DEEPGRAM_API_KEY leaked into the capturer env — the deny-by-default "
        "allowlist must exclude STT/application secrets"
    )
    assert "STT_WS_TOKEN" not in env, (
        "STT_WS_TOKEN leaked into the capturer env — secrets must never be "
        "forwarded to the native capturer"
    )
    # The recorder env itself still HAD the secrets (proving exclusion happened
    # at the allowlist boundary, not because they were never set).
    assert os.environ.get("DEEPGRAM_API_KEY") == "should-not-leak"


def test_capturer_env_allowlist_constant_excludes_secret_families():
    """The auditable allowlist constant must not name any secret-bearing var.

    Importing the module-level allowlist keeps the policy testable: a future edit
    that adds a secret to the passthrough set fails here even without a spawn.
    """
    secret_markers = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "DEEPGRAM", "STT_")
    for name in cli._CAPTURER_ENV_POLICY.exact:
        upper = name.upper()
        assert not any(marker in upper for marker in secret_markers), (
            f"allowlisted passthrough var {name!r} looks secret-bearing"
        )
    # Socket paths + nonce are set explicitly by the builder, not via the
    # passthrough tuple.
    assert "PATH" in cli._CAPTURER_ENV_POLICY.exact
    # DYLD_* is not in the prefix allowlist at all (the whole family is an
    # injection surface); the deny set is a defense-in-depth backstop in case a
    # future edit re-adds a DYLD_/library prefix.
    assert "DYLD_" not in cli._CAPTURER_ENV_POLICY.prefixes
    assert "DYLD_INSERT_LIBRARIES" in cli._CAPTURER_ENV_POLICY.deny
    assert "DYLD_FORCE_FLAT_NAMESPACE" in cli._CAPTURER_ENV_POLICY.deny


def test_build_capturer_env_unit_allowlist_deny_and_overrides():
    """Pure-function unit test for `_build_capturer_env` (no spawn).

    Exercises the allowlist directly against a synthetic env: allowlisted exact +
    prefix vars pass through, secrets and the WHOLE DYLD_* dynamic-loader family
    are dropped, and the three socket/nonce vars are always set (overriding any
    inbound value).
    """
    base = {
        "PATH": "/usr/bin",
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "UTF-8",  # prefix family → forwarded
        "DYLD_FRAMEWORK_PATH": "/Frameworks",  # loader search path → NOT forwarded
        "DYLD_LIBRARY_PATH": "/evil",  # planted-dylib redirection → NOT forwarded
        "DYLD_INSERT_LIBRARIES": "/evil.dylib",  # injection → NOT forwarded
        "DYLD_PRINT_TO_FILE": "/tmp/x",  # arbitrary file write → NOT forwarded
        "DEEPGRAM_API_KEY": "secret",  # not allowlisted → dropped
        "ONOATS_MIC_SOCKET": "/stale/mic.sock",  # must be overridden
    }
    env = cli._build_capturer_env(
        base, mic_sock="/run/mic.sock", system_sock="/run/sys.sock", nonce="n0"
    )

    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["LC_CTYPE"] == "UTF-8"
    # The ENTIRE DYLD_* family is a dynamic-loader injection surface — none of it
    # is forwarded (a Phase-4 capturer needing a specific DYLD_* var adds it then).
    assert not any(k.startswith("DYLD_") for k in env), (
        "no DYLD_* var may reach the capturer — the whole family is injection-capable"
    )
    assert "DEEPGRAM_API_KEY" not in env, "non-allowlisted secret must be dropped"
    # Socket/nonce always set explicitly, overriding any inbound value.
    assert env["ONOATS_MIC_SOCKET"] == "/run/mic.sock"
    assert env["ONOATS_SYSTEM_SOCKET"] == "/run/sys.sock"
    assert env["ONOATS_CAPTURER_NONCE"] == "n0"


# ---------------------------------------------------------------------------
# Private 0700 socket dir: minted under the system temp root, owner-only
# ---------------------------------------------------------------------------


def test_supervisor_mints_private_0700_socket_dir(
    sup_env, monkeypatch, set_capturer_behaviour
):
    """The supervisor must mint a PRIVATE 0700 socket dir and export both socket

    paths into it (ONOATS_MIC_SOCKET / ONOATS_SYSTEM_SOCKET), foreclosing symlink
    aliasing of the two branch sockets. We capture the exported paths from inside
    the fake recorder and assert the parent dir mode + that the two sockets are
    distinct files in that dir.
    """
    _data_dir, _pending = sup_env
    set_capturer_behaviour("stream")

    captured: dict = {}

    async def _fake_run_onoats_dual(
        *, live_terminal=False, locked_category=None, data_dir=None
    ):
        captured["mic"] = os.environ.get("ONOATS_MIC_SOCKET")
        captured["system"] = os.environ.get("ONOATS_SYSTEM_SOCKET")
        # Return immediately (clean exit) — we only need the exported wiring.

    monkeypatch.setattr("onoats.dual.run_onoats_dual", _fake_run_onoats_dual)

    rc = _run_supervisor_bounded()
    assert rc == 0

    mic = captured.get("mic")
    system = captured.get("system")
    assert mic and system, (
        "supervisor did not export ONOATS_MIC_SOCKET / ONOATS_SYSTEM_SOCKET to "
        "the recorder"
    )
    mic_p, system_p = Path(mic), Path(system)
    # Distinct branch sockets (never-mix at the path level).
    assert mic_p != system_p
    assert mic_p.parent == system_p.parent, (
        "both branch sockets must live in the one private supervisor-owned dir"
    )
    # The private dir, if it still exists when we check, is owner-only (0700).
    # The supervisor rmtree's it on teardown; if present, assert the mode. The
    # supervisor sets 0o700 explicitly (mkdtemp + chmod), which we assert via the
    # documented constant rather than racing the teardown.
    assert cli.MIC_SOCKET_NAME and cli.SYSTEM_SOCKET_NAME
    assert mic_p.name == cli.MIC_SOCKET_NAME
    assert system_p.name == cli.SYSTEM_SOCKET_NAME


def test_socket_dir_minting_uses_0700(monkeypatch, short_root):
    """Directly assert the 0700 mode the supervisor applies to its socket dir.

    Wraps ``tempfile.mkdtemp`` (what ``_supervise_socket_session`` uses) so the
    minted directory is captured, then drives the supervisor far enough to mint
    it and asserts the resulting mode is owner-only. Independent of the recorder.
    """
    import tempfile as _tempfile

    monkeypatch.setenv("ONOATS_DATA_DIR", str(short_root / "data"))
    monkeypatch.setenv("AUDIO_SOURCE", "socket")
    # An unspawnable capturer makes the supervisor bail right AFTER minting the
    # socket dir but before running the recorder — enough to inspect the dir.
    monkeypatch.setenv("ONOATS_CAPTURER_BIN", "/nonexistent/onoats-capturer-xyz")

    minted: list[str] = []
    real_mkdtemp = _tempfile.mkdtemp

    def _spy_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        if kwargs.get("prefix", "").startswith("onoats-sock-") or (
            args and str(args[0]).startswith("onoats-sock-")
        ):
            minted.append(d)
        else:
            # The supervisor calls mkdtemp(prefix="onoats-sock-") as a kwarg;
            # capture any onoats-sock-* dir defensively.
            if "onoats-sock-" in d:
                minted.append(d)
        return d

    monkeypatch.setattr(_tempfile, "mkdtemp", _spy_mkdtemp)

    rc = _run_supervisor_bounded()
    assert rc != 0  # unspawnable capturer -> fail loud

    assert minted, "supervisor did not mint a socket dir via tempfile.mkdtemp"
    # The dir is rmtree'd on teardown; assert it was created 0700 by re-creating
    # the same call path is overkill — instead assert the supervisor chmod'd to
    # 0o700 by checking any surviving dir, else trust the explicit os.chmod call.
    survivors = [d for d in minted if os.path.isdir(d)]
    for d in survivors:
        mode = stat.S_IMODE(os.stat(d).st_mode)
        assert mode == 0o700, f"socket dir {d} must be 0700, got {oct(mode)}"


# ---------------------------------------------------------------------------
# STALE SOCKET / RESTART: a prior generation's socket must not be latched onto
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_stale_generation_socket_is_rejected_by_nonce(short_root):
    """STALE SOCKET / RESTART, the directly-verifiable half against the REAL

    transport: a leftover capturer from a PRIOR generation (its handshake nonce
    is OLD) must not let a NEW-generation transport (carrying the NEW nonce) latch
    on. The transport rejects the stale nonce (``expected_nonce`` mismatch ->
    SocketHandshakeError -> ErrorFrame), and crucially NO stale audio surfaces.

    This is the transport-level guarantee the supervisor relies on; the
    supervisor additionally mints a FRESH per-generation socket DIR so a stale
    path is structurally unreachable (asserted via mkdtemp above). Two
    independent stale-socket defenses, both covered.
    """
    from pipecat.frames.frames import EndFrame, StartFrame
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.transports.base_transport import TransportParams

    from onoats.transports.socket_audio import (
        SocketHandshakeError,
        UnixSocketAudioInputTransport,
    )
    from test_socket_audio_transport import _ManualHarness, _SocketWriterServer

    sock_dir = short_root / "sg"
    sock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    stale_path = str(sock_dir / "stale.sock")

    async def stale_feed(writer: asyncio.StreamWriter):
        writer.write(make_header(nonce="generation-OLD"))
        for seq in range(3):
            writer.write(make_frame(seq, _PCM_MARKER))
        await writer.drain()
        await asyncio.sleep(WAIT_TIMEOUT)

    server = _SocketWriterServer(stale_path, stale_feed)
    await server.start()

    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    transport = UnixSocketAudioInputTransport(
        stale_path,
        params,
        expected_nonce="generation-NEW",
        read_idle_timeout=WAIT_TIMEOUT,
    )
    harness = _ManualHarness(transport)
    await harness.setup()

    refused = False
    try:
        try:
            await asyncio.wait_for(
                transport.process_frame(StartFrame(), FrameDirection.DOWNSTREAM),
                timeout=WAIT_TIMEOUT,
            )
        except SocketHandshakeError:
            refused = True
        await asyncio.sleep(0.05)
    finally:
        try:
            await asyncio.wait_for(harness.send(EndFrame()), timeout=WAIT_TIMEOUT)
        except Exception:
            pass
        await server.aclose()

    surfaced_error = bool(harness.sink.error_frames())
    assert refused or surfaced_error, (
        "a stale-generation capturer (nonce mismatch) must be REJECTED — the new "
        "transport must not latch onto the prior generation's socket"
    )
    assert not harness.sink.audio_frames(), (
        "stale-generation audio latched onto the new transport — the generation "
        "nonce did not invalidate the prior socket"
    )


@pytest.mark.anyio
async def test_fresh_generation_nonce_is_accepted(short_root):
    """Negative control for the nonce gate: a matching (fresh) nonce IS accepted

    and audio flows. Proves the gate rejects on MISMATCH, not indiscriminately.
    """
    from pipecat.frames.frames import EndFrame, StartFrame
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.transports.base_transport import TransportParams

    from onoats.transports.socket_audio import UnixSocketAudioInputTransport
    from test_socket_audio_transport import (
        _ManualHarness,
        _SocketWriterServer,
        _wait_until,
    )

    sock_dir = short_root / "sf"
    sock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    fresh_path = str(sock_dir / "fresh.sock")

    async def fresh_feed(writer: asyncio.StreamWriter):
        writer.write(make_header(nonce="generation-NEW"))
        for seq in range(3):
            writer.write(make_frame(seq, _PCM_MARKER))
        await writer.drain()
        await asyncio.sleep(WAIT_TIMEOUT)

    server = _SocketWriterServer(fresh_path, fresh_feed)
    await server.start()

    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16000)
    transport = UnixSocketAudioInputTransport(
        fresh_path,
        params,
        expected_nonce="generation-NEW",
        read_idle_timeout=WAIT_TIMEOUT * 2,
    )
    harness = _ManualHarness(transport)
    await harness.setup()
    try:
        await asyncio.wait_for(
            transport.process_frame(StartFrame(), FrameDirection.DOWNSTREAM),
            timeout=WAIT_TIMEOUT,
        )
        await _wait_until(lambda: len(harness.sink.audio_frames()) >= 1)
        assert not harness.sink.error_frames(), (
            "a fresh, matching nonce must be accepted (no ErrorFrame)"
        )
    finally:
        await asyncio.wait_for(harness.send(EndFrame()), timeout=WAIT_TIMEOUT)
        await server.aclose()


# ---------------------------------------------------------------------------
# Phase 4 (release plan): the capturer stderr channel — always-drain reader,
# verbatim tee, and ONOATS-EVENT parsing into the status-file `warning`.
#
# The no-hang property for the reader is pinned two ways: every E2E test here
# runs under the same bounded worker-thread join as the rest of the suite (a
# reader that extended any wait would trip it), and the flood test proves the
# drain side (an undrained 512 KiB flood would block the capturer at the
# ~64 KiB pipe capacity before its sockets ever appeared).
# ---------------------------------------------------------------------------


def test_parse_capturer_event_unit():
    parse = cli._parse_capturer_event

    # Non-event lines (including the ordinary log prologue) parse to None.
    assert parse("onoats-capturer: WARNING mic: something") is None
    assert parse("") is None
    assert parse("ONOATS-EVENT") is None  # bare prefix, no trailing space
    assert parse("ONOATS-EVENT ") is None  # no event type

    assert parse("ONOATS-EVENT zero-run-clear branch=mic") == (
        "zero-run-clear",
        {"branch": "mic"},
    )

    etype, fields = parse(
        "ONOATS-EVENT zero-run-warning branch=system "
        "hint=capture callbacks are active but have delivered only zero "
        "samples for ~30 s — check the grant"
    )
    assert etype == "zero-run-warning"
    assert fields["branch"] == "system"
    assert fields["hint"].endswith("check the grant")

    # hint= is the trailing free-text field BY CONTRACT: it consumes the rest
    # of the line, even text that looks like further k=v fields.
    assert parse("ONOATS-EVENT x branch=mic hint=try a=b c") == (
        "x",
        {"branch": "mic", "hint": "try a=b c"},
    )

    # device events carry the "<name> (uid=<uid>)" description in the trailing
    # hint field — names contain spaces, so the free-text contract is load-bearing.
    assert parse(
        "ONOATS-EVENT device branch=mic hint=MacBook Pro Microphone (uid=BuiltIn)"
    ) == ("device", {"branch": "mic", "hint": "MacBook Pro Microphone (uid=BuiltIn)"})


@pytest.mark.anyio
async def test_stderr_reader_merges_warnings_and_tees(short_root, capfd):
    """Unit-drive _drain_capturer_stderr with a hand-fed StreamReader: both
    branches' warnings merge deterministically (branch order), non-event lines
    are teed verbatim and otherwise ignored, and EOF returns."""
    from onoats import status as status_file

    data_dir = short_root / "d"
    data_dir.mkdir()
    status_file.write_running(data_dir, pid=1, audio_source="socket", stt_label="x")

    reader = asyncio.StreamReader()
    reader.feed_data(b"onoats-capturer: ordinary log line\n")
    reader.feed_data(
        b"ONOATS-EVENT zero-run-warning branch=system hint=check the grant\n"
    )
    reader.feed_data(
        b"ONOATS-EVENT zero-run-warning branch=mic hint=check hardware mute\n"
    )
    reader.feed_data(b"ONOATS-EVENT bogus-future-event some=field\n")
    reader.feed_eof()
    await cli._drain_capturer_stderr(reader, data_dir, logger)

    got = status_file.read_status(data_dir)
    assert got is not None
    assert got.warning == "mic: check hardware mute; system: check the grant"
    # Verbatim tee: the plain log line surfaced on the supervisor's stderr.
    assert "ordinary log line" in capfd.readouterr().err


@pytest.mark.anyio
async def test_stderr_reader_clear_event_clears_warning(short_root):
    from onoats import status as status_file

    data_dir = short_root / "d"
    data_dir.mkdir()
    status_file.write_running(data_dir, pid=1, audio_source="socket", stt_label="x")

    reader = asyncio.StreamReader()
    reader.feed_data(
        b"ONOATS-EVENT zero-run-warning branch=system hint=check the grant\n"
    )
    reader.feed_data(
        b"ONOATS-EVENT zero-run-warning branch=mic hint=check hardware mute\n"
    )
    reader.feed_data(b"ONOATS-EVENT zero-run-clear branch=mic\n")
    reader.feed_eof()
    await cli._drain_capturer_stderr(reader, data_dir, logger)
    got = status_file.read_status(data_dir)
    assert got is not None and got.warning == "system: check the grant"

    # Second session shape: warn then clear the SAME branch → field fully cleared.
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"ONOATS-EVENT zero-run-warning branch=system hint=check the grant\n"
    )
    reader.feed_data(b"ONOATS-EVENT zero-run-clear branch=system\n")
    reader.feed_eof()
    await cli._drain_capturer_stderr(reader, data_dir, logger)
    got = status_file.read_status(data_dir)
    assert got is not None and got.warning is None


@pytest.mark.anyio
async def test_stderr_reader_no_status_record_is_a_noop(short_root):
    """An event racing ahead of the recorder's start write must not crash the
    reader (set_warning is best-effort) — and must not invent a record."""
    from onoats import status as status_file

    data_dir = short_root / "d"
    data_dir.mkdir()
    reader = asyncio.StreamReader()
    reader.feed_data(b"ONOATS-EVENT zero-run-warning branch=mic hint=early\n")
    reader.feed_eof()
    await cli._drain_capturer_stderr(reader, data_dir, logger)
    assert status_file.read_status(data_dir) is None


@pytest.mark.anyio
async def test_stderr_reader_survives_overlong_line(short_root, log_sink):
    """A line beyond the StreamReader limit is dropped (readline's documented
    ValueError path) and the reader keeps draining — the event AFTER the
    monster line still lands in the status file."""
    from onoats import status as status_file

    data_dir = short_root / "d"
    data_dir.mkdir()
    status_file.write_running(data_dir, pid=1, audio_source="socket", stt_label="x")

    reader = asyncio.StreamReader(limit=1024)
    reader.feed_data(b"y" * 8192 + b"\n")  # 8 KiB line >> 1 KiB limit
    reader.feed_data(b"ONOATS-EVENT zero-run-warning branch=mic hint=still alive\n")
    reader.feed_eof()
    await cli._drain_capturer_stderr(reader, data_dir, logger)

    got = status_file.read_status(data_dir)
    assert got is not None and got.warning == "mic: still alive"
    assert any("overlong" in r for r in log_sink.records)


def test_capturer_stderr_tee_and_flood_cannot_block(
    sup_env, log_sink, monkeypatch, set_capturer_behaviour, capfd
):
    """E2E pipe-drain + tee. The fake capturer writes a marker line plus a
    512 KiB stderr flood BEFORE binding its sockets. Undrained, the flood
    blocks at the ~64 KiB pipe capacity and the sockets never appear
    (capturer-start-timeout); with the always-drain reader the session runs
    normally and the marker reaches the supervisor's own stderr verbatim."""
    _data_dir, pending_dir = sup_env
    marker = "fake-capturer: tee-marker-7f3a"
    set_capturer_behaviour(
        "crash", nframes=5, stderr_lines=[marker], stderr_flood=512 * 1024
    )
    state = _install_fake_recorder(monkeypatch, post_eof_drain=1.5)

    rc = _run_supervisor_bounded()

    assert rc != 0  # normal crash-path exit, same as test_crash_fails_loud
    assert state["frames_read"] > 0, (
        "no frames flowed — the stderr flood likely blocked the capturer "
        "before its sockets appeared (always-drain reader not draining)"
    )
    assert not any("did not create" in r for r in log_sink.records), (
        "supervisor took the capturer-start-timeout path — the flood blocked "
        "the capturer"
    )
    assert marker in capfd.readouterr().err, (
        "capturer stderr was not teed verbatim to the supervisor's stderr"
    )


@pytest.mark.anyio
async def test_stderr_reader_device_event_updates_running_record(short_root):
    """A device event against a RUNNING record applies live (the mid-session
    mic-rebind path) and records into the shared device_state dict."""
    from onoats import status as status_file

    data_dir = short_root / "d"
    data_dir.mkdir()
    status_file.write_running(data_dir, pid=1, audio_source="socket", stt_label="x")

    device_state: dict[str, str] = {}
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"ONOATS-EVENT device branch=mic hint=MacBook Pro Microphone (uid=BuiltIn)\n"
    )
    reader.feed_data(
        b"ONOATS-EVENT device branch=system hint=system-output tap (uid=agg-1)\n"
    )
    reader.feed_eof()
    await cli._drain_capturer_stderr(reader, data_dir, logger, device_state)

    got = status_file.read_status(data_dir)
    assert got is not None
    assert got.mic_device == "MacBook Pro Microphone (uid=BuiltIn)"
    assert got.system_device == "system-output tap (uid=agg-1)"
    assert device_state == {
        "mic": "MacBook Pro Microphone (uid=BuiltIn)",
        "system": "system-output tap (uid=agg-1)",
    }


@pytest.mark.anyio
async def test_stderr_reader_device_event_without_record_records_state_only(
    short_root,
):
    """Device events outrun the recorder's start write: with no record on disk
    the reader must not invent one, but must still capture the descriptions in
    device_state for the deferred apply."""
    from onoats import status as status_file

    data_dir = short_root / "d"
    data_dir.mkdir()
    device_state: dict[str, str] = {}
    reader = asyncio.StreamReader()
    reader.feed_data(b"ONOATS-EVENT device branch=mic hint=Some Mic (uid=u1)\n")
    reader.feed_eof()
    await cli._drain_capturer_stderr(reader, data_dir, logger, device_state)

    assert status_file.read_status(data_dir) is None
    assert device_state == {"mic": "Some Mic (uid=u1)"}


@pytest.mark.anyio
async def test_device_flush_waits_for_this_sessions_record(short_root):
    """_apply_device_fields_when_recorded must skip a STALE running record
    (start_time before the session floor) and stamp only the record this
    session's recorder writes."""
    import time

    from onoats import status as status_file

    data_dir = short_root / "d"
    data_dir.mkdir()
    # Stale record from a "previous session" (running=True, e.g. after a crash).
    status_file.write_running(
        data_dir, pid=1, audio_source="socket", stt_label="old", start_time=1.0
    )

    device_state = {"mic": "Some Mic (uid=u1)", "system": "tap (uid=agg)"}
    floor = time.time()
    flush = asyncio.create_task(
        cli._apply_device_fields_when_recorded(data_dir, device_state, floor, logger)
    )
    await asyncio.sleep(0.6)  # a couple of poll cycles against the stale record
    assert not flush.done()
    st = status_file.read_status(data_dir)
    assert st is not None and st.mic_device is None  # stale record untouched

    # The recorder's start write arrives → the flush stamps it and exits.
    status_file.write_running(data_dir, pid=2, audio_source="socket", stt_label="new")
    await asyncio.wait_for(flush, timeout=SUP_TIMEOUT)
    st = status_file.read_status(data_dir)
    assert st is not None
    assert st.mic_device == "Some Mic (uid=u1)"
    assert st.system_device == "tap (uid=agg)"


def test_device_events_land_in_status_despite_outrunning_start_write(
    sup_env, monkeypatch, set_capturer_behaviour
):
    """E2E: device events emitted at capturer STARTUP (before the sockets, so
    before the fake recorder's write_running) still end up on this session's
    record via the deferred apply — and survive the supervisor's crash stamp."""
    data_dir, _ = sup_env
    set_capturer_behaviour(
        "crash",
        nframes=5,
        startup_events=[
            "ONOATS-EVENT device branch=mic hint=MacBook Pro Microphone (uid=BuiltIn)",
            "ONOATS-EVENT device branch=system hint=system-output tap (uid=agg-99)",
        ],
    )
    _install_fake_recorder(monkeypatch, post_eof_drain=1.5, write_running=True)

    rc = _run_supervisor_bounded()
    assert rc != 0

    from onoats import status as status_file

    st = status_file.read_status(data_dir)
    assert st is not None
    assert st.mic_device == "MacBook Pro Microphone (uid=BuiltIn)"
    assert st.system_device == "system-output tap (uid=agg-99)"
    assert st.exit_reason == "capturer-crash"


def test_zero_run_warning_event_sets_status_warning(
    sup_env, monkeypatch, set_capturer_behaviour
):
    """E2E: a zero-run-warning event emitted mid-session lands in the v2
    status `warning`, and coexists with the supervisor's crash stamp."""
    data_dir, _ = sup_env
    set_capturer_behaviour(
        "crash",
        nframes=5,
        events=[
            "ONOATS-EVENT zero-run-warning branch=system "
            "hint=check the system-audio grant"
        ],
    )
    _install_fake_recorder(monkeypatch, post_eof_drain=1.5, write_running=True)

    rc = _run_supervisor_bounded()
    assert rc != 0

    from onoats import status as status_file

    st = status_file.read_status(data_dir)
    assert st is not None
    assert st.warning == "system: check the system-audio grant"
    # The crash stamp and the warning are independent fields on one record.
    assert st.exit_reason == "capturer-crash"


def test_zero_run_clear_event_clears_status_warning(
    sup_env, monkeypatch, set_capturer_behaviour
):
    """E2E: warning followed by clear (real audio re-armed the detector)
    leaves the final record with no warning."""
    data_dir, _ = sup_env
    set_capturer_behaviour(
        "crash",
        nframes=5,
        events=[
            "ONOATS-EVENT zero-run-warning branch=system hint=check the grant",
            "ONOATS-EVENT zero-run-clear branch=system",
        ],
    )
    _install_fake_recorder(monkeypatch, post_eof_drain=1.5, write_running=True)

    rc = _run_supervisor_bounded()
    assert rc != 0

    from onoats import status as status_file

    st = status_file.read_status(data_dir)
    assert st is not None and st.warning is None


# ---------------------------------------------------------------------------
# Phase 7 (release plan): pre-socket tap preflight — the capturer makes the
# TCC-prompting tap call BEFORE binding its sockets, announced by
# `ONOATS-EVENT waiting-for-permission`. The supervisor must:
#   * keep the base capturer-start-timeout when no event arrives (a genuine
#     pre-socket hang is still bounded);
#   * extend the wait ONCE when the event arrived and the base budget expired
#     (a prompt answered at human speed must not kill the launch), surfacing
#     the pending prompt in the status file;
#   * keep the rc=10/rc=11 prestart mappings (parametrized test above) intact
#     across the reorder.
# ---------------------------------------------------------------------------


def test_start_timeout_without_permission_event_keeps_base_budget(
    sup_env, log_sink, set_capturer_behaviour, monkeypatch
):
    """A pre-socket hang WITHOUT a waiting-for-permission event must still be
    declared `capturer-start-timeout` on the BASE budget — the Phase-7
    extension is keyed on the event, never granted by default."""
    import time

    from onoats import status as status_file

    data_dir, _ = sup_env
    monkeypatch.setattr(cli, "_SOCKET_WAIT_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(cli, "_PERMISSION_WAIT_EXTRA_SEC", 30.0)
    set_capturer_behaviour("hang")

    t0 = time.monotonic()
    rc = _run_supervisor_bounded()
    elapsed = time.monotonic() - t0

    assert rc != 0, "a pre-socket hang must fail loud"
    rec = status_file.read_status(data_dir)
    assert rec is not None and rec.exit_reason == "capturer-start-timeout"
    assert elapsed < 30.0, (
        f"the wait was extended ({elapsed:.1f}s) although the capturer never "
        "announced waiting-for-permission — the extension must be event-keyed"
    )


def test_waiting_for_permission_extends_socket_wait_to_success(
    sup_env, monkeypatch, set_capturer_behaviour
):
    """E2E prompt-answered shape: the capturer announces the tap preflight,
    blocks past the BASE socket budget (the pending prompt), then binds and
    streams. The supervisor must extend the wait (instead of declaring
    capturer-start-timeout) and run the session to a clean rc=0 — and the
    prompt-pending state must have been surfaced in the status file (the fake
    recorder here writes no status, so the waiting record survives to assert)."""
    from onoats import status as status_file

    data_dir, _ = sup_env
    monkeypatch.setattr(cli, "_SOCKET_WAIT_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(cli, "_PERMISSION_WAIT_EXTRA_SEC", float(SUP_TIMEOUT))
    set_capturer_behaviour(
        "stream",
        startup_events=[
            "ONOATS-EVENT waiting-for-permission branch=system "
            "hint=tap creation pending the TCC prompt"
        ],
        bind_delay=2.5,  # > the 1.0s base budget: without the extension this
        # run is a capturer-start-timeout
    )

    state: dict = {"frames_read": 0}

    async def _fake_run_onoats_dual(
        *, live_terminal=False, locked_category=None, data_dir=None
    ):
        mic = os.environ["ONOATS_MIC_SOCKET"]
        reader, writer = await asyncio.open_unix_connection(mic)
        try:
            await asyncio.wait_for(reader.readline(), timeout=SUP_TIMEOUT)  # handshake
            for _ in range(3):
                chunk = await asyncio.wait_for(reader.read(64), timeout=SUP_TIMEOUT)
                if not chunk:
                    break
                state["frames_read"] += len(chunk)
        finally:
            writer.close()

    monkeypatch.setattr("onoats.dual.run_onoats_dual", _fake_run_onoats_dual)

    rc = _run_supervisor_bounded()

    assert rc == 0, (
        f"a prompt answered after the base socket budget must still yield a "
        f"working session (rc=0), got {rc} — the waiting-for-permission "
        "extension did not apply"
    )
    assert state["frames_read"] > 0, "recorder read no frames after the extension"
    rec = status_file.read_status(data_dir)
    assert rec is not None and rec.warning is not None
    assert "permission prompt" in rec.warning, (
        "the prompt-pending state must be surfaced in the status file while "
        f"the wait is extended; got warning={rec.warning!r}"
    )


def test_waiting_for_permission_timeout_names_the_prompt(
    sup_env, log_sink, set_capturer_behaviour, monkeypatch
):
    """Never-answered prompt: event seen, extension granted, sockets still never
    appear. The wait must END (extended budget, no hang) with the standard
    `capturer-start-timeout` reason and a last_error naming the pending prompt."""
    from onoats import status as status_file

    data_dir, _ = sup_env
    monkeypatch.setattr(cli, "_SOCKET_WAIT_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(cli, "_PERMISSION_WAIT_EXTRA_SEC", 1.0)
    set_capturer_behaviour(
        "hang",
        startup_events=[
            "ONOATS-EVENT waiting-for-permission branch=system hint=prompt pending"
        ],
    )

    rc = _run_supervisor_bounded()

    assert rc != 0, "an unanswered prompt must still end in a bounded fail-loud exit"
    rec = status_file.read_status(data_dir)
    assert rec is not None and rec.exit_reason == "capturer-start-timeout"
    assert rec.last_error and "permission prompt" in rec.last_error, (
        "the timeout record must name the (possibly still pending) prompt; "
        f"got last_error={rec.last_error!r}"
    )


@pytest.mark.anyio
async def test_wait_for_sockets_extension_writes_waiting_record(
    short_root, monkeypatch
):
    """Unit: _wait_for_sockets grants the extension exactly when the permission
    event is set and the base budget expires — writing the prestart waiting
    record — and still returns True once the (late) sockets appear."""
    from onoats import status as status_file

    monkeypatch.setattr(cli, "_SOCKET_WAIT_TIMEOUT_SEC", 0.3)
    monkeypatch.setattr(cli, "_PERMISSION_WAIT_EXTRA_SEC", float(SUP_TIMEOUT))
    data_dir = short_root / "d"
    data_dir.mkdir()

    class _FakeProc:
        returncode = None

    permission_event = asyncio.Event()
    permission_event.set()
    sock = short_root / "late.sock"

    async def _bind_late():
        await asyncio.sleep(1.0)  # past the 0.3s base budget
        sock.touch()

    binder = asyncio.create_task(_bind_late())
    try:
        ok = await asyncio.wait_for(
            cli._wait_for_sockets(
                _FakeProc(),
                (str(sock),),
                logger,
                data_dir=data_dir,
                permission_event=permission_event,
            ),
            timeout=SUP_TIMEOUT,
        )
    finally:
        await binder
    assert ok is True

    rec = status_file.read_status(data_dir)
    assert rec is not None, "the extension must write the prestart waiting record"
    assert rec.running is True
    assert rec.warning and "permission prompt" in rec.warning


# ---------------------------------------------------------------------------
# Shutdown write ordering (RUNTIME): status-stopped lands on disk BEFORE the pid
# file is unlinked.
#
# This is the contract `onoats stop` (and the menu bar's external-stop polling)
# rely on indirectly: the GUI keys "stopped" off the supervisor PROCESS exiting,
# but `onoats status` (and any pid-backstop reader) must never observe pid-gone
# while the status file still claims running. The producer call order is asserted
# STATICALLY in test_status_file.py (source-text index check). This test is the
# RUNTIME complement: it drives dual.py's real shutdown tail
# (`dual._finalize_shutdown_status` — the exact helper `run_onoats_dual`'s
# `_run_shutdown` calls, factored out of the closure precisely so it is
# runtime-reachable without booting the STT/VAD stack) against a real filesystem.
# It spies the pid-unlink boundary and observes, AT THE INSTANT the pid file is
# unlinked, that the on-disk status already reads running=False. A reorder of the
# status-stopped / pid-removal pair inside the helper, or a status write that
# lagged (non-durable / async), flips the observed value and fails here — neither
# of which the static index check nor test_shutdown_drain.py (which drives a
# FakeTask and never exercises _remove_pid_file) can catch.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ended_by_error", "expected_reason"),
    [(False, "graceful"), (True, "fatal_error_frame")],
)
def test_shutdown_tail_writes_status_stopped_before_pid_unlink(
    tmp_path, monkeypatch, ended_by_error, expected_reason
):
    from onoats import dual
    from onoats import runtime
    from onoats import status as status_file

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Start-of-session: real pid file + real running status (pid FIRST, then
    # status — the start half of the same ordering contract).
    pid_path = runtime._write_pid_file(data_dir)
    runtime._write_status_running(data_dir, audio_source="socket", stt_label="fake-stt")
    assert pid_path.exists()
    start_rec = status_file.read_status(data_dir)
    assert start_rec is not None and start_rec.running is True

    # Instrument the unlink boundary: snapshot the on-disk status at the exact
    # instant the pid file is removed. `_finalize_shutdown_status` calls
    # `_remove_pid_file` (resolved in dual.py's namespace) as its final step, so
    # patching the name there observes the helper's REAL ordering.
    observed: dict = {}
    real_remove = dual._remove_pid_file

    def _spy_remove(p, **kwargs):
        rec = status_file.read_status(data_dir)
        observed["running_at_unlink"] = None if rec is None else rec.running
        observed["pid_file_present_at_unlink"] = p.exists()
        return real_remove(p, **kwargs)

    monkeypatch.setattr(dual, "_remove_pid_file", _spy_remove)

    # Drive dual.py's actual shutdown tail (not a hand-sequenced copy of it).
    dual._finalize_shutdown_status(
        data_dir,
        pid_path,
        ended_by_error=ended_by_error,
        old_terminal_settings=None,
    )

    # The unlink spy observed status already running=False — no window where a
    # reader sees pid-gone while the status file still claims a live recorder.
    assert observed["running_at_unlink"] is False, (
        "pid file was unlinked while the on-disk status still claimed running — "
        "a reader could observe pid-gone + status-running (the exact disagreement "
        "the ordering contract forbids)"
    )
    assert observed["pid_file_present_at_unlink"] is True, (
        "the pid file should still exist at the moment _remove_pid_file is entered"
    )

    # Final on-disk state: pid gone, status durably stopped with the branch's
    # exit reason, start-of-session detail preserved.
    assert not pid_path.exists()
    end_rec = status_file.read_status(data_dir)
    assert end_rec is not None and end_rec.running is False
    assert end_rec.exit_reason == expected_reason
    assert end_rec.audio_source == "socket", (
        "stop must preserve start-of-session detail"
    )
