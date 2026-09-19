"""Unit tests for `onoats._redact` — targets the public module directly
(round 10 architecture finding: round 9's tests only ever addressed the
`runtime._safe_exc_text` compat alias, so the leaf module itself had no
test naming its own public symbol).

Round 10 found 3 new gaps in round 9's rewrite (all reproduced live by
independent reviewers before this fix):

1. A password containing `/`, `?`, or `#` bypassed redaction entirely —
   the credential search region was bounded by those characters BEFORE
   checking for `@`, so the `@` fell outside the search and the whole
   credential leaked.
2. `_redact_bare_leading_credential` was not anchored to an actual
   credential shape — any `@` before the first `/?#` triggered a
   destructive split, corrupting non-credential leading prose (e.g.
   "contact admin@example.com for help" -> "example.com for help").
3. `_display_target`/`_endpoint_label` (separate call sites, not
   `safe_exc_text` itself) had the identical `/?#` bypass via
   `urlsplit`'s netloc-bounded `username`/`password` properties.

This file covers (1) and (2) directly against `onoats._redact`; (3) is
covered in `tests/test_runtime_preflight.py`
(`test_display_target_never_leaks_on_a_malformed_uri`) and
`tests/test_websocket_stt_reconnect.py` (`_endpoint_label` cases).

Round 4 replaced the three-tier scanner these cases were written against
with a single candidate scan plus two positive gates (see the module
docstring). Every case below still holds; the tier vocabulary in the older
docstrings is historical. The generated sweep at the bottom of this file —
not any hand-picked list — is the specification: hand-picked cases are what
missed all four credential leaks round 4 found.
"""

import itertools

import pytest

from onoats._redact import display_uri, redact_uri, safe_exc_text, strip_query


def test_password_containing_slash_is_still_redacted():
    exc = Exception(
        "ws://secretuser:hunter/2@stt.example.internal:2020/ isn't a valid "
        "URI: nonempty path required"
    )
    safe = safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hunter" not in safe
    assert safe == (
        "ws://stt.example.internal:2020/ isn't a valid URI: nonempty path required"
    )


def test_password_containing_question_mark_is_still_redacted():
    exc = Exception("secretuser:hunter?2@stt.example.internal:2020 isn't a valid URI")
    safe = safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hunter" not in safe
    assert safe == "stt.example.internal:2020 isn't a valid URI"


def test_password_containing_hash_is_still_redacted():
    exc = Exception("secretuser:hunter#2@stt.example.internal:2020 isn't a valid URI")
    safe = safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hunter" not in safe
    assert safe == "stt.example.internal:2020 isn't a valid URI"


def test_scheme_prefixed_password_containing_hash_is_still_redacted():
    exc = Exception("ws://u:p#w@host:9999/ isn't a valid URI")
    safe = safe_exc_text(exc)
    assert "u:p#w" not in safe
    assert safe == "ws://host:9999/ isn't a valid URI"


def test_leading_prose_with_unrelated_at_sign_is_not_corrupted():
    """Round-10 finding: `_redact_bare_leading_credential` had no
    credential-shape gate, so ANY leading text containing an `@` before
    the first `/?#` was destructively split — with no credential present
    at all."""
    text = "Cannot reach the server: contact admin@example.com for help"
    assert safe_exc_text(Exception(text)) == text


def test_leading_prose_two_words_with_at_sign_is_not_corrupted():
    text = "connection to user@host failed"
    assert safe_exc_text(Exception(text)) == text


def test_leading_prose_credentials_for_phrasing_is_not_corrupted():
    text = "missing credentials for user@internal"
    assert safe_exc_text(Exception(text)) == text


def test_multiple_credentials_in_one_message_both_redacted():
    """Round-10 security-lens finding: a message with two
    `scheme://user:pass@host` spans and no path on the first leaked the
    second credential (the first match's candidate swallowed the second
    URI's scheme prefix)."""
    exc = Exception("ws://u1:p1@h1 and ws://u2:p2@h2 both failed")
    safe = safe_exc_text(exc)
    assert "u1:p1" not in safe
    assert "u2:p2" not in safe
    assert safe == "ws://h1 and ws://h2 both failed"


def test_nested_url_in_unrelated_query_string_not_corrupted():
    """Round-10 codex finding: a nested `scheme://` URL embedded in an
    unrelated query string must not have its outer, credential-free URL
    corrupted — only the genuinely credential-shaped inner span is
    touched."""
    text = "https://host/path?redirect=https://user:pass@evil/path"
    # The scanner itself (userinfo only) touches nothing but the credential.
    assert redact_uri(text) == "https://host/path?redirect=https://evil/path"
    # `safe_exc_text` then composes the query strip (round-5 finding 4), so
    # the query — nested URL and all — is dropped wholesale.
    safe = safe_exc_text(Exception(text))
    assert "user:pass" not in safe
    assert safe == "https://host/path"


def test_unrelated_at_sign_in_trailing_prose_after_real_credential():
    """Round-10 logic-lens-adjacent case: a message with a genuine
    credential AND an unrelated `@` later in trailing prose must redact
    only the real credential, not collapse/corrupt the trailing text."""
    exc = Exception("ws://user:pass@host:9999 isn't valid, contact admin@example.com")
    safe = safe_exc_text(exc)
    assert "user:pass" not in safe
    assert safe == "ws://host:9999 isn't valid, contact admin@example.com"


def test_unrelated_query_string_at_sign_still_untouched():
    """Pre-existing invariant (round 8 case e) must still hold with the
    round-10 rewrite: an unrelated `@` inside a query string must never
    be mistaken for a credential."""
    text = "GET https://example.com/api?redirect=user@example.org failed: 502"
    assert redact_uri(text) == text
    # The URI's query is dropped by `safe_exc_text`'s query strip; the
    # surrounding prose ("GET ", " failed: 502") is untouched.
    assert safe_exc_text(Exception(text)) == "GET https://example.com/api failed: 502"


def test_unrelated_query_string_with_colon_still_untouched():
    """Codex-adversarial finding: a `user:pass@host` shape inside an
    unrelated redirect query string was misclassified as authority
    credentials by tier 3 (which lacked tier 2's "no '=' in the gap"
    guard), corrupting the real path and query down to just the host."""
    text = "ws://host/path?redirect=user:pass@example.org"
    assert redact_uri(text) == text
    assert safe_exc_text(Exception(text)) == "ws://host/path"


def test_password_with_multiple_spaces_is_still_redacted():
    """Codex-adversarial finding: tier 3 only tolerated one extra
    whitespace-delimited word, so a password with two or more embedded
    spaces reached the log/error text unredacted."""
    exc = Exception("ws://u:p more words@host/path is invalid")
    safe = safe_exc_text(exc)
    assert "p more words" not in safe
    assert safe == "ws://host/path is invalid"


def test_no_credential_shaped_substring_passes_through_unchanged():
    text = "connection refused: host unreachable"
    assert safe_exc_text(Exception(text)) == text


def test_word_colon_prefixed_diagnostic_with_unrelated_at_sign_untouched():
    """Round-2 review-gauntlet regression: tier 3's old gate fired on ANY
    colon in the first whitespace-delimited token, which is the shape of
    almost every prefixed diagnostic message ("Error:", "stt_server
    error:"). It then `rfind`-searched the ENTIRE remainder for an `@` and
    discarded everything up to and including it, destroying ordinary
    diagnostic text that happened to contain an unrelated `@` — even
    though no credential was present anywhere."""
    text = "Error: connect to user@host failed"
    assert safe_exc_text(Exception(text)) == text


def test_word_colon_prefixed_diagnostic_two_word_prefix_untouched():
    text = "stt_server error: could not reach user@host"
    assert safe_exc_text(Exception(text)) == text


def test_multiple_unrelated_at_signs_in_prefixed_prose_untouched():
    """Round-2 regression: because tier 3 used `rfind` over the whole
    string, a message with several unrelated `@`s kept only the text after
    the LAST one."""
    text = "Error: mail admin@a.com or ops@b.com"
    assert safe_exc_text(Exception(text)) == text


def test_base64_shaped_password_is_still_redacted():
    """Round-2 review-gauntlet regression (credential leak): a URI password
    containing both `/` (or `?`/`#`) and `=` — i.e. any base64-shaped
    token — bypassed redaction entirely, because the query-string guard
    rejected on `=` presence in the gap alone, with no check for an actual
    `?` marking real query structure."""
    exc = Exception("ws://user:/+++rd7K/rq+AQI=@stt.local:8765/ isn't a valid URI")
    safe = safe_exc_text(exc)
    assert "rd7K" not in safe
    assert "AQI=" not in safe
    assert safe == "ws://stt.local:8765/ isn't a valid URI"


