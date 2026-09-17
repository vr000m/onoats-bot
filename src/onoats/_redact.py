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

**Algorithm (round 3 of the second gauntlet loop — the seventh rewrite of
this logic; rounds 7/8/9/10 and rounds 1/2 of this loop each patched a
narrower gate and each left a new one).** The recurring root cause across
all six prior attempts was not any individual gate but two conflations:

* **"is there a port here" vs "is there a credential here"** — a
  colon-bonded token was treated as evidence of userinfo, so an ordinary
  ``host:8765`` authority engaged the widest search tier; and
* **the scheme-prefixed authority path vs the bare, scheme-less path** —
  one function served both, so a port heuristic written for the authority
  path also fired on the bare path (where a colon-bonded token is *always*
  a password, never a port, so the heuristic inverted into a credential
  leak), and a bare-path prose case relaxed a gate the authority path
  relied on.

This rewrite separates the two paths explicitly and gates on the candidate
span being **credential-shaped**, not on any port-like or colon-like
proxy.

**The two paths and their invariants** (``_find_credential_at``'s ``bare``
argument selects one; nothing else in this module branches on it, and no
heuristic below is shared between them except where stated):

1. **Authority path** (``bare=False``) — ``start`` sits immediately after a
   ``scheme://`` prefix, so ``text[start:]`` opens with a URI *authority*.
   An ``@`` inside that authority is userinfo by RFC 3986, unconditionally,
   password or not (tier 1 below). ``host:PORT`` lives on this path and
   nowhere else, so the all-digit port check (``_is_host_port_token``) is
   reachable only from here.
2. **Bare path** (``bare=True``) — ``start`` is the message's first
   non-whitespace character (see the anchoring note below). There is no
   host and no port before the ``@``: a colon-bonded token here is a
   password or it is prose, never a port. Every tier on this path,
   tier 1 included, therefore requires the positive credential-shape gate
   (``_is_userinfo_shaped``); nothing on this path may consult a port
   heuristic.

