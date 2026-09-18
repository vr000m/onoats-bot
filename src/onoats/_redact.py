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

**Why this is a scanner and not a URL parser.** Round 8 re-opened the
question of replacing the whole module with ``urllib.parse.urlsplit``-first
parsing, or with a canonical re-render (emit ``scheme://`` plus a
positively-recognised ``host[:port]`` and drop everything else). Both were
weighed and both were rejected, for reasons that are properties of the
*inputs*, not of this implementation:

* ``urlsplit`` cannot see the credentials this module exists for. It reports
  ``username``/``password`` as ``None`` — silently, without raising — for
  exactly the malformed shapes that reach here, because an unencoded ``/``,
  ``?`` or ``#`` in a password ends its netloc early. The module's whole
  reason to exist is that ``websockets.InvalidURI`` embeds a URI *the parser
  already refused*. Parsing first would therefore hand the easy, well-formed
  cases to a parser and leave every hard case on the heuristic path — the
  hard cases being the only ones that have ever leaked.
* A canonical re-render does kill the "fabricated host" and "half a password
  survives" failure modes by construction, and it is genuinely attractive.
  It does not kill the leak family, because the residual ambiguity is not a
  boundary problem: ``user:1234`` and ``localhost:8765`` are the same string
  in the same grammar (limitations 2, 4 and 5 below), so *recognising* the
  authority needs the same heuristics that *locating* it needs today. It
  also changes the output contract of every assertion in the generated
  sweep, which is the only artefact that has ever caught one of these bugs.

