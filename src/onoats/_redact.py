"""Credential redaction for exception text (and raw URIs) that reach
user-visible output.

Leaf module: imports nothing from ``onoats`` (stdlib-only). Both
``onoats.runtime`` (the startup/preflight path) and
``onoats.stt.websocket_stt_service`` (the live-session reconnect path) need
to strip ``user:pass@`` userinfo out of a third-party exception's ``str()``
(or a raw connect URI) before it reaches a log line or a user-visible error
message — a raw ``websockets.exceptions.InvalidURI`` (``f"{uri} isn't a
valid URI: {msg}"``) embeds the exact, unredacted connect URI the caller
typed in, credential and all. Previously ``safe_exc_text`` (as
``_safe_exc_text``) lived in ``runtime.py`` and ``websocket_stt_service.py``
imported it as a private cross-module symbol — inverting this codebase's
dependency direction. Giving redaction its own leaf module, with public
entry points, lets both sides import it top-level with no cycle and no
reach into a leaf-module's underscore names.

**Algorithm (round 10 — fourth rewrite of this logic; see the dev plan's
Round 7/8/9/10 findings for the history of narrower attempts that each left
a gap).** Every prior rewrite bounded the "candidate credential span" using
a SINGLE character class applied uniformly, and every one of those classes
excluded some character a real (if malformed) password can legitimately
contain — round 7 excluded whitespace; round 8's scheme-less path excluded
whitespace; round 9 excluded ``/``, ``?``, ``#`` from BOTH the credential
search region *and* required an ``@`` to already be inside that narrowed
region, so any of those characters in the password made the whole ``@``
invisible to the search and the entire string passed through unredacted.
Round 9 also introduced the opposite failure: `_redact_bare_leading_credential`
was not actually anchored to a credential *shape*, just to string-start, so
ANY leading prose containing an ``@`` before the first ``/?#`` (e.g. "contact
admin@example.com for help") was destructively "redacted" down to
"example.com for help" with no credential present at all.

This rewrite drops the single-character-class approach and instead performs
a **tiered search for the credential-marking ``@``**, widening the search
window only when there is a positive signal that widening is warranted —
never unconditionally. Given a starting position (right after a
``scheme://`` prefix, or the start of the message for the scheme-less
"bare" case — see below), and an ``outer_stop`` bound (the position of the
next nested ``scheme://`` occurrence, if any, else end-of-string — this is
what keeps one URI's authority from ever reaching into a second URI's
territory):

1. **Tier 1** — the common case, matching every prior round's baseline:
   search for ``@`` in the span up to the first of ``/``, ``?``, ``#``, or
   whitespace (or ``outer_stop``). If found, this is authoritative: it
   correctly handles a normal credential, a message with no credential at
   all (the same "no ``/?#``-or-whitespace-crossing ``@``" test that already
   protects an unrelated query-string ``user@host`` — see case (e) below),
   and a message with multiple credentials (each URI's authority is
   naturally bounded before the next one is even considered, since nothing
   in this tier ever looks past the first stop character).
2. **Tier 2** — only tried when tier 1 finds no ``@``: re-search ignoring
   ``/``, ``?``, ``#`` as stops (bounded only by whitespace / ``outer_stop``)
   — this is what a password containing one of those characters needs (a
   *malformed* URI is exactly what ``InvalidURI`` is raised for, so this is
   a realistic shape, not an adversarial one). This widened match is used
   **only if** the text between tier 1's stop point and the found ``@``
   contains no ``=`` — an ``=`` there is the signature of URL *query*
   structure (``?redirect=user@host``), which is exactly the shape that
   must NOT be swallowed (this is what keeps case (e) below correct even
   though tier 2 no longer stops at ``?``).
3. **Tier 3** — only tried when tier 1 AND tier 2 both find no ``@``, and
   only when the text up to the first whitespace already contains a ``:``
   (a `user:` -shaped start — the gate that keeps this tier from firing on
   arbitrary two-word prose): re-search bounded by the *second* whitespace
   (or ``outer_stop``) instead of the first. This is what a password
   containing a raw, un-encoded space needs (case (a) below) — tolerating
   exactly one extra whitespace-delimited word, never scanning further.

Each tier reports both the ``@`` position and the window boundary it used,
so the caller redacts exactly ``[start, @)`` and resumes unmodified output
at the window boundary (not at ``outer_stop`` — see the multi-credential
case (c) below for why those two are NOT the same thing).

**Invariant:** every character of the original string that is not part of a
credential span discarded by one of the three tiers above is preserved
verbatim in the output — including trailing diagnostic text, a second URI
in the same message, and any ``@`` that does not pass one of the three
tiers' checks.

Traced against the required cases:

    (a) scheme-less, password has a space —
        ``"secretuser:hunter 2@stt.example.internal:2020 isn't a valid URI: ..."``
        tier 1 (up to first space, "secretuser:hunter") has no ``@``; tier 2
        (still bounded by that same first space, since no ``/?#`` appears
        before it) also fails; tier 3 sees ``:`` before the first space and
        extends to the second space, finding the ``@`` there — redacts to
        ``"stt.example.internal:2020 isn't a valid URI: ..."``.
    (b) scheme-prefixed, trailing prose with no port-shaped suffix —
        ``"ws://user:pass@host:9999 isn't a valid URI: nonempty path required"``
        tier 1 (up to the first space, before "isn't") finds the ``@``
        immediately — redacts to ``"ws://host:9999"`` and splices the rest
        of the string back completely unmodified (never re-inspected, so it
        can never be swallowed no matter what it contains).
    (c) multiple credentials in one message —
        ``"ws://u1:p1@h1 and ws://u2:p2@h2 both failed"`` — the first
        match's tier 1 window is bounded at the first whitespace (the space
        before "and"), which comes long before the second ``ws://`` even if
        ``outer_stop`` (the second scheme's start) is used as the *outer*
        cap — the window boundary actually used for redaction is the
        *tighter* of the two, so "h1" is correctly isolated and " and " is
        spliced back verbatim before the second URI is processed
        independently.
    (d) password containing ``@`` — tier 1 finds ``@`` via ``rfind`` (last
        occurrence in the window), so an embedded ``@`` in the password is
        kept on the discarded side, not leaked as a fake host.
    (e) unrelated query-string ``user@host`` —
        ``"https://example.com/api?redirect=user@example.org"`` — tier 1
        (up to the first ``/``) has no ``@``; tier 2 widens past ``/`` and
        ``?`` and DOES find the ``@`` in "redirect=user@example.org", but
        the text between tier 1's stop (the ``/``) and that ``@`` is
        ``"api?redirect=user"``, which contains ``=`` — tier 2 refuses the
        match — tier 3 requires a ``:`` before the first whitespace, and
        there is no whitespace here at all so tier 3's gate (checked
        against the first-whitespace-bounded prefix) also fails to engage.
        The message passes through completely unchanged.
    (f) password containing ``/``, ``?``, or ``#`` —
        ``"ws://user:pa/ss@host/path"`` — tier 1 (up to the first ``/``,
        right after "pa") has no ``@``; tier 2 widens past that ``/`` and
        finds the ``@`` in "ss@host/path"; the text between tier 1's stop
        and the ``@`` is ``"ss"`` — no ``=`` — tier 2 accepts, redacting to
        ``"ws://host/path"``. The ``?`` and ``#`` cases are symmetric.
    (g) nested URL inside an unrelated query string —
        ``"https://host/path?redirect=https://user:pass@evil/path"`` — the
        outer match's ``outer_stop`` is capped at the second ``https://``
        occurrence (found independently by the same top-level scan), so the
        outer tier 1/2/3 search never even looks past "path?redirect=" —
        finds no credential there, leaves it untouched — and the *inner*
        ``https://user:pass@evil/path`` is redacted on its own, independent
        pass. The outer URL's structure survives intact; only the inner,
        genuinely credential-shaped span is touched.
    (h) leading prose containing an unrelated ``@``, no credential present —
        ``"connection to user@host failed"`` — the bare-leading path (see
        below) requires the SAME three-tier search anchored to string
        start: tier 1 (up to the first space, "connection") has no ``@``
        and no ``:``, so tier 3 never even attempts to engage — the message
        passes through completely unchanged.
    (i) IPv6 host in brackets — brackets and colons aren't whitespace or
        ``/?#``, so the host re-bounding step keeps ``"[::1]:443"`` intact.
    (j) no credential-shaped substring anywhere — no tier ever finds a
        usable ``@``, so nothing is ever replaced; the message passes
        through unchanged.

The scheme-less (bare) case is applied **only** at the literal start of the
message (after any leading whitespace, preserved verbatim) — this is what
keeps an unrelated ``user@host``-shaped substring elsewhere in a message,
or leading prose that happens to contain both a colon and an ``@`` within
its first two words (a narrow, acknowledged residual ambiguity — see the
module's test suite for the exact boundary), from being misread as a
credential. It is anchored this way because it is specifically the leading
token of ``f"{uri} isn't a valid URI: {msg}"`` — the one realistic shape a
scheme-less credential reaches this function through.

**Known, accepted limitation:** a password containing TWO OR MORE of
``/``, ``?``, ``#`` in a way that makes the tier-2 "no ``=`` in the gap"
check ambiguous with genuine query structure cannot be perfectly
distinguished from a look-alike non-credential string using only local
syntax — this is a fundamentally ambiguous problem for a generic text
scanner with no schema information, not a solvable bug. The tiers above are
biased toward NOT leaking a credential (tier 1/2/3 lean permissive) while
still protecting the one concretely-reported "don't corrupt an unrelated
query string" case (e). ``redact_uri`` below (used by
``runtime._display_target`` and ``websocket_stt_service._endpoint_label``,
both of which start from structured ``host``/``port``/``uri`` connect
kwargs rather than arbitrary exception text) is a thin wrapper around the
same tiered search — round 10 found that `_display_target`'s prior
implementation instead trusted `urllib.parse.urlsplit`'s `username`/
`password` properties, which silently report no userinfo at all (rather
than raising) when the password contains an unencoded ``/``, ``?``, or
``#`` — the exact malformed shape this module exists to still redact.
"""

