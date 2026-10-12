"""Phase 2 tests: ``UnixSocketAudioTransport`` wrapper + ``AUDIO_SOURCE`` wiring.

These cover the Phase-2 acceptance criteria from
``docs/dev_plans/20260607-feature-menubar-coreaudio-socket-transport.md``:

  (a) **KEYSTONE never-mix invariant** — drive the real ``_build_dual_pipeline``
      from ``onoats.dual`` with two real ``UnixSocketAudioTransport`` s, each fed
      by an independent pure-Python unix-socket fixture. A *distinguishable*
      payload written to the MIC socket may ONLY ever surface tagged ``me``; a
      distinguishable payload written to the SYSTEM socket may ONLY ever surface
      tagged ``them``. No code path fans one socket to both arms or merges them
      pre-STT. This protects downstream speaker attribution — see the plan's
      "Invariant (do not violate)".

  (b) **Negative guard** — socket mode REFUSES TO START (raises *before* the
      pipeline runs) when the two branch sockets resolve to the same path. Both a
      raw identical path AND a symlink / relative-path alias of one onto the other
      are rejected, because the guard compares ``Path(...).resolve()`` rather than
      raw strings.

  (c) **No-PortAudio assertion** — ``AUDIO_SOURCE=socket`` neither imports nor
      calls the PortAudio device-enumeration path. ``select_dual_input_devices``
      (and any PyAudio entrypoint) is patched to raise if touched, the socket-mode
      construction is driven, and the patch is asserted never invoked. No native
      binary is needed.

The sockets are fed by a pure-Python writer reused from the Phase-1 suite
(``tests/test_socket_audio_transport.py``) — **no native code, no Swift binary**.
Async tests run under anyio's pytest plugin (a transitive dep); there is no
``pytest-asyncio`` requirement. Every wait is bounded so a hang fails the test
rather than blocking CI. Socket paths live under a short ``/tmp``-based root to
stay within the macOS ``AF_UNIX`` ~104-byte limit.

Interface (verified against the landed Phase-2 implementation):

  - ``UnixSocketAudioTransport(socket_path, *, sample_rate=16000, ...)`` — a
    ``BaseTransport`` wrapper exposing ``.input()`` (the swap seam) and an
    input-only ``.output()`` that refuses.
  - ``onoats.dual._build_socket_transports(cfg)`` — reads ``cfg.mic_socket`` /
    ``cfg.system_socket``, applies the resolved-path never-mix guard, and returns
    ``(mic_transport, system_transport, mic_label, system_label)``. The
    ``cfg.audio_source == "socket"`` branch in ``dual.py`` dispatches to it and
    never reaches ``_build_portaudio_transports`` (which calls
    ``select_dual_input_devices`` / PyAudio).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from pipecat.frames.frames import TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.tests.utils import SleepFrame, run_test
from pipecat.transports.base_input import BaseInputTransport

# Reuse the Phase-1 wire helpers / fixtures rather than reinventing the framing.
# Sibling module in tests/ — importable by bare name under pytest's default
# (prepend) import mode, since tests/ is not a package.
from test_socket_audio_transport import (  # noqa: E402
    WAIT_TIMEOUT,
    _SocketWriterServer,
    anyio_backend,  # noqa: F401 - re-exported fixture (pins anyio to asyncio)
    make_frame,
    make_header,
    pcm_from_samples,
)

from onoats.transports.socket_audio import UnixSocketAudioTransport

# Two distinguishable PCM payloads — one per socket. A single sample value carried
# end-to-end lets a test assert *which* socket a surfaced frame originated from.
MIC_MARKER = 111  # mic / "me" branch payload
SYSTEM_MARKER = 222  # system / "them" branch payload


@pytest.fixture
def two_paths(tmp_path_factory):
    """Two short, distinct unix-socket paths under a shared short ``/tmp`` root.

    ``AF_UNIX`` paths are capped (~104 bytes on macOS); pytest's default
    ``tmp_path`` lives under a long ``/var/folders/...`` prefix that blows the
    limit, so build a deliberately short directory instead.
    """
    import shutil
    import tempfile
    import uuid

    base = Path(tempfile.gettempdir()) / f"od{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True, exist_ok=True)
    mic = base / "m.sock"
    system = base / "s.sock"
    try:
        yield base, str(mic), str(system)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _cfg(mic_socket: str, system_socket: str):
    """Build an ``OnoatsConfig`` in socket mode with the two given paths.

    Constructs the config directly from a raw ``[audio]`` section so the
    never-mix guard / builder can be driven without mutating process env.
    """
    from onoats.config import OnoatsConfig

    return OnoatsConfig(
        raw={
            "audio": {
                "source": "socket",
                "mic_socket": mic_socket,
                "system_socket": system_socket,
            }
        }
    )


# ---------------------------------------------------------------------------
# Wrapper unit surface: .input() returns the input transport; .output() refuses
# ---------------------------------------------------------------------------


def test_wrapper_input_returns_input_transport(two_paths):
    """``UnixSocketAudioTransport.input()`` returns a single ``BaseInputTransport``.

    This is the swap seam ``_build_dual_pipeline`` relies on: it only ever calls
    ``.input()`` on each transport object.
    """
    _base, mic, _system = two_paths
    transport = UnixSocketAudioTransport(mic)
    inp = transport.input()
    assert isinstance(inp, BaseInputTransport)
    # Stable across calls (one branch == one input == one socket).
    assert transport.input() is inp


def test_wrapper_output_is_refused(two_paths):
    """The socket transport is input-only; ``.output()`` must refuse loudly."""
    _base, mic, _system = two_paths
    transport = UnixSocketAudioTransport(mic)
    with pytest.raises(NotImplementedError):
        transport.output()


def test_two_wrappers_have_independent_inputs(two_paths):
    """Two wrappers (one per branch) must not share an input transport instance.

    A shared input would be the most direct way to collapse ``me`` and ``them``
    onto one socket — the inverse of the never-mix invariant.
    """
    _base, mic, system = two_paths
    mic_t = UnixSocketAudioTransport(mic)
    sys_t = UnixSocketAudioTransport(system)
    assert mic_t.input() is not sys_t.input()


# ---------------------------------------------------------------------------
# Stub VAD / STT processors for the keystone end-to-end run
# ---------------------------------------------------------------------------


class _MarkerSTT(FrameProcessor):
    """A deterministic stand-in for the per-branch STT service.

    Real STT is heavy + non-deterministic and irrelevant to the *topology* under
    test. This converts each ``InputAudioRawFrame`` whose first sample equals a
    known marker into a ``TranscriptionFrame`` whose text records that marker, so
    a surfaced transcript is traceable back to the socket that produced it. All
    other frames pass through untouched. It performs **no** cross-branch routing —
    the only way a marker reaches the wrong ``SourceTagger`` is if the pipeline
    wiring fanned/merged the sockets, which is exactly what the test forbids.
    """

    def __init__(self, label: str):
        super().__init__()
        self._label = label

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        from pipecat.frames.frames import InputAudioRawFrame

        if isinstance(frame, InputAudioRawFrame) and len(frame.audio) >= 2:
            import array

            samples = array.array("h")
            samples.frombytes(frame.audio[:2])
            marker = samples[0]
            await self.push_frame(
                TranscriptionFrame(
                    text=f"marker:{marker}",
                    user_id="",  # SourceTagger overwrites with me/them
                    timestamp="2026-01-01T00:00:00Z",
                ),
                direction,
            )
            return
        await self.push_frame(frame, direction)


class _PassThroughVAD(FrameProcessor):
    """A no-op stand-in for ``VADProcessor`` (keeps the arm shape, no analysis)."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class _NoopSilenceDetector(FrameProcessor):
    """A no-op stand-in for ``DualSilenceDetector`` (tail of the dual pipeline)."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


def _marker(marker: int) -> bytes:
    """A one-sample PCM16 LE payload carrying a branch marker."""
    return pcm_from_samples([marker])


async def _feed(marker: int):
    """Build a fixture ``feed`` coroutine that emits a handshake + marker frames."""

    pcm = _marker(marker)

    async def feed(writer: asyncio.StreamWriter):
        writer.write(make_header())
        for seq in range(4):
            writer.write(make_frame(seq, pcm))
        await writer.drain()
        # Stay connected so the branch does not EOF mid-test.
        await asyncio.sleep(WAIT_TIMEOUT)

    return feed


# ---------------------------------------------------------------------------
# (a) KEYSTONE never-mix invariant
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_keystone_never_mix_socket_to_source_tag(two_paths):
    """KEYSTONE: mic-socket audio surfaces ONLY ``me``; system-socket ONLY ``them``.

    Drives the *real* ``_build_dual_pipeline`` (from ``onoats.dual``) with two real
    ``UnixSocketAudioTransport`` s — one per branch — each fed by an independent
    socket fixture writing a *distinguishable* payload (mic -> sample ``111``,
    system -> sample ``222``). The arms carry the real ``SourceTagger`` s that
    ``_build_dual_pipeline`` installs (``source="me"`` / ``source="them"``).

    The assertion is **direction-explicit**: after the run, every transcript whose
    text records the mic marker must be tagged ``me`` and NONE tagged ``them``, and
    vice-versa for the system marker. A single fanned/merged code path — one socket
    reaching both arms, or the two streams joined pre-STT — would surface a marker
    under the wrong tag and fail. This is the load-bearing test the plan flags for
    human review.
    """
    from onoats.dual import _build_dual_pipeline, _build_socket_transports

    _base, mic_path, system_path = two_paths

    # Build the two branch transports through the REAL production seam — the same
    # builder dual.run_onoats_dual uses in socket mode (applies the never-mix
    # guard, reads cfg.mic_socket/system_socket). This keeps the keystone test
    # exercising the actual wiring, not a hand-assembled approximation.
    mic_transport, system_transport, _mic_label, _system_label = (
        _build_socket_transports(_cfg(mic_path, system_path))
    )

    # Tail collector: a sink that records every TranscriptionFrame reaching the
    # very end of the dual pipeline (downstream of both SourceTaggers).
    transcript_buffer = _NoopSilenceDetector()  # transcript_buffer slot (pass-through)
    silence_detector = _NoopSilenceDetector()  # silence_detector slot (pass-through)

    pipeline = _build_dual_pipeline(
        mic_transport,
        system_transport,
        _PassThroughVAD(),  # mic_vad
        _PassThroughVAD(),  # system_vad
        _MarkerSTT("mic"),  # mic_stt
        _MarkerSTT("system"),  # system_stt
        transcript_buffer,
        silence_detector,
        call_id="test-keystone",
    )

    mic_server = _SocketWriterServer(mic_path, await _feed(MIC_MARKER))
    system_server = _SocketWriterServer(system_path, await _feed(SYSTEM_MARKER))
    await mic_server.start()
    await system_server.start()
    try:
        down, _up = await asyncio.wait_for(
            run_test(
                pipeline,
                # SleepFrame lets the socket readers push several frames through
                # the pipeline before the harness sends its terminating EndFrame.
                frames_to_send=[SleepFrame(0.4)],
            ),
            timeout=WAIT_TIMEOUT * 2,
        )
    finally:
        await mic_server.aclose()
        await system_server.aclose()

    transcripts = [f for f in down if isinstance(f, TranscriptionFrame)]
    assert transcripts, "no TranscriptionFrame surfaced — pipeline did not route audio"

    mic_text = f"marker:{MIC_MARKER}"
    system_text = f"marker:{SYSTEM_MARKER}"

    me_tags = {f.text for f in transcripts if f.user_id == "me"}
    them_tags = {f.text for f in transcripts if f.user_id == "them"}

    # Every surfaced transcript carries the canonical me/them enum (never empty,
    # never some other value) — the SourceTagger contract.
    assert all(f.user_id in {"me", "them"} for f in transcripts), {
        f.user_id for f in transcripts
    }

    # Both branches actually produced output (the test exercised both sockets).
    assert any(f.text == mic_text for f in transcripts), "mic socket produced nothing"
    assert any(f.text == system_text for f in transcripts), (
        "system socket produced nothing"
    )

    # DIRECTION-EXPLICIT never-mix assertions:
    #   mic marker  -> ONLY ever tagged me   (never them)
    #   system marker -> ONLY ever tagged them (never me)
    assert mic_text in me_tags, "mic-socket audio did not surface tagged `me`"
    assert mic_text not in them_tags, (
        "MIX DETECTED: mic-socket audio surfaced tagged `them`"
    )
    assert system_text in them_tags, "system-socket audio did not surface tagged `them`"
    assert system_text not in me_tags, (
        "MIX DETECTED: system-socket audio surfaced tagged `me`"
    )

    # Stronger statement of the same invariant: the set of texts seen under each
    # tag is disjoint and each maps to exactly its own socket.
    assert me_tags == {mic_text}, f"`me` arm saw foreign markers: {me_tags}"
    assert them_tags == {system_text}, f"`them` arm saw foreign markers: {them_tags}"


# ---------------------------------------------------------------------------
# (b) Negative guard: same resolved socket path refuses to start
# ---------------------------------------------------------------------------


def _socket_builder():
    """Return ``onoats.dual._build_socket_transports``, failing loudly if absent.

    This is the Phase-2 ``cfg.audio_source == "socket"`` builder that applies the
    resolved-path never-mix guard. It takes an ``OnoatsConfig`` and returns
    ``(mic_transport, system_transport, mic_label, system_label)``.
    """
    import onoats.dual as dual_mod

    builder = getattr(dual_mod, "_build_socket_transports", None)
    if builder is None:
        pytest.fail(
            "Phase-2 never-mix guard entry point not found: expected "
            "onoats.dual._build_socket_transports(cfg) to resolve+compare socket "
            "paths and refuse identical sockets. If the impl names it differently, "
            "update this test to match."
        )
    return builder


def test_guard_refuses_identical_raw_paths(two_paths):
    """Two identical raw socket paths must refuse to start (raise before run)."""
    _base, mic, _system = two_paths
    builder = _socket_builder()
    with pytest.raises((ValueError, RuntimeError)) as excinfo:
        builder(_cfg(mic, mic))
    # The error should name the collision, not some unrelated failure.
    assert "socket" in str(excinfo.value).lower()


def test_guard_refuses_symlink_alias(two_paths):
    """A symlink alias of the mic socket onto the system slot must be rejected.

    The guard compares ``Path(...).resolve()``, so a symlink that points the
    system path at the mic path collapses both branches onto one socket and must
    refuse — even though the raw strings differ.
    """
    base, mic, _system = two_paths
    builder = _socket_builder()

    # Create a real file at the mic path so the symlink resolves to it.
    Path(mic).write_bytes(b"")
    alias = base / "alias.sock"
    alias.symlink_to(mic)

    assert str(alias) != mic  # raw strings differ ...
    assert alias.resolve() == Path(mic).resolve()  # ... but resolve identically.

    with pytest.raises((ValueError, RuntimeError)) as excinfo:
        builder(_cfg(mic, str(alias)))
    assert "socket" in str(excinfo.value).lower()


def test_guard_refuses_relative_path_alias(two_paths):
    """A relative-path alias resolving to the mic socket must also be rejected.

    Guards the same invariant via a non-symlink alias: ``./<dir>/m.sock`` and the
    absolute mic path resolve to the same file, so the guard (resolving both) must
    refuse, even though the raw strings differ.
    """
    base, mic, _system = two_paths
    builder = _socket_builder()

    cwd = os.getcwd()
    try:
        os.chdir(base)
        rel = os.path.join(".", Path(mic).name)  # "./m.sock" relative to base
        assert rel != mic
        assert Path(rel).resolve() == Path(mic).resolve()
        with pytest.raises((ValueError, RuntimeError)) as excinfo:
            builder(_cfg(mic, rel))
        assert "socket" in str(excinfo.value).lower()
    finally:
        os.chdir(cwd)


def test_guard_accepts_distinct_paths(two_paths):
    """Two genuinely distinct socket paths must NOT be rejected by the guard.

    Negative control for the negative guard: prove it rejects on *collision*, not
    indiscriminately. The builder constructs transports without connecting, so it
    must return two independent branch transports for distinct paths.
    """
    _base, mic, system = two_paths
    builder = _socket_builder()
    assert Path(mic).resolve() != Path(system).resolve()
    mic_t, sys_t, mic_label, system_label = builder(_cfg(mic, system))
    # Independent branches: one socket each (the inverse of a collapse).
    assert mic_t.input() is not sys_t.input()
    # Labels reflect the two distinct resolved paths.
    assert mic_label != system_label


# ---------------------------------------------------------------------------
# Generation nonce: cfg.capturer_nonce must reach the transport handshake gate
# ---------------------------------------------------------------------------


def test_socket_transports_thread_capturer_nonce(two_paths):
    """The supervisor's generation nonce must reach BOTH branch transports.

    The Phase-3 supervisor mints a per-launch nonce and exports
    ONOATS_CAPTURER_NONCE; the recorder resolves it via cfg.capturer_nonce. It
    must be threaded into each transport as ``expected_nonce`` so a capturer that
    handshakes with a missing/stale nonce is rejected — otherwise the supervisor
    mints a nonce nobody enforces (the stale/foreign-generation check is dead).
    """
    from onoats.config import OnoatsConfig

    _base, mic, system = two_paths
    nonce = "gen-deadbeef00"
    cfg = OnoatsConfig(
        raw={
            "audio": {
                "source": "socket",
                "mic_socket": mic,
                "system_socket": system,
                "capturer_nonce": nonce,
            }
        }
    )
    mic_t, sys_t, _mic_label, _system_label = _socket_builder()(cfg)
    assert mic_t.input()._expected_nonce == nonce
    assert sys_t.input()._expected_nonce == nonce


def test_socket_transports_no_nonce_gating_when_unset(two_paths):
    """Manual socket mode (no supervisor → no nonce) must NOT gate on a nonce.

    ``expected_nonce`` is ``None`` so any handshake is accepted — backward
    compatible with running socket mode without the supervisor.
    """
    _base, mic, system = two_paths
    mic_t, sys_t, _a, _b = _socket_builder()(_cfg(mic, system))
    assert mic_t.input()._expected_nonce is None
    assert sys_t.input()._expected_nonce is None


def test_socket_paths_are_expanduser_expanded():
    """``~`` in a config socket path must reach the transport EXPANDED.

    ``asyncio.open_unix_connection`` does not expand ``~``, so a config value
    like ``~/onoats/mic.sock`` would silently fail to connect if the raw string
    were handed to the transport. The builder must pass the expanduser'd path.
    """
    import os

    from onoats.config import OnoatsConfig

    cfg = OnoatsConfig(
        raw={
            "audio": {
                "source": "socket",
                "mic_socket": "~/oa-test-mic.sock",
                "system_socket": "~/oa-test-sys.sock",
            }
        }
    )
    mic_t, sys_t, _a, _b = _socket_builder()(cfg)
    home = os.path.expanduser("~")
    assert "~" not in mic_t.input()._socket_path
    assert "~" not in sys_t.input()._socket_path
    assert mic_t.input()._socket_path == os.path.join(home, "oa-test-mic.sock")
    assert sys_t.input()._socket_path == os.path.join(home, "oa-test-sys.sock")


# ---------------------------------------------------------------------------
# (c) No-PortAudio assertion: socket mode skips device enumeration entirely
# ---------------------------------------------------------------------------


def test_socket_mode_skips_portaudio_device_enumeration(two_paths, monkeypatch):
    """``AUDIO_SOURCE=socket`` must NEVER touch the PortAudio enumeration path.

    Patches ``select_dual_input_devices`` (the dual recorder's PyAudio
    device-resolution entry point) to raise if it is *ever* called, then drives
    the real socket-mode transport builder and asserts the PortAudio path was
    never invoked. Mirrors the Review-Focus "no PortAudio on the socket path"
    criterion. No native binary is required for any of this.
    """
    import sys

    _base, mic, system = two_paths

    called = {"select_dual_input_devices": False}

    import onoats.config.audio_devices as audio_devices_mod

    def _boom(*args, **kwargs):
        called["select_dual_input_devices"] = True
        raise AssertionError(
            "select_dual_input_devices (PortAudio enumeration) was called in "
            "AUDIO_SOURCE=socket mode — socket mode must skip device resolution."
        )

    monkeypatch.setattr(
        audio_devices_mod, "select_dual_input_devices", _boom, raising=True
    )
    # Also defend the binding referenced inside dual.py's portaudio builder, in
    # case it is imported at module scope rather than function-locally.
    import onoats.dual as dual_mod

    if hasattr(dual_mod, "select_dual_input_devices"):
        monkeypatch.setattr(dual_mod, "select_dual_input_devices", _boom, raising=False)

    # Sanity: config resolves to socket mode with the two distinct paths.
    cfg = _cfg(mic, system)
    assert cfg.audio_source == "socket"
    assert cfg.mic_socket == mic
    assert cfg.system_socket == system

    # Drive the socket-mode transport construction (the seam that replaces the
    # LocalAudioTransport build). It must build two socket transports WITHOUT
    # touching select_dual_input_devices.
    mic_t, sys_t, _mic_label, _system_label = dual_mod._build_socket_transports(cfg)

    assert called["select_dual_input_devices"] is False, (
        "socket mode invoked the PortAudio device-enumeration path"
    )

    # No native binary needed: PyAudio must not be imported as a side effect of
    # the socket-mode build.
    assert "pyaudio" not in sys.modules, (
        "socket-mode construction imported pyaudio — it must stay native-free"
    )

    # Two independent branch transports (one socket each).
    assert mic_t.input() is not sys_t.input()


# ---------------------------------------------------------------------------
# Shared recorder arg handling (dual.main + the socket supervisor route through
# _apply_recorder_args so interactive/category handling can never drift).
# ---------------------------------------------------------------------------


def test_apply_recorder_args_normalizes_valid_category(tmp_path, monkeypatch):
    import argparse

    import onoats.dual as dual_mod

    # Configure a real category set (uncategorized is deliberately unlockable).
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("ONOATS_CATEGORIES", "work,personal")

    ns = argparse.Namespace(interactive=False, category="Work", live_terminal=False)
    assert dual_mod._apply_recorder_args(ns) is None
    assert ns.category == "work"  # normalized in place


def test_apply_recorder_args_rejects_invalid_category_on_stderr(
    tmp_path, monkeypatch, capsys
):
    import argparse

    import onoats.dual as dual_mod

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("ONOATS_CATEGORIES", raising=False)

    ns = argparse.Namespace(
        interactive=False, category="definitely-not-a-category", live_terminal=False
    )
    rc = dual_mod._apply_recorder_args(ns)
    assert rc == 1
    captured = capsys.readouterr()
    assert "Error:" in captured.err  # stderr, not stdout
    assert captured.out == ""


def test_dual_main_invalid_category_short_circuits_before_run(
    tmp_path, monkeypatch, capsys
):
    """dual.main routes through _apply_recorder_args: an invalid category returns
    rc=1 without ever entering run_onoats_dual."""
    import onoats.dual as dual_mod

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("ONOATS_CATEGORIES", raising=False)

    def _boom(*_a, **_k):
        raise AssertionError("run_onoats_dual must not run on an invalid category")

    monkeypatch.setattr(dual_mod.asyncio, "run", _boom)
    rc = dual_mod.main(["--category", "definitely-not-a-category"])
    assert rc == 1
    assert "Error:" in capsys.readouterr().err


# --- Diarization preflight at the run_onoats_dual entrypoint -----------------


def _diar_dual_env(monkeypatch, tmp_path, *, flag):
    from onoats import dual as dual_mod
    from onoats import runtime

    monkeypatch.setenv("ONOATS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ONOATS_DIARIZATION", flag)
    monkeypatch.setenv("AUDIO_SOURCE", "portaudio")
    order: list[str] = []
    monkeypatch.setattr(
        dual_mod, "_acquire_instance_lock", lambda d: order.append("lock")
    )

    def _boom(name):
        def _f(*a, **k):
            order.append(name)
            raise RuntimeError(f"{name} reached")

        return _f

    monkeypatch.setattr(dual_mod, "_build_portaudio_transports", _boom("portaudio"))
    monkeypatch.setattr(dual_mod, "_build_socket_transports", _boom("socket"))
    return dual_mod, runtime, order


def test_run_onoats_dual_diarization_preflight_failure_is_rc1_before_capture(
    monkeypatch, tmp_path
):
    dual_mod, runtime, order = _diar_dual_env(monkeypatch, tmp_path, flag="1")

    def _preflight(cfg):
        order.append("preflight")
        raise runtime.DiarizationPreflightError("diarization: extra missing")

    monkeypatch.setattr(runtime, "_preflight_diarization", _preflight)
    rc = dual_mod.main([])
    assert rc == 1
    assert order == ["lock", "preflight"]  # no transport builder / PortAudio call


def test_run_onoats_dual_flag_off_never_calls_preflight(monkeypatch, tmp_path):
    dual_mod, runtime, order = _diar_dual_env(monkeypatch, tmp_path, flag="0")
    monkeypatch.setattr(
        runtime,
        "_preflight_diarization",
        lambda cfg: pytest.fail("preflight must not run with the flag off"),
    )
    with pytest.raises(RuntimeError, match="portaudio reached"):
        dual_mod.main([])
    assert order == ["lock", "portaudio"]


def test_single_input_logs_ignored_warning_once(monkeypatch, tmp_path):
    from loguru import logger

    from onoats import __main__ as main_mod

    monkeypatch.setenv("ONOATS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ONOATS_DIARIZATION", "1")
    monkeypatch.setattr(main_mod, "_acquire_instance_lock", lambda d: None)
    records: list[str] = []
    sink = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        # Stop right after the flag check: the next import-heavy step is the
        # native deps; force a controlled failure there.
        import onoats.config as cfgmod

        orig = cfgmod.load_config
        calls = {"n": 0}

        def _load():
            calls["n"] += 1
            return orig()

        monkeypatch.setattr(cfgmod, "load_config", _load)
        from pipecat.audio.vad import silero

        monkeypatch.setattr(
            silero,
            "SileroVADAnalyzer",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")),
            raising=False,
        )
        with pytest.raises(Exception):
            asyncio.run(main_mod.run_onoats())
    finally:
        logger.remove(sink)
    ignored = [r for r in records if "ignored in single-input" in r]
    assert len(ignored) == 1


# ---------------------------------------------------------------------------
# Phase 1 (them-channel diarization): hoisted `_preflight_diarization` in
# `run_onoats_dual` — after `_acquire_instance_lock`, before PortAudio opens /
# socket transports are built; never called when the flag is off.
# ---------------------------------------------------------------------------


class _Stop(Exception):
    """Sentinel raised by the builder spies to cut run_onoats_dual short."""


def _dual_spies(monkeypatch, tmp_path, *, preflight_raises: bool):
    import onoats.dual as dual_mod
    from onoats import runtime
    from onoats.config import audio_devices
    from onoats.runtime import SttPreflightError

    calls: list[str] = []

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("ONOATS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STT_SERVICE", "websocket")

    def _lock(*_a, **_k):
        calls.append("lock")

    def _preflight(*_a, **_k):
        calls.append("preflight")
        if preflight_raises:
            raise SttPreflightError("diarization: weights not downloaded")

    def _builder(name):
        def _b(*_a, **_k):
            calls.append(name)
            raise _Stop

        return _b

    monkeypatch.setattr(dual_mod, "_acquire_instance_lock", _lock)
    monkeypatch.setattr(runtime, "_preflight_diarization", _preflight)
    monkeypatch.setattr(dual_mod, "_build_socket_transports", _builder("socket"))
    monkeypatch.setattr(dual_mod, "_build_portaudio_transports", _builder("portaudio"))
    monkeypatch.setattr(
        audio_devices, "select_dual_input_devices", _builder("select_devices")
    )
    return dual_mod, calls


@pytest.mark.parametrize("source", ["portaudio", "socket"])
def test_dual_diarization_preflight_failure_blocks_capture(
    source, tmp_path, monkeypatch
):
    from onoats.runtime import SttPreflightError

    monkeypatch.setenv("AUDIO_SOURCE", source)
    monkeypatch.setenv("ONOATS_DIARIZATION", "1")
    dual_mod, calls = _dual_spies(monkeypatch, tmp_path, preflight_raises=True)

    with pytest.raises(SttPreflightError):  # mapped to rc=1 by dual.main
        asyncio.run(dual_mod.run_onoats_dual(data_dir=tmp_path / "data"))

    assert calls == ["lock", "preflight"], (
        "no transport builder / select_dual_input_devices may run once "
        f"preflight fails; saw {calls}"
    )


@pytest.mark.parametrize(
    ("source", "builder"), [("portaudio", "portaudio"), ("socket", "socket")]
)
def test_dual_diarization_lock_then_preflight_precede_capture(
    source, builder, tmp_path, monkeypatch
):
    monkeypatch.setenv("AUDIO_SOURCE", source)
    monkeypatch.setenv("ONOATS_DIARIZATION", "1")
    dual_mod, calls = _dual_spies(monkeypatch, tmp_path, preflight_raises=False)

    with pytest.raises(_Stop):
        asyncio.run(dual_mod.run_onoats_dual(data_dir=tmp_path / "data"))

    assert calls[:3] == ["lock", "preflight", builder], calls


@pytest.mark.parametrize("source", ["portaudio", "socket"])
def test_dual_diarization_flag_off_never_preflights(source, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIO_SOURCE", source)
    monkeypatch.delenv("ONOATS_DIARIZATION", raising=False)
    dual_mod, calls = _dual_spies(monkeypatch, tmp_path, preflight_raises=True)

    with pytest.raises(_Stop):
        asyncio.run(dual_mod.run_onoats_dual(data_dir=tmp_path / "data"))

    assert "preflight" not in calls
    assert calls[0] == "lock"


# ---------------------------------------------------------------------------
# Phase 7 (them-channel diarization): feeder/labeler wiring in the system arm
# ---------------------------------------------------------------------------
#
# ``_build_dual_pipeline(..., diarization=<bool>, diarizer_factory=<stub>)``
# inserts ``DiarizationFeeder`` IMMEDIATELY BEFORE ``system_stt`` and
# ``DiarizationLabeler`` after the system ``SourceTagger`` — system arm only.
# The mic arm is identical flag on/off. No test here ever imports ``mlx_audio``;
# the model is always a stub injected via ``diarizer_factory`` (invoked on the
# worker thread).


class _FakeInput(FrameProcessor):
    """Stand-in for ``transport.input()`` (structure-only builds)."""

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)


class _FakeTransport:
    def __init__(self):
        self._input = _FakeInput()

    def input(self):
        return self._input


@pytest.fixture
def captured_arms(monkeypatch):
    """Capture the ``(mic_arm, system_arm)`` lists handed to ``ParallelPipeline``."""
    import pipecat.pipeline.parallel_pipeline as pp_mod

    captured: dict[str, list] = {}
    real = pp_mod.ParallelPipeline

    def spy(*arms, **kw):
        captured["mic"], captured["system"] = list(arms[0]), list(arms[1])
        return real(*arms, **kw)

    monkeypatch.setattr(pp_mod, "ParallelPipeline", spy)
    return captured


def _build_structure(builder, captured, *, diarization, two_paths=None, **extra):
    """Build the dual pipeline (structure only) and return ``(mic_arm, system_arm, sys_stt)``."""
    from onoats.dual import _build_dual_pipeline, _build_socket_transports

    if builder == "socket":
        _b, mic_path, system_path = two_paths
        mic_t, system_t, _ml, _sl = _build_socket_transports(
            _cfg(mic_path, system_path)
        )
    else:  # "portaudio": the default-path shape, without opening PortAudio
        mic_t, system_t = _FakeTransport(), _FakeTransport()
    system_stt = _MarkerSTT("system")
    _build_dual_pipeline(
        mic_t,
        system_t,
        _PassThroughVAD(),
        _PassThroughVAD(),
        _MarkerSTT("mic"),
        system_stt,
        _NoopSilenceDetector(),
        _NoopSilenceDetector(),
        call_id="test-diar-struct",
        diarization=diarization,
        diarizer_factory=_stub_factory if diarization else None,
        **extra,
    )
    return captured["mic"], captured["system"], system_stt


# ---- stub diarizer model (matches the surface DiarizerWorker uses) ----------


class _StubModel:
    """Quacks like the mlx_audio diarizer: 160-sample rows, slot 0 always active."""

    sample_rate = 16000

    def __init__(self, raise_in_feed: bool = False):
        import types

        self.config = types.SimpleNamespace(
            num_speakers=8,
            output_subsampling_factor=1,
            processor_config=types.SimpleNamespace(hop_length=160),
        )
        self._raise = raise_in_feed
        self._total = 0
        self._emitted = 0

    def set_streaming_config(self, preset):
        self.preset = preset

    def init_streaming_state(self):
        import types

        return types.SimpleNamespace(frames_processed=0)

    def feed(self, audio, state, sample_rate=16000, final=False):
        import types

        import numpy as np

        if self._raise:
            raise RuntimeError("stub feed() boom")
        self._total += len(audio)
        rows = self._total // 160 - self._emitted
        if final:
            rows += 1 if self._total % 160 else 0
        rows = max(rows, 0)
        self._emitted += rows
        probs = np.zeros((rows, 8), dtype=np.float32)
        if rows:
            probs[:, 0] = 0.99
        state.frames_processed = self._emitted
        return types.SimpleNamespace(speaker_probs=probs), state


def _stub_factory():
    return _StubModel()


def _raising_factory():
    raise RuntimeError("stub factory boom")


def _raising_feed_factory():
    return _StubModel(raise_in_feed=True)


# ---- structure: mic arm unchanged, feeder adjacent, system arm only ----------


@pytest.mark.parametrize("builder", ["portaudio", "socket"])
def test_mic_arm_processor_types_identical_flag_on_off(
    builder, two_paths, captured_arms
):
    mic_off, _s_off, _ = _build_structure(
        builder, captured_arms, diarization=False, two_paths=two_paths
    )
    types_off = [type(p) for p in mic_off]
    mic_on, _s_on, _ = _build_structure(
        builder, captured_arms, diarization=True, two_paths=two_paths
    )
    types_on = [type(p) for p in mic_on]
    assert types_off == types_on
    from onoats.processors.diarization_feeder import DiarizationFeeder
    from onoats.processors.diarization_labeler import DiarizationLabeler

    assert not any(
        isinstance(p, (DiarizationFeeder, DiarizationLabeler)) for p in mic_on
    )


@pytest.mark.parametrize("builder", ["portaudio", "socket"])
def test_flag_off_system_arm_has_no_diarization_processors(
    builder, two_paths, captured_arms
):
    from onoats.processors.diarization_feeder import DiarizationFeeder
    from onoats.processors.diarization_labeler import DiarizationLabeler

    _m, system, _ = _build_structure(
        builder, captured_arms, diarization=False, two_paths=two_paths
    )
    assert not any(
        isinstance(p, (DiarizationFeeder, DiarizationLabeler)) for p in system
    )


@pytest.mark.parametrize("builder", ["portaudio", "socket"])
def test_flag_on_feeder_immediately_before_system_stt(
    builder, two_paths, captured_arms
):
    from onoats.processors.diarization_feeder import DiarizationFeeder
    from onoats.processors.diarization_labeler import DiarizationLabeler
    from onoats.processors.source_tagger import SourceTagger

    _m, system, system_stt = _build_structure(
        builder, captured_arms, diarization=True, two_paths=two_paths
    )
    i = system.index(system_stt)
    assert isinstance(system[i - 1], DiarizationFeeder)
    assert sum(isinstance(p, DiarizationFeeder) for p in system) == 1
    assert sum(isinstance(p, DiarizationLabeler) for p in system) == 1
    # Labeler is downstream of the system SourceTagger (which follows the STT).
    assert isinstance(system[i + 1], SourceTagger)
    assert isinstance(system[i + 2], DiarizationLabeler)
    # Feeder + labeler share one session timeline.
    assert system[i - 1]._timeline is system[i + 2]._timeline


def test_feeder_adjacent_to_stt_after_audio_dump(
    two_paths, captured_arms, monkeypatch, tmp_path
):
    """With ONOATS_AUDIO_DUMP on, the dump processor precedes the feeder."""
    from onoats.processors import audio_dump
    from onoats.processors.audio_dump import RawAudioDumpProcessor
    from onoats.processors.diarization_feeder import DiarizationFeeder

    monkeypatch.setenv("ONOATS_AUDIO_DUMP", "1")
    monkeypatch.setattr(audio_dump, "resolve_dump_dir", lambda: tmp_path)
    _m, system, system_stt = _build_structure(
        "portaudio", captured_arms, diarization=True
    )
    i = system.index(system_stt)
    assert isinstance(system[i - 1], DiarizationFeeder)
    assert isinstance(system[i - 2], RawAudioDumpProcessor)


def test_diarizer_factory_not_invoked_at_build_time(two_paths, captured_arms):
    """The factory runs on the worker thread (after StartFrame), never at build."""
    from onoats.dual import _build_dual_pipeline

    calls: list[str] = []

    def factory():
        calls.append("called")
        return _StubModel()

    _build_dual_pipeline(
        _FakeTransport(),
        _FakeTransport(),
        _PassThroughVAD(),
        _PassThroughVAD(),
        _MarkerSTT("mic"),
        _MarkerSTT("system"),
        _NoopSilenceDetector(),
        _NoopSilenceDetector(),
        diarization=True,
        diarizer_factory=factory,
    )
    assert calls == []


# ---- behaviour: me/them x flag on/off matrix, through the built arms --------

_ATTRS = ("onoats_speakers", "onoats_overlap")
_SEG_ATTRS = ("onoats_segment_start_sample", "onoats_segment_end_sample")
_FRAME_SAMPLES = 160


class _StampingSTT(FrameProcessor):
    """Marker STT that mirrors production stamping: only ``stamp=True`` (them arm,
    flag on in production) sets ``onoats_segment_*``. It never sets
    ``onoats_speakers`` / ``onoats_overlap`` — only the labeler may.
    """

    def __init__(self, *, stamp: bool):
        super().__init__()
        self._stamp = stamp
        self._samples = 0

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import InputAudioRawFrame, StartFrame

        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self._samples = 0
        if isinstance(frame, InputAudioRawFrame):
            start = self._samples
            self._samples += len(frame.audio) // 2
            out = TranscriptionFrame(
                text=f"t:{start}", user_id="", timestamp="2026-01-01T00:00:00Z"
            )
            if self._stamp:
                out.onoats_segment_start_sample = start
                out.onoats_segment_end_sample = self._samples
            await self.push_frame(out, direction)
            return
        await self.push_frame(frame, direction)


class _Recorder(FrameProcessor):
    """Pass-through that records TranscriptionFrames (the ``TranscriptBuffer`` slot)."""

    def __init__(self):
        super().__init__()
        self.transcripts: list = []

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            self.transcripts.append(frame)
        await self.push_frame(frame, direction)


async def _feed_blocks(marker: int, n_frames: int = 4):
    pcm = pcm_from_samples([marker] + [0] * (_FRAME_SAMPLES - 1))

    async def feed(writer: asyncio.StreamWriter):
        writer.write(make_header())
        for seq in range(n_frames):
            writer.write(make_frame(seq, pcm))
        await writer.drain()
        await asyncio.sleep(WAIT_TIMEOUT)

    return feed


async def _run_socket_dual(two_paths, *, diarization, factory, buffer=None):
    """Drive the real ``_build_dual_pipeline`` over two sockets; return (down, up)."""
    from onoats.dual import _build_dual_pipeline, _build_socket_transports

    _base, mic_path, system_path = two_paths
    mic_t, system_t, _ml, _sl = _build_socket_transports(_cfg(mic_path, system_path))
    pipeline = _build_dual_pipeline(
        mic_t,
        system_t,
        _PassThroughVAD(),
        _PassThroughVAD(),
        _StampingSTT(stamp=False),  # mic: stamp kwarg is False in production
        _StampingSTT(stamp=diarization),  # system: stamped only with flag on
        buffer if buffer is not None else _Recorder(),
        _NoopSilenceDetector(),
        call_id="test-diar-matrix",
        diarization=diarization,
        diarizer_factory=factory if diarization else None,
    )
    mic_server = _SocketWriterServer(mic_path, await _feed_blocks(MIC_MARKER))
    system_server = _SocketWriterServer(system_path, await _feed_blocks(SYSTEM_MARKER))
    await mic_server.start()
    await system_server.start()
    try:
        return await asyncio.wait_for(
            run_test(pipeline, frames_to_send=[SleepFrame(0.5)]),
            timeout=WAIT_TIMEOUT * 2,
        )
    finally:
        await mic_server.aclose()
        await system_server.aclose()


def _split(down):
    ts = [f for f in down if isinstance(f, TranscriptionFrame)]
    return (
        [f for f in ts if f.user_id == "me"],
        [f for f in ts if f.user_id == "them"],
    )


def _has_any(frame, names):
    return [n for n in names if hasattr(frame, n)]


@pytest.mark.anyio
async def test_matrix_me_flag_off_never_carries_diarization_attrs(two_paths):
    down, _up = await _run_socket_dual(two_paths, diarization=False, factory=None)
    me, them = _split(down)
    assert me and them
    for f in me:
        assert _has_any(f, _ATTRS + _SEG_ATTRS) == []


@pytest.mark.anyio
async def test_matrix_me_flag_on_never_carries_diarization_attrs(two_paths):
    down, _up = await _run_socket_dual(
        two_paths, diarization=True, factory=_stub_factory
    )
    me, them = _split(down)
    assert me and them
    for f in me:
        assert _has_any(f, _ATTRS + _SEG_ATTRS) == []


@pytest.mark.anyio
async def test_matrix_them_flag_off_never_carries_any_attr(two_paths):
    """Req 8: flag off => ``them`` frames carry none of speakers/overlap/segment_*."""
    down, _up = await _run_socket_dual(two_paths, diarization=False, factory=None)
    _me, them = _split(down)
    assert them
    for f in them:
        assert _has_any(f, _ATTRS + _SEG_ATTRS) == []


@pytest.mark.anyio
async def test_matrix_them_flag_on_carries_attrs_only_via_labeler(
    two_paths, monkeypatch
):
    """Flag on => ``them`` frames are labelled; the labeler is the only writer.

    A spy on the system ``SourceTagger``'s successor proves the STT/tagger never
    set the labeler-owned attributes: they appear after the labeler only.
    """
    from onoats.processors.diarization_labeler import DiarizationLabeler

    seen_before_labeler: list = []
    real_process = DiarizationLabeler.process_frame

    async def spy(self, frame, direction):
        if isinstance(frame, TranscriptionFrame):
            seen_before_labeler.append(_has_any(frame, _ATTRS))
        return await real_process(self, frame, direction)

    monkeypatch.setattr(DiarizationLabeler, "process_frame", spy)

    down, _up = await _run_socket_dual(
        two_paths, diarization=True, factory=_stub_factory
    )
    _me, them = _split(down)
    assert them
    assert seen_before_labeler, "labeler never saw a transcript"
    assert all(a == [] for a in seen_before_labeler), seen_before_labeler
    for f in them:
        assert isinstance(f.onoats_speakers, list) and f.onoats_speakers
        assert f.onoats_overlap is False
        assert f.onoats_segment_end_sample > f.onoats_segment_start_sample >= 0


# ---- behaviour: wired-arm degrade never drops a transcript ------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "factory", [_raising_factory, _raising_feed_factory], ids=["factory", "feed"]
)
async def test_wired_arm_degrade_still_delivers_transcripts(two_paths, factory):
    from pipecat.frames.frames import ErrorFrame

    buffer = _Recorder()
    down, up = await _run_socket_dual(
        two_paths, diarization=True, factory=factory, buffer=buffer
    )
    _me, them = _split(down)
    # Every them transcript reached the TranscriptBuffer slot, unlabelled.
    assert them, "degraded diarizer dropped `them` transcripts"
    assert {f.text for f in them} == {
        f.text for f in buffer.transcripts if f.user_id == "them"
    }
    for f in them:
        assert _has_any(f, _ATTRS) == []
    # Session continued: mic transcripts unaffected; no ErrorFrame either way.
    assert _me
    assert not [f for f in list(down) + list(up) if isinstance(f, ErrorFrame)]


@pytest.mark.anyio
async def test_wired_arm_degrade_reaches_real_transcript_buffer(two_paths, tmp_path):
    """Same degrade, with the real ``TranscriptBuffer`` as the sink."""
    from onoats.processors.transcript_buffer import TranscriptBuffer

    buffer = TranscriptBuffer(data_dir=tmp_path / "data", use_frame_source=True)
    seen: list = []
    real = buffer.process_frame

    async def spy(frame, direction):
        if isinstance(frame, TranscriptionFrame):
            seen.append(frame)
        return await real(frame, direction)

    buffer.process_frame = spy  # type: ignore[method-assign]
    await _run_socket_dual(
        two_paths, diarization=True, factory=_raising_factory, buffer=buffer
    )
    assert [f for f in seen if f.user_id == "them"]