def test_base64_shaped_password_with_equals_and_slash_scheme_variant():
    exc = Exception("ws://user:ab/cd=ef@host/path isn't a valid URI")
    safe = safe_exc_text(exc)
    assert "ab/cd=ef" not in safe
    assert safe == "ws://host/path isn't a valid URI"


def test_scheme_authority_with_port_and_unrelated_at_sign_untouched():
    """Round-2 review-gauntlet security finding: on the scheme-prefixed
    path, `text[start:first_ws]` IS the authority, so an ordinary
    `host:PORT` satisfied tier 3's old colon-in-first-token gate
    unconditionally — a port is exactly as colon-bonded as a real
    credential. Tier 3 then widened past the port, found an unrelated `@`
    later in trailing prose, and fabricated a fake host, destroying the
    real host and the whole diagnostic tail."""
    text = "ws://host:8765 isn't a valid URI: see user@guide"
    assert safe_exc_text(Exception(text)) == text


def test_scheme_authority_with_port_multiple_unrelated_at_signs_untouched():
    text = "ws://host:8765 failed: peer admin@corp rejected token"
    assert safe_exc_text(Exception(text)) == text


def test_redact_uri_base64_shaped_password():
    assert (
        redact_uri("ws://user:/+++rd7K/rq+AQI=@stt.local:8765/")
        == "ws://stt.local:8765/"
    )


def test_ipv6_host_survives_redaction_intact():
    exc = Exception("ws://user:pass@[::1]:443/path isn't a valid URI")
    safe = safe_exc_text(exc)
    assert "user:pass" not in safe
    assert safe == "ws://[::1]:443/path isn't a valid URI"


# ---------------------------------------------------------------------------
# redact_uri — used by runtime._display_target and
# websocket_stt_service._endpoint_label, both of which start from a
# structured connect URI rather than arbitrary exception text.
# ---------------------------------------------------------------------------


def test_redact_uri_password_containing_slash():
    """Round-10 security-review finding: `_display_target`'s old
    `urlsplit`-based implementation returned this RAW and unredacted,
    because `urlsplit` silently reports no userinfo when the password
    contains an unencoded `/`."""
    assert redact_uri("ws://u:pa/ss@host:9999/") == "ws://host:9999/"


def test_redact_uri_password_containing_hash():
    assert redact_uri("ws://u:pa#ss@host:9999/") == "ws://host:9999/"


def test_redact_uri_password_containing_question_mark():
    assert redact_uri("ws://u:pa?ss@host:9999/") == "ws://host:9999/"


def test_redact_uri_normal_credential_still_redacted():
    assert redact_uri("ws://u:pass@host:9999/") == "ws://host:9999/"


def test_redact_uri_no_credential_passes_through():
    assert redact_uri("ws://host:9999/") == "ws://host:9999/"


def test_redact_uri_ipv6_host_intact():
    assert redact_uri("ws://u:pass@[::1]:443/path") == "ws://[::1]:443/path"


# ---------------------------------------------------------------------------
# Round-3 regressions: the three findings below were all symptoms of ONE root
# cause — tier 3 conflating "is there a port here" with "is there a credential
# here", and the scheme-prefixed authority path sharing heuristics with the
# bare, scheme-less path. The fix separates the two paths and gates on the
# candidate span being credential-shaped; these cases pin both directions.
# ---------------------------------------------------------------------------


def test_tier3_does_not_fire_on_non_credential_leading_tokens():
    """Round-3 finding 1: tier 3's gate was not credential-shaped, so ANY
    first token whose colon-bonded remainder was non-numeric let it discard
    everything up to the LAST `@` in the message — destroying
    credential-free diagnostic text and fabricating a host out of prose."""
    for text in (
        "[::1]:443 isn't a valid URI: mail user@x",
        "ws:/host isn't a valid URI: contact user@guide",
        "2026-09-17T10:00:00 connect failed for user@host",
        "unix:/tmp/x.sock failed: see admin@corp.com",
        "C:/Users/me/file not found: mail ops@corp.com",
    ):
        assert safe_exc_text(Exception(text)) == text, text


def test_authority_port_followed_by_a_path_is_still_recognized_as_a_port():
    """Round-3 finding 2: the all-digits port gate measured the colon-bonded
    token to the first WHITESPACE, so `host:8765/path` ("8765/path") was not
    all-digits, the gate passed, and tier 3 destroyed the real host, port,
    path and diagnostic tail while fabricating a fake host. The token is now
    measured to the first `/?#`-or-whitespace, i.e. to the end of the
    authority's first segment."""
    for text in (
        "ws://host:8765/path failed: could not reach user@relay",
        "wss://stt.internal:2020/v1 failed: could not reach user@relay",
        "wss://[::1]:2020/v1 failed: could not reach user@relay",
    ):
        assert safe_exc_text(Exception(text)) == text, text


def test_bare_path_all_digit_password_segment_is_not_read_as_a_port():
    """Round-3 finding 3 (HIGH, credential leak — the inverse of finding 2):
    the all-digits port veto was also applied on the BARE, scheme-less path,
    where there is no host:port at all and a colon-bonded token is ALWAYS the
    password. A password whose leading whitespace-delimited segment was all
    digits therefore made the veto true, which DISABLED the widen refusal and
    let the full credential through unredacted."""
    exc = Exception("user:1234 5678@host isnt a valid URI: nonempty path required")
    safe = safe_exc_text(exc)
    assert "1234" not in safe
    assert "5678" not in safe
    assert "user:" not in safe
    assert safe == "host isnt a valid URI: nonempty path required"


def test_bare_path_credential_with_a_space_still_redacts():
    """The positive half of finding 3's fix: removing the port veto from the
    bare path must not cost the case it was co-located with."""
    exc = Exception(
        "secretuser:hunter 2@stt.example.internal:2020 isn't a valid URI: "
        "nonempty path required"
    )
    safe = safe_exc_text(exc)
    assert "secretuser" not in safe
    assert "hunter" not in safe
    assert safe == (
        "stt.example.internal:2020 isn't a valid URI: nonempty path required"
    )


def test_bare_path_tier1_requires_credential_shape():
    """Tier 1 on the bare path is prose, not an authority: an `@` in the
    leading token with no `user:pass` shape in front of it is not a
    credential. (Previously tier 1 accepted unconditionally on both paths,
    so a message opening with a bare email address was destructively
    truncated.)"""
    text = "admin@example.com for help"
    assert safe_exc_text(Exception(text)) == text


def test_authority_path_at_sign_with_a_dotted_tail_is_over_redacted():
    """Limitation 5, and deliberately so: an `@` in a *path* segment is
    character-for-character the genuine `ws://user:1234/seg@host.tld` the
    sweep requires be redacted, so the ambiguity resolves toward redaction.

    Round 8 extended this from dotted tails to bare reg-name tails
    (`_tail_accepts` branch (c)). `ws://host:8765/p/user@y` used to survive
    only because `y` carried no `.`/`:`/`/` — and so did
    `ws://user:S3CRETPW/seg@localhost`, whose credential leaked whole.
    `localhost` is this project's own canonical STT host and will never grow
    a dot, so the bare tail had to be accepted and this shape goes with it.
    """
    assert safe_exc_text(Exception("wss://host:443/path/to/a@b.com")) == "wss://b.com"
    assert safe_exc_text(Exception("ws://host:8765/p/user@y")) == "ws://y"


def test_tier3_widening_is_bounded_to_two_extra_words():
    """`_MAX_USERINFO_SPACES`: two raw spaces in a password still redact
    (the widest genuine shape this module has ever been asked to handle),
    while a whole sentence of prose between the colon and an unrelated `@`
    does not — that unbounded reach is what five consecutive rounds of
    destructive truncation traced back to."""
    safe = safe_exc_text(Exception("ws://u:p more words@host/path is invalid"))
    assert safe == "ws://host/path is invalid"
    text = "u:p one two three four@host is invalid"
    assert safe_exc_text(Exception(text)) == text


# ---------------------------------------------------------------------------
# Round-4 regressions. All four leaks below were live against round 3's
# three-tier scanner and were found by independent reviewers, not by the
# case list above — which is why this file now ends in a generated sweep.
# ---------------------------------------------------------------------------


def test_digit_first_password_segment_does_not_veto_authority_redaction():
    """Round-4 finding 1 (Critical, full-credential leak): round 3's
    `_is_host_port_token` veto read an all-digit first password segment as a
    port and abandoned the search, returning the whole credential. The port
    heuristic is gone entirely — `_tail_accepts` now supplies the positive
    evidence the veto was standing in for."""
    exc = Exception("ws://user:1234 abcd@host:8765 isn't a valid URI")
    safe = safe_exc_text(exc)
    assert "user" not in safe
    assert "1234" not in safe
    assert "abcd" not in safe
    assert safe == "ws://host:8765 isn't a valid URI"


