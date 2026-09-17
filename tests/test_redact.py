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
"""

from onoats._redact import redact_uri, safe_exc_text


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
