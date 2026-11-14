"""Transcript date grouping is LOCAL, while stored timestamps stay UTC.

Regression for the soak bug: a 19:21 PDT recording (= 02:21 UTC the next day)
filed its transcript under the UTC date (2026-06-08) instead of the local date
it actually happened (2026-06-07). The fix lives only in the renderer
(``session_date``) — the JSONL queue files are untouched and remain UTC.
"""

from __future__ import annotations

from conftest import with_tz
from onoats.jsonl import Session, Utterance
from onoats.render import session_date


def test_aware_utc_groups_by_local_date():
    # 02:21 UTC on the 8th is 19:21 PDT on the 7th -> local date is the 7th.
    s = Session(
        session_id="session_20260607_192132_x",
        utterances=[
            Utterance(time="2026-06-08T02:21:32.360+00:00", text="x", source="me")
        ],
    )
    with with_tz("America/Los_Angeles"):
        assert session_date(s) == "2026-06-07"


def test_aware_utc_local_grouping_is_tz_relative():
    # The same instant lands on the 8th in a UTC+ zone (Tokyo, 11:21 JST).
    s = Session(
        session_id="s",
        utterances=[Utterance(time="2026-06-08T02:21:32+00:00", text="x", source="me")],
    )
    with with_tz("Asia/Tokyo"):
        assert session_date(s) == "2026-06-08"


def test_naive_timestamp_taken_as_is():
    # Naive (no offset) -> assumed local already; not shifted.
    s = Session(
        session_id="s",
        utterances=[Utterance(time="2026-06-06T12:00:00", text="x", source="me")],
    )
    assert session_date(s) == "2026-06-06"


def test_falls_back_to_session_id_stamp_when_no_utterances():
    s = Session(session_id="session_20260607_192132_abc", utterances=[])
    assert session_date(s) == "2026-06-07"