def test_at_inside_username_does_not_truncate_the_credential_search():
    """Round-4 finding 2 (High, leaks on every normal startup INFO line):
    round 3's tier 1 took `rfind("@", start, tier1_stop)` and returned
    unconditionally on the authority path, so an `@` inside the USERNAME won
    whenever a `/?#`-or-space in the password truncated the window before
    the real credential-terminating `@`. The scan now walks every `@` in the
    window and keeps the rightmost that passes the gates."""
    safe = redact_uri("ws://alice@corp.com:Xy/9zQvHunter@stt.internal:2020/")
    assert "alice" not in safe
    assert "Xy" not in safe
    assert "Hunter" not in safe
    assert safe == "ws://stt.internal:2020/"


def test_at_inside_username_in_exception_text_is_also_redacted():
    exc = Exception(
        "ws://alice@corp.com:Xy/9zQvHunter@stt.internal:2020/ isn't a valid URI"
    )
    safe = safe_exc_text(exc)
    assert "Hunter" not in safe
    assert safe == "ws://stt.internal:2020/ isn't a valid URI"


def test_empty_username_token_auth_userinfo_is_redacted():
    """Round-4 finding 3: RFC 3986 permits empty userinfo, and `:token@host`
    is a live token-auth convention. Round 3's `colon <= start` check
    rejected it outright."""
    assert redact_uri("ws://:s3cr3t@host:8765/") == "ws://host:8765/"
    safe = safe_exc_text(Exception(":s3cr3t@stt.internal:2020 isn't a valid URI"))
    assert "s3cr3t" not in safe
    assert safe == "stt.internal:2020 isn't a valid URI"


def test_email_shaped_username_on_the_bare_path_is_redacted():
    """Round-4 finding 4: `@` was in `_USERNAME_FORBIDDEN`, so an
    email-as-username credential was unredactable on the bare path — the
    opposite of the authority path's behaviour for the same input."""
    safe = safe_exc_text(
        Exception("alice@corp.com:hunter2@stt.internal:2020 isn't a valid URI")
    )
    assert "hunter2" not in safe
    assert "alice" not in safe
    assert safe == "stt.internal:2020 isn't a valid URI"


def test_colon_bonded_prose_prefix_no_longer_destroys_diagnostics():
    """Round-4 finding 5: bounded widening alone still destroyed
    credential-free text, because the shape gate inspected only the first
    colon and never constrained the span between it and the `@`.
    `_tail_accepts` closes it structurally: a bare, single-label tail plus a
    password carrying a second `:` (or a `/`) is prose, not userinfo."""
    for text in (
        "2026-09-17T10:00:00 connect user@host",
        "2026-09-17T10:00:00 connect failed for user@host",
        "C:/Users/bob connect user@host",
        "C:/Users/bob failed for user@host",
    ):
        assert safe_exc_text(Exception(text)) == text, text


def test_password_with_question_mark_and_equals_is_still_redacted():
    """Round-4 finding 6: the query-structure guard vetoed a genuine
    password containing `?` followed later by `=`. The guard is gone; the
    username gate (`?redirect=user` carries a `/` or lacks a colon) rejects
    real query structure on its own."""
    safe = redact_uri("ws://user:pa?ss=x@host:8765/")
    assert "pa?ss=x" not in safe
    assert safe == "ws://host:8765/"


# ---------------------------------------------------------------------------
# Round 5 — destructive over-redaction / host fabrication. The credential
# (where there is one) was already removed correctly; the bug was that the
# *real host* was destroyed too and a fabricated one substituted from later
# in the string, in exactly the `_display_target` / `_endpoint_label` output
# operators diagnose from.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    (
        "wss://host:443/v1?redirect=user@example.com",
        "ws://localhost:8765/v1?user=bob@corp.com",
    ),
    ids=str,
)
def test_ported_uri_with_an_at_sign_in_its_query_keeps_its_host(text):
    """Round-5 finding 1: `_is_userinfo_shaped` took the FIRST `:` in the
    span, which on a scheme-prefixed URI with a port is the PORT colon. The
    span then read as `user=host` / `pass=443`, `_tail_accepts` passed on
    the query's dotted tail, and the real host was replaced by it
    (`wss://example.com`). There is no credential in either of these."""
    assert redact_uri(text) == text


@pytest.mark.parametrize(
    "text,expected",
    (
        (
            "wss://user:pass@host.example.com/v1 failed: admin@example.com",
            "wss://host.example.com/v1 failed: admin@example.com",
        ),
        (
            "wss://user:pass@1.2.3.4:8765/v1 oops x@y.z",
            "wss://1.2.3.4:8765/v1 oops x@y.z",
        ),
        (
            "user:pass@host.example.com/v1 failed: admin@example.com",
            "host.example.com/v1 failed: admin@example.com",
        ),
    ),
    ids=str,
)
def test_trailing_prose_email_cannot_hijack_a_real_credential(text, expected):
    """Round-5 finding 2: rightmost-`@`-wins kept scanning after the real
    in-authority credential was found, so a later `@` in trailing prose (or
    a query) satisfied both gates and the true host was discarded along with
    the credential. Swept across every credential shape by the
    ` failed: admin@example.com` suffix in `_SUFFIXES` too."""
    assert "user:pass" not in safe_exc_text(Exception(text))
    assert safe_exc_text(Exception(text)) == expected


def test_password_containing_a_scheme_prefix_does_not_leak():
    """Round-5 finding 3 (codex P1): `outer_stop` was the next
    `scheme://`-shaped match, which for a password containing `://` falls
    INSIDE the password — before the real terminating `@`. No candidate was
    found in that truncated window, the loop then re-anchored on the fake
    scheme match, and `user:secret` was emitted verbatim."""
    text = "wss://user:secret://tail@host.example.com"
    safe = redact_uri(text)
    assert "user" not in safe
    assert "secret" not in safe
    assert safe == "wss://host.example.com"
    assert safe_exc_text(Exception(text)) == "wss://host.example.com"


def test_safe_exc_text_strips_a_query_string_token():
    """Round-5 finding 4: `safe_exc_text` did not compose `strip_query` the
    way both display call sites do, so `websockets.InvalidURI`'s message
    (`f"{uri} isn't a valid URI: {msg}"`) carried `?token=...` verbatim into
    the reconnect warning and the status-file warnings."""
    text = "wss://host:443/v1?token=s3cr3t isn't a valid URI: nonempty path required"
    safe = safe_exc_text(Exception(text))
    assert "s3cr3t" not in safe
    assert safe == "wss://host:443/v1 isn't a valid URI: nonempty path required"


def test_safe_exc_text_strips_a_query_from_every_uri_in_the_message():
    safe = safe_exc_text(Exception("ws://a/x?t=1 and wss://b/y#t=2 both failed"))
    assert safe == "ws://a/x and wss://b/y both failed"


# ---------------------------------------------------------------------------
# Round 6. Two new leaks (A, B) plus four more instances of the fabrication
# class. Root cause of the fabrication class: the "is this span already a
# complete, well-formed authority?" question was only ever asked of an
# *incumbent* candidate, never at the acceptance point — so a prose `@`
# accepted as the FIRST candidate deleted a real, already-terminated
# authority in front of it. `_scan` now asks it before the loop too.
# ---------------------------------------------------------------------------


def test_scheme_less_uri_query_string_is_stripped():
    """Round-6 finding A (HIGH, leak): `_strip_query_spans` only scanned
    from `scheme://` matches, but a scheme-less URI is exactly what raises
    `websockets.InvalidURI` — the commonest operator typo of all — so its
    `?token=` reached every reconnect warning and status-file warning
    verbatim."""
    text = (
        "stt.example.com:8765/v1?token=QUERYSECRET isn't a valid URI: "
        "scheme isn't ws or wss"
    )
    safe = safe_exc_text(Exception(text))
    assert "QUERYSECRET" not in safe
    assert safe == "stt.example.com:8765/v1 isn't a valid URI: scheme isn't ws or wss"


@pytest.mark.parametrize(
    "text,expected",
    (
        ("stt.example.com:8765/v1#token=S3CR3T", "stt.example.com:8765/v1"),
        ("stt.example.com:8765?token=S3CR3T", "stt.example.com:8765"),
        ("[::1]:8765/v1?token=S3CR3T", "[::1]:8765/v1"),
        ("h.local/v1?t=S3CR3T failed", "h.local/v1 failed"),
    ),
    ids=str,
)
def test_scheme_less_query_strip_shapes(text, expected):
    assert safe_exc_text(Exception(text)) == expected


@pytest.mark.parametrize(
    "text",
    (
        # Prose, not a URI: the scheme-less query strip must not cut here.
        "Traceback? no, a warning",
        "Error: what? nothing",
        "connection refused: host unreachable",
        "C:/Users/me/file not found: mail ops@corp.com",
    ),
    ids=str,
)
def test_scheme_less_query_strip_leaves_prose_alone(text):
    assert safe_exc_text(Exception(text)) == text


