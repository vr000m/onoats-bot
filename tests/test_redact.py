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

from onoats._redact import redact_uri, safe_exc_text, strip_query


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
    exc = Exception("https://host/path?redirect=https://user:pass@evil/path")
    safe = safe_exc_text(exc)
    assert "user:pass" not in safe
    assert safe == "https://host/path?redirect=https://evil/path"


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
    assert safe_exc_text(Exception(text)) == text


def test_unrelated_query_string_with_colon_still_untouched():
    """Codex-adversarial finding: a `user:pass@host` shape inside an
    unrelated redirect query string was misclassified as authority
    credentials by tier 3 (which lacked tier 2's "no '=' in the gap"
    guard), corrupting the real path and query down to just the host."""
    text = "ws://host/path?redirect=user:pass@example.org"
    assert safe_exc_text(Exception(text)) == text


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
# Generated sweep. Round 4's four leaks all lived in combinations no
# hand-written case covered, so the axes below are swept exhaustively rather
# than sampled: password delimiter x username shape x digit-first password
# x scheme-prefixed/bare x host shape.
# ---------------------------------------------------------------------------

_PASSWORD_TAILS = ("", "/seg", "?q", "#frag", " word")
_USERNAMES = ("user", "1user", "", "alice@corp.com")
_PASSWORD_HEADS = ("hunter2", "1234", "AB+cd=")
_HOSTS = ("stt.example.internal:2020", "host:8765", "h.local")
_SUFFIXES = ("", " isn't a valid URI: nonempty path required")


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


@pytest.mark.parametrize(
    "text",
    (
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
    ),
    ids=str,
)
def test_sweep_non_credential_authorities_pass_through_unchanged(text):
    assert safe_exc_text(Exception(text)) == text


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
