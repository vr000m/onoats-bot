#!/usr/bin/env python3
"""Validate the onoats.dev contracts that ``build_prompts.py`` does not own.

This module is the owner of the rule values below; the docs point here. Each
contract has one helper, and each checks the whole set (every page, tag, link,
image and rule), never "some match exists":

- ``_chrome_errors``: every ``*.html`` under ``site/public`` has exactly one
  ``<nav aria-label="Primary">`` and one ``<footer>``, equal to the home
  page's. The only allowed difference is ``aria-current="page"``, which must
  mark exactly the nav link whose ``href`` is the page's own path, and no
  link on a page the nav does not list (the home page, the 404 page).
- ``_asset_copy_errors``: each ``ASSET_COPIES`` file is byte-identical to its
  canonical source.
- ``_image_name_errors``: every file under ``site/public/img/`` (served with an
  immutable cache) matches ``VERSIONED_IMAGE``.
- ``_reference_errors``: every ``/img/<name>`` in a published ``.css``,
  ``.html`` or ``.txt`` file is versioned and exists.
- ``_fragment_errors``: every site-local ``href`` with a ``#fragment`` on a
  published page reaches a published page that carries that ``id``.
- ``_social_card_errors``: the home page has exactly one ``og:image``,
  ``twitter:image``, ``og:image:width`` and ``og:image:height`` (and none of
  the alias tags in ``SOCIAL_ALIASES``); both URLs are the same
  ``https://onoats.dev/img/<file>.png``; the declared and actual size is
  ``CARD_SIZE``; ``render_social_card.swift`` writes that file.
- ``_assetsignore_errors``: ``site/public/.assetsignore`` lists exactly ``.*``.
- ``_headers_errors``: ``GLOBAL_HEADERS`` (including ``REQUIRED_CSP``) only in
  the ``/*`` rule; one immutable ``Cache-Control`` only in ``/img/*``.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
import zlib
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DIR = Path("site/public")
IMAGE_DIR = PUBLIC_DIR / "img"
HOME_PAGE = PUBLIC_DIR / "index.html"  # shared-chrome reference
ASSET_COPIES = {
    Path("site/public/favicon.svg"): Path("assets/brand/onoats-mark.svg"),
    Path("site/public/img/onoats-mark-1.6.0.svg"): Path("assets/brand/onoats-mark.svg"),
    Path("site/public/img/onoats-1.6.0-2-recording.png"): Path(
        "assets/screenshots/onoats-1.6.0-2-recording.png"
    ),
    Path("site/public/img/onoats-1.6.0-3-settings.png"): Path(
        "assets/screenshots/onoats-1.6.0-3-settings.png"
    ),
}
VERSIONED_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9-]*-\d+\.\d+\.\d+(?:-[a-z0-9-]+)?\.(?:avif|gif|jpe?g|png|svg|webp)"
)
IMAGE_REFERENCE = re.compile(r"/img/(?P<name>[a-zA-Z0-9._-]+)")
SOCIAL_URL = re.compile(r"https://onoats\.dev/img/(?P<name>[a-zA-Z0-9._-]+\.png)")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SOCIAL_SOURCE = Path("site/scripts/render_social_card.swift")
CARD_SIZE = (1200, 630)
SOCIAL_TAGS = ("og:image", "og:image:height", "og:image:width", "twitter:image")
# Crawlers may read these instead of og:image/twitter:image, so a stale copy
# could advertise a different card.
SOCIAL_ALIASES = frozenset({"og:image:secure_url", "og:image:url", "twitter:image:src"})
HEADERS = PUBLIC_DIR / "_headers"
ASSETSIGNORE = PUBLIC_DIR / ".assetsignore"
REFERENCE_SUFFIXES = frozenset({".css", ".html", ".txt"})
HEADER_LINE = re.compile(r"(?P<name>[A-Za-z0-9-]+):[ \t]*(?P<value>\S.*)")
SITE_ORIGIN = "https://onoats.dev"
GLOBAL_RULE = "/*"
IMAGE_RULE = "/img/*"
REQUIRED_CSP = (
    "default-src 'none'; img-src 'self'; style-src 'self' 'unsafe-inline';"
    " base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
# Lower-cased name -> (name as written, the one value the /* rule must carry).
GLOBAL_HEADERS = {
    "content-security-policy": ("Content-Security-Policy", REQUIRED_CSP),
    "referrer-policy": ("Referrer-Policy", "strict-origin-when-cross-origin"),
    "strict-transport-security": ("Strict-Transport-Security", "max-age=31536000"),
    "x-content-type-options": ("X-Content-Type-Options", "nosniff"),
    "x-frame-options": ("X-Frame-Options", "DENY"),
}
NAV = re.compile(r'<nav aria-label="Primary">.*?</nav>', re.DOTALL)
FOOTER = re.compile(r"<footer>.*?</footer>", re.DOTALL)
NAV_LINK = re.compile(r"<a\b(?P<attrs>[^>]*)>")
HREF = re.compile(r'\bhref="(?P<href>[^"]*)"')
CURRENT = ' aria-current="page"'


def _rel(repo_root: Path, path: Path) -> Path:
    return path.relative_to(repo_root)


def _block(repo_root: Path, page: Path, pattern: re.Pattern[str], label: str) -> str:
    """The page's one block matching *pattern*; a second copy is an error too."""

    blocks = pattern.findall(page.read_text(encoding="utf-8"))
    if len(blocks) != 1:
        problem = "missing" if not blocks else f"{len(blocks)} copies of the"
        raise ValueError(f"{_rel(repo_root, page)}: {problem} shared {label}")
    return blocks[0]