def test_at_inside_the_password_cannot_pin_the_scan_short():
    """Round-6 finding B (MEDIUM, leak): an `@` *inside* the userinfo that
    happened to be followed by a host-shaped token (`p@ss`) looked like a
    settled authority, so the real terminating `@` was refused as a rival
    and `_redact_text` deleted only up to the wrong one — leaving most of
    the password in cleartext. Whitespace is the one authority terminator a
    typed-in password can contain unencoded, so a host-shaped span ending at
    whitespace settles nothing when the very next word carries on with more
    userinfo."""
    text = (
        "http://user:p@ss w0rd:x@stt.example.com:8765/v1 isn't a valid URI: "
        "scheme isn't ws or wss"
    )
    safe = redact_uri(text)
    assert "w0rd" not in safe
    assert "user:p" not in safe
    assert safe == (
        "http://stt.example.com:8765/v1 isn't a valid URI: scheme isn't ws or wss"
    )
    assert safe_exc_text(Exception(text)) == safe


@pytest.mark.parametrize(
    "text",
    (
        # Round-6 `_redact.py:315` — a path-bearing, query-free URI plus two
        # prose words plus an email collapsed to the email's domain.
        "wss://host.example.com:443/v1 failed: user@corp.com",
        "ws://1.2.3.4:8765/v1 failed: user@corp.com",
        # Round-6 `_redact.py:373` — the same on the scheme-less path.
        "unix:/tmp/x.sock error: svc@host.com",
        "host.example.com:443/v1 failed: user@corp.com",
        # Round-6 `_redact.py:246` — `_not_query_of_path` only recognised a
        # query that followed a `/path`, so a path-free `host:port?query`
        # (and its `#fragment` twin) still fabricated a host.
        "wss://host.example.com:443?redirect=bob@corp.com",
        "wss://host.example.com:443#redirect=bob@corp.com",
        "wss://stt.internal:2020?user=bob@corp.com",
    ),
    ids=str,
)
def test_an_already_terminated_authority_is_never_deleted(text):
    """The fabrication invariant, stated once: when the anchor's own
    authority already ends well-formed, there is no userinfo in it and no
    later `@` may delete it."""
    assert redact_uri(text) == text
    assert safe_exc_text(Exception(text)).startswith(text.split("?")[0].split("#")[0])


def test_query_strip_never_truncates_an_unredacted_credential():
    """Round-6 `_redact.py:499` (Minor): composing the query strip on top of
    the scanner could cut inside a password the scanner had declined to
    redact, emitting a fabricated host (`wss://user:a/b`) and losing the
    real one. Both steps now cut only where the text in front of the `?` is
    a well-formed authority — which also lets the scanner accept this
    credential outright, since a truncated authority proves the `?` is a
    password character."""
    uri = "wss://user:a/b?c@host/v1"
    assert redact_uri(uri) == "wss://host/v1"
    assert display_uri(uri) == "wss://host/v1"
    assert safe_exc_text(Exception(uri)) == "wss://host/v1"
    # `strip_query` alone sees a netloc (`user:a`) that is not a well-formed
    # `host[:port]`, so it fails closed rather than emitting `wss://user:a/b`.
    assert strip_query(uri) == uri


def test_display_uri_is_the_single_composition_of_both_steps():
    """Round-5 finding 8: `strip_query(redact_uri(x))` was open-coded
    identically at both display call sites."""
    assert display_uri("wss://u:p@host:443/v1?token=s3cr3t") == "wss://host:443/v1"
    assert display_uri("ws://127.0.0.1:8765") == "ws://127.0.0.1:8765"


# ---------------------------------------------------------------------------
# Round 7. Two HIGH leaks in the scheme-less query strip (A, B), two more
# credential leaks in `_not_query_of_path`'s and `_next_uri_boundary`'s
# handling of password characters (C, D), and one query-strip truncation.
# ---------------------------------------------------------------------------


def test_strip_query_strips_a_scheme_less_uri():
    """Round-7 finding A (HIGH, leak): `strip_query` parsed with `urlsplit`,
    which gives a scheme-less URI an empty `netloc`, so its fail-closed
    `host[:port]` guard returned the string unchanged — query and token
    intact — through `display_uri`, the documented single owner of rendering
    a whole connect URI. `strip_query` and `_strip_query_spans` are now one
    implementation, so the scheme-less anchor exists on both paths."""
    assert strip_query("stt.example.com:8765/v1?token=SEKRET") == (
        "stt.example.com:8765/v1"
    )
    assert display_uri("stt.example.com:8765/v1?token=SEKRET") == (
        "stt.example.com:8765/v1"
    )
    assert display_uri("u:pw@stt.example.com:8765/v1?token=SEKRET") == (
        "stt.example.com:8765/v1"
    )


@pytest.mark.parametrize(
    "text,expected",
    (
        (
            "localhost:8765/v1?token=SEKRET isn't a valid URI: x",
            "localhost:8765/v1 isn't a valid URI: x",
        ),
        ("stt-box:8765/v1?token=SEKRET", "stt-box:8765/v1"),
        ("stt:8765?token=SEKRET", "stt:8765"),
        ("localhost/v1?token=SEKRET", "localhost/v1"),
        ("localhost?token=SEKRET", "localhost"),
    ),
    ids=str,
)
def test_scheme_less_single_label_host_query_is_stripped(text, expected):
    """Round-7 finding B (HIGH, leak): round 6 closed the scheme-less anchor
    for *dotted* hosts only, and `localhost` — this project's own canonical
    local STT endpoint — has no dot. A numeric port, or a `key=value` query
    behind a colon-free label, is now accepted as evidence in its place."""
    assert safe_exc_text(Exception(text)) == expected


def test_digit_shaped_password_is_not_mistaken_for_a_complete_authority():
    """Round-7 finding C (codex, leak): `_not_query_of_path` vetoed the real
    credential because `user:1234` fullmatches `host[:port]` and a `/path`
    preceded the `?` — round 4's digit-password bug reopened through the
    query gate. The veto now needs a dotted host with a path, or a
    `key=value` query; `?q@` is neither."""
    for uri, expected in (
        (
            "ws://user:1234/x?q@host.example.com:8765/v1",
            "ws://host.example.com:8765/v1",
        ),
        ("ws://user:/x?q@host.example.com", "ws://host.example.com"),
        ("ws://user:1234/x?q@host.example.com", "ws://host.example.com"),
    ):
        assert redact_uri(uri) == expected, uri
        assert display_uri(uri) == expected, uri
        assert safe_exc_text(Exception(uri)) == expected, uri


def test_a_scheme_token_after_a_password_slash_does_not_truncate_the_scan():
    """Round-7 finding D (codex, leak): `_next_uri_boundary` measured a
    `scheme://`-shaped match against the *authority's* `/?#` boundary, but an
    unencoded `/` in a password IS that boundary — so `x://` inside the
    password fell on the far side, truncated the search window before the
    real terminating `@`, and `user:p` was emitted verbatim. The yardstick is
    now the whitespace-delimited token: a genuine second URI is always its
    own token."""
    uri = "ws://user:p/x://tail@host.example.com/v1"
    assert redact_uri(uri) == "ws://host.example.com/v1"
    assert safe_exc_text(Exception(uri)) == "ws://host.example.com/v1"
    # The round-5 shape this boundary rule exists for still works, and a
    # genuine second URI in its own token is still a boundary.
    assert redact_uri("wss://user:secret://tail@host.example.com") == (
        "wss://host.example.com"
    )
    assert safe_exc_text(Exception("ws://u1:p1@h1 and ws://u2:p2@h2 both failed")) == (
        "ws://h1 and ws://h2 both failed"
    )


def test_query_strip_does_not_stop_at_a_scheme_inside_a_query_value():
    """Round-7 Medium (logic): the scheme-less query strip was bounded by
    `_next_uri_boundary`, so a query VALUE containing `://` ended the cut
    early and the rest of the query was glued straight onto the path
    (`...:8765/v1wss://relay.example.com/`). The cut now always runs to the
    token's own end."""
    text = (
        "stt.example.com:8765/v1?next=wss://u:QUERYSECRET@relay.example.com/ "
        "isn't a valid URI: x"
    )
    safe = safe_exc_text(Exception(text))
    assert "QUERYSECRET" not in safe
    assert safe == "stt.example.com:8765/v1 isn't a valid URI: x"


