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


def test_authority_path_unrelated_at_sign_in_a_path_segment_untouched():
    """The port gate also covers tier 2: an unrelated `user@host` inside a
    path segment of a `host:PORT` authority must not be read as userinfo."""
    text = "ws://host:8765/p/user@y"
    assert safe_exc_text(Exception(text)) == text


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
# Generated sweep. Round 4's four leaks all lived in combinations no
# hand-written case covered, so the axes below are swept exhaustively rather
# than sampled: password delimiter x username shape x digit-first password
# x scheme-prefixed/bare x host shape.
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
                    for scheme in ("ws://", "wss://", ""):
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
    "ws://host:8765/p/user@y",
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


def _query_corpus():
    """Round-6 finding A, swept: every URI token shape that carries a query,
    with and without a scheme. The scheme-less half is the one
    `_strip_query_spans` never scanned."""
    for scheme in ("ws://", "wss://", ""):
        for host in ("stt.example.internal:2020", "h.local", "[::1]:2020"):
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