**The credential-shape gate (`_is_userinfo_shaped`).** A span qualifies as
userinfo only if it holds a ``:`` that is bonded directly to a
non-whitespace character, and the *username* before that ``:`` is non-empty
and free of ``/ ? # [ ] @ :`` and whitespace. That single positive test is
what rejects — structurally, not case by case — an IPv6 literal
(``[::1]:443``: a ``[`` in the username), an ordinary ``"Word: "``
diagnostic prefix (``Error: ...``: whitespace right after the colon), a
path-bearing span (``host/path?redirect=user``: a ``/`` in the username),
and a prose span with no colon at all (``contact admin@example.com``).

**Three tiers, widening only on positive evidence.** Given ``start`` and an
``outer_stop`` bound (the position of the next nested ``scheme://``
occurrence, if any, else end-of-string — this is what keeps one URI's
authority from ever reaching into a second URI's territory):

1. **Tier 1** — search for ``@`` up to the first of ``/``, ``?``, ``#`` or
   whitespace. Authority path: accepted as-is (RFC). Bare path: accepted
   only if shape-gated.
2. **Tier 2** — only if tier 1 found nothing: re-search ignoring ``/?#``
   as stops (still bounded by whitespace), for a password containing one of
   those characters — a *malformed* URI is exactly what ``InvalidURI`` is
   raised for. Requires the shape gate and the query-structure guard below.
3. **Tier 3** — only if tiers 1 and 2 found nothing: widen past whitespace
   too, for a password containing raw, un-encoded spaces. Requires the same
   shape gate and query-structure guard, and is **bounded**: see below.

**Bounded widening (`_MAX_USERINFO_SPACES`).** Tier 3 widens by at most two
further whitespace-delimited words, not to ``outer_stop``. An unbounded
tier 3 is precisely what, in five consecutive rounds, let a message whose
first token merely *looked* colon-bonded discard everything up to the last
``@`` in the message — turning credential-free diagnostics
(``"2026-09-17T10:00:00 connect failed for user@host"``,
``"unix:/tmp/x.sock failed: see admin@corp.com"``) into a fabricated host.
The accepted trade-off is explicit: a password with three or more raw
spaces is not redacted. It is bounded in the other direction too — the
gates above mean the widened tiers only ever engage on a genuinely
credential-shaped span.

**The query-string guard (`_looks_like_query_structure`).** Tiers 2 and 3
widen past tier 1's stop point, which reopens the one ambiguity tier 1
avoids: is the ``@`` genuine authority userinfo, or an unrelated
``user@host`` inside a query value (``?redirect=user@host``)? Resolved
**structurally**: an actual ``?`` must appear between tier 1's stop point
and the candidate ``@``, and an ``=`` between that ``?`` and the ``@``. No
``?`` in the gap means there is no query component to confuse the ``@``
with, however many ``=`` characters the gap holds — so a base64-shaped
password (whose ``/`` and ``=`` padding co-occur routinely) is still
redacted, while ``?redirect=user@host`` is still left alone.

Each tier reports both the ``@`` position and the window boundary it used,
so the caller redacts exactly ``[start, @)`` and resumes unmodified output
at the window boundary (not at ``outer_stop`` — see case (c) below).

**Invariant:** every character of the original string that is not part of a
credential span discarded by one of the three tiers is preserved verbatim
in the output — including trailing diagnostic text, a second URI in the
same message, and any ``@`` that does not pass one of the tiers' checks.

Traced against the required cases:

    (a) scheme-less, password with a raw space —
        ``"secretuser:hunter 2@stt.example.internal:2020 isn't a valid URI: ..."``
        tiers 1/2 (bounded at the space after "hunter") find no ``@``;
        tier 3 widens two words, finds the ``@``, and the span
        ``"secretuser:hunter 2"`` passes the shape gate (username
        "secretuser" is clean) — redacts to
        ``"stt.example.internal:2020 isn't a valid URI: ..."``.
    (b) scheme-prefixed, trailing prose —
        ``"ws://user:pass@host:9999 isn't a valid URI: nonempty path required"``
        — tier 1 finds the ``@`` inside the authority; everything from the
        window boundary on is spliced back unmodified and never
        re-inspected.
    (c) multiple credentials in one message —
        ``"ws://u1:p1@h1 and ws://u2:p2@h2 both failed"`` — each match's
        window boundary is the tighter of its tier stop and ``outer_stop``
        (the next ``scheme://``), so " and " is spliced back verbatim and
        the second URI is processed independently.
    (d) password containing ``@`` — each tier uses ``rfind`` (last ``@`` in
        its window), so an embedded ``@`` stays on the discarded side.
    (e) unrelated query-string ``user@host`` —
        ``"https://example.com/api?redirect=user@example.org"`` — tier 1
        finds nothing; tiers 2/3 find the ``@`` but the span
        ``"example.com/api?redirect=user"`` fails the shape gate (no colon
        at all), and the query-structure guard rejects it independently.
        Unchanged.
    (e2) query-string ``user:pass@host`` (colon variant) —
        ``"ws://host/path?redirect=user:pass@example.org"`` — the span's
        username would be ``"host/path?redirect=user"``, which contains
        ``/`` and ``?`` — shape gate rejects; the query-structure guard
        rejects it a second time. Unchanged.
    (f) password containing ``/``, ``?``, or ``#`` —
        ``"ws://user:pa/ss@host/path"`` — tier 2 widens past the ``/``,
        the span ``"user:pa/ss"`` is shape-clean, no ``?`` in the gap —
        redacts to ``"ws://host/path"``.
    (g) nested URL inside an unrelated query string —
        ``"https://host/path?redirect=https://user:pass@evil/path"`` — the
        outer match's ``outer_stop`` is capped at the inner ``https://``,
        so only the inner, genuinely credential-shaped span is redacted.
    (h) leading prose with an unrelated ``@`` —
        ``"connection to user@host failed"`` — tier 3's window reaches the
        ``@``, but the span has no colon: shape gate rejects. Unchanged.
    (i) IPv6 host in brackets — ``"ws://user:pass@[::1]:443/path"`` is
        redacted by tier 1 to ``"ws://[::1]:443/path"``; conversely a
        message *opening* with ``"[::1]:443 ..."`` and containing an
        unrelated ``@`` later is rejected by the shape gate's ``[``.
    (j) no credential-shaped substring anywhere — no tier finds a usable
        ``@``; the message passes through unchanged.
    (k) ordinary ``"Word: "`` diagnostic prose with an unrelated ``@`` —
        ``"Error: connect to user@host failed"`` — the colon is followed by
        whitespace, so the shape gate rejects. Unchanged.
    (l) several unrelated ``@``\\ s in prose — ``"Error: mail admin@a.com or
        ops@b.com"`` — same gate, and tier 3's bounded window does not even
        reach the second ``@``. Unchanged.
    (m) base64-shaped password (contains ``/`` and ``=``) —
        ``"ws://user:AB/cd+EF=@host:8765/"`` — tier 2 accepts (no ``?`` in
        the gap, so the ``=`` is not query structure) — redacts to
        ``"ws://host:8765/"``.
    (n) authority with a port, unrelated ``@`` in trailing prose —
        ``"ws://host:8765 isn't a valid URI: see user@guide"`` — tier 1
        finds nothing and ``_is_host_port_token`` recognizes ``host:8765``
        as an authority, so the widened tiers never run. Unchanged.
    (n2) the same with a path after the port —
        ``"ws://host:8765/path failed: could not reach user@relay"`` — the
        port token is measured to the first ``/?#`` or whitespace, i.e.
        ``"8765"``, so the path after it does not disguise the port.
        Unchanged. (Round-2 regression: the check measured to the first
        whitespace, so ``"8765/path"`` was "not all digits" and the message
        was destroyed down to ``"ws://relay"``.)
    (n3) the bare-path inverse — ``"user:1234 5678@host isnt a valid URI:
        ..."`` — an all-digit leading password segment must NOT be read as
        a port here: there is no authority on this path. The port check is
        unreachable from the bare path, so tier 3 redacts normally.
        (Round-2 regression: the shared port check fired here and
        *disabled* the widen refusal, leaking the whole credential.)

The bare path is applied **only** at the literal start of the message
(after any leading whitespace, preserved verbatim) — it is specifically the
leading token of ``f"{uri} isn't a valid URI: {msg}"``, the one realistic
shape a scheme-less credential reaches this function through. An
unanchored bare search would read any ``user@host``-shaped substring
anywhere in a message as a credential.

**Known, accepted limitations:** (1) a password containing a genuine ``?``
followed later by a genuine ``=`` cannot be distinguished from real query
structure by local syntax alone; (2) a password with three or more raw
spaces exceeds tier 3's bound; (3) an authority-path password whose whole
first segment is digits (``ws://user:12/34@host``) reads as a port. All
three are ambiguities a generic text scanner with no schema information
cannot resolve, and each is resolved in the direction that cannot destroy
credential-free diagnostic text. ``redact_uri`` below (used by
``runtime._display_target`` and ``websocket_stt_service._endpoint_label``,
both of which start from structured ``host``/``port``/``uri`` connect
kwargs rather than arbitrary exception text) is a thin wrapper around the
same tiered search — ``urllib.parse.urlsplit``'s ``username``/``password``
properties cannot be used instead: they silently report no userinfo at all
(rather than raising) when the password contains an unencoded ``/``,
``?``, or ``#`` — the exact malformed shape this module exists to redact.
"""

from __future__ import annotations

import re

_SCHEME_PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_TIER1_STOP_RE = re.compile(r"[/?#\s]")
_WHITESPACE_RE = re.compile(r"\s")

# Characters a URI username (the part of the userinfo before the `:`) can
# never contain: `/?#` end the authority, `[]` belong to an IPv6 host
# literal, `@` ends the userinfo, `:` starts the password. Whitespace is
# rejected separately (`str.isspace`), so a raw space is disqualifying in the
# *username* even though the *password* may contain one — see the module
# docstring's shape gate.
_USERNAME_FORBIDDEN = frozenset("/?#[]@:")

# How much whitespace a userinfo span may contain before the widened tier 3
# refuses it. A raw, un-encoded space in a password is realistic (case (a));
# a whole sentence of diagnostic prose between the colon and some unrelated
# `@` is not. An UNBOUNDED tier 3 is what repeatedly destroyed credential-free
# diagnostic text and fabricated fake hosts — see the module docstring's
# "Bounded widening" note for the trade-off this constant encodes.
_MAX_USERINFO_SPACES = 2


def _first_stop(text: str, start: int, pattern: re.Pattern[str], limit: int) -> int:
    """Position of the first match of `pattern` in `text[start:limit]`, or
    `limit` if there is none."""
    m = pattern.search(text, start, limit)
    return m.start() if m else limit


def _looks_like_query_structure(text: str, gap_start: int, at: int) -> bool:
    """Is `text[gap_start:at]` shaped like URL query structure (`?key=...`)?

    Used to veto a tier 2/3 widened `@` match that is really an unrelated
    `user@host` sitting inside a query string's value, e.g.
    `?redirect=user@host`. Structural, not merely "does `=` appear anywhere
    in the gap": that alone also fires on a malformed password's OWN `=`
    (a base64 token's padding, say), which has nothing to do with query
    structure and must still be redacted — see module docstring case (m).
    Requires an actual `?` in the gap, and an `=` between that `?` and
    `at`; no `?` at all means there is no query component to confuse this
    `@` with, however many `=` characters the gap otherwise contains.
    """
    qpos = text.find("?", gap_start, at)
    return qpos != -1 and "=" in text[qpos:at]


def _is_userinfo_shaped(text: str, start: int, at: int) -> bool:
    """Is `text[start:at]` shaped like a ``user:pass`` userinfo?

    The positive credential-shape gate. Requires, in order:

    * a ``:`` inside the span (a bare ``user@host`` with no password is
      userinfo only on the authority path, where tier 1 already accepts it
      by RFC; everywhere else a colon is the one structural marker that
      distinguishes a credential from prose);
    * that ``:`` immediately bonded to a non-whitespace character — an
      ordinary ``"Word: "`` diagnostic prefix ends its token in ``": "``,
      which no credential ever does;
    * a non-empty *username* before it, free of ``/?#[]@:`` and whitespace.

    The username test is what rejects, structurally rather than by special
    case, every non-credential shape that used to reach the widened tiers:
    an IPv6 literal (``[::1]:443`` — the ``[``), a scheme-less path
    (``unix:/tmp/x.sock``, ``C:/Users/...`` — the password side is checked
    by the caller's bound, but a *path-bearing* username like
    ``host/path?redirect=user`` is rejected right here), and any span that
    has already crossed an ``@`` or an authority delimiter.
    """
    colon = text.find(":", start, at)
    if colon == -1:
        return False
    if colon + 1 >= len(text) or text[colon + 1].isspace():
        return False
    if colon <= start:
        return False
    return not any(
        ch in _USERNAME_FORBIDDEN or ch.isspace() for ch in text[start:colon]
    )


def _is_host_port_token(text: str, start: int, tier1_stop: int) -> bool:
    """Is `text[start:tier1_stop]` an ordinary ``host:PORT`` authority?

    Authority path only (a bare, scheme-less credential has no host:port
    before its ``@`` — see the module docstring's two-path invariant). A
    port is all-digits by RFC 3986 and runs to the end of the authority's
    first segment, i.e. to `tier1_stop` (the first ``/?#`` or whitespace) —
    NOT merely to the first whitespace, which is what let ``host:8765/path``
    slip past the round-2 version of this check and be treated as a
    credential.
    """
    colon = text.find(":", start, tier1_stop)
    return colon != -1 and text[colon + 1 : tier1_stop].isdigit()


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


def _find_credential_at(
    text: str, start: int, outer_stop: int, *, bare: bool
) -> tuple[int, int] | tuple[None, None]:
    """Locate the credential-marking ``@`` in `text[start:outer_stop]`.

    `bare` selects the code path: ``False`` means `start` sits immediately
    after a ``scheme://`` prefix (the *authority* path), ``True`` means it
    sits at the start of the message (the *bare* path). The two paths have
    different invariants and must not share heuristics — see the module
    docstring.

    Returns `(at, window_end)` where `text[start:at]` is the userinfo to
    discard and `text[at + 1:window_end]` is the credential-free remainder
    to keep, or `(None, None)` if no credential is judged present.
    """
    tier1_stop = _first_stop(text, start, _TIER1_STOP_RE, outer_stop)

    # --- Tier 1 -----------------------------------------------------------
    # Authority path: RFC 3986 is unambiguous — everything before an ``@``
    # inside the authority IS userinfo, password or not. Bare path: the same
    # span is just the message's leading words, so it must pass the shape
    # gate before it can be called a credential.
    at = text.rfind("@", start, tier1_stop)
    if at != -1 and (not bare or _is_userinfo_shaped(text, start, at)):
        return at, tier1_stop

    # Beyond tier 1 the span is no longer bounded by the authority's own
    # delimiters, so both paths now require positive credential shape — and
    # the authority path first rules out the one non-credential shape that
    # IS colon-bonded and username-clean: an ordinary ``host:PORT``.
    if not bare and _is_host_port_token(text, start, tier1_stop):
        return None, None

    # --- Tier 2 (ignore /?# as stops; still bounded by whitespace) ---------
    first_ws = _first_stop(text, start, _WHITESPACE_RE, outer_stop)
    at = text.rfind("@", start, first_ws)
    if (
        at != -1
        and _is_userinfo_shaped(text, start, at)
        and not _looks_like_query_structure(text, tier1_stop, at)
    ):
        return at, first_ws

    # --- Tier 3 (ignore whitespace too, but only for a bounded number of
    # words — see `_MAX_USERINFO_SPACES`) ----------------------------------
    tier3_stop = _bounded_widen(text, first_ws, outer_stop, _MAX_USERINFO_SPACES)
    at = text.rfind("@", start, tier3_stop)
    if (
        at != -1
        and _is_userinfo_shaped(text, start, at)
        and not _looks_like_query_structure(text, tier1_stop, at)
    ):
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
    at, window_end = _find_credential_at(
        text, stripped_offset, bare_outer_stop, bare=True
    )
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
        at, window_end = _find_credential_at(
            text, authority_start, outer_stop, bare=False
        )
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