# ---------------------------------------------------------------------------
# Round 8. Three fresh credential/query leak classes, all from RFC 3986
# authority shapes no corpus generator had ever produced: a scheme-relative
# `//host` reference, an *empty* authority, and a bare single-label host
# with no port and no path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    (
        (
            "//user:SEKRET@host.example.com:8765/v1?token=TOK",
            "//host.example.com:8765/v1",
        ),
        ("//user:SEKRET@host.example.com/v1", "//host.example.com/v1"),
        ("//:SEKRET@localhost:8765/v1?token=TOK", "//localhost:8765/v1"),
        ("//localhost:8765/v1?token=TOK", "//localhost:8765/v1"),
        ("//[::1]:2020/v1?token=TOK", "//[::1]:2020/v1"),
    ),
    ids=str,
)
def test_protocol_relative_uri_is_redacted_and_stripped(text, expected):
    """Round-8 finding 1 (HIGH, leak): RFC 3986 §4.2 scheme-relative
    references defeated *both* scheme-less anchors from one cause measured in
    two places. The credential scan anchored on the first `/`, so every
    candidate's username span held a `/` and `_is_userinfo_shaped` refused
    it; the query strip anchored there too, so `_AUTHORITY_STOP_RE` matched
    at offset zero and the authority span it tested was empty. Both anchors
    now go through `_authority_anchor`."""
    assert display_uri(text) == expected
    assert safe_exc_text(Exception(text)) == expected


@pytest.mark.parametrize(
    "text",
    (
        "// note: see bob@corp.com",
        "//  spaced comment",
        "///triple/slash?not=a-host",
    ),
    ids=str,
)
def test_protocol_relative_anchor_does_not_step_into_prose(text):
    """`_authority_anchor` advances only over a genuine authority: a `//`
    followed by whitespace or a third `/` keeps the pre-round-8 anchor, so
    comment-shaped and path-shaped prose is untouched."""
    assert safe_exc_text(Exception(text)) == text


def _empty_authority_corpus():
    """Round-8 finding 2: `ws:///v1?token=...`. `websockets.uri.parse_uri`
    raises `InvalidURI` on this shape and embeds the whole token in the
    message, so it reaches `safe_exc_text` in production."""
    for scheme in ("ws://", "wss://"):
        for path in ("", "/", "/v1", "/v1/x"):
            for mark in ("?", "#"):
                for suffix in ("", " isn't a valid URI: x"):
                    head = f"{scheme}{path}"
                    yield f"{head}{mark}token=QUERYSECRET{suffix}", f"{head}{suffix}"


@pytest.mark.parametrize("text,expected", list(_empty_authority_corpus()), ids=str)
def test_empty_authority_uri_still_has_its_query_stripped(text, expected):
    """Round-8 finding 2 (HIGH, leak): an empty authority immediately
    followed by `/`, `?` or `#` is *well-formed* RFC 3986, not a truncated
    one — but `_HOST_PORT_RE` needs at least one character, so `_query_cut`
    read the `None` as "the `?` is a password character" and let the token
    through. A zero-length span cannot hold userinfo, so there is no password
    for the `?` to belong to and the cut is unconditionally safe."""
    safe = safe_exc_text(Exception(text))
    assert "QUERYSECRET" not in safe, (text, safe)
    assert safe == expected, (text, safe)


_BARE_HOST_PASSWORD_TAILS = ("/seg", "?q", "#frag", ":x", "/a/b", "?a=1", "#f")
_BARE_HOSTS = ("localhost", "stt-box", "host")


def _bare_host_credential_corpus():
    """Round-8 finding 3: an illegal-character password against a bare
    single-label host — no dot, no port, no path. `_tail_accepts` had a rule
    for a dotted tail and a rule for a legal password and no rule at all for
    a plain reg-name, which is what `localhost` is."""
    for user in ("user", "", "alice@corp.com"):
        for head in ("hunter2", "1234", "AB+cd="):
            for tail in _BARE_HOST_PASSWORD_TAILS:
                for host in _BARE_HOSTS:
                    for scheme in ("ws://", "wss://", "//"):
                        for suffix in ("", " failed: admin@example.com"):
                            password = head + tail
                            yield (
                                f"{scheme}{user}:{password}@{host}{suffix}",
                                password,
                                user,
                                f"{scheme}{host}{suffix}",
                            )


@pytest.mark.parametrize(
    "text,password,user,expected", list(_bare_host_credential_corpus()), ids=str
)
def test_sweep_bare_single_label_host_credentials_are_redacted(
    text, password, user, expected
):
    """Round-8 finding 3 (HIGH, leak): emitted verbatim before this round,
    and with trailing prose it was worse than verbatim — the scan fell
    through to the prose's own `@` and fabricated a host
    (`ws://user:S3CRETPW/seg@host failed: admin@example.com` ->
    `ws://example.com`). Both halves are asserted: the userinfo is gone AND
    the real host plus the trailing prose survive."""
    safe = safe_exc_text(Exception(text))
    assert password not in safe, (text, safe)
    if user:
        assert user not in safe, (text, safe)
    assert safe == expected, (text, safe)


@pytest.mark.parametrize(
    "text,expected",
    (
        # Round-8 finding 4 (codex): `_not_query_of_path` vetoed the real
        # credential because `user:1234` fullmatches `host[:port]` and `x=1`
        # looked like a real query. A single label with no path behind it is
        # the weakest authority there is, and only while nothing has been
        # accepted yet.
        ("ws://user:1234?x=1@host.example.com/v1", "ws://host.example.com/v1"),
        ("ws://user:1234#x=1@host.example.com/v1", "ws://host.example.com/v1"),
        # Round-8 finding 5 (codex): the settled-authority override demanded
        # an RFC-legal password, which a password whose `/` ended the
        # authority early can never be. The widened word `/seg` is not a word
        # any sentence contains, which is evidence of its own.
        ("ws://user:1234 /seg@host.example.com/v1", "ws://host.example.com/v1"),
        ("ws://user:1234 ?q@host.example.com/v1", "ws://host.example.com/v1"),
    ),
    ids=str,
)
def test_round8_codex_credential_shapes_are_redacted(text, expected):
    assert display_uri(text) == expected
    assert safe_exc_text(Exception(text)) == expected


@pytest.mark.parametrize(
    "text",
    (
        # The other side of finding 5's override: a widened span containing a
        # plain prose label must never overturn a settled authority.
        "wss://host.example.com:443/v1 failed: user@corp.com",
        "unix:/tmp/x.sock error: svc@host.com",
        "ws://host:8765/path failed: could not reach user@relay",
        "C:/Users/bob connect user@host",
        "2026-09-17T10:00:00 connect user@host",
    ),
    ids=str,
)
def test_round8_override_widening_does_not_destroy_diagnostics(text):
    assert redact_uri(text) == text


# ---------------------------------------------------------------------------
# Round 9. Three leak classes, one shared root cause each, all found by the
# logic/security lenses in the gaps round 8's 94k-case fuzz left open. See
# the fuzz-axis comments at the bottom of this file for the methodology fix.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    (
        # Round-9 C1 (CRITICAL, leak). A query `@` in a parameter that is not
        # the last one. The pre-acceptance escape in `_not_query_of_path`
        # accepted the `@` as userinfo, so the cut deleted the `?` in front of
        # it and the query strip composed on top had nothing left to cut:
        # `SEKRET` was rendered as part of a fabricated hostname.
        ("ws://host:8765?r=bob@corp.com&token=SEKRET", "ws://host:8765"),
        ("ws://localhost:8765?r=bob@corp.com&token=SEKRET", "ws://localhost:8765"),
        ("ws://host:8765?bob@corp.com&token=SEKRET", "ws://host:8765"),
        (
            "wss://h.example:443/v1?r=bob@corp.com&token=SEKRET",
            "wss://h.example:443/v1",
        ),
    ),
    ids=str,
)
def test_round9_query_at_sign_before_another_parameter_keeps_the_host(text, expected):
    """The root cause is not the gate but `_HOST_PORT_RE`: its reg-name class
    admitted `&` and `=`, so `corp.com&token=SEKRET` fullmatched `host[:port]`
    and the remainder of a query string passed for an authority. `_HOST_CHAR`
    excludes both."""
    assert display_uri(text) == expected
    assert safe_exc_text(Exception(text)) == expected
    assert "SEKRET" not in display_uri(text)


@pytest.mark.parametrize(
    "text,expected",
    (
        # Round-9 C3 (CRITICAL, leak) — an *empty* authority after the `@`.
        # Branch (a) needs a non-empty tail, (b) a legal password, and (c)
        # demanded `host_end > at + 1`, so all three refused and the whole
        # credential was emitted verbatim. Reachable from a directly
        # configured `STT_WS_URI` with no exception in the path at all.
        ("ws://user:S3CRET/x@", "ws://"),
        ("ws://user:S3CRET/x@?token=T", "ws://"),
        ("ws://user:S3CRET/x@#f", "ws://"),
        ("wss://:S3CRET?q@", "wss://"),
        # Round-9 codex bonus 1 — a widened password whose `/` disqualifies
        # branch (b), against a bare tail that disqualifies (a), with the
        # `@` one word past the anchor so (c)'s `at < first_ws` refused too.
        ("ws://user:1234 /seg@localhost", "ws://localhost"),
        ("ws://user:1234 /seg@stt-box", "ws://stt-box"),
    ),
    ids=str,
)
def test_round9_empty_and_widened_tails_are_redacted(text, expected):
    assert display_uri(text) == expected
    assert safe_exc_text(Exception(text)) == expected