def _normalise_nav(block: str) -> str:
    return block.replace(CURRENT, "")


def _own_path(repo_root: Path, page: Path) -> str:
    """The URL path a page is served at: ``a/index.html`` is ``/a/``."""

    served = page.relative_to(repo_root / PUBLIC_DIR).as_posix()
    if served == "index.html" or served.endswith("/index.html"):
        served = served[: -len("index.html")]
    return "/" + served


def _aria_current_errors(label: Path, nav: str, own: str) -> list[str]:
    """``aria-current="page"`` marks the nav link to *own* and nothing else."""

    links = [match["attrs"] for match in NAV_LINK.finditer(nav)]
    hrefs = [(HREF.search(attrs) or {"href": None})["href"] for attrs in links]
    marked = [href for href, attrs in zip(hrefs, links) if CURRENT in attrs]
    expected = [own] if own in hrefs else []
    if marked != expected or nav.count(CURRENT) != len(marked):
        where = f"only the link to {own}" if expected else "no link on this page"
        return [f'{label}: aria-current="page" must mark {where}']
    return []


def _png_size(data: bytes) -> tuple[int, int] | None:
    """Return a PNG's width and height, or None unless the whole file is sound.

    Walks every chunk: IHDR first, each CRC valid, at least one IDAT, and IEND
    as the last bytes. A truncated or padded file is not a usable card even
    when its header still names the right size.
    """

    if data[:8] != PNG_SIGNATURE:
        return None
    position, size, has_data = 8, None, False
    while position + 12 <= len(data):
        length, kind = struct.unpack(">I4s", data[position : position + 8])
        body_end = position + 8 + length
        if body_end + 4 > len(data):
            return None
        body = data[position + 8 : body_end]
        (crc,) = struct.unpack(">I", data[body_end : body_end + 4])
        if zlib.crc32(kind + body) != crc:
            return None
        if position == 8:
            if kind != b"IHDR" or length != 13:
                return None
            size = struct.unpack(">II", body[:8])
        has_data = has_data or kind == b"IDAT"
        position = body_end + 4
        if kind == b"IEND":
            return size if has_data and position == len(data) else None
    return None


