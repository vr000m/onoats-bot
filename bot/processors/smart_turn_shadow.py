"""Read-only SmartTurn shadow observer for the dual-input pipeline.

Runs ``LocalSmartTurnAnalyzerV3`` alongside the existing VAD path
without changing commit behaviour. At each VAD-stopped event the
analyser is asked whether the turn looks complete; the verdict is
logged for offline comparison against the VAD-only baseline. No frame
is ever swallowed, modified, or held back — downstream STT continues
to commit on raw VAD exactly as before.

Per the dev plan in
``docs/dev_plans/20260420-design-whisper-websocket-server.md`` the
spike order is: prototype on ``me`` first, measure mid-turn
fragmentation against the 2026-04-21 corpus, then mirror to ``them``
and consider flipping the commit decision over to SmartTurn.

Gated by ``KODA_SMART_TURN_SHADOW=1`` so the analyser only loads when
explicitly enabled — keeps cold-start cost out of the default bot
path. When enabled, verdicts are mirrored to JSONL under
``<KODA_DATA_DIR>/shadow/verdicts/<YYYY-MM-DD>/<call_id>.jsonl`` so we
have durable evidence regardless of whether stdout was captured.

Concurrency model: ``append_audio`` and ``analyze_end_of_turn`` share
the analyser's internal audio buffer. Both are serialised through
``_analyse_lock`` so the executor thread iterating the buffer cannot
race with the event loop appending to it. ``started_at`` is captured
synchronously at the VAD-stopped event so a queued analyse cannot be
mislabelled with the next turn's timestamp.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

from pipecat.audio.turn.base_turn_analyzer import EndOfTurnState
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    StartFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


def smart_turn_shadow_enabled() -> bool:
    """Whether the shadow observer should be wired into the pipeline."""
    return os.environ.get("KODA_SMART_TURN_SHADOW", "").lower() in {"1", "true", "yes"}


def resolve_verdict_dir() -> Path:
    """Resolve today's verdict JSONL root with restrictive perms."""
    from shared.store import shadow_data_dir

    today = datetime.now().strftime("%Y-%m-%d")
    out = shadow_data_dir() / "verdicts" / today
    out.mkdir(parents=True, exist_ok=True)
    try:
        out.chmod(0o700)
    except OSError:
        pass
    return out


