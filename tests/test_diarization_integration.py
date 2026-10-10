"""End-to-end them-channel diarization on a real clip and the real model.

Local-only, NOT a CI gate (CI is MLX-free). Skips unless all of these hold:
``mlx_audio`` is importable (the ``diarization`` extra), the model weights are
in the local HF cache (nothing here downloads), and ``ONOATS_DIARIZATION_FIXTURE``
points at a 16 kHz mono PCM16 WAV of a conversation with at least two clearly
separated speakers. No clip is committed: the plan's committed-clip route needs a
redistributable recording and none is available, so this is the env-path fallback.

Run (from a checkout)::

    ONOATS_DIARIZATION_FIXTURE=/path/to/clip_16k.wav \\
        uv run --extra diarization pytest -m diarization_integration

Optional: ``ONOATS_DIARIZATION_FIXTURE_OFFSET_SEC`` (default 0) and
``ONOATS_DIARIZATION_FIXTURE_SEC`` (default 90) select the slice that is fed.

Real pieces: ``DiarizationFeeder`` -> real ``DiarizerWorker`` loading the real model
through the production loader -> ``DiarizationTimeline`` -> ``DiarizationLabeler``,
inside a real pipecat ``PipelineTask``. Faked piece: the STT. ``TranscriptionFrame``s
are stamped with their sample span directly (what ``WebSocketSTTService`` does from
VAD), so there is no decoder or socket; the span stamping is pinned by
``tests/test_websocket_stt_stamping.py``.

The audio is fed faster than real time, so the labeler's hold is widened: a frame
waits for coverage instead of expiring unlabelled while the worker catches up. Metal
inference is not bit-deterministic, so there is no exact-label-sequence assertion.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
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
from onoats.processors.diarization_feeder import DiarizationFeeder
from onoats.processors.diarization_labeler import DiarizationLabeler
from onoats.stt import diarization_model
from onoats.stt.diarization_timeline import DiarizationTimeline

pytestmark = pytest.mark.diarization_integration

FIXTURE_ENV = "ONOATS_DIARIZATION_FIXTURE"
OFFSET_ENV = "ONOATS_DIARIZATION_FIXTURE_OFFSET_SEC"
SECONDS_ENV = "ONOATS_DIARIZATION_FIXTURE_SEC"
DEFAULT_SECONDS = 90.0

CHUNK_SAMPLES = 320  # 20 ms, the live capture chunk
UTTERANCE_SEC = 4.0
HOLD_SEC = 120.0  # wider than a fast feed needs; see the module docstring
RUN_TIMEOUT_SEC = 300.0
LABEL = re.compile(r"^S([0-9]+)$")


@pytest.fixture(scope="module", autouse=True)
def _purge_mlx_audio_after_module():
    """The real model import must not leak into other test modules: several
    assert ``mlx_audio`` is absent from ``sys.modules`` to pin the import-free
    guarantee."""
    yield
    for name in [
        n for n in sys.modules if n == "mlx_audio" or n.startswith("mlx_audio.")
    ]:
        del sys.modules[name]


class _Collector(FrameProcessor):
    """Terminal sink: records what reaches the end of the them arm."""

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


def _require_stack() -> None:
    try:
        import mlx_audio.vad  # noqa: F401
    except ImportError as exc:
        pytest.skip(
            f"mlx_audio not importable (install the `diarization` extra): {exc}"
        )
    if not diarization_model.weights_cached():
        pytest.skip(f"{diarization_model.REPO_ID} weights not in the local HF cache")


def _fixture_pcm() -> bytes:
    raw = os.environ.get(FIXTURE_ENV)
    if not raw:
        pytest.skip(f"{FIXTURE_ENV} not set (path to a 16 kHz mono PCM16 WAV)")
    path = Path(raw).expanduser()
    if not path.is_file():
        pytest.skip(f"{FIXTURE_ENV} is not a file: {path}")
    with wave.open(str(path), "rb") as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (
            PIPELINE_SAMPLE_RATE,
            1,
            2,
        ), f"{path} must be {PIPELINE_SAMPLE_RATE} Hz mono PCM16"
        offset = int(float(os.environ.get(OFFSET_ENV, "0")) * PIPELINE_SAMPLE_RATE)
        length = int(
            float(os.environ.get(SECONDS_ENV, DEFAULT_SECONDS)) * PIPELINE_SAMPLE_RATE
        )
        wav.setpos(min(offset, wav.getnframes()))
        pcm = wav.readframes(length)
    # whole chunks only, so feeder and stamps agree on the sample count
    usable = len(pcm) // (CHUNK_SAMPLES * 2) * (CHUNK_SAMPLES * 2)
    assert usable >= int(30 * PIPELINE_SAMPLE_RATE) * 2, (
        "fixture slice is shorter than 30 s; need room for several utterances"
    )
    return pcm[:usable]


def _run_pipeline(pcm: bytes) -> tuple[DiarizationTimeline, _Collector, int]:
    chunk_bytes = CHUNK_SAMPLES * 2
    chunks = [pcm[i : i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]
    total_samples = len(pcm) // 2

    timeline = DiarizationTimeline()
    # The default queue bound (3000 chunks) is a live-capture backpressure cap; a
    # faster-than-real-time feed would overflow it and punch GAPs into coverage.
    feeder = DiarizationFeeder(timeline, queue_max=len(chunks) + 1)
    labeler = DiarizationLabeler(timeline, hold_sec=HOLD_SEC)
    sink = _Collector()

    frames: list[Frame] = [
        InputAudioRawFrame(audio=c, sample_rate=PIPELINE_SAMPLE_RATE, num_channels=1)
        for c in chunks
    ]
    step = int(UTTERANCE_SEC * PIPELINE_SAMPLE_RATE)
    for n, start in enumerate(range(0, total_samples - step + 1, step)):
        frame = TranscriptionFrame(text=f"utt-{n}", user_id="them", timestamp="t")
        frame.onoats_segment_start_sample = start
        frame.onoats_segment_end_sample = start + step
        frames.append(frame)
    frames.append(EndFrame())

    async def _go() -> None:
        task = PipelineTask(
            Pipeline([feeder, labeler, sink]),
            params=PipelineParams(
                audio_in_sample_rate=PIPELINE_SAMPLE_RATE, allow_interruptions=False
            ),
            cancel_on_idle_timeout=False,
        )
        await task.queue_frames(frames)
        await asyncio.wait_for(
            PipelineRunner(handle_sigint=False).run(task), RUN_TIMEOUT_SEC
        )

    asyncio.run(_go())
    return timeline, sink, total_samples


def test_them_utterances_get_distinct_speaker_labels_on_a_real_clip():
    _require_stack()
    pcm = _fixture_pcm()

    timeline, sink, total_samples = _run_pipeline(pcm)

    assert not timeline.is_degraded, "diarizer degraded on a healthy clip"
    assert timeline.is_flushed
    assert not sink.errors, [e.error for e in sink.errors]
    assert timeline.coverage_high_water() == total_samples

    # fail open: every utterance is delivered, labelled or not
    expected = int(total_samples // (UTTERANCE_SEC * PIPELINE_SAMPLE_RATE))
    assert [f.text for f in sink.transcripts] == [f"utt-{n}" for n in range(expected)]

    labelled = [f for f in sink.transcripts if getattr(f, "onoats_speakers", None)]
    assert labelled, "no utterance was labelled"

    slots = timeline._slot_count  # model num_speakers: upper bound for S<n>
    assert slots, "worker never published the model's slot count"
    seen: set[str] = set()
    for f in labelled:
        assert isinstance(f.onoats_overlap, bool)
        for label in f.onoats_speakers:
            m = LABEL.match(label)
            assert m, f"{label!r} is not S<n>"
            assert 1 <= int(m.group(1)) <= slots, f"{label} beyond {slots} slots"
            seen.add(label)
    assert len(seen) >= 2, f"only {sorted(seen)} across {len(labelled)} utterances"