class _MetaTags(HTMLParser):
    """Every ``<meta>`` keyed by its ``property`` or ``name``, in page order.

    Parsing (rather than matching one attribute order) means a duplicate tag
    cannot hide behind a different spelling of the same element.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        values = dict(attrs)
        for key in (values.get("property"), values.get("name")):
            if key:
                self.tags.append((key, values.get("content") or ""))


def _social_card_errors(repo_root: Path, home: Path) -> list[str]:
    """The card the home page advertises must be the one the renderer writes.

    The advertised file is read from the page metadata, not named here, so
    bumping it for a new release cannot leave this check pointing at the old PNG.
    Each tag must appear exactly once: a crawler may read the first of several.
    """

    label = home.relative_to(repo_root)
    try:
        text = home.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{label}: cannot read ({exc.strerror})"]
    parser = _MetaTags()
    parser.feed(text)
    parser.close()
    found = {key: [v for k, v in parser.tags if k == key] for key in SOCIAL_TAGS}
    errors = [
        f"{label}: needs exactly one {key} meta tag, found {len(values)}"
        for key, values in found.items()
        if len(values) != 1
    ]
    errors += [
        f"{label}: {key} must not be set; og:image names the social card"
        for key in sorted(SOCIAL_ALIASES & {key for key, _ in parser.tags})
    ]
    if errors:
        return errors
    if found["og:image"] != found["twitter:image"]:
        return [f"{label}: og:image and twitter:image must name one social card"]
    declared = (found["og:image:width"][0], found["og:image:height"][0])
    if declared != tuple(str(pixels) for pixels in CARD_SIZE):
        errors.append(f"{label}: og:image:width/height must be 1200 and 630")
    reference = SOCIAL_URL.fullmatch(found["og:image"][0])
    if reference is None:
        errors.append(
            f"{label}: the social card URL must be https://onoats.dev/img/<file>"
        )
        return errors

    name = reference["name"]
    card = repo_root / IMAGE_DIR / name
    if card.is_file() and _png_size(card.read_bytes()) != CARD_SIZE:
        # a missing card is already reported by the reference scan
        errors.append(f"{IMAGE_DIR / name}: must be a 1200x630 PNG")
    source = repo_root / SOCIAL_SOURCE
    if not source.is_file():
        errors.append(f"{SOCIAL_SOURCE}: missing social-card source")
    elif name not in source.read_text(encoding="utf-8"):
        errors.append(f"{SOCIAL_SOURCE}: does not write {name}")
    return errors


def _parse_headers(
    text: str,
) -> tuple[list[tuple[str, list[tuple[str, str]]]], list[str]]:
    """Split Cloudflare ``_headers`` text into ``(path pattern, headers)`` rules.

    A line that starts in column 0 opens a rule; an indented ``Name: value``
    line belongs to the rule above it. Header names are lower-cased. Anything
    else (an indented line before the first rule, a line that is not
    ``Name: value``, a detach ``! Name``) is reported, never skipped.
    """

    rules: list[tuple[str, list[tuple[str, str]]]] = []
    errors: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not line[0].isspace():
            rules.append((stripped, []))
            continue
        match = HEADER_LINE.fullmatch(stripped)
        if not rules:
            errors.append(f"{HEADERS}:{number}: header line before any path rule")
        elif match is None:
            errors.append(f"{HEADERS}:{number}: not a 'Name: value' header line")
        else:
            rules[-1][1].append((match["name"].lower(), match["value"]))
    return rules, errors


def _pattern_matches(pattern: str, path: str) -> bool:
    """Whether a ``_headers`` URL pattern (``*`` splat, ``:name`` placeholder) matches."""

    regex = re.escape(pattern).replace(r"\*", ".*")
    regex = re.sub(r":[A-Za-z]\w*", "[^/]+", regex)
    return re.fullmatch(regex, path) is not None


def _csp_directives(policy: str) -> tuple[dict[str, frozenset[str]], list[str]]:
    """Directive name -> source set, plus every directive named twice.

    Browsers use the first copy of a repeated directive, so a repeat is an
    error rather than something to merge.
    """

    directives: dict[str, frozenset[str]] = {}
    repeated: list[str] = []
    for part in policy.split(";"):
        tokens = part.split()
        if not tokens:
            continue
        name = tokens[0].lower()
        if name in directives:
            repeated.append(name)
        else:
            directives[name] = frozenset(tokens[1:])
    return directives, repeated


def _csp_errors(policy: str) -> list[str]:
    """The policy keeps every committed directive with the same sources.

    Order is free. Another fetch directive (``*-src``) is rejected because it
    can only widen ``default-src 'none'``; other additions are allowed.
    """

    required, _ = _csp_directives(REQUIRED_CSP)
    directives, repeated = _csp_directives(policy)
    label = f"{HEADERS}: Content-Security-Policy"
    errors = [f"{label} repeats {name}" for name in repeated]
    for name, sources in required.items():
        if directives.get(name) != sources:
            errors.append(f"{label} must keep {name} {' '.join(sorted(sources))}")
    for name in sorted(set(directives) - set(required)):
        if "-src" in name:
            errors.append(f"{label} adds {name}, which widens default-src 'none'")
    return errors


def _headers_errors(text: str, image_paths: list[str]) -> list[str]:
    """Global security headers only in /*; immutable caching only in /img/*.

    Cloudflare applies every rule whose pattern matches a request and joins
    repeated headers, so each check is about the rule a header sits in, not
    whether the text appears somewhere in the file. *image_paths* are the
    served paths of every file under public/img/; no rule but /img/* may set
    Cache-Control on any of them.
    """

    rules, errors = _parse_headers(text)
    patterns = [pattern for pattern, _ in rules]
    for pattern in sorted({p for p in patterns if patterns.count(p) > 1}):
        errors.append(f"{HEADERS}: {pattern} has more than one rule")

    def placed(header: str) -> list[tuple[str, str]]:
        return [
            (pattern, value)
            for pattern, headers in rules
            for name, value in headers
            if name == header
        ]

    for header, (written, value) in GLOBAL_HEADERS.items():
        found = placed(header)
        is_csp = header == "content-security-policy"
        if [pattern for pattern, _ in found] != [GLOBAL_RULE] or (
            not is_csp and found[0][1] != value
        ):
            errors.append(
                f"{HEADERS}: the {GLOBAL_RULE} rule, and only it, must set"
                f" {written}: {value}"
            )
        elif is_csp:
            errors.extend(_csp_errors(found[0][1]))

    cache = placed("cache-control")
    image_cache = [value for pattern, value in cache if pattern == IMAGE_RULE]
    if len(image_cache) != 1 or "immutable" not in image_cache[0].lower():
        errors.append(f"{HEADERS}: {IMAGE_RULE} must set one immutable Cache-Control")
    for pattern, value in cache:
        if pattern == IMAGE_RULE:
            continue
        if "immutable" in value.lower():
            errors.append(f"{HEADERS}: {pattern} sets an immutable Cache-Control")
            continue
        overlap = next(
            (
                path
                for path in image_paths
                if _pattern_matches(pattern, path)
                or _pattern_matches(pattern, SITE_ORIGIN + path)
            ),
            None,
        )
        if overlap is not None:
            errors.append(f"{HEADERS}: {pattern} also sets Cache-Control on {overlap}")
    return errors


def _assetsignore_errors(repo_root: Path) -> list[str]:
    """Wrangler uploads every file under public/ that .assetsignore does not list.

    The file must list exactly ``.*``: that keeps local dotfiles (.DS_Store)
    off the site, and allowing no other pattern means it cannot drop a page.
    """

    try:
        text = (repo_root / ASSETSIGNORE).read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{ASSETSIGNORE}: missing ({exc.strerror})"]
    patterns = [
        line.rstrip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    if patterns != [".*"]:
        return [f"{ASSETSIGNORE}: must list exactly one pattern, .*, not {patterns}"]
    return []


def _published_pages(repo_root: Path) -> list[Path]:
    """The home page first, then every other ``*.html`` under ``site/public``.

    Every published page carries the shared chrome, so a new page cannot be
    left out of the comparison by forgetting to list it.
    """

    home = repo_root / HOME_PAGE
    others = (repo_root / PUBLIC_DIR).rglob("*.html")
    return [home] + sorted(page for page in others if page != home)


def _chrome_errors(repo_root: Path, pages: list[Path]) -> list[str]:
    """Each page's navigation and footer match the home page's."""

    try:
        expected_nav = _normalise_nav(_block(repo_root, pages[0], NAV, "navigation"))
        expected_footer = _block(repo_root, pages[0], FOOTER, "footer")
    except OSError as exc:
        return [f"{_rel(repo_root, pages[0])}: cannot read ({exc.strerror})"]
    except ValueError as exc:
        return [str(exc)]
    errors: list[str] = []
    for index, page in enumerate(pages):
        label = _rel(repo_root, page)
        try:
            nav = _block(repo_root, page, NAV, "navigation")
            errors.extend(_aria_current_errors(label, nav, _own_path(repo_root, page)))
            if index == 0:
                continue
            if _normalise_nav(nav) != expected_nav:
                errors.append(
                    f"{label}: shared navigation differs beyond aria-current=page"
                )
            if _block(repo_root, page, FOOTER, "footer") != expected_footer:
                errors.append(f"{label}: shared footer differs")
        except OSError as exc:
            errors.append(f"{label}: cannot read ({exc.strerror})")
        except ValueError as exc:
            errors.append(str(exc))
    return errors


def _asset_copy_errors(repo_root: Path) -> list[str]:
    """Each declared public copy is byte-identical to its canonical source."""

    errors: list[str] = []
    for public_path, canonical_path in ASSET_COPIES.items():
        try:
            public = (repo_root / public_path).read_bytes()
            canonical = (repo_root / canonical_path).read_bytes()
        except OSError as exc:
            missing = Path(exc.filename).relative_to(repo_root)
            errors.append(f"{missing}: cannot read ({exc.strerror})")
            continue
        if public != canonical:
            errors.append(f"{public_path}: differs from {canonical_path}")
    return errors


def _image_files(repo_root: Path) -> list[Path]:
    """Every file served from /img/, in subdirectories too."""

    return sorted(path for path in (repo_root / IMAGE_DIR).rglob("*") if path.is_file())


def _image_name_errors(repo_root: Path) -> list[str]:
    """Every file served from /img/ (immutable cache) has a versioned name."""

    return [
        f"{_rel(repo_root, image)}: filename is not versioned"
        for image in _image_files(repo_root)
        if VERSIONED_IMAGE.fullmatch(image.name) is None
    ]


def _reference_errors(repo_root: Path) -> list[str]:
    """Every /img/ reference in a published text file is versioned and exists."""

    errors: list[str] = []
    for source in sorted((repo_root / PUBLIC_DIR).rglob("*")):
        if source.suffix not in REFERENCE_SUFFIXES:
            continue
        label = _rel(repo_root, source)
        for match in IMAGE_REFERENCE.finditer(source.read_text(encoding="utf-8")):
            name = match["name"]
            if VERSIONED_IMAGE.fullmatch(name) is None:
                errors.append(f"{label}: unversioned /img/{name}")
            if not (repo_root / IMAGE_DIR / name).is_file():
                errors.append(f"{label}: missing /img/{name}")
    return errors


class _Anchors(HTMLParser):
    """Every ``id`` and every ``href`` on one page, in page order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key == "id" and value:
                self.ids.add(value)
            elif key in ("href", "xlink:href") and value is not None:
                self.hrefs.append(value)


def _anchors(page: Path) -> _Anchors:
    parser = _Anchors()
    parser.feed(page.read_text(encoding="utf-8"))
    parser.close()
    return parser


def _fragment_errors(repo_root: Path, pages: list[Path]) -> list[str]:
    """Every site-local ``href`` with a ``#fragment`` names an id on its target.

    Same-page (``#x``), absolute (``/a/#x``) and relative (``../#x``) links are
    resolved against the page's own path; the target must be a published page
    and carry that ``id``. An empty fragment (the top of the page) and links
    with a scheme or host are not checked.
    """

    served = {_own_path(repo_root, page): page for page in pages}
    anchors = {page: _anchors(page) for page in pages}
    errors: list[str] = []
    for page in pages:
        label = _rel(repo_root, page)
        own = _own_path(repo_root, page)
        for href in anchors[page].hrefs:
            target = urlsplit(urljoin(own, href))
            if not target.fragment or target.scheme or target.netloc:
                continue
            reached = served.get(target.path or own)
            if reached is None:
                errors.append(f"{label}: {href} reaches no published page")
            elif unquote(target.fragment) not in anchors[reached].ids:
                errors.append(
                    f"{label}: {href} names no id on {_rel(repo_root, reached)}"
                )
    return errors


def _headers_file_errors(repo_root: Path) -> list[str]:
    try:
        text = (repo_root / HEADERS).read_text(encoding="utf-8")
    except OSError as exc:
        return [f"{HEADERS}: cannot read ({exc.strerror})"]
    image_dir = repo_root / IMAGE_DIR
    image_paths = [
        "/img/" + image.relative_to(image_dir).as_posix()
        for image in _image_files(repo_root)
    ]
    return _headers_errors(text, image_paths)


def collect_errors(repo_root: Path) -> list[str]:
    """Return every site-contract violation below *repo_root*."""

    pages = _published_pages(repo_root)
    return [
        *_chrome_errors(repo_root, pages),
        *_asset_copy_errors(repo_root),
        *_image_name_errors(repo_root),
        *_reference_errors(repo_root),
        *_fragment_errors(repo_root, pages),
        *_social_card_errors(repo_root, pages[0]),
        *_assetsignore_errors(repo_root),
        *_headers_file_errors(repo_root),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    args = parser.parse_args(argv)
    errors = collect_errors(args.repo_root.resolve())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("site contracts: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
