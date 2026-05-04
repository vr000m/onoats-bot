"""Read-only SmartTurn shadow observer for the dual-input pipeline.

Runs ``LocalSmartTurnAnalyzerV3`` alongside the existing VAD path without
changing commit behaviour. At each VAD-stopped event the analyser is asked
whether the turn looks complete; the verdict is logged for offline
comparison against the VAD-only baseline. No frame is ever swallowed,
modified, or held back — downstream STT continues to commit on raw VAD
exactly as before.

Per the dev plan in
``docs/dev_plans/20260420-design-whisper-websocket-server.md`` the spike
order is: prototype on ``me`` first, measure mid-turn fragmentation
against the 2026-04-21 corpus, then mirror to ``them`` and consider
flipping the commit decision over to SmartTurn.

Gated by ``KODA_SMART_TURN_SHADOW=1`` so the analyser only loads when
explicitly enabled — keeps cold-start cost out of the default bot path.
When enabled, verdicts are mirrored to JSONL under
``<KODA_DATA_DIR>/shadow/verdicts/<YYYY-MM-DD>/<call_id>.jsonl`` so we
have durable evidence regardless of whether stdout was captured.
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
    """Resolve the verdict JSONL root, honouring KODA_DATA_DIR."""
    from shared.store import DEFAULT_DATA_DIR

    base = Path(os.environ.get("KODA_DATA_DIR", str(DEFAULT_DATA_DIR))).expanduser()
    today = datetime.now().strftime("%Y-%m-%d")
    return base / "shadow" / "verdicts" / today


class SmartTurnShadowObserver(FrameProcessor):
    """Log what SmartTurn would have decided at every VAD-stopped event.

    One instance per branch; place after the branch's VADProcessor and
    before STT so plain VAD frames carry implicit branch identity from the
    pipeline arm. Forwards every frame untouched; never raises on analyser
    failure (logs and continues — the bot must not depend on shadow output).

    When ``call_id`` and ``verdict_dir`` are provided each verdict is also
    appended to ``<verdict_dir>/<call_id>.jsonl`` so the data survives even
    if stdout is not captured (e.g. headless / launchctl runs).
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
        # _init_sample_rate but leaves _sample_rate=0 until set_sample_rate
        # fires from a StartFrame. Pass it here so set_sample_rate's
        # ``_init_sample_rate or sample_rate`` clause picks it up.
        self._analyzer = LocalSmartTurnAnalyzerV3(sample_rate=sample_rate)
        self._configured_sample_rate = sample_rate
        self._in_speech = False
        self._turn_started_at: float | None = None
        # Serialise analyse calls per branch — overlapping VAD-stopped
        # events on the same branch would otherwise contend for the
        # internal audio buffer.
        self._analyse_lock = asyncio.Lock()

        self._call_id = call_id
        self._jsonl_path: Path | None = None
        if call_id and verdict_dir is not None:
            try:
                verdict_dir.mkdir(parents=True, exist_ok=True)
                self._jsonl_path = verdict_dir / f"{call_id}.jsonl"
                logger.info(f"SmartTurnShadow[{source}]: persisting verdicts to {self._jsonl_path}")
            except Exception as exc:
                logger.warning(
                    f"SmartTurnShadow[{source}]: could not open verdict dir {verdict_dir}: {exc}"
                )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        try:
            if isinstance(frame, StartFrame):
                # Wake the analyser's internal sample-rate state so
                # append_audio's chunk-duration maths doesn't divide by zero.
                self._analyzer.set_sample_rate(
                    getattr(frame, "audio_in_sample_rate", self._configured_sample_rate)
                )
            elif isinstance(frame, InputAudioRawFrame):
                # Feed every frame to the analyser regardless of VAD state;
                # the analyser tracks pre-speech buffer + silence internally.
                self._analyzer.append_audio(frame.audio, is_speech=self._in_speech)
            elif isinstance(frame, VADUserStartedSpeakingFrame):
                self._in_speech = True
                self._turn_started_at = time.monotonic()
            elif isinstance(frame, VADUserStoppedSpeakingFrame):
                self._in_speech = False
                asyncio.create_task(self._shadow_analyse())
        except Exception as exc:
            # Shadow must never break the live pipeline. Log and move on.
            logger.warning(f"SmartTurnShadow[{self._source}]: observe error: {exc}")

        await self.push_frame(frame, direction)

    async def _shadow_analyse(self) -> None:
        started = self._turn_started_at
        async with self._analyse_lock:
            try:
                state, metrics = await self._analyzer.analyze_end_of_turn()
            except Exception as exc:
                logger.warning(
                    f"SmartTurnShadow[{self._source}]: analyse_end_of_turn failed: {exc}"
                )
                return
        verdict = "COMPLETE" if state == EndOfTurnState.COMPLETE else "INCOMPLETE"
        turn_secs = (time.monotonic() - started) if started is not None else None
        # Single structured line per VAD-stopped event so a grep across the
        # bot log produces a clean comparison corpus.
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
        # Best-effort metrics serialisation — the analyser returns a
        # MetricsData dataclass-ish object whose shape isn't stable across
        # Pipecat versions. Stringify on failure rather than dropping.
        if metrics is not None:
            try:
                if hasattr(metrics, "__dict__"):
                    record["metrics"] = {k: v for k, v in vars(metrics).items() if _is_jsonable(v)}
                else:
                    record["metrics"] = str(metrics)
            except Exception:
                record["metrics"] = None
        try:
            with open(self._jsonl_path, "a") as fh:
                fh.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.warning(f"SmartTurnShadow[{self._source}]: jsonl append failed: {exc}")


def _is_jsonable(v) -> bool:
    try:
        json.dumps(v)
        return True
    except (TypeError, ValueError):
        return False
