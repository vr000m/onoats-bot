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


def test_no_credential_shaped_substring_passes_through_unchanged():
    text = "connection refused: host unreachable"
    assert safe_exc_text(Exception(text)) == text


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