@pytest.mark.parametrize(
    "text,expected",
    (
        # Round-9 H2 (HIGH, leak). `scheme:///…` is an empty authority
        # followed by a path. The anchor sat on the extra `/`, so every
        # candidate's username span held one and `_is_userinfo_shaped`
        # refused it. The slash run is re-emitted, not skipped: dropping it
        # silently deleted characters and broke the empty-authority query
        # corpus.
        ("ws:///user:pass@host", "ws:///host"),
        ("ws:///user:pass@host:8765/v1?token=T", "ws:///host:8765/v1"),
        ("wss:////user:pass@h.local/v1", "wss:////h.local/v1"),
        # The bare-path twin, redacted through `_redact_text`'s retry anchor.
        ("///user:pass@host", "///host"),
        ("////user:pass@h.local/v1", "////h.local/v1"),
    ),
    ids=str,
)
def test_round9_extra_slash_authority_is_redacted(text, expected):
    assert display_uri(text) == expected
    assert safe_exc_text(Exception(text)) == expected


@pytest.mark.parametrize(
    "text,expected",
    (
        # Round-9 HIGH (architecture). The round-8 dotless-tail rule bounded
        # its tail at the next *whitespace* while its sibling
        # `_tail_accepts` branch (c), written in the same commit, bounded it
        # at `_AUTHORITY_STOP_RE` and documents why. A trailing path
        # therefore flipped the answer. Both forms now agree.
        ("ws://alice@corp.com:1234?a=1@localhost", "ws://localhost"),
        ("ws://alice@corp.com:1234?a=1@localhost/v1", "ws://localhost/v1"),
        ("ws://alice@corp.com:1234?a=1@stt-box:8765/v1", "ws://stt-box:8765/v1"),
    ),
    ids=str,
)
def test_round9_dotless_tail_rule_ignores_a_trailing_path(text, expected):
    assert display_uri(text) == expected


# ---------------------------------------------------------------------------
# Round 10. No behaviour change: the two tests below pin the *cost* of
# limitation 4 on both sides, because every attempt to close either side
# re-opens the other and the docstring had, until now, denied that the leak
# side existed at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    (
        # Limitation 4, leak half. The sibling of the round-9 sweep case
        # `ws://alice@corp.com:1234?a=1@localhost` (which redacts whole),
        # differing only in that the tail carries a dot -- so the dotless-tail
        # rule in `_not_query_of_path` does not fire, `corp.com:1234`
        # fullmatches a dotted `host[:port]`, and the `a=1` evidence vetoes
        # the second `@`. The pre-`?` half of the password survives.
        #
        # Found by round 10 and NOT by any earlier sweep: the generated
        # `_credential_corpus` has no `?key=value` password tail, and
        # `_bare_host_credential_corpus` has the tail but only bare,
        # single-label hosts -- so `?a=1` crossed with a dotted host crossed
        # with an `@`-bearing username was never generated.
        ("ws://alice@corp.com:1234?a=1@h.example", "ws://corp.com:1234"),
        ("wss://alice@corp.com:1234?a=1@h.example", "wss://corp.com:1234"),
        ("ws://alice@corp.com:1234?a=1@h.example/v1", "ws://corp.com:1234"),
        ("ws://alice@corp.com:1234?a=1@host.example.com:8765", "ws://corp.com:1234"),
    ),
    ids=str,
)
def test_limitation4_dotted_query_tail_keeps_half_the_password(text, expected):
    """Asserted as the *documented* behaviour, not as desirable behaviour.

    Closing it means letting the second `@` through, which is the same move
    as dropping the dotless-tail rule -- and that fabricates a host for every
    shape `test_limitation4_token_userinfo_counter_family` pins. Round 10
    measured eight variants of the two gates against three corpora; each one
    moved cases between the leak column and the fabrication column and none
    reduced both. If a future round changes this expectation it must also
    change that one, and must say which of the two costs it is buying.
    """
    assert display_uri(text) == expected
    assert safe_exc_text(Exception(text)) == expected


@pytest.mark.parametrize(
    "text,expected",
    (
        # The counter-family: a *token* userinfo (RFC-legal, colon-free) in
        # front of a real authority whose query carries an `@`. These keep
        # their real host today only because `_not_query_of_path` vetoes on
        # the dotted tail...
        ("ws://alice@host.example:443?x=peer@corp.com", "ws://host.example:443"),
        ("ws://TOKEN123@host.example:443?r=bob@corp.com", "ws://host.example:443"),
        (
            "wss://alice@corp.example.com/v1?redirect=user@example.com",
            "wss://corp.example.com/v1",
        ),
        # ...and lose it when the tail is dotless, which is limitation 4's
        # fabrication half showing up in the same family. Pinned so the
        # asymmetry is visible rather than surprising.
        ("ws://alice@host.example.com:8765?x=peer@localhost", "ws://localhost"),
    ),
    ids=str,
)
def test_limitation4_token_userinfo_counter_family(text, expected):
    """The reason no local rule can close limitation 4's leak half.

    `ws://alice@host.example:443?x=peer@corp.com` (token userinfo, real host
    `host.example:443`, query value `corp.com`) and
    `ws://alice@corp.com:1234?a=1@h.example` (username `alice@corp.com`,
    password `1234?a=1`, host `h.example`) are the same grammar. Any rule
    that redacts the second fabricates a host for the first; round 10's
    `origin > start and ":" not in accepted_userinfo` discriminator did
    exactly that, trading 36 leaks for 144 new fabrications.
    """
    assert display_uri(text) == expected
    assert safe_exc_text(Exception(text)) == expected


@pytest.mark.parametrize(
    "text,expected",
    (
        # Round-9, found by the new query-secret fuzz rather than by a lens:
        # redacting a credential whose host is empty leaves a token that is
        # nothing but a query, and `_scheme_less_authority_is_evident`
        # refused a zero-length authority — so the step that exists to remove
        # a `?token=` let it through.
        ("user:hunter2@?token=SEKRET", ""),
        ("user:hunter2@/?token=SEKRET", "/"),
        ("ws://user:hunter2@?token=SEKRET", "ws://"),
        ("ws://user:hunter2@/v1?token=SEKRET", "ws:///v1"),
    ),
    ids=str,
)
def test_round9_query_only_token_is_stripped(text, expected):
    assert display_uri(text) == expected
    assert safe_exc_text(Exception(text)) == expected


# ---------------------------------------------------------------------------
# Generated sweep. Round 4's four leaks all lived in combinations no
# hand-written case covered, so the axes below are swept exhaustively rather
# than sampled: password delimiter x username shape x digit-first password
# x scheme-prefixed/bare x host shape.
#
# Round 8 added `//` to every scheme axis: the scheme-relative reference had
# never appeared in any corpus, and it defeated both scheme-less anchors.
# ---------------------------------------------------------------------------

_PASSWORD_TAILS = (
    "",
    "/seg",
    "?q",
    "#frag",
    " word",
    "://tail",
    # Round-6 finding B: an `@` inside the password whose own tail is a
    # clean, host-shaped token. Every pre-round-6 sweep axis put a
    # non-numeric port after the in-userinfo `@` (`corp.com:hunter2`), which
    # is why no generated case reached the settled-authority tie-break with
    # a host-shaped tail and the leak survived five rounds.
    "@ss word:x",
    " w@rd",
)
_USERNAMES = ("user", "1user", "", "alice@corp.com")
_PASSWORD_HEADS = ("hunter2", "1234", "AB+cd=")
_HOSTS = ("stt.example.internal:2020", "host:8765", "h.local")
_SUFFIXES = (
    "",
    " isn't a valid URI: nonempty path required",
    # Round-5 finding 2: trailing prose carrying an unrelated email. The
    # rightmost-`@`-wins scan used to let this `@` beat the real
    # in-authority one, discarding the true host and substituting
    # `example.com`. Swept across every credential shape, not hand-picked.
    " failed: admin@example.com",
)


def _credential_corpus():
    for user in _USERNAMES:
        for head in _PASSWORD_HEADS:
            for tail in _PASSWORD_TAILS:
                for host in _HOSTS:
                    for scheme in ("ws://", "wss://", "", "//"):
                        for suffix in _SUFFIXES:
                            password = head + tail
                            uri = f"{scheme}{user}:{password}@{host}/"
                            yield (
                                f"{uri}{suffix}",
                                user,
                                password,
                                f"{scheme}{host}/{suffix}",
                                uri,
                                f"{scheme}{host}/",
                            )


