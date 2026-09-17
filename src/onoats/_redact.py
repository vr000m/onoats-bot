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

**The algorithm.** The credential's *start* is never guessed: it is either
the character after a ``scheme://`` prefix (the **authority path**) or the
message's first non-whitespace character (the **bare path**). Only the
*end* — which ``@`` terminates the userinfo — is in question. So:

1. Compute the search window: from ``start`` to the first whitespace, then
   widened by at most :data:`_MAX_USERINFO_SPACES` further whitespace-
   delimited words, and capped by ``outer_stop`` (see
   :func:`_next_uri_boundary`).
2. Walk every ``@`` in that window left to right and keep the rightmost
   *accepted* one, so an ``@`` embedded in the username or the password
   stays on the discarded side. Inside the authority proper — before the
   first ``/?#``-or-whitespace — RFC 3986 settles acceptance outright
   (everything before an ``@`` in an authority *is* userinfo). Past that
   boundary the credential is *malformed*, its password carrying an
   unencoded ``/``, ``?``, ``#`` or space that ended the authority early,
   and all three gates below apply.
3. Refuse any candidate that would overturn a *settled* authority from a
   later whitespace-delimited word without a self-evidently-legal password.
   See :func:`_scan`.

**The gates** — three eligibility gates, then one settled-authority test.
All are positive; nothing is accepted by failing to match something else.

* :func:`_is_userinfo_shaped` — the span ``text[start:at]`` must hold a
  ``:`` bonded directly to a non-whitespace character, with a username
  before it free of ``/ ? # [ ] :`` and whitespace. This rejects an IPv6
  literal (``[::1]:443``), an ordinary ``"Word: "`` diagnostic prefix, a
  path-bearing span, and prose with no colon at all. The username may be
  empty (``:token@host``) and may itself contain ``@``.