from __future__ import annotations

import re

_SCHEME_PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_TIER1_STOP_RE = re.compile(r"[/?#\s]")
_WHITESPACE_RE = re.compile(r"\s")


def _first_stop(text: str, start: int, pattern: re.Pattern[str], limit: int) -> int:
    """Position of the first match of `pattern` in `text[start:limit]`, or
    `limit` if there is none."""
    m = pattern.search(text, start, limit)
    return m.start() if m else limit


def _find_credential_at(
    text: str, start: int, outer_stop: int
) -> tuple[int, int] | tuple[None, None]:
    """Locate the credential-marking ``@`` in `text[start:outer_stop]`.

    Returns `(at, window_end)` where `text[start:at]` is the userinfo to
    discard and `text[at + 1:window_end]` is the credential-free remainder
    to keep, or `(None, None)` if no credential is judged present. See the
    module docstring for the three-tier search this implements.
    """
    tier1_stop = _first_stop(text, start, _TIER1_STOP_RE, outer_stop)
    at = text.rfind("@", start, tier1_stop)
    if at != -1:
        return at, tier1_stop

    tier2_stop = _first_stop(text, start, _WHITESPACE_RE, outer_stop)
    at = text.rfind("@", start, tier2_stop)
    if at != -1 and "=" not in text[tier1_stop:at]:
        return at, tier2_stop

    first_ws = _first_stop(text, start, _WHITESPACE_RE, outer_stop)
    if ":" not in text[start:first_ws]:
        return None, None
    tier3_stop = (
        _first_stop(text, first_ws + 1, _WHITESPACE_RE, outer_stop)
        if first_ws < outer_stop
        else outer_stop
    )
    at = text.rfind("@", start, tier3_stop)
    if at != -1:
        return at, tier3_stop
    return None, None