@pytest.mark.parametrize(
    "text,user,password,expected,uri,redacted_uri",
    list(_credential_corpus()),
    ids=str,
)
def test_sweep_every_credential_shape_is_fully_redacted(
    text, user, password, expected, uri, redacted_uri
):
    """Security invariant: for every generated `user:pass@host` shape, no
    part of the userinfo survives, and the host/suffix survive verbatim.
    Asserted through both entry points — `safe_exc_text` (exception text
    with optional trailing prose) and `redact_uri` (the bare URI)."""
    safe = safe_exc_text(Exception(text))
    assert password not in safe, (text, safe)
    if user:
        assert user not in safe, (text, safe)
    assert safe == expected, (text, safe)
    assert redact_uri(uri) == redacted_uri, uri


_PROSE_CORPUS = [
    # (leading token, joining words) -- credential-free text that must never
    # be truncated, swept against several unrelated `@` shapes.
    ("connection", "to"),
    ("Error:", "connect to"),
    ("stt_server error:", "could not reach"),
    ("2026-09-17T10:00:00", "connect"),
    ("C:/Users/bob", "connect"),
    ("unix:/tmp/x.sock", "failed: see"),
    ("[::1]:443", "cannot reach"),
]


@pytest.mark.parametrize("head,mid", _PROSE_CORPUS, ids=str)
@pytest.mark.parametrize("tail", ("user@host", "admin@corp", "ops@internal"))
def test_sweep_credential_free_prose_is_never_truncated(head, mid, tail):
    """The other half of the invariant: five consecutive rounds destroyed
    credential-free diagnostics by widening the search into prose. A bare,
    single-label host after the `@` is the shape that has to survive."""
    text = f"{head} {mid} {tail}"
    assert safe_exc_text(Exception(text)) == text


_NON_CREDENTIAL_CORPUS = (
    "ws://host:8765 isn't a valid URI: see user@guide",
    "ws://host:8765/path failed: could not reach user@relay",
    "wss://stt.internal:2020/v1 failed: could not reach user@relay",
    "wss://[::1]:2020/v1 failed: could not reach user@relay",
    # Deliberately NOT `ws://host:8765/p/user@y`: a path `@` with a bare
    # single-label tail is limitation 5, over-redacted since round 8 so that
    # `ws://user:S3CRETPW/seg@localhost` can be redacted at all. See
    # `test_authority_path_at_sign_with_a_dotted_tail_is_over_redacted`.
    "ws://host/path?redirect=user@example.org",
    "ws://host/path?redirect=user:pass@example.org",
    "GET https://example.com/api?redirect=user@example.org failed: 502",
    "admin@example.com for help",
    "connection refused: host unreachable",
    "u:p one two three four@host is invalid",
    # Round-5 finding 1: a ported, path-and-query-bearing URI whose `@` is
    # an ordinary query-value character. The port colon used to be read as
    # the userinfo colon, so the real host was discarded and the query's
    # tail substituted for it (`wss://example.com`).
    "wss://host:443/v1?redirect=user@example.com",
    "ws://localhost:8765/v1?user=bob@corp.com",
    # Round-6 `_redact.py:246`: the path-free twins of the two above. The
    # query guard used to require a `/path` in front of the `?`.
    "wss://host.example.com:443?redirect=bob@corp.com",
    "wss://host.example.com:443#redirect=bob@corp.com",
    "wss://stt.internal:2020?user=bob@corp.com",
    # Round-6 `_redact.py:315` / `:373`: a real, already-terminated
    # authority followed by <=2 prose words and an email address.
    "wss://host.example.com:443/v1 failed: user@corp.com",
    "unix:/tmp/x.sock error: svc@host.com",
    "host.example.com:443/v1 failed: user@corp.com",
)


@pytest.mark.parametrize("text", _NON_CREDENTIAL_CORPUS, ids=str)
def test_sweep_non_credential_authorities_pass_through_unchanged(text):
    """The scanner is userinfo-only, so every one of these must survive it
    character-for-character. (`safe_exc_text` additionally drops any query
    string — see `test_sweep_non_credential_authorities_keep_their_host`.)"""
    assert redact_uri(text) == text


@pytest.mark.parametrize("text", _NON_CREDENTIAL_CORPUS, ids=str)
def test_sweep_non_credential_authorities_keep_their_host(text):
    """Round-5 findings 1 and 2: the destructive failure mode is not a leak
    but a *fabricated host* — the real authority discarded and replaced by
    a hostname pulled from later in the string. Whatever the query strip
    removes, the scheme + authority prefix must always survive intact."""
    safe = safe_exc_text(Exception(text))
    head = text.split("?")[0].split("#")[0]
    assert safe.startswith(head), (text, safe)


# ---------------------------------------------------------------------------
# Round 7. `_credential_corpus` and `_NON_CREDENTIAL_CORPUS` are generated
# *disjointly*: one sweeps credential shapes with no query-`@`, the other
# query-`@` shapes with no credential. The fabricated-host class survived two
# rounds inside the gap between them — a URI carrying BOTH a real
# `user:pass@` userinfo AND a later `@` in its query collapsed to the query's
# domain (`wss://user:pass@host.example.com/v1?r=bob@corp.com` ->
# `wss://corp.com`), while its credential-free twin was handled correctly.
# The two families are crossed here so the gap cannot reopen silently.
# ---------------------------------------------------------------------------

_QUERY_AT_TAILS = (
    "?r=bob@corp.com",
    # Deliberately not `redirect=user@...`: the sweep asserts the username
    # is absent from the output, and a query value spelling it would make
    # every case a false failure rather than a real one.
    "?redirect=peer@example.com",
    "#f=bob@corp.com",
    "?a=1&r=bob@corp.com#frag",
)
_COMBINED_HOSTS = (
    "host.example.com",
    "host.example.com:443",
    "[::1]:443",
    "1.2.3.4:8765",
    "localhost:8765",
)


def _combined_corpus():
    """Credential shapes crossed with query-`@` shapes.

    The invariant has two halves and both are asserted: the userinfo is
    gone, *and* the scheme+authority+path in front of the `?`/`#` survives
    character-for-character. A fabricated host satisfies the first half on
    its own, which is why five rounds of leak-only assertions never caught
    this class.
    """
    for user, password in (
        ("user", "pass"),
        ("user", "1234"),
        ("", "s3cr3t"),
        ("alice@corp.com", "hunter2"),
        ("user", "AB+cd="),
    ):
        for host in _COMBINED_HOSTS:
            for path in ("/v1", "/", ""):
                for tail in _QUERY_AT_TAILS:
                    for scheme in ("ws://", "wss://", ""):
                        head = f"{scheme}{host}{path}"
                        yield (
                            f"{scheme}{user}:{password}@{host}{path}{tail}",
                            password,
                            user,
                            head,
                        )


@pytest.mark.parametrize("uri,password,user,head", list(_combined_corpus()), ids=str)
def test_sweep_credential_plus_query_at_keeps_the_real_host(uri, password, user, head):
    """Round-7 HIGH (logic lens): a credential and a query-`@` in the same
    URI composed into the fabricated-host class. `_not_query_of_path`
    measured the authority from the *anchor*, so the already-accepted
    `user:pass@` prefix was still inside the span, `_HOST_PORT_RE` (which
    excludes `@`) could never match it, and the gate that exists to refuse
    exactly this never fired. It now measures from the acceptance point."""
    for rendered in (redact_uri(uri), safe_exc_text(Exception(uri))):
        assert password not in rendered, (uri, rendered)
        if user:
            assert user not in rendered, (uri, rendered)
        assert rendered.startswith(head), (uri, rendered)
    # The query strip removes the `@`-bearing tail outright.
    assert safe_exc_text(Exception(uri)) == head, uri
    assert display_uri(uri) == head, uri


def _query_corpus():
    """Round-6 finding A, swept: every URI token shape that carries a query,
    with and without a scheme. The scheme-less half is the one
    `_strip_query_spans` never scanned.

    Round-7 finding B added the single-label host axis. Round 6 closed the
    scheme-less anchor for *dotted* hosts only, and `localhost` — this
    project's own canonical local STT endpoint — has no dot and never will,
    so `localhost:8765/v1?token=...` kept leaking through every sink the
    round-6 fix was written for.
    """
    for scheme in ("ws://", "wss://", "", "//"):
        for host in (
            "stt.example.internal:2020",
            "h.local",
            "[::1]:2020",
            "localhost:8765",
            "localhost",
            "stt:8765",
        ):
            for path in ("", "/", "/v1"):
                for mark in ("?", "#"):
                    for suffix in ("", " isn't a valid URI: nonempty path required"):
                        head = f"{scheme}{host}{path}"
                        yield (
                            f"{head}{mark}token=QUERYSECRET{suffix}",
                            f"{head}{suffix}",
                        )


