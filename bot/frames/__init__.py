"""Koda-local frame types that extend Pipecat's frame hierarchy."""

from bot.frames.branch_vad import (
    BranchVADUserStartedSpeakingFrame,
    BranchVADUserStoppedSpeakingFrame,
)

__all__ = [
    "BranchVADUserStartedSpeakingFrame",
    "BranchVADUserStoppedSpeakingFrame",
]
