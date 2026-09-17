"""Credential redaction for exception text (and raw URIs) that reach
user-visible output.

Leaf module: imports nothing from ``onoats`` (stdlib-only). Both
``onoats.runtime`` (the startup/preflight path) and
``onoats.stt.websocket_stt_service`` (the live-session reconnect path) need
to strip ``user:pass@`` userinfo out of a third-party exception's ``str()``
(or a raw connect URI) before it reaches a log line or a user-visible error
message — a raw ``websockets.exceptions.InvalidURI``
(``f"{uri} isn't a valid URI: {msg}"``) embeds the exact, unredacted connect
URI the caller typed in, credential and all.

**Why this is a rewrite, not a patch (round 4 of the second gauntlet loop —
the eighth rewrite of this logic).** Every previous version was a *tiered*
scanner: three successively wider searches, each with its own accept
conditions and its own vetoes (an all-digit ``host:PORT`` veto, a
query-structure veto, a tier-1-accepts-unconditionally-on-the-authority-path
rule). Every round, patching one tier's condition either reopened an older
leak from a new angle or created a new one, because a veto written to
protect one tier silently disabled a protection another tier relied on. Two
of the four credential leaks found in round 4 were *caused* by vetoes added
in round 3.

This version has **no tiers and no vetoes.** There is one candidate scan and
two positive gates, and a candidate is redacted only when it passes them.

**The algorithm.** The credential's *start* is never guessed: it is either
the character after a ``scheme://`` prefix (the **authority path**) or the
message's first non-whitespace character (the **bare path**). Only the
*end* — which ``@`` terminates the userinfo — is in question. So:

1. Compute the search window: from ``start`` to the first whitespace, then
   widened by at most :data:`_MAX_USERINFO_SPACES` further whitespace-
   delimited words (a raw, un-encoded space in a typed-in password is
   realistic; a whole sentence of diagnostic prose is not), and capped by
   ``outer_stop`` — the next ``scheme://`` occurrence, which is what keeps
   one URI's authority from reaching into a second URI's territory.
2. Walk every ``@`` in that window left to right and keep the *last* one
   that passes the gates for this path. The rightmost accepted ``@`` is the
   one to split at, so an ``@`` embedded in either the username or the
   password stays on the discarded side.

**The two gates** (both positive tests; nothing is accepted by failing to
match something else):

* :func:`_is_userinfo_shaped` — the span ``text[start:at]`` must hold a
  ``:`` bonded directly to a non-whitespace character, with a username
  before it free of ``/ ? # [ ] :`` and whitespace. This is what rejects an
  IPv6 literal (``[::1]:443`` — the ``[``), an ordinary ``"Word: "``
  diagnostic prefix (whitespace right after the colon), a path- or
  query-bearing span (``host/path?redirect=user`` — the ``/``), and prose
  with no colon at all (``connection to user@host failed``). The username
  may be empty (``:token@host`` is RFC-legal userinfo and a real
  token-auth convention) and may itself contain ``@`` (an email-as-username
  ``alice@corp.com:pw@host``).
* :func:`_tail_accepts` — required for every candidate that sits past the
  authority's own ``/?#``-or-whitespace boundary, i.e. exactly the
  *malformed* shapes this module exists for. Either (a) the token after the
  ``@`` looks like a URI continuation (it contains ``.``, ``:``, ``/`` or
  ``[`` — a host, a ``host:port``, or a host followed by a path), or (b)
  the password is RFC-legal userinfo apart from its spaces (no
  ``/ ? # [ ] :``). One of those two is true of every genuine credential
  and false of the prose shapes that six rounds of destructive truncation
  traced back to (``2026-09-17T10:00:00 connect user@host``,
  ``C:/Users/bob connect user@host``).

On the **authority path** a candidate inside the authority proper (before
the first ``/?#`` or whitespace) is accepted unconditionally: RFC 3986 says
everything before an ``@`` in an authority *is* userinfo, password or not.
On the **bare path** there is no authority, so :func:`_is_userinfo_shaped`
is required for every candidate — a message opening with a bare email
address is prose, not a credential.

The port heuristic and the query-structure guard that previous rounds
needed are both **gone**, not relaxed: ``ws://host:8765 ... user@guide``
and ``?redirect=user@host`` are now rejected by the two gates above
(a bare ``guide`` tail; a ``/``-bearing username), so there is nothing left
for a special case to decide, and nothing left to invert into a leak.

**Invariant:** the output is the input with zero or more spans of the form
``text[start:at + 1]`` deleted, where each ``start`` is a
``scheme://``-or-message-start anchor and each ``at`` is an ``@`` accepted
by the gates above. Every other character — trailing diagnostic text, a
second URI in the same message, any ``@`` that fails the gates — is
preserved verbatim.

**Known, accepted limitations** (each re-verified against *this*
implementation, not inherited from an earlier docstring):

1. A password crossing more than :data:`_MAX_USERINFO_SPACES` whitespace
   boundaries is not redacted by :func:`safe_exc_text` (the widened window
   does not reach its ``@``). Unbounded widening is what destroyed
   credential-free diagnostics in five consecutive rounds.
2. Prose of the exact shape ``word:word word@host.tld`` — a colon bonded
   with no space after it, within the widen bound, followed by a
   dotted/ported tail — is indistinguishable from a real credential by
   local syntax and *is* discarded (``"note:see bob@corp.com"`` ->
   ``"corp.com"``). Resolved toward redaction because the alternative is a
   leak.
3. Conversely, a credential whose password contains ``/`` or ``:`` *and*
   whose host is a bare single-label name (``user:a/b c@host``) fails both
   halves of :func:`_tail_accepts` and is not redacted.
4. :func:`safe_exc_text` and :func:`redact_uri` do not touch a credential
   carried in a URI *query string* (``wss://host/v1?token=s3cr3t``);
   userinfo is all the scanner claims. Display call sites that render a
   whole connect URI compose :func:`strip_query` on top — a query string
   has no diagnostic value in a log line, so it is dropped wholesale
   rather than inspected for secrets.

Named cases live in ``tests/test_redact.py``, alongside a generated sweep
over delimiter x digit-prefix x username-``@`` x scheme-prefixed/bare
combinations — hand-picked case lists are what missed every gap round 4
found, so the sweep, not this docstring, is the specification.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SCHEME_PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_AUTHORITY_STOP_RE = re.compile(r"[/?#\s]")
_WHITESPACE_RE = re.compile(r"\s")

# Characters a URI username (the part of the userinfo before the `:`) can
# never contain: `/?#` end the authority, `[]` belong to an IPv6 host
# literal, `:` starts the password. Whitespace is rejected separately
# (`str.isspace`). `@` is deliberately NOT here: a real-world
# email-as-username (`alice@corp.com:pw@host`) is unencoded userinfo, and
# excluding `@` made that whole credential shape unredactable.
_USERNAME_FORBIDDEN = frozenset("/?#[]:")

# Characters that make a password *not* RFC-legal userinfo. A password made
# only of legal characters plus spaces is credential-shaped on its own
# evidence — see `_tail_accepts` branch (b).
_PASSWORD_ILLEGAL = frozenset("/?#[]:")

# A token that continues a URI after the userinfo's `@`: a dotted host, a
# `host:port`, a host followed by a path, or a bracketed IPv6 literal. Prose
# words carry none of these.
_HOST_CONTINUATION = frozenset(".:/[")

# How far the search window may widen past the first whitespace. A raw,
# un-encoded space in a typed-in password is realistic; a whole sentence of
# diagnostic prose between a colon and some unrelated `@` is not. Unbounded
# widening is what repeatedly destroyed credential-free diagnostic text and
# fabricated fake hosts.
_MAX_USERINFO_SPACES = 2


def _first_stop(text: str, start: int, pattern: re.Pattern[str], limit: int) -> int:
    """Position of the first match of `pattern` in `text[start:limit]`, or
    `limit` if there is none."""
    m = pattern.search(text, start, limit)
    return m.start() if m else limit


def _bounded_widen(text: str, stop: int, outer_stop: int, tokens: int) -> int:
    """Advance `stop` past up to `tokens` more whitespace-delimited words.

    `stop` points at a whitespace character (or at `outer_stop`). Returns the
    position of the whitespace `tokens` words later, or `outer_stop`.
    Whitespace *runs* are skipped as one separator, so "a  b" costs one word,
    not two.
    """
    for _ in range(tokens):
        if stop >= outer_stop:
            return outer_stop
        pos = stop
        while pos < outer_stop and text[pos].isspace():
            pos += 1
        stop = _first_stop(text, pos, _WHITESPACE_RE, outer_stop)
    return stop


def _at_positions(text: str, lo: int, hi: int) -> list[int]:
    """Every index of ``@`` in ``text[lo:hi]``, ascending."""
    found: list[int] = []
    pos = text.find("@", lo, hi)
    while pos != -1:
        found.append(pos)
        pos = text.find("@", pos + 1, hi)
    return found


def _is_userinfo_shaped(text: str, start: int, at: int) -> bool:
    """Is `text[start:at]` shaped like a ``user:pass`` userinfo?

    Requires a ``:`` bonded directly to a non-whitespace character, and a
    username before it free of `_USERNAME_FORBIDDEN` and whitespace. The
    username may be empty (RFC 3986 permits empty userinfo, and
    ``:token@host`` is a live token-auth convention). See the module
    docstring for what this rejects and why it is stated positively.
    """
    colon = text.find(":", start, at)
    if colon == -1:
        return False
    if colon + 1 >= len(text) or text[colon + 1].isspace():
        return False
    return not any(
        ch in _USERNAME_FORBIDDEN or ch.isspace() for ch in text[start:colon]
    )


def _tail_accepts(text: str, start: int, at: int, outer_stop: int) -> bool:
    """Positive evidence that the ``@`` at `at` really terminates userinfo.

    Required for every candidate past the authority's own `/?#`-or-
    whitespace boundary — i.e. for exactly the malformed shapes a
    well-formed-URI parser cannot handle. Either:

    (a) the token after the ``@`` continues a URI (`_HOST_CONTINUATION`), or
    (b) the password is RFC-legal userinfo apart from its spaces.

    A prose span such as ``"2026-09-17T10:00:00 connect user@host"`` fails
    both — a bare ``host`` tail, and a password (``"00:00 connect user"``)
    carrying a second colon.
    """
    tail_end = _first_stop(text, at + 1, _WHITESPACE_RE, outer_stop)
    tail = text[at + 1 : tail_end]
    if tail and any(ch in _HOST_CONTINUATION for ch in tail):
        return True
    colon = text.find(":", start, at)
    if colon == -1:
        return False
    password = text[colon + 1 : at]
    return bool(password) and not any(ch in _PASSWORD_ILLEGAL for ch in password)


def _search_window(text: str, start: int, outer_stop: int) -> tuple[int, int, int]:
    """`(authority_stop, first_whitespace, window_end)` for a candidate scan."""
    authority_stop = _first_stop(text, start, _AUTHORITY_STOP_RE, outer_stop)
    first_ws = _first_stop(text, start, _WHITESPACE_RE, outer_stop)
    window_end = _bounded_widen(text, first_ws, outer_stop, _MAX_USERINFO_SPACES)
    return authority_stop, first_ws, window_end


def _find_authority_credential(text: str, start: int, outer_stop: int) -> int | None:
    """Index of the userinfo-terminating ``@``, for a `start` sitting
    immediately after a ``scheme://`` prefix, or ``None``.

    Deliberately a separate function from `_find_bare_credential` rather
    than one function with a flag: the two paths' accept rules differ in
    kind (RFC authority semantics vs. prose), and every round that shared
    one body between them leaked a credential when a rule written for one
    path fired on the other.
    """
    authority_stop, _first_ws, window_end = _search_window(text, start, outer_stop)
    best: int | None = None
    for at in _at_positions(text, start, window_end):
        if at < authority_stop:
            # RFC 3986: inside an authority, everything before an `@` IS
            # userinfo — password or not, shape or no shape.
            best = at
        elif _is_userinfo_shaped(text, start, at) and _tail_accepts(
            text, start, at, outer_stop
        ):
            best = at
    return best


def _find_bare_credential(text: str, start: int, outer_stop: int) -> int | None:
    """Index of the userinfo-terminating ``@``, for a `start` sitting at the
    message's first non-whitespace character, or ``None``.

    There is no authority here, so there is no RFC rule to lean on: a
    leading ``admin@example.com`` is an email address in prose far more
    often than it is a credential. Every candidate must therefore be
    credential-*shaped*, and a candidate past the first ``/?#``-or-
    whitespace boundary must clear `_tail_accepts` as well.
    """
    authority_stop, _first_ws, window_end = _search_window(text, start, outer_stop)
    best: int | None = None
    for at in _at_positions(text, start, window_end):
        if not _is_userinfo_shaped(text, start, at):
            continue
        if at < authority_stop or _tail_accepts(text, start, at, outer_stop):
            best = at
    return best


def _redact_text(text: str) -> str:
    """Core implementation shared by `safe_exc_text` and `redact_uri`.

    A bare, standalone URI is just degenerate "exception text" with no
    trailing prose, so one scan handles both callers — see the module
    docstring for the algorithm and the invariant it maintains.
    """
    n = len(text)
    out: list[str] = []

    # The bare path is anchored at the message's first non-whitespace
    # character, and bounded by the first `scheme://` occurrence. An
    # unanchored bare search would read any `user@host`-shaped substring
    # anywhere in a message as a credential.
    stripped_offset = n - len(text.lstrip())
    first_scheme = _SCHEME_PREFIX_RE.search(text)
    bare_outer_stop = first_scheme.start() if first_scheme else n
    at = _find_bare_credential(text, stripped_offset, bare_outer_stop)
    if at is not None:
        out.append(text[:stripped_offset])
        cursor = at + 1
    else:
        cursor = 0

    for m in _SCHEME_PREFIX_RE.finditer(text, cursor):
        out.append(text[cursor : m.start()])
        out.append(m.group(0))
        authority_start = m.end()
        next_scheme = _SCHEME_PREFIX_RE.search(text, authority_start)
        outer_stop = next_scheme.start() if next_scheme else n
        at = _find_authority_credential(text, authority_start, outer_stop)
        cursor = authority_start if at is None else at + 1

    out.append(text[cursor:])
    return "".join(out)


def safe_exc_text(exc: BaseException) -> str:
    """Render `str(exc)` with any embedded URI userinfo stripped.

    Handles both a `scheme://user:pass@host` shape (anywhere in the
    message, including more than one) and a scheme-less `user:pass@host`
    shape (only when it is the message's leading token). See the module
    docstring for the algorithm, the invariant, and the accepted limits.
    """
    return _redact_text(str(exc))


def redact_uri(uri: str) -> str:
    """Strip `user:pass@` userinfo from a single, standalone URI string.

    For `runtime._display_target` and `websocket_stt_service._endpoint_label`,
    both of which build their input from structured `host`/`port`/`uri`
    connect kwargs. Deliberately reuses `_redact_text` rather than
    `urllib.parse.urlsplit`'s netloc-bounded `username`/`password`
    properties: `urlsplit` silently reports no userinfo at all (rather than
    raising) when the password contains an unencoded `/`, `?`, or `#` — the
    exact malformed shape this module exists to redact, and the root cause
    of `_display_target`'s round-10 leak.
    """
    return _redact_text(uri)


def strip_query(uri: str) -> str:
    """Drop a URI's query string and fragment for safe display.

    Composed on top of :func:`redact_uri` by the display call sites that
    render a whole connect URI (``runtime._display_target``,
    ``websocket_stt_service._endpoint_label``). The scanner above strips
    only *userinfo*; a token passed via a query string
    (``wss://host:443/v1?token=s3cr3t`` — a real operator-supplied
    ``STT_WS_URI`` shape) survives it untouched and would otherwise echo
    verbatim into every log line and preflight error those two build.

    Dropped wholesale rather than inspected: a query string carries no
    diagnostic value in an endpoint label, so there is nothing to weigh
    against the risk of guessing which parameter is the secret. Lives here,
    not at either call site, because both call sites need it and neither
    module may import the other — the same reason this module exists.
    """
    try:
        parts = urlsplit(uri)
    except ValueError:
        # Fail closed the same way the rest of this module does: an
        # unparseable URI keeps whatever `redact_uri` already made of it
        # rather than being re-rendered from half-parsed components.
        return uri
    if not (parts.query or parts.fragment):
        return uri
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
