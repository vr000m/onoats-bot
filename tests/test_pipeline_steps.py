"""Pipeline step hook: default-empty identity + ordered custom registration."""

from __future__ import annotations

from onoats import pipeline
from onoats.jsonl import Session, Utterance


def _session() -> Session:
    return Session(
        session_id="session_20260606_120000_abcd1234",
        category="work",
        utterances=[Utterance(time="2026-06-06T12:00:00", text="hi", source="me")],
    )


def test_default_steps_empty():
    assert pipeline.default_steps() == []


def test_apply_empty_steps_is_identity():
    session = _session()
    result = pipeline.apply_steps(session, [])
    assert result is session
    # None also identity
    assert pipeline.apply_steps(session, None) is session


def test_baseline_registers_no_steps():
    assert pipeline.registered_steps() == {}
    # The reserved extension-point names exist as documentation only.
    assert pipeline.EXTENSION_POINTS == ("clean", "segment", "classify")


def test_registered_custom_steps_applied_in_order():
    order: list[str] = []

    def step_a(session: Session) -> Session:
        order.append("a")
        session.category = session.category + "-a"
        return session

    def step_b(session: Session) -> Session:
        order.append("b")
        session.category = session.category + "-b"
        return session

    session = _session()
    result = pipeline.apply_steps(session, [step_a, step_b])

    assert order == ["a", "b"]
    assert result.category == "work-a-b"


def test_register_unregister_roundtrip():
    def noop(session: Session) -> Session:
        return session

    try:
        pipeline.register("clean", noop)
        assert "clean" in pipeline.registered_steps()
    finally:
        pipeline.unregister("clean")
    assert pipeline.registered_steps() == {}
