"""Live smoke: a real speech WAV through the REAL ``WebSocketSTTService`` to a REAL server.

Run: ``ONOATS_STT_FIXTURE=/path/clip_16k.wav uv run pytest -m stt_ws_integration -s -q tests/test_stt_ws_integration.py``

Local-only, NOT a CI gate (CI has no STT server). Skips unless
``ONOATS_STT_FIXTURE`` (fallback ``ONOATS_DIARIZATION_FIXTURE``) names a 16 kHz
mono PCM16 WAV AND the resolved STT endpoint answers a handshake within a short
timeout. No audio is committed.

Optional: ``ONOATS_STT_FIXTURE_OFFSET_SEC`` (default 0) and
``ONOATS_STT_FIXTURE_SEC`` (default 45) select the slice that is fed.

Real pieces: the endpoint is resolved exactly as the recorder does
(``runtime._resolve_stt_ws_target(runtime._ws_env(load_config()))``, so
``STT_WS_URI`` > ``STT_WS_SOCKET`` > ``STT_WS_HOST``+``STT_WS_PORT`` > the runtime
default), then ``VADProcessor`` with the production ``SileroVADAnalyzer`` ->
``WebSocketSTTService`` inside a real ``PipelineTask``. Nothing is faked. The
transcript text is model output, so the assertions are structural (no fatal
error, some transcripts, a word-count floor), never exact text.
"""

from __future__ import annotations

import asyncio
import os
import wave
from pathlib import Path

import pytest
from pipecat.frames.frames import (
    EndFrame,
    ErrorFrame,
    Frame,
    InputAudioRawFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from onoats.config.audio_devices import PIPELINE_SAMPLE_RATE

pytestmark = pytest.mark.stt_ws_integration

FIXTURE_ENVS = ("ONOATS_STT_FIXTURE", "ONOATS_DIARIZATION_FIXTURE")
OFFSET_ENV = "ONOATS_STT_FIXTURE_OFFSET_SEC"
SECONDS_ENV = "ONOATS_STT_FIXTURE_SEC"
DEFAULT_SECONDS = 45.0

CHUNK_SAMPLES = 320  # 20 ms, the live capture chunk
PACE_SPEEDUP = 4.0  # feed at real-time/4: fast, but the server keeps up
TRAILING_SILENCE_SEC = 3.0  # > the VAD stop window, so the last span closes
HANDSHAKE_TIMEOUT_SEC = 3.0
RUN_TIMEOUT_SEC = 300.0
WORDS_PER_SPEECH_SEC_FLOOR = 20 / 45  # conservative: >= 20 words per 45 s


class _Collector(FrameProcessor):
    """Terminal sink: records transcripts and errors."""

    def __init__(self) -> None:
        super().__init__()
        self.transcripts: list[TranscriptionFrame] = []
        self.errors: list[ErrorFrame] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            self.transcripts.append(frame)
        elif isinstance(frame, ErrorFrame):
            self.errors.append(frame)
        await self.push_frame(frame, direction)


def _fixture_pcm() -> tuple[bytes, float]:
    raw = next((os.environ[n] for n in FIXTURE_ENVS if os.environ.get(n)), None)
    if not raw:
        pytest.skip(f"{' / '.join(FIXTURE_ENVS)} not set (16 kHz mono PCM16 WAV)")
    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(f"STT fixture is not a file: {path}")
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
            PIPELINE_SAMPLE_RATE,
            1,
            2,
        ), f"{path} must be {PIPELINE_SAMPLE_RATE} Hz mono PCM16"
        offset = int(float(os.environ.get(OFFSET_ENV, "0")) * PIPELINE_SAMPLE_RATE)
        seconds = float(os.environ.get(SECONDS_ENV, DEFAULT_SECONDS))
        wav.setpos(min(offset, wav.getnframes()))
        pcm = wav.readframes(int(seconds * PIPELINE_SAMPLE_RATE))
    usable = len(pcm) // (CHUNK_SAMPLES * 2) * (CHUNK_SAMPLES * 2)
    assert usable >= int(10 * PIPELINE_SAMPLE_RATE) * 2, (
        "fixture slice is shorter than 10 s; check the offset/length"
    )
    return pcm[:usable], usable / 2 / PIPELINE_SAMPLE_RATE


