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
