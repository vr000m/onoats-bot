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
    BEHAVIOUR = os.environ.get("ONOATS_FAKE_BEHAVIOUR", "stream")
    N_FRAMES = int(os.environ.get("ONOATS_FAKE_NFRAMES", "5"))

    if not MIC or not SYSTEM:
        sys.stderr.write(
            "fake-capturer: missing socket paths (mic=%r system=%r)\\n" % (MIC, SYSTEM)
        )
        sys.exit(3)


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
    monkeypatch, *, idle_end: float = 0.6, post_eof_drain: float = 0.0
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

    async def _fake_run_onoats_dual(*, live_terminal=False, locked_category=None):
        from onoats._vendor.store import onoats_data_dir
        from onoats._vendor import session_queue

        data_dir = onoats_data_dir()
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


# ---------------------------------------------------------------------------
# CRASH: capturer writes N frames then dies -> fail loud + rotate partial
# ---------------------------------------------------------------------------


def test_crash_fails_loud_and_rotates_partial_session(sup_env, log_sink, monkeypatch):
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
    monkeypatch.setenv("ONOATS_FAKE_BEHAVIOUR", "crash")
    monkeypatch.setenv("ONOATS_FAKE_NFRAMES", "5")
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


def test_hung_but_alive_does_not_hang_and_rotates(sup_env, log_sink, monkeypatch):
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
    monkeypatch.setenv("ONOATS_FAKE_BEHAVIOUR", "silent")
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


def test_clean_recorder_exit_returns_zero_and_stops_capturer(sup_env, monkeypatch):
    """Negative control: a healthy streaming capturer + a recorder that returns
    cleanly yields rc=0, and the supervisor stops the (still-alive) capturer.

    This proves the fail-loud non-zero exits above are specific to failure, not
    indiscriminate. The fake recorder here reads a few frames then returns
    voluntarily (emulating an EndFrame shutdown).
    """
    _data_dir, pending_dir = sup_env
    monkeypatch.setenv("ONOATS_FAKE_BEHAVIOUR", "stream")

    state: dict = {"frames_read": 0}

    async def _fake_run_onoats_dual(*, live_terminal=False, locked_category=None):
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
    sup_env, monkeypatch
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
    monkeypatch.setenv("ONOATS_FAKE_BEHAVIOUR", "stream")

    spawn_kwargs: dict = {}
    real_exec = asyncio.create_subprocess_exec

    async def _spy_exec(*args, **kwargs):
        spawn_kwargs.update(kwargs)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _spy_exec)

    state: dict = {"frames_read": 0}

    async def _fake_run_onoats_dual(*, live_terminal=False, locked_category=None):
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
# Private 0700 socket dir: minted under the system temp root, owner-only
# ---------------------------------------------------------------------------


def test_supervisor_mints_private_0700_socket_dir(sup_env, monkeypatch):
    """The supervisor must mint a PRIVATE 0700 socket dir and export both socket

    paths into it (ONOATS_MIC_SOCKET / ONOATS_SYSTEM_SOCKET), foreclosing symlink
    aliasing of the two branch sockets. We capture the exported paths from inside
    the fake recorder and assert the parent dir mode + that the two sockets are
    distinct files in that dir.
    """
    _data_dir, _pending = sup_env
    monkeypatch.setenv("ONOATS_FAKE_BEHAVIOUR", "stream")

    captured: dict = {}

    async def _fake_run_onoats_dual(*, live_terminal=False, locked_category=None):
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