@pytest.mark.parametrize("text,expected", list(_query_corpus()), ids=str)
def test_sweep_every_query_bearing_uri_is_stripped(text, expected):
    safe = safe_exc_text(Exception(text))
    assert "QUERYSECRET" not in safe, (text, safe)
    assert safe == expected, (text, safe)


# ---------------------------------------------------------------------------
# strip_query — composed on top of redact_uri by the two display call sites.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "uri,expected",
    (
        ("wss://host:443/v1?token=s3cr3t", "wss://host:443/v1"),
        ("wss://host:443/v1#s3cr3t", "wss://host:443/v1"),
        ("wss://host:443/v1?a=1&token=s3cr3t#frag", "wss://host:443/v1"),
        ("wss://host:443/v1", "wss://host:443/v1"),
        ("ws://host", "ws://host"),
        ("unix:/tmp/stt.sock", "unix:/tmp/stt.sock"),
    ),
    ids=str,
)
def test_strip_query_drops_query_and_fragment(uri, expected):
    """Round-4 finding 20: the scanner is userinfo-only, so a token in a
    query string echoed verbatim into every endpoint label and preflight
    error. Both display call sites now compose this on top of
    `redact_uri`."""
    assert strip_query(uri) == expected


def test_strip_query_composes_with_redact_uri():
    assert strip_query(redact_uri("wss://u:p@host:443/v1?token=s3cr3t")) == (
        "wss://host:443/v1"
    )


def test_strip_query_leaves_an_unparseable_uri_to_redact_uri():
    """Fails closed: an unparseable URI keeps whatever `redact_uri` made of
    it rather than being re-rendered from half-parsed components."""
    bad = "ws://u:p@[::1/v1?token=s3cr3t"
    assert strip_query(bad) == bad


# ---------------------------------------------------------------------------
# Round 8. Cross-product fuzz over every axis at once, asserted as loops
# rather than parametrized cases: ~92k inputs is too many ids for pytest to
# carry, and the value here is coverage of *combinations*, not per-case
# reporting. This is what found the last round-8 leak
# (`ws://user:p/x@localhost?token=T` — a bare host with a query behind it,
# which `_tail_accepts` branch (c) measured past).
# ---------------------------------------------------------------------------

_FUZZ_SCHEMES = ("ws://", "wss://", "", "//", "unix:")
_FUZZ_USERS = ("user", "", "alice@corp.com", "1u")
_FUZZ_PASSWORDS = (
    "hunter2",
    "1234",
    "AB+cd=",
    "p/x",
    "p?q",
    "p#f",
    "p:z",
    "p w",
    "p ?q",
    "p /s",
    "p://t",
)
_FUZZ_HOSTS = (
    "localhost",
    "host:8765",
    "h.local",
    "stt.ex.com:2020",
    "[::1]:443",
    "host",
    "",
)
_FUZZ_PATHS = ("", "/", "/v1", "/v1/x")
# Every query axis that carries a secret spells it `QSEKRET`, and every
# `@`-bearing one exists in two forms: the `@` in the LAST parameter, and the
# `@` in a parameter with a secret-bearing one *behind* it.
#
# Round 9 root cause of the methodology gap: until this round every query-`@`
# axis put the `@` last. A userinfo cut that swallows the `?` in front of the
# `@` leaves the tail of the query behind as ordinary text — and once the `?`
# is gone, the query strip composed on top of the scanner has nothing to cut,
# so whatever followed the `@` is rendered verbatim as if it were a hostname.
# With the `@` always last there was nothing after it to leak, so ~94k cases
# reported the module clean while
# `ws://host:8765?r=bob@corp.com&token=SEKRET` rendered as
# `ws://corp.com&token=SEKRET`.
_FUZZ_QUERIES = (
    "",
    "?token=QSEKRET",
    "#f",
    "?a=1&r=bob@corp.com",
    "?q@x",
    "?r=bob@corp.com&token=QSEKRET",
    "?q@x&token=QSEKRET",
    "#f=bob@corp.com&token=QSEKRET",
)
_FUZZ_SUFFIXES = ("", " failed: admin@example.com", " isn't a valid URI: x")


def _is_subsequence(out: str, src: str) -> bool:
    it = iter(src)
    return all(ch in it for ch in out)


def test_fuzz_output_is_always_a_deletion_of_the_input():
    """The module's stated invariant, asserted directly: the output is the
    input with zero or more spans deleted. Nothing is ever rewritten,
    re-ordered or synthesised — which is what makes "apply exactly once" safe
    advice rather than a correctness requirement (a second pass can delete a
    little more, but can never re-expose what the first pass removed)."""
    checked = 0
    for parts in itertools.product(
        _FUZZ_SCHEMES,
        _FUZZ_USERS,
        _FUZZ_PASSWORDS,
        _FUZZ_HOSTS,
        _FUZZ_PATHS,
        _FUZZ_QUERIES,
        _FUZZ_SUFFIXES,
    ):
        scheme, user, password, host, path, query, suffix = parts
        text = f"{scheme}{user}:{password}@{host}{path}{query}{suffix}"
        checked += 1
        for rendered in (safe_exc_text(Exception(text)), display_uri(text)):
            assert _is_subsequence(rendered, text), (text, rendered)
            # Second pass: still deletion-only, never re-exposure.
            assert _is_subsequence(display_uri(rendered), rendered), (text, rendered)
    assert checked > 90_000, checked


def test_fuzz_single_token_credentials_never_leak():
    """The security half. Restricted to credentials that fit in ONE
    whitespace-delimited token: those are unambiguous by construction, so
    there is no prose reading to trade against and a surviving password is a
    leak, full stop. (Spaced passwords are limitations 2 and 3 and are swept
    separately, with their expected outputs rather than a blanket rule.)

    Round 9 removed the `if h` filter that had excluded the **empty host**
    from this assertion since the sweep was written. An empty authority is
    well-formed RFC 3986, not a degenerate input, and it defeated every
    branch of `_tail_accepts` at once — so `ws://user:p/x@` and
    `ws://user:p/x@?token=T` were emitted whole, password and all, straight
    out of a directly-configurable `STT_WS_URI` with no exception anywhere in
    the path. Filtering a shape out of a security assertion is how a leak
    survives a 94k-case sweep.
    """
    checked = 0
    for parts in itertools.product(
        ("ws://", "wss://", "ws:///", "", "//", "///"),
        _FUZZ_USERS,
        tuple(p for p in _FUZZ_PASSWORDS if " " not in p),
        _FUZZ_HOSTS,
        _FUZZ_PATHS,
        _FUZZ_QUERIES,
    ):
        scheme, user, password, host, path, query = parts
        uri = f"{scheme}{user}:{password}@{host}{path}{query}"
        checked += 1
        for rendered in (display_uri(uri), safe_exc_text(Exception(uri))):
            assert password not in rendered, (uri, rendered)
            if user:
                assert user not in rendered, (uri, rendered)
    assert checked > 10_000, checked


def test_fuzz_a_query_borne_secret_never_survives():
    """The third half, and the one the module had no sweep for at all: a
    secret carried in the **query string** rather than the userinfo.

    `?token=…` is the shape this project's own `STT_WS_URI` uses, and the
    query strip that removes it is composed *on top of* the userinfo scanner
    — so it only ever sees whatever the scanner left behind. Any cut that
    deletes the `?` disarms it silently. Swept over the same cross product as
    the credential fuzz, with and without a credential in front, because the
    disarming cut is made by the credential scan.
    """
    checked = 0
    secret_queries = tuple(q for q in _FUZZ_QUERIES if "QSEKRET" in q)
    for parts in itertools.product(
        ("ws://", "wss://", "ws:///", "", "//"),
        _FUZZ_USERS,
        tuple(p for p in _FUZZ_PASSWORDS if " " not in p),
        _FUZZ_HOSTS,
        _FUZZ_PATHS,
        secret_queries,
        ("", " isn't a valid URI: x"),
    ):
        scheme, user, password, host, path, query, suffix = parts
        if not host and not scheme.rstrip("/"):
            # Limitation 7: a token with no scheme AND no host has nothing
            # vouching for it at all, and `/v1?token=…` is the same string as
            # the prose `///triple/slash?not=a-host` that
            # `test_protocol_relative_anchor_does_not_step_into_prose` pins.
            # Skipped rather than dropped from the axes so the gap stays
            # visible; the scheme-prefixed twin is asserted by
            # `test_empty_authority_uri_still_has_its_query_stripped`.
            continue
        for userinfo in (f"{user}:{password}@", ""):
            uri = f"{scheme}{userinfo}{host}{path}{query}{suffix}"
            checked += 1
            for rendered in (display_uri(uri), safe_exc_text(Exception(uri))):
                assert "QSEKRET" not in rendered, (uri, rendered)
    assert checked > 10_000, checked