class SmartTurnShadowObserver(FrameProcessor):
    """Log what SmartTurn would have decided at every VAD-stopped event.

    One instance per branch; place after the branch's VADProcessor and
    before STT so plain VAD frames carry implicit branch identity from
    the pipeline arm. Forwards every frame untouched; never raises on
    analyser failure (logs and continues — the bot must not depend on
    shadow output).

    When ``call_id`` and ``verdict_dir`` are provided each verdict is
    also appended to ``<verdict_dir>/<call_id>_<source>.jsonl`` so the
    data survives even if stdout is not captured. Per-source files
    avoid byte-level interleaving when the two branches run
    concurrently.
    """

    def __init__(
        self,
        *,
        source: str,
        sample_rate: int,
        call_id: str | None = None,
        verdict_dir: Path | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._source = source
        # BaseTurnAnalyzer stores the constructor sample_rate as
        # _init_sample_rate but leaves _sample_rate=0 until
        # set_sample_rate fires from a StartFrame. Pass it here so
        # set_sample_rate's ``_init_sample_rate or sample_rate`` clause
        # picks it up.
        self._analyzer = LocalSmartTurnAnalyzerV3(sample_rate=sample_rate)
        self._configured_sample_rate = sample_rate
        self._in_speech = False
        self._turn_started_at: float | None = None
        # Single lock guards both append_audio and analyze_end_of_turn:
        # the analyser's _audio_buffer would otherwise be mutated by the
        # event loop while the executor thread iterates it.
        self._analyse_lock = asyncio.Lock()
        self._pending_task: asyncio.Task | None = None
        # While analyse holds the lock, audio frames buffer here instead
        # of waiting on the lock — otherwise the live pipeline serialises
        # behind ONNX inference (~50–200 ms). Cap at 10 s of audio
        # (320 KB at 16 kHz s16le) so a runaway / hung analyse can't
        # bloat memory; beyond the cap we drop frames from the analyser
        # only (the live pipeline still forwards them).
        self._pending_audio: list[tuple[bytes, bool]] = []
        self._pending_bytes = 0
        self._pending_audio_cap = 10 * 16000 * 2

        self._call_id = call_id
        self._jsonl_path: Path | None = None
        if call_id and verdict_dir is not None:
            try:
                verdict_dir.mkdir(parents=True, exist_ok=True)
                # Per-source path so concurrent appends from `me` and
                # `them` instances cannot interleave bytes.
                self._jsonl_path = verdict_dir / f"{call_id}_{source}.jsonl"
                logger.info(f"SmartTurnShadow[{source}]: persisting verdicts to {self._jsonl_path}")
            except Exception as exc:
                logger.warning(
                    f"SmartTurnShadow[{source}]: could not open verdict dir {verdict_dir}: {exc}"
                )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        try:
            if isinstance(frame, StartFrame):
                self._analyzer.set_sample_rate(
                    getattr(frame, "audio_in_sample_rate", self._configured_sample_rate)
                )
            elif isinstance(frame, InputAudioRawFrame):
                # Buffer-and-flush pattern. If analyse is in flight,
                # don't wait for the lock (that serialises live audio
                # ingestion with ONNX inference); buffer the bytes.
                # Otherwise acquire the lock briefly (uncontested ≈ μs),
                # drain any backlog in order, then append this frame.
                # `.locked()` -> `async with` is race-free in single-
                # threaded asyncio: an unlocked acquire returns without
                # yielding, so no concurrent task can take the lock
                # between the check and the acquire.
                if self._analyse_lock.locked():
                    if self._pending_bytes < self._pending_audio_cap:
                        self._pending_audio.append((frame.audio, self._in_speech))
                        self._pending_bytes += len(frame.audio)
                else:
                    async with self._analyse_lock:
                        if self._pending_audio:
                            for audio, was_speech in self._pending_audio:
                                self._analyzer.append_audio(audio, is_speech=was_speech)
                            self._pending_audio.clear()
                            self._pending_bytes = 0
                        self._analyzer.append_audio(frame.audio, is_speech=self._in_speech)
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                self._in_speech = True
                self._turn_started_at = time.monotonic()
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._in_speech = False
                # Capture started_at *now* — a queued analyse running
                # after the next VAD-started frame would otherwise read
                # the new turn's timestamp.
                started_at = self._turn_started_at
                self._pending_task = asyncio.create_task(self._shadow_analyse(started_at))
            elif isinstance(frame, (EndFrame, CancelFrame)):
                await self._drain_pending_task()
        except Exception as exc:
            logger.warning(f"SmartTurnShadow[{self._source}]: observe error: {exc}")

        await self.push_frame(frame, direction)

    async def _drain_pending_task(self) -> None:
        task = self._pending_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(task, timeout=1.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
        except Exception as exc:
            logger.warning(f"SmartTurnShadow[{self._source}]: pending task drain failed: {exc}")

    async def _shadow_analyse(self, started_at: float | None) -> None:
        async with self._analyse_lock:
            try:
                state, metrics = await self._analyzer.analyze_end_of_turn()
            except Exception as exc:
                logger.warning(
                    f"SmartTurnShadow[{self._source}]: analyse_end_of_turn failed: {exc}"
                )
                return
        verdict = (
            state.name
            if hasattr(state, "name")
            else ("COMPLETE" if state == EndOfTurnState.COMPLETE else "INCOMPLETE")
        )
        turn_secs = (time.monotonic() - started_at) if started_at is not None else None
        logger.info(
            f"smart_turn_shadow source={self._source} verdict={verdict} turn_secs={turn_secs:.2f}"
            if turn_secs is not None
            else f"smart_turn_shadow source={self._source} verdict={verdict}"
        )

        if self._jsonl_path is not None:
            self._append_jsonl(verdict, turn_secs, metrics)

    def _append_jsonl(self, verdict: str, turn_secs: float | None, metrics) -> None:
        record: dict = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "call_id": self._call_id,
            "source": self._source,
            "verdict": verdict,
            "turn_secs": turn_secs,
        }
        if metrics is not None:
            try:
                if hasattr(metrics, "__dict__"):
                    record["metrics"] = {k: v for k, v in vars(metrics).items() if _is_jsonable(v)}
                else:
                    record["metrics"] = str(metrics)
            except Exception:
                record["metrics"] = None
        try:
            line = (json.dumps(record) + "\n").encode("utf-8")
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            if nofollow:
                flags |= nofollow
            fd = os.open(str(self._jsonl_path), flags, 0o600)
            try:
                # Single write(2); POSIX guarantees atomicity for
                # O_APPEND writes <= PIPE_BUF (typically 4096 bytes).
                # Verdict records are well under that, but per-source
                # paths in __init__ already eliminate cross-branch
                # interleaving even for over-PIPE_BUF lines.
                os.write(fd, line)
            finally:
                os.close(fd)
        except Exception as exc:
            logger.warning(f"SmartTurnShadow[{self._source}]: jsonl append failed: {exc}")


def _is_jsonable(v) -> bool:
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False