def _resolve_endpoint() -> dict[str, object]:
    """The recorder's own endpoint resolution (env + config.toml + default)."""
    from onoats.config import load_config
    from onoats.runtime import _resolve_stt_ws_target, _ws_env

    return _resolve_stt_ws_target(_ws_env(load_config()), warn_on_cleartext=False)


def _require_server(kwargs: dict[str, object]) -> None:
    from stt_server.client import TranscriptionClient

    client = TranscriptionClient(
        socket_path=kwargs.get("socket_path"),
        host=kwargs.get("host"),
        port=kwargs.get("port"),
        uri=kwargs.get("uri"),
        auth_token=kwargs.get("auth_token"),
    )

    async def _probe() -> None:
        try:
            await asyncio.wait_for(client.connect(), HANDSHAKE_TIMEOUT_SEC)
        finally:
            await client.close()

    try:
        asyncio.run(_probe())
    except Exception as exc:  # unreachable, auth, wrong service: all a skip here
        from onoats._redact import safe_exc_text

        # Never format the raw kwargs/exception: they can carry the auth token
        # or URI userinfo, and a skip reason lands in CI/terminal output.
        pytest.skip(
            f"no STT server answered a handshake ({_describe(kwargs)}): "
            f"{type(exc).__name__}: {safe_exc_text(exc)}"
        )


def _describe(kwargs: dict[str, object]) -> str:
    from onoats.runtime import _display_target

    return _display_target(kwargs)


def _run_pipeline(
    pcm: bytes, kwargs: dict[str, object]
) -> tuple[_Collector, list[str]]:
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.audio.vad_processor import VADProcessor

    from onoats.stt.websocket_stt_service import WebSocketSTTService

    chunk_bytes = CHUNK_SAMPLES * 2
    chunks = [pcm[i : i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]
    silence = bytes(chunk_bytes)
    n_silence = int(TRAILING_SILENCE_SEC * PIPELINE_SAMPLE_RATE / CHUNK_SAMPLES)
    chunk_sec = CHUNK_SAMPLES / PIPELINE_SAMPLE_RATE

    vad = VADProcessor(vad_analyzer=SileroVADAnalyzer(sample_rate=PIPELINE_SAMPLE_RATE))
    stt = WebSocketSTTService(language="en", **kwargs)
    sink = _Collector()

    async def _feed(task: PipelineTask) -> None:
        for audio in [*chunks, *([silence] * n_silence)]:
            await task.queue_frame(
                InputAudioRawFrame(
                    audio=audio, sample_rate=PIPELINE_SAMPLE_RATE, num_channels=1
                )
            )
            await asyncio.sleep(chunk_sec / PACE_SPEEDUP)
        await task.queue_frame(EndFrame())

    async def _go() -> None:
        task = PipelineTask(
            Pipeline([vad, stt, sink]),
            params=PipelineParams(
                audio_in_sample_rate=PIPELINE_SAMPLE_RATE, allow_interruptions=False
            ),
            cancel_on_idle_timeout=False,
        )
        feeder = asyncio.create_task(_feed(task))
        try:
            await asyncio.wait_for(
                PipelineRunner(handle_sigint=False).run(task), RUN_TIMEOUT_SEC
            )
        finally:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)

    asyncio.run(_go())
    return sink, [e.error for e in sink.errors]


def test_real_clip_is_transcribed_by_the_real_server():
    pcm, speech_sec = _fixture_pcm()
    kwargs = _resolve_endpoint()
    _require_server(kwargs)
    endpoint = _describe(kwargs)

    sink, errors = _run_pipeline(pcm, kwargs)

    text = " ".join(f.text.strip() for f in sink.transcripts if f.text.strip())
    words = len(text.split())
    print(f"\n[stt_ws_integration] endpoint: {endpoint}")
    print(f"[stt_ws_integration] {len(sink.transcripts)} transcripts, {words} words")
    print(f"[stt_ws_integration] transcript: {text}")

    assert not any(e.fatal for e in sink.errors), errors
    assert not errors, errors
    assert sink.transcripts, "no TranscriptionFrame came back"
    floor = int(speech_sec * WORDS_PER_SPEECH_SEC_FLOOR)
    assert words >= floor, f"only {words} words for {speech_sec:.0f} s (floor {floor})"