def _next_scheme_start(text: str, pos: int) -> int:
    m = _SCHEME_PREFIX_RE.search(text, pos)
    return m.start() if m else len(text)


def _redact_text(text: str) -> str:
    """Core implementation shared by `safe_exc_text` and `redact_uri`.

    A bare, standalone URI is just degenerate "exception text" with no
    trailing prose, so the same tiered search handles both callers
    correctly — see the module docstring for the full algorithm and the
    invariant it maintains.
    """
    n = len(text)
    out: list[str] = []

    stripped_offset = len(text) - len(text.lstrip())
    first_scheme = _SCHEME_PREFIX_RE.search(text)
    bare_outer_stop = first_scheme.start() if first_scheme else n
    at, window_end = _find_credential_at(text, stripped_offset, bare_outer_stop)
    if at is not None:
        out.append(text[:stripped_offset])
        out.append(text[at + 1 : window_end])
        cursor = window_end
    else:
        cursor = 0

    for m in _SCHEME_PREFIX_RE.finditer(text, cursor):
        out.append(text[cursor : m.start()])
        out.append(m.group(0))
        authority_start = m.end()
        outer_stop = _next_scheme_start(text, authority_start)
        at, window_end = _find_credential_at(text, authority_start, outer_stop)
        if at is not None:
            out.append(text[at + 1 : window_end])
            cursor = window_end
        else:
            cursor = authority_start

    out.append(text[cursor:])
    return "".join(out)


def safe_exc_text(exc: BaseException) -> str:
    """Render `str(exc)` with any embedded URI userinfo stripped.

    Handles both a `scheme://user:pass@host` shape (anywhere in the
    message, including more than one) and a scheme-less `user:pass@host`
    shape (only when it is the message's leading token — see module
    docstring). See the module docstring for the full algorithm and the
    invariant it maintains.
    """
    return _redact_text(str(exc))


def redact_uri(uri: str) -> str:
    """Strip `user:pass@` userinfo from a single, standalone URI string.

    For `runtime._display_target` and `websocket_stt_service._endpoint_label`,
    both of which build their input from structured `host`/`port`/`uri`
    connect kwargs. Deliberately reuses `_redact_text`'s tiered search
    rather than `urllib.parse.urlsplit`'s netloc-bounded `username`/
    `password` properties: `urlsplit` silently reports no userinfo at all
    (rather than raising) when the password contains an unencoded `/`,
    `?`, or `#` — the exact shape that must still be redacted here (this
    was `_display_target`'s round-10-reported bug: it trusted `urlsplit`'s
    truthy check and returned the raw, credential-bearing URI unchanged
    whenever that check went silently wrong).
    """
    return _redact_text(uri)
