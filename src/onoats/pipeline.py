"""Pluggable, default-empty post-read pipeline for the onoats converter.

The converter applies an ordered list of ``Session -> Session`` steps between
reading the JSONL and rendering markdown. At baseline the list is EMPTY
(identity) — onoats ships no transform steps.

The names ``clean`` / ``segment`` / ``classify`` are reserved no-op extension
points for a future ``[llm]`` extra to register against; the baseline registers
NONE of them. A downstream queue consumer does NOT use this hook — it consumes
the queue files and runs its own pipeline.
"""

from __future__ import annotations

from collections.abc import Callable

from onoats.jsonl import Session

Step = Callable[[Session], Session]

# Reserved extension-point names a future ``[llm]`` extra may register against.
# Baseline registers NONE — they exist only to document the contract.
EXTENSION_POINTS: tuple[str, ...] = ("clean", "segment", "classify")

# Registry of named steps. EMPTY at baseline (identity pipeline).
_REGISTRY: dict[str, Step] = {}


def register(name: str, fn: Step) -> None:
    """Register a named ``Session -> Session`` step (used by optional extras)."""
    _REGISTRY[name] = fn


def unregister(name: str) -> None:
    """Remove a registered step if present (no error if absent)."""
    _REGISTRY.pop(name, None)


def registered_steps() -> dict[str, Step]:
    """Return a copy of the current registry (empty at baseline)."""
    return dict(_REGISTRY)


def default_steps() -> list[Step]:
    """The ordered default step list — EMPTY (identity) at baseline."""
    return []


def apply_steps(session: Session, steps: list[Step] | None = None) -> Session:
    """Apply an ordered step list to a session.

    ``None`` or ``[]`` is the identity transform — the session is returned
    unchanged. Steps are applied left to right; each receives the previous
    step's output.
    """
    if not steps:
        return session
    for step in steps:
        session = step(session)
    return session
