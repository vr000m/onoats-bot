"""Branch-aware VAD frames for the dual-input bot.

Pipecat's ``VADUserStartedSpeakingFrame`` / ``VADUserStoppedSpeakingFrame``
carry no user_id field. The dual-input pipeline has two VAD branches
(mic, loopback) merged via ParallelPipeline; the silence coordinator
downstream needs to know which branch a VAD event came from.

Subclassing rather than mutating Pipecat frames keeps branch identity as a
first-class dataclass field. Downstream ``isinstance(frame, VADUser*SpeakingFrame)``
checks still match, so we don't disturb any Pipecat frame-routing behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipecat.frames.frames import (
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)


@dataclass
class BranchVADUserStartedSpeakingFrame(VADUserStartedSpeakingFrame):
    source: str = ""
    source_order: int = 0


@dataclass
class BranchVADUserStoppedSpeakingFrame(VADUserStoppedSpeakingFrame):
    source: str = ""
    source_order: int = 0