* :func:`_tail_accepts` — either (a) the token after the ``@`` looks like a
  URI continuation (it contains ``.``, ``:``, ``/`` or ``[``), or (b) the
  password is RFC-legal userinfo apart from its spaces
  (:func:`_password_is_legal_userinfo`).
* :func:`_not_query_of_path` — the text before the first ``?``/``#`` in the
  span must not *already* be a well-formed authority-plus-optional-path.
  That is the canonical shape of a URI with a query
  (``wss://host:443/v1?redirect=user@example.com``,
  ``wss://host.example.com:443?redirect=bob@corp.com``), whose ``@`` is an
  ordinary query-value character; reading it as userinfo fabricated a
  hostname out of the query's tail and destroyed the real one.
* :func:`_scan`'s settled-authority test, which is *not* a fourth
  eligibility gate but the tie-break between two eligible candidates. An
  authority is settled either before any ``@`` is accepted
  (``text[start:authority_stop]`` is itself a complete ``host[:port]``, so
  the anchor has no userinfo at all) or after one is
  (:func:`_incumbent_is_settled`). Only
  :func:`_password_is_legal_userinfo` overturns it — never
  :func:`_tail_accepts` branch (a), which every email address in every
  diagnostic satisfies. The first of those two is the check that was
  missing for five rounds, and every "fabricated host" finding in them was
  a candidate accepted as the *first* ``@`` against an authority that had
  already terminated well-formed.

On the **bare path** there is no scheme to anchor an authority, so
:func:`_is_userinfo_shaped` is required for *every* candidate, in-authority
ones included — a message opening with a bare email address is prose.

**Invariant:** the output is the input with zero or more spans of the form
``text[start:at + 1]`` deleted, where each ``start`` is a
``scheme://``-or-message-start anchor and each ``at`` is an ``@`` accepted
above. Every other character — trailing diagnostic text, a second URI in
the same message, any ``@`` that fails the gates — is preserved verbatim.
:func:`safe_exc_text` then composes :func:`_strip_query_spans` on top, for
the same reason the display call sites compose :func:`strip_query`: the
scanner is userinfo-only, and ``InvalidURI``'s message carries the query
string (``?token=s3cr3t``) through verbatim otherwise. Both query-stripping
steps extend the invariant rather than weaken it: each cuts only where the
text in front of the ``?``/``#`` is a well-formed authority (optionally with
a path), so neither can truncate a span the scanner declined to redact.

**Known, accepted limitations** (each re-verified against *this*
implementation, not inherited from an earlier docstring):

1. A password crossing more than :data:`_MAX_USERINFO_SPACES` whitespace
   boundaries is not redacted by :func:`safe_exc_text`. Unbounded widening
   is what destroyed credential-free diagnostics in five consecutive
   rounds.
2. Prose of the exact shape ``word:word word@host.tld`` is
   indistinguishable from a real credential by local syntax and *is*
   discarded (``"note:see bob@corp.com"`` -> ``"corp.com"``). Resolved
   toward redaction because the alternative is a leak.
3. Conversely, a credential whose password contains ``/`` or ``:`` *and*
   whose host is a bare single-label name (``user:a/b c@host``) fails both
   halves of :func:`_tail_accepts` and is not redacted.
4. A *path-free* URI whose host is a bare single label and whose query
   carries an ``@`` (``wss://localhost:443?redirect=bob@corp.com``) is
   over-redacted to ``wss://corp.com``. ``user:1234`` and ``localhost:443``
   are the same grammar, so with no path to tell them apart
   :func:`_not_query_of_path` demands a dotted host before it will refuse a
   credential, and resolves the single-label case toward redaction like
   limitation 2. The same URI *with* a path (``.../v1?redirect=...``), and
   the same URI with a dotted host, both keep their real authority.
5. A well-formed URI whose ``@`` sits in a *path* segment with no query
   (``wss://host:443/path/to/a@b.com``) is still over-redacted to
   ``wss://b.com``: it is character-for-character the same grammar as the
   genuine ``wss://user:1234/seg@host.tld`` the sweep requires be redacted,
   so the ambiguity is resolved toward redaction like limitation 2.

The generated sweep in ``tests/test_redact.py``, not this docstring, is the
specification.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

_SCHEME_PREFIX_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")
_AUTHORITY_STOP_RE = re.compile(r"[/?#\s]")
_WHITESPACE_RE = re.compile(r"\s")
_QUERY_START_RE = re.compile(r"[?#]")

# A syntactically complete RFC 3986 host (reg-name or bracketed IPv6
# literal) with an optional numeric port, and nothing else. This module asks
# it exactly one question, in three places: *is this span already a
# well-formed authority?* If it is, the span needs no userinfo to explain
# it. If it is not — `corp.com:AB+cd=` is not a `host:port` — then the
# authority was truncated early by an unencoded character in a password, and
# the real terminating `@` is still ahead.
_HOST_PORT_RE = re.compile(r"(?:\[[^\[\]\s]+\]|[^@/?#\[\]:\s]+)(?::[0-9]*)?")

# The same question asked of a *multi-label* reg-name (or a bracketed IPv6
# literal). A single-label span is a far weaker claim to being a real host:
# `user:1234` is character-for-character a `host:port`, which is why the
# `host[:port]` test alone may never veto a credential outright. Where the
# evidence has to stand on its own — a query string with no path in front of
# it, and the scheme-less anchor in `_strip_query_spans` — the dotted form is
# required instead.
_DOTTED_HOST_PORT_RE = re.compile(
    r"(?:\[[^\[\]\s]+\]|[^@/?#\[\]:\s]+\.[^@/?#\[\]:\s]+)(?::[0-9]*)?"
)

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


def _next_uri_boundary(text: str, start: int, limit: int) -> int:
    """Where this anchor's scan must stop: the next *genuine* ``scheme://``.

    A ``scheme://``-shaped match that falls **before** this authority's own
    ``/?#``-or-whitespace boundary is not the start of a second URI — it is
    inside the current candidate's own password (``wss://user:secret://tail@
    host.example.com``, whose ``secret://`` matches the scheme pattern).
    Honouring it truncated the search window short of the real terminating
    ``@``, so no candidate was found and the leftover ``user:secret`` was
    emitted verbatim. Such a match is skipped and the search resumes past
    it.

    A match sitting exactly *at* `start` is never skipped: on the bare path
    the anchor is the message's first character, which is the ``ws://`` of a
    leading URI itself. Skipping that one would hand the bare scan the whole
    scheme-prefixed URI, which the authority path owns.
    """
    authority_stop = _first_stop(text, start, _AUTHORITY_STOP_RE, limit)
    pos = start
    while True:
        m = _SCHEME_PREFIX_RE.search(text, pos, limit)
        if m is None:
            return limit
        if m.start() >= authority_stop or m.start() <= start:
            return m.start()
        pos = m.end()


def _incumbent_is_settled(text: str, best: int, rival: int, outer_stop: int) -> bool:
    """Has the ``@`` at `best` *visibly finished* the authority it terminates?

    Two conditions, both required:

    1. The span from `best + 1` to the next ``/?#``-or-whitespace is a
       complete ``host[:port]``. False when it was cut short by an unencoded
       character in a password (``alice@corp.com:AB+cd=?q@host`` —
       ``corp.com:AB+cd=`` is not a ``host:port``), which is exactly when
       the real terminating ``@`` is still ahead.
    2. That host is followed by ``/``, ``?``, ``#``, the end of the scan —
       or, if it is followed by whitespace, by at least one whitespace-
       delimited word containing no ``@`` before `rival`. Whitespace is the
       one authority terminator a typed-in password can contain unencoded,
       so a host-shaped span ending at whitespace has *not* finished
       anything when the very next word carries on with more userinfo
       (``http://user:p@ss w0rd:x@stt.example.com`` — ``ss`` is host-shaped,
       and treating it as settled pinned the scan to an ``@`` inside the
       password and emitted most of the password verbatim). Prose in
       between (``... failed: admin@example.com``) does end the URI token,
       which is the case condition 2 exists to keep protected.
    """
    stop = _first_stop(text, best + 1, _AUTHORITY_STOP_RE, outer_stop)
    if _HOST_PORT_RE.fullmatch(text, best + 1, stop) is None:
        return False
    if stop >= outer_stop or not text[stop].isspace():
        return True
    word_start = stop
    for m in _WHITESPACE_RE.finditer(text, stop, rival):
        word_start = m.end()
    return bool(text[stop:word_start].strip())


def _userinfo_colon(text: str, start: int, at: int) -> int:
    """Index of the ``:`` separating username from password in
    ``text[start:at]``, or ``-1`` when the span holds no colon.

    The *first* colon: RFC 3986 userinfo is ``username ":" everything-else``,
    so a later colon is a password character, never a second separator.
    Three predicates below each need this one index, and each used to
    re-derive it with its own `str.find` call and its own reading of the
    ``-1`` case.
    """
    return text.find(":", start, at)


def _is_userinfo_shaped(text: str, start: int, at: int) -> bool:
    """Is `text[start:at]` shaped like a ``user:pass`` userinfo?

    Requires a ``:`` bonded directly to a non-whitespace character, and a
    username before it free of `_USERNAME_FORBIDDEN` and whitespace. The
    username may be empty (RFC 3986 permits empty userinfo, and
    ``:token@host`` is a live token-auth convention). See the module
    docstring for what this rejects and why it is stated positively.
    """
    colon = _userinfo_colon(text, start, at)
    if colon == -1:
        return False
    if colon + 1 >= len(text) or text[colon + 1].isspace():
        return False
    return not any(
        ch in _USERNAME_FORBIDDEN or ch.isspace() for ch in text[start:colon]
    )


def _not_query_of_path(text: str, start: int, at: int) -> bool:
    """Reject an ``@`` that sits in the *query* of a path-bearing URI.

    ``wss://host:443/v1?redirect=user@example.com`` carries no credential at
    all, but ``host`` reads as a username, ``443`` as a password, and the
    query's ``example.com`` tail satisfies `_tail_accepts` — so the real
    host was discarded and a fabricated one substituted, in exactly the
    ``_display_target``/``_endpoint_label`` output operators diagnose from.

    The discriminator is positive and structural: take the first ``?``/``#``
    in the candidate span and ask whether what precedes it is *already* a
    well-formed URI prefix — a complete ``host[:port]``, optionally followed
    by a path. If it is, the URI has explained itself without any userinfo
    and the ``@`` is an ordinary query-value character.

    Requiring a ``/`` before the ``?`` (the earlier rule) missed the
    path-free shape ``wss://host.example.com:443?redirect=bob@corp.com``
    entirely, which fabricated ``wss://corp.com``. Requiring only
    ``host[:port]`` would over-reach the other way: ``user:1234`` *is* a
    well-formed ``host:port``, so a path-free span must additionally carry a
    dotted (or bracketed) host before its evidence is strong enough to
    refuse a credential.
    """
    if _userinfo_colon(text, start, at) == -1:
        return True
    query = _QUERY_START_RE.search(text, start, at)
    if query is None:
        return True
    slash = text.find("/", start, query.start())
    authority_stop = query.start() if slash == -1 else slash
    if _HOST_PORT_RE.fullmatch(text, start, authority_stop) is None:
        # Truncated authority: the `?`/`#` is a password character, not a
        # query marker (`ws://user:a/b?c@host/v1`).
        return True
    if slash != -1:
        return False
    return _DOTTED_HOST_PORT_RE.fullmatch(text, start, authority_stop) is None


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
    return _password_is_legal_userinfo(text, start, at)


def _password_is_legal_userinfo(text: str, start: int, at: int) -> bool:
    """Is ``text[colon + 1 : at]`` RFC-legal userinfo apart from its spaces?

    A password made only of legal characters is credential-shaped on its own
    evidence, with no help from whatever follows the ``@`` — which is what
    makes it the one kind of evidence strong enough to overturn an already
    well-formed authority (see `_scan`'s override rule).
    """
    colon = _userinfo_colon(text, start, at)
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


def _scan(text: str, start: int, outer_stop: int, *, bare: bool) -> int | None:
    """Index of the userinfo-terminating ``@`` for one anchor, or ``None``.

    One left-to-right pass over every ``@`` in the search window, keeping
    the rightmost *accepted* one. Rightmost-wins is what keeps an ``@``
    embedded in the username or the password on the discarded side
    (``alice@corp.com:pw@host``).

    **Eligibility.** Inside the authority proper (before the first
    ``/?#``-or-whitespace) RFC 3986 settles it: everything before an ``@``
    *is* userinfo, password or not, shape or no shape — except on the bare
    path, where there is no scheme to anchor an authority and a leading
    ``admin@example.com`` is prose far more often than a credential, so
    `_is_userinfo_shaped` is required there too. Past that boundary — the
    malformed shapes this module exists for, where an unencoded character
    in the password ended the authority early — all three gates apply.

    **The settled-authority rule.** Rightmost-wins alone let a trailing
    ``admin@example.com`` in diagnostic prose beat the real credential,
    discarding the true host and substituting a fabricated one
    (``"wss://user:pass@host.example.com/v1 failed: admin@example.com"``
    collapsed to ``"wss://example.com"``). So once the authority question is
    *settled*, a candidate from a later whitespace-delimited word may only
    overturn it with evidence that does not come from the prose it sits in:
    a password that is self-evidently RFC-legal userinfo
    (`_password_is_legal_userinfo`). `_tail_accepts` branch (a)'s "the next
    word looks like a host" is not enough, because every email address in
    every diagnostic satisfies it.

    The authority is settled in **two** ways, and for five rounds only the
    second was ever consulted:

    * **Before any ``@`` is accepted** — ``text[start:authority_stop]`` is
      itself already a complete ``host[:port]``, so the anchor's authority
      terminated well-formed with no userinfo in it at all
      (``wss://host.example.com:443/v1 failed: user@corp.com``,
      ``unix:/tmp/x.sock error: svc@host.com``). Nothing tested this, so a
      later prose ``@`` was accepted as the *first* candidate and the whole
      real authority — host, port, path — was deleted in front of it. This
      is the acceptance-point gap the fabrication class kept regrowing
      around.
    * **After one is accepted** — the incumbent visibly finished an
      authority (`_incumbent_is_settled`).

    Neither is a veto. ``user:1234`` is character-for-character a
    ``host:port``, so ``ws://user:1234 abcd@host:8765`` presents a complete
    "authority" that is really a credential with a space in its password; a
    legal password overturns it, exactly as it overturns an incumbent.
    """
    authority_stop, first_ws, window_end = _search_window(text, start, outer_stop)
    best: int | None = None
    settled = _HOST_PORT_RE.fullmatch(text, start, authority_stop) is not None
    for at in _at_positions(text, start, window_end):
        if at < authority_stop:
            if bare and not _is_userinfo_shaped(text, start, at):
                continue
        elif not (
            _is_userinfo_shaped(text, start, at)
            and _not_query_of_path(text, start, at)
            and _tail_accepts(text, start, at, outer_stop)
        ):
            continue
        if best is not None:
            settled = _incumbent_is_settled(text, best, at, outer_stop)
        if (
            settled
            and at >= first_ws
            and not _password_is_legal_userinfo(text, start, at)
        ):
            continue
        best = at
    return best


def _find_authority_credential(text: str, start: int, outer_stop: int) -> int | None:
    """Scan anchored immediately after a ``scheme://`` prefix."""
    return _scan(text, start, outer_stop, bare=False)


def _find_bare_credential(text: str, start: int, outer_stop: int) -> int | None:
    """Scan anchored at the message's first non-whitespace character."""
    return _scan(text, start, outer_stop, bare=True)


def _redact_text(text: str) -> str:
    """Core implementation shared by `safe_exc_text` and `redact_uri`.

    A bare, standalone URI is just degenerate "exception text" with no
    trailing prose, so one scan handles both callers — see the module
    docstring for the algorithm and the invariant it maintains.
    """
    n = len(text)
    out: list[str] = []

    # The bare path is anchored at the message's first non-whitespace
    # character, and bounded by the first *genuine* `scheme://` occurrence.
    # An unanchored bare search would read any `user@host`-shaped substring
    # anywhere in a message as a credential.
    stripped_offset = n - len(text.lstrip())
    bare_outer_stop = _next_uri_boundary(text, stripped_offset, n)
    at = _find_bare_credential(text, stripped_offset, bare_outer_stop)
    if at is not None:
        out.append(text[:stripped_offset])
        cursor = at + 1
    else:
        cursor = 0

    for m in _SCHEME_PREFIX_RE.finditer(text):
        if m.start() < cursor:
            # Already consumed — either by the bare-path redaction above or
            # by a preceding authority whose password contained this
            # `scheme://`-shaped text (`user:secret://tail@host`). Emitting
            # it again would duplicate the span `_next_uri_boundary`
            # deliberately refused to treat as a URI boundary.
            continue
        out.append(text[cursor : m.start()])
        out.append(m.group(0))
        authority_start = m.end()
        outer_stop = _next_uri_boundary(text, authority_start, n)
        at = _find_authority_credential(text, authority_start, outer_stop)
        cursor = authority_start if at is None else at + 1

    out.append(text[cursor:])
    return "".join(out)


def _query_cut(
    text: str, authority_start: int, limit: int, host_re: re.Pattern[str]
) -> tuple[int, int] | None:
    """`(cut, token_end)` for the query of the URI token at `authority_start`.

    ``None`` when the token has no query, or — the part that matters — when
    the span in front of the ``?``/``#`` is *not* a well-formed authority.
    A truncated authority means the ``?`` is an unencoded password character
    (``wss://user:a/b?c@host/v1``), and cutting there would emit
    ``wss://user:a/b``: a fabricated host, half a password, and no trace of
    the real one — the very failure `_redact_text` is careful to avoid,
    reintroduced by the step composed on top of it.

    `host_re` is `_DOTTED_HOST_PORT_RE` for the scheme-less anchor, where
    there is no ``scheme://`` vouching that the leading token is a URI at
    all and an ordinary prose word (``Traceback?``) would otherwise be cut.
    """
    token_end = _first_stop(text, authority_start, _WHITESPACE_RE, limit)
    authority_stop = _first_stop(text, authority_start, _AUTHORITY_STOP_RE, token_end)
    if host_re.fullmatch(text, authority_start, authority_stop) is None:
        return None
    cut = _first_stop(text, authority_start, _QUERY_START_RE, token_end)
    if cut >= token_end:
        return None
    return cut, token_end


def _strip_query_spans(text: str) -> str:
    """Drop the query string and fragment of every ``scheme://`` URI in
    `text`, leaving the surrounding diagnostic prose untouched.

    The in-place counterpart to :func:`strip_query`, which can only take a
    whole-string URI. ``websockets.InvalidURI`` renders as
    ``f"{uri} isn't a valid URI: {msg}"``, so the *exception* path was the
    one sink an operator-supplied ``?token=s3cr3t`` still reached verbatim
    while both display call sites were already composing `strip_query`.
    Dropped wholesale rather than inspected, for the same reason: a query
    string carries no diagnostic value in a log line, so there is nothing to
    weigh against the risk of guessing which parameter is the secret.

    Anchored the same two ways :func:`_redact_text` is — after a
    ``scheme://`` prefix, *and* at the message's first non-whitespace
    character. Scanning only from ``scheme://`` left the commonest operator
    typo of all leaking: a scheme-less URI is exactly what raises
    ``websockets.InvalidURI``, and
    ``stt.example.com:8765/v1?token=s3cr3t isn't a valid URI: scheme isn't
    ws or wss`` carried its token through verbatim.
    """
    n = len(text)
    out: list[str] = []
    cursor = 0
    stripped_offset = n - len(text.lstrip())
    bare = _query_cut(
        text,
        stripped_offset,
        _next_uri_boundary(text, stripped_offset, n),
        _DOTTED_HOST_PORT_RE,
    )
    if bare is not None:
        cut, cursor = bare
        out.append(text[:cut])
    for m in _SCHEME_PREFIX_RE.finditer(text):
        if m.start() < cursor:
            continue
        found = _query_cut(text, m.end(), n, _HOST_PORT_RE)
        if found is None:
            continue
        cut, token_end = found
        out.append(text[cursor:cut])
        cursor = token_end
    out.append(text[cursor:])
    return "".join(out)


def safe_exc_text(exc: BaseException) -> str:
    """Render `str(exc)` with any embedded URI userinfo *and* query string
    stripped.

    Handles both a `scheme://user:pass@host` shape (anywhere in the
    message, including more than one) and a scheme-less `user:pass@host`
    shape (only when it is the message's leading token). See the module
    docstring for the algorithm, the invariant, and the accepted limits.
    """
    return _strip_query_spans(_redact_text(str(exc)))


def redact_uri(uri: str) -> str:
    """Strip `user:pass@` userinfo from a single, standalone URI string.

    Deliberately reuses `_redact_text` rather than
    `urllib.parse.urlsplit`'s netloc-bounded `username`/`password`
    properties: `urlsplit` silently reports no userinfo at all (rather than
    raising) when the password contains an unencoded `/`, `?`, or `#` — the
    exact malformed shape this module exists to redact, and the root cause
    of `_display_target`'s round-10 leak.

    Userinfo only. Display call sites want :func:`display_uri`, which adds
    the query strip.
    """
    return _redact_text(uri)


def strip_query(uri: str) -> str:
    """Drop a URI's query string and fragment for safe display.

    The scanner above strips only *userinfo*; a token passed via a query
    string (``wss://host:443/v1?token=s3cr3t`` — a real operator-supplied
    ``STT_WS_URI`` shape) survives it untouched and would otherwise echo
    verbatim into every log line and preflight error the display call sites
    build.
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
    if _HOST_PORT_RE.fullmatch(parts.netloc.rpartition("@")[2]) is None:
        # The netloc is not a well-formed `host[:port]`, so `urlsplit` split
        # this URI at an unencoded character inside the userinfo, not at a
        # real query marker: `wss://user:a/b?c@host/v1` has netloc `user:a`
        # and "query" `c@host/v1`. Re-rendering without the "query" would
        # emit `wss://user:a/b` — half a password presented as a host, and
        # the real host gone. Fail closed, as the `ValueError` branch does.
        return uri
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def display_uri(uri: str) -> str:
    """The one way to render a whole connect URI in user-visible output.

    ``strip_query(redact_uri(uri))`` was open-coded identically in
    ``runtime._display_target`` and
    ``websocket_stt_service._endpoint_label``. Two call sites composing the
    same two-step policy by hand is one call site away from a third that
    forgets the second step — which is precisely how the query-string leak
    reached the exception path. Lives here, not at either call site,
    because neither module may import the other.
    """
    return strip_query(redact_uri(uri))