What *was* structural about rounds 1-8 is narrower and is fixed here: the
authority's **start** and its **well-formedness** were each re-derived
independently at four sites (:func:`_scan`, :func:`_query_cut`,
:func:`_incumbent_is_settled`, :func:`_not_query_of_path`), and RFC 3986's
two authority edge cases — a scheme-relative ``//host`` reference (§4.2) and
an *empty* authority (``ws:///path``) — were handled at none of them. Both
are now handled once, at :func:`_authority_anchor` and in
:func:`_query_cut`'s well-formedness test.

**The algorithm.** The credential's *start* is never guessed: it is either
the character after a ``scheme://`` prefix (the **authority path**) or the
message's first non-whitespace character, advanced past a leading ``//``
(the **bare path**). Only the *end* — which ``@`` terminates the userinfo —
is in question. So:

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
  URI continuation (it contains ``.``, ``:``, ``/`` or ``[``), (b) the
  password is RFC-legal userinfo apart from its spaces
  (:func:`_password_is_legal_userinfo`), or (c) the tail is a complete bare
  reg-name (``localhost``, ``stt-box``) and the ``@`` sits inside the
  anchor's *own* whitespace-delimited token.
* :func:`_not_query_of_path` — the text before the first ``?``/``#``, read
  from the **acceptance point** (one past the rightmost ``@`` accepted so
  far, not from the anchor), must not *already* be a well-formed
  authority-plus-optional-path with a real query behind it. That is the
  canonical shape of a URI with a query
  (``wss://host:443/v1?redirect=user@example.com``,
  ``wss://user:pass@host.example.com/v1?r=bob@corp.com``), whose ``@`` is an
  ordinary query-value character; reading it as userinfo fabricated a
  hostname out of the query's tail and destroyed the real one. This is also
  the only gate that settles a *same-token* rival, which the
  whitespace-scoped settled-authority test below cannot reach.
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
   toward redaction because the alternative is a leak. This covers the case
   where the ``word:word`` is itself a *settled* authority
   (``"ws://localhost:8765 retry as admin@corp.com"`` -> ``"ws://corp.com"``,
   destroying a credential-free diagnostic): the widened span
   ``8765 retry as admin`` is RFC-legal userinfo, so it is
   character-for-character the genuine ``ws://user:1234 retry as admin@…``
   the sweep requires be redacted. Demanding stronger tail evidence to
   overturn a settled authority would close it, and would simultaneously
   leak ``ws://user:1234 pw@localhost`` — this project's own canonical
   endpoint. Round 8 quarantined it on those grounds; see
   :func:`_scan`'s override rule.
3. A credential whose password contains ``/``, ``?``, ``#`` or a second
   ``:`` *and* which crosses a whitespace boundary into a word that is
   itself plain prose (``user:a/b c@host``) fails every branch of
   :func:`_tail_accepts` and both halves of :func:`_scan`'s override, and is
   not redacted. Within a single token the same shape *is* redacted
   (``ws://user:a/b@localhost``, ``ws://user:1234 /seg@host.tld/v1``), which
   is the shape an operator actually types; the spaced-plus-illegal
   combination survives only because the alternative is to delete the
   trailing half of ordinary diagnostics.
4. A URI whose query carries an ``@`` but *names no parameter*
   (``wss://localhost:443?bob@corp.com``) is over-redacted to
   ``wss://corp.com``. ``user:1234`` and ``localhost:443`` are the same
   grammar, so :func:`_not_query_of_path` needs either a dotted host with a
   path or a ``key=value`` query before it will refuse a credential, and
   resolves the rest toward redaction like limitation 2. Every real query
   shape (``?token=…``, ``?redirect=…``) keeps its authority.
5. A well-formed URI whose ``@`` sits in a *path* segment with no query
   (``wss://host:443/path/to/a@b.com``, ``ws://host:8765/p/user@y``) is
   still over-redacted to ``wss://b.com`` / ``ws://y``: it is
   character-for-character the same grammar as the genuine
   ``wss://user:1234/seg@host.tld`` and ``ws://user:pw/seg@localhost`` the
   sweep requires be redacted, so the ambiguity is resolved toward redaction
   like limitation 2. Round 8 extended this from dotted tails to bare
   reg-name tails (:func:`_tail_accepts` branch (c)) because ``localhost``
   is this project's own canonical STT host and had no dot to offer.
6. A scheme-less credential is redacted, and a scheme-less query stripped,
   only when the URI is the message's **leading** token
   (``user:pw@host/v1?token=…`` yes; ``could not connect:
   user:pw@host/v1?token=…`` no). Both anchors are deliberate: an
   unanchored search for a ``user:pass@host``-shaped substring anywhere in
   a message is what destroyed credential-free diagnostics in five
   consecutive rounds. No call site produces the non-leading shape — every
   third-party message this module is fed (``websockets.InvalidURI``'s
   ``f"{uri} isn't a valid URI: {msg}"``) leads with the URI — so the
   exposure is latent, not live. Re-check this limitation before feeding
   :func:`safe_exc_text` an exception type that embeds a URI mid-sentence.
   A leading ``//`` (RFC 3986 §4.2 scheme-relative reference) counts as the
   leading token and is anchored past — see :func:`_authority_anchor`.

**Apply exactly once.** Every entry point here is idempotent in the sense
that matters — a second pass can never re-expose a span the first pass
deleted — but it is *not* a fixed point: re-feeding an already-redacted
string can delete a little more, because deleting a credential can leave a
shape the scanner reads as a new one (300k-case fuzz confirms deletion-only,
never re-exposure). No call site double-applies; :func:`display_uri` is the
one composition of the two steps, and it is the only thing display sites
call.

The generated sweep in ``tests/test_redact.py``, not this docstring, is the
specification.
"""

from __future__ import annotations

import re

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

# A *single-label* reg-name carrying a real, non-empty numeric port
# (`localhost:8765`, `stt-box:8765`). `localhost` is this project's own
# canonical local STT endpoint, so the scheme-less query strip may not demand
# a dotted host of it — but it may demand the port, which is the evidence
# that separates an endpoint from a prose word.
_SINGLE_LABEL_PORT_RE = re.compile(r"[^@/?#\[\]:\s]+:[0-9]+")

# A single label with no colon at all: `localhost`, `stt`. The weakest host
# claim this module recognises, and only ever in company with a
# `key=value`-shaped query (see `_scheme_less_authority_is_evident`).
_BARE_LABEL_RE = re.compile(r"[^@/?#\[\]:\s]+")

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


def _authority_anchor(text: str, start: int) -> int:
    """Advance a scheme-less anchor past a leading ``//``.

    RFC 3986 §4.2: a relative reference beginning ``//`` is
    *scheme-relative*, and its authority starts two characters in.
    ``//user:pass@host:8765/v1?token=…`` therefore defeated **both**
    scheme-less anchors at once, from one cause measured in two places: the
    credential scan rejected every candidate because ``//user`` carries a
    ``/`` in the username span (`_is_userinfo_shaped`), and the query strip
    found `_AUTHORITY_STOP_RE` matching at offset zero, so the authority
    span it tested was empty. Advancing the anchor once, here, fixes both.

    Only a genuine authority is stepped over: ``// note: see bob@corp.com``
    (a comment, or prose) has whitespace after the slashes, and ``///x`` has
    a third, so neither is advanced and both keep the pre-existing
    prose-safe behaviour.
    """
    if not text.startswith("//", start):
        return start
    nxt = start + 2
    if nxt >= len(text) or text[nxt].isspace() or text[nxt] == "/":
        return start
    return nxt


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

    A ``scheme://``-shaped match **inside this anchor's own whitespace-
    delimited token** is not the start of a second URI — it is inside the
    current candidate's own password (``wss://user:secret://tail@
    host.example.com``, whose ``secret://`` matches the scheme pattern).
    Honouring it truncated the search window short of the real terminating
    ``@``, so no candidate was found and the leftover ``user:secret`` was
    emitted verbatim. Such a match is skipped and the search resumes past
    it.

    The token, not the authority's ``/?#``-or-whitespace boundary, is the
    yardstick. An unencoded ``/`` in a password *is* that boundary
    (``ws://user:p/x://tail@host.example.com/v1``), so measuring against it
    put the password's own ``x://`` on the far side and truncated the window
    to ``user:p`` — the same leak in a new place. Whitespace is the one
    terminator a typed-in password cannot cross silently, and a genuine
    second URI in a diagnostic is always its own token
    (``ws://u1:p1@h1 and ws://u2:p2@h2``). A real nested URI inside one token
    (``https://host/p?redirect=https://u:p@evil/x``) costs nothing by being
    skipped here: the outer span is not userinfo-shaped, so the outer scan
    declines it and :func:`_redact_text`'s loop re-anchors on the inner
    ``scheme://`` as before.

    A match sitting exactly *at* `start` is never skipped: on the bare path
    the anchor is the message's first character, which is the ``ws://`` of a
    leading URI itself. Skipping that one would hand the bare scan the whole
    scheme-prefixed URI, which the authority path owns.
    """
    token_end = _first_stop(text, start, _WHITESPACE_RE, limit)
    pos = start
    while True:
        m = _SCHEME_PREFIX_RE.search(text, pos, limit)
        if m is None:
            return limit
        if m.start() >= token_end or m.start() <= start:
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


def _not_query_of_path(text: str, start: int, origin: int, at: int) -> bool:
    """Reject an ``@`` that sits in the *query* of a path-bearing URI.

    ``wss://host:443/v1?redirect=user@example.com`` carries no credential at
    all, but ``host`` reads as a username, ``443`` as a password, and the
    query's ``example.com`` tail satisfies `_tail_accepts` — so the real
    host was discarded and a fabricated one substituted, in exactly the
    ``_display_target``/``_endpoint_label`` output operators diagnose from.

    The discriminator is positive and structural: take the first ``?``/``#``
    after `origin` and ask whether what precedes it is *already* a
    well-formed URI prefix — a complete ``host[:port]``, optionally followed
    by a path. If it is, the URI has explained itself without any userinfo
    and the ``@`` is an ordinary query-value character.

    **Two spans, not one.** `start` is the anchor, and it is what the
    *userinfo* question is asked of (``text[start:at]`` holds the colon).
    `origin` is the **acceptance point** — one past the rightmost ``@``
    already accepted, or `start` when none is — and it is what the
    *authority* question must be asked of. Measuring the authority from
    `start` left the already-accepted ``user:pass@`` prefix inside the span,
    so ``_HOST_PORT_RE`` (which excludes ``@``) could never match and the
    gate never fired: ``wss://user:pass@host.example.com/v1?r=bob@corp.com``
    collapsed to ``wss://corp.com``. The credential-free twin was handled
    correctly, which is why two generated corpora that never crossed
    credentials with query-``@``s missed it for two rounds.

    **The evidence.** Requiring a ``/`` before the ``?`` (the round-5 rule)
    missed ``wss://host.example.com:443?redirect=bob@corp.com``. Requiring
    only ``host[:port]`` over-reaches the other way: ``user:1234`` *is* a
    well-formed ``host:port``, so ``ws://user:1234/x?q@host.example.com/v1``
    had its real credential refused and leaked whole. So the veto needs one
    of two independent pieces of positive evidence:

    * a **dotted (or bracketed) host followed by a path** — the shape no
      credential can imitate, and the one that must hold even for a
      fragment carrying no ``=`` (``.../v1#f@g.com``); or
    * a **``key=value``-shaped query** between the ``?``/``#`` and the
      ``@``. A real query names its parameters; ``?q@host`` and ``#frag@``
      are unencoded password characters, which is exactly how
      ``user:1234/x?q@…`` and ``alice@corp.com:1234?q@…`` differ from
      ``localhost:8765/v1?user=bob@corp.com``.
    """
    if _userinfo_colon(text, start, at) == -1:
        return True
    query = _QUERY_START_RE.search(text, origin, at)
    if query is None:
        return True
    slash = text.find("/", origin, query.start())
    authority_stop = query.start() if slash == -1 else slash
    if _HOST_PORT_RE.fullmatch(text, origin, authority_stop) is None:
        # Truncated authority: the `?`/`#` is a password character, not a
        # query marker (`ws://user:a/b?c@host/v1`).
        return True
    dotted = _DOTTED_HOST_PORT_RE.fullmatch(text, origin, authority_stop) is not None
    if dotted and slash != -1:
        return False
    if not dotted and slash == -1 and origin == start:
        # Weakest possible authority: a single label, no path, nothing but a
        # `?` behind it — and *nothing accepted yet*, so this span is still
        # the raw anchor. `user:1234` is that shape and so is
        # `localhost:8765`, and no local syntax separates them, but
        # `ws://user:1234?x=1@host.example.com/v1` is a real credential whose
        # password merely happens to open a `key=value` run, so the `=`
        # evidence is not strong enough to veto on its own here.
        #
        # `origin == start` is what keeps that from re-opening the round-7
        # class. Once an `@` has been accepted the span is no longer a
        # userinfo candidate at all — it is the authority the credential
        # pointed at (`ws://user:pass@localhost:8765?r=bob@corp.com`) — and
        # a single label with a real port is then exactly as good a host as
        # a dotted one, which `_SINGLE_LABEL_PORT_RE` says elsewhere.
        return True
    tail_end = _first_stop(text, at + 1, _WHITESPACE_RE, len(text))
    if (
        _HOST_PORT_RE.fullmatch(text, at + 1, tail_end) is not None
        and _DOTTED_HOST_PORT_RE.fullmatch(text, at + 1, tail_end) is None
    ):
        # The `@` is followed by a *dotless* reg-name (`@localhost`,
        # `@stt-box:8765`). Every shape this veto exists to protect ends in
        # an email address, and an email address has a dotted domain; a
        # query value that ends at an `@` followed by a bare single label is
        # not a query value at all. Found by round 8's own generated sweep:
        # `ws://alice@corp.com:1234?a=1@localhost` presents `corp.com:1234`
        # as a dotted authority with a `key=value` query behind it — exactly
        # `ws://alice@corp.com:hunter2@host.example.com:443?r=bob@corp.com`,
        # which must keep its host — and the tail is the only thing that
        # tells them apart.
        return True
    return "=" not in text[query.start() + 1 : at]


def _tail_accepts(
    text: str, start: int, at: int, outer_stop: int, first_ws: int
) -> bool:
    """Positive evidence that the ``@`` at `at` really terminates userinfo.

    Required for every candidate past the authority's own `/?#`-or-
    whitespace boundary — i.e. for exactly the malformed shapes a
    well-formed-URI parser cannot handle. Either:

    (a) the token after the ``@`` continues a URI (`_HOST_CONTINUATION`),
    (b) the password is RFC-legal userinfo apart from its spaces, or
    (c) the tail is a complete bare reg-name and the ``@`` is inside the
        anchor's own whitespace-delimited token.

    A prose span such as ``"2026-09-17T10:00:00 connect user@host"`` fails
    all three — a bare ``host`` tail, a password (``"00:00 connect user"``)
    carrying a second colon, and an ``@`` three words downstream of the
    anchor.

    Branch (c) is round 8's. A bare reg-name is a perfectly legal RFC 3986
    host, and ``localhost`` — this project's own canonical STT endpoint —
    is one, so ``ws://user:S3CRETPW/seg@localhost`` and
    ``ws://user:pw?q@localhost`` (password characters that end the authority
    early, against a host with no dot, no port and no path) satisfied
    neither (a) nor (b) and were emitted verbatim, credential and all. With
    trailing prose they were worse than verbatim: the scan fell through to
    the prose's own ``@`` and fabricated a host from it.

    `first_ws` is what keeps (c) from swallowing diagnostics. Inside one
    token ``user:pw/seg@localhost`` has no prose reading; across a space it
    has almost nothing else, and every credential-free shape the sweep
    protects (``"Error: connect to user@host"``,
    ``"C:/Users/bob connect user@host"``) puts its ``@`` in a later word.
    The price is limitation 5, now extended to bare tails: a well-formed
    ``ws://host:8765/p/user@y`` is over-redacted to ``ws://y``, because it
    is the same string grammar.
    """
    tail_end = _first_stop(text, at + 1, _WHITESPACE_RE, outer_stop)
    tail = text[at + 1 : tail_end]
    if tail and any(ch in _HOST_CONTINUATION for ch in tail):
        return True
    if _password_is_legal_userinfo(text, start, at):
        return True
    # Branch (c) measures the tail to the *authority's* own terminator, not
    # to the whitespace branch (a) uses: `ws://user:p/x@localhost?token=T`
    # has a bare host AND a query, and bounding at the whitespace made the
    # tail `localhost?token=T`, which no `host[:port]` can fullmatch — so the
    # credential was emitted verbatim with its own query behind it. Found by
    # round 8's fuzz over the cross product, not by a hand-written case.
    host_end = _first_stop(text, at + 1, _AUTHORITY_STOP_RE, outer_stop)
    return (
        at < first_ws
        and host_end > at + 1
        and _HOST_PORT_RE.fullmatch(text, at + 1, host_end) is not None
        and _is_userinfo_shaped(text, start, at)
    )


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


def _widened_span_is_not_prose(text: str, first_ws: int, at: int) -> bool:
    """Is every word the widening added to the password *not* a prose word?

    The second way a candidate may overturn a settled authority, alongside
    `_password_is_legal_userinfo`. That one asks whether the password is
    RFC-legal, which a password cannot be once an unencoded ``/``, ``?``,
    ``#`` or second ``:`` is what ended the authority early — precisely the
    malformed shape this module exists for. So
    ``ws://user:1234 /seg@host.example.com/v1`` had its real credential
    refused and ``user:1234 /seg`` emitted verbatim: ``user:1234`` is a
    complete ``host:port`` and therefore "settled", and the password's ``/``
    disqualified the only override there was.

    The evidence here is the widened words themselves. A word carrying a
    URI-structural character (``/ ? # : [ ] @``) is not a word that appears
    in a sentence; a bare label (`_BARE_LABEL_RE`) is. Requiring *every*
    added word to be structural keeps the diagnostics the sweep protects —
    ``"wss://host.example.com:443/v1 failed: user@corp.com"`` adds
    ``failed:`` (structural) and ``user`` (a plain label), so the override
    stays refused and the authority stands.
    """
    if at <= first_ws:
        return False
    words = text[first_ws:at].split()
    if not words:
        return False
    return not any(_BARE_LABEL_RE.fullmatch(word) for word in words)


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
    (`_password_is_legal_userinfo`), or a widened span made entirely of
    words no sentence would contain (`_widened_span_is_not_prose`).
    `_tail_accepts` branch (a)'s "the next word looks like a host" is not
    enough, because every email address in every diagnostic satisfies it.

    Neither override can distinguish ``ws://localhost:8765 retry as
    admin@corp.com`` (credential-free prose, destroyed) from
    ``ws://user:1234 retry as admin@corp.com`` (a real credential): the two
    are the same string in the same grammar. Round 8 considered demanding a
    dotted or ported tail before overturning a settled authority, which
    closes the first — and opens ``ws://user:1234 pw@localhost``, a leak
    against this project's own canonical host. Kept as limitation 2.

    Settledness is **one** question asked of whichever authority is current,
    re-answered — not accumulated — each time the current authority changes.
    It is *assigned*, never OR-ed, and that is deliberate: the two readings
    below are answers to the same question about different spans, and the
    later one supersedes the earlier one outright.

    * **Before any ``@`` is accepted** the authority runs from the anchor:
      ``text[start:authority_stop]`` being already a complete ``host[:port]``
      means it terminated well-formed with no userinfo in it at all
      (``wss://host.example.com:443/v1 failed: user@corp.com``,
      ``unix:/tmp/x.sock error: svc@host.com``). Nothing tested this for five
      rounds, so a later prose ``@`` was accepted as the *first* candidate
      and the whole real authority — host, port, path — was deleted in front
      of it.
    * **Once one is accepted** the authority runs from ``best + 1`` instead,
      and only `_incumbent_is_settled` can speak for it. Keeping the stale
      pre-acceptance answer alive here would be wrong, not conservative: it
      describes a span that is no longer the authority.

    Neither is a veto. ``user:1234`` is character-for-character a
    ``host:port``, so ``ws://user:1234 abcd@host:8765`` presents a complete
    "authority" that is really a credential with a space in its password; a
    legal password overturns it, exactly as it overturns an incumbent.

    The ``at >= first_ws`` guard on that override is load-bearing and stays:
    within a *single* token the two grammars are identical, and
    ``ws://user:1234/seg@host:8765/`` — a genuine credential whose
    digit-shaped password makes ``user:1234`` a perfect ``host:port`` — must
    still be redacted. Same-token candidates are therefore settled by
    `_not_query_of_path`, not here.
    """
    authority_stop, first_ws, window_end = _search_window(text, start, outer_stop)
    best: int | None = None
    settled = _HOST_PORT_RE.fullmatch(text, start, authority_stop) is not None
    for at in _at_positions(text, start, window_end):
        # The authority begins one past the rightmost `@` accepted so far;
        # the userinfo still begins at the anchor. `_not_query_of_path` is
        # the one gate that asks about the *authority*, so it is the one
        # gate that takes both.
        origin = start if best is None else best + 1
        if at < authority_stop:
            if bare and not _is_userinfo_shaped(text, start, at):
                continue
        elif not (
            _is_userinfo_shaped(text, start, at)
            and _not_query_of_path(text, start, origin, at)
            and _tail_accepts(text, start, at, outer_stop, first_ws)
        ):
            continue
        if best is not None:
            settled = _incumbent_is_settled(text, best, at, outer_stop)
        if (
            settled
            and at >= first_ws
            and not _password_is_legal_userinfo(text, start, at)
            and not _widened_span_is_not_prose(text, first_ws, at)
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
    stripped_offset = _authority_anchor(text, n - len(text.lstrip()))
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


def _scheme_less_authority_is_evident(
    text: str, start: int, authority_stop: int, cut: int, token_end: int
) -> bool:
    """Is the scheme-less token at `start` really a URI, not prose?

    No ``scheme://`` vouches for it, so the evidence has to come from the
    token itself. Three accepted shapes, weakest last:

    * a **dotted reg-name or bracketed IPv6 literal**, optional port
      (``stt.example.com:8765``, ``[::1]:2020``);
    * a **single label with a real, non-empty numeric port**
      (``localhost:8765``, ``stt-box:8765``). Demanding a dot here is what
      let this project's own canonical local endpoint leak its
      ``?token=`` — ``localhost`` has no dot and never will;
    * a **single label with no colon at all**, but only alongside a
      ``key=value``-shaped query (``localhost/v1?token=…``). A prose word
      followed by a question mark (``Traceback? no, a warning``) carries no
      ``=``, and anything with a colon but no numeric port (``C:``,
      ``Error:``, ``unix:``) is refused outright.
    """
    if _DOTTED_HOST_PORT_RE.fullmatch(text, start, authority_stop) is not None:
        return True
    if _SINGLE_LABEL_PORT_RE.fullmatch(text, start, authority_stop) is not None:
        return True
    if _BARE_LABEL_RE.fullmatch(text, start, authority_stop) is None:
        return False
    return "=" in text[cut + 1 : token_end]


def _query_cut(
    text: str, authority_start: int, limit: int, *, bare: bool
) -> tuple[int, int] | None:
    """`(cut, token_end)` for the query of the URI token at `authority_start`.

    ``None`` when the token has no query, or — the part that matters — when
    the span in front of the ``?``/``#`` is *not* a well-formed authority.
    A truncated authority means the ``?`` is an unencoded password character
    (``wss://user:a/b?c@host/v1``), and cutting there would emit
    ``wss://user:a/b``: a fabricated host, half a password, and no trace of
    the real one — the very failure `_redact_text` is careful to avoid,
    reintroduced by the step composed on top of it.

    `bare` selects `_scheme_less_authority_is_evident` over the plain
    ``host[:port]`` test a ``scheme://`` prefix already justifies.

    The cut always ends at `token_end`, the token's own first whitespace.
    Nothing shorter: a query *value* may legitimately contain ``://``
    (``?next=wss://relay.example.com/``), and clamping the span to that
    ``scheme://`` cut the query in half and glued its tail straight onto the
    path.
    """
    token_end = _first_stop(text, authority_start, _WHITESPACE_RE, limit)
    cut = _first_stop(text, authority_start, _QUERY_START_RE, token_end)
    if cut >= token_end:
        return None
    authority_stop = _first_stop(text, authority_start, _AUTHORITY_STOP_RE, token_end)
    if bare:
        if not _scheme_less_authority_is_evident(
            text, authority_start, authority_stop, cut, token_end
        ):
            return None
    elif authority_stop == authority_start:
        # An *empty* authority (`ws:///v1?token=…`, `ws://?token=…`) is
        # well-formed RFC 3986, not a truncated one: `authority_stop` can
        # only land on `/`, `?` or `#` here (whitespace is `token_end`), and
        # a zero-length span cannot hold userinfo, so there is no password
        # for the `?` to belong to. `_HOST_PORT_RE` requires at least one
        # character, so it read this as truncation and let the query through
        # — verified against the real `websockets.uri.parse_uri`, which
        # raises `InvalidURI` on exactly this shape and embeds the token.
        pass
    elif _HOST_PORT_RE.fullmatch(text, authority_start, authority_stop) is None:
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
    lead = n - len(text.lstrip())
    # The scheme-less anchor exists only when the leading token is *not*
    # itself a `scheme://` URI — that one belongs to the loop below. Beyond
    # that single question the anchor is bounded by its own token
    # (`_query_cut`), never by `_next_uri_boundary`. `_authority_anchor`
    # steps it past a leading `//`, whose authority starts two characters in.
    bare = (
        None
        if _SCHEME_PREFIX_RE.match(text, lead) is not None
        else _query_cut(text, _authority_anchor(text, lead), n, bare=True)
    )
    if bare is not None:
        cut, cursor = bare
        out.append(text[:cut])
    for m in _SCHEME_PREFIX_RE.finditer(text):
        if m.start() < cursor:
            continue
        found = _query_cut(text, m.end(), n, bare=False)
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

    One implementation, not two. This used to parse with
    ``urllib.parse.urlsplit`` and re-render, while :func:`_strip_query_spans`
    scanned the same shapes by hand for the exception path — and the two
    diverged exactly where it hurt: ``urlsplit`` gives a *scheme-less* URI an
    empty ``netloc``, so the fail-closed ``host[:port]`` guard returned
    ``stt.example.com:8765/v1?token=SEKRET`` unchanged, query and all,
    through :func:`display_uri` — the documented single owner of rendering a
    whole connect URI — into ``runtime._display_target``,
    ``websocket_stt_service._endpoint_label``, every ``SttPreflightError``
    and every reconnect log line. A whole-string URI is just degenerate
    exception text, so it now takes the same scanner, which anchors on the
    scheme-less shape too.
    """
    return _strip_query_spans(uri)


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
