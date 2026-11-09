#!/usr/bin/env python3
"""Validate onoats.dev source and generated-file invariants."""

from __future__ import annotations

import argparse
import re
import struct
import sys
import zlib
from pathlib import Path

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
HOME_PAGE = Path("site/public/index.html")  # shared-chrome reference
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
META_IMAGE = re.compile(
    r'<meta (?:property|name)="(?P<key>og:image|twitter:image)" content="(?P<url>[^"]*)"'
)
META_SIZE = re.compile(
    r'<meta property="og:image:(?P<axis>width|height)" content="(?P<pixels>\d+)"'
)
HEADERS = "site/public/_headers"
HEADER_LINE = re.compile(r"(?P<name>[A-Za-z0-9-]+):[ \t]*(?P<value>\S.*)")
IMAGE_PROBE = "/img/onoats-mark-1.6.0.svg"  # any URL served from public/img/
NAV = re.compile(r'<nav aria-label="Primary">.*?</nav>', re.DOTALL)
FOOTER = re.compile(r"<footer>.*?</footer>", re.DOTALL)


def _block(page: Path, pattern: re.Pattern[str], label: str) -> str:
    match = pattern.search(page.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{page}: missing shared {label}")
    return match.group(0)


def _normalise_nav(block: str) -> str:
    return block.replace(' aria-current="page"', "")


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


def _social_card_errors(repo_root: Path, home: Path) -> list[str]:
    """The card the home page advertises must be the one the renderer writes.

    The advertised file is read from the page metadata, not named here, so
    bumping it for a new release cannot leave this check pointing at the old PNG.
    """

    label = home.relative_to(repo_root)
    try:
        text = home.read_text(encoding="utf-8")
    except OSError as exc:
        return [str(exc)]
    urls = {m["key"]: m["url"] for m in META_IMAGE.finditer(text)}
    if set(urls) != {"og:image", "twitter:image"} or len(set(urls.values())) != 1:
        return [f"{label}: og:image and twitter:image must name one social card"]
    reference = SOCIAL_URL.fullmatch(urls["og:image"])
    if reference is None:
        return [f"{label}: the social card URL must be https://onoats.dev/img/<file>"]

    name = reference["name"]
    errors: list[str] = []
    card = repo_root / "site/public/img" / name
    if card.is_file():  # a missing card is already reported by the reference scan
        declared = {m["axis"]: int(m["pixels"]) for m in META_SIZE.finditer(text)}
        if _png_size(card.read_bytes()) != (1200, 630):
            errors.append(f"site/public/img/{name}: must be a 1200x630 PNG")
        if declared != {"width": 1200, "height": 630}:
            errors.append(f"{label}: og:image:width/height must be 1200 and 630")
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


def _headers_errors(text: str) -> list[str]:
    """HSTS lives only in the global rule; immutable caching only under /img/*.

    Cloudflare applies every rule whose pattern matches a request and joins
    repeated headers, so each check is about the rule a header sits in, not
    whether the text appears somewhere in the file.
    """

    rules, errors = _parse_headers(text)
    patterns = [pattern for pattern, _ in rules]
    for pattern in sorted({p for p in patterns if patterns.count(p) > 1}):
        errors.append(f"{HEADERS}: {pattern} has more than one rule")

    hsts = [
        (pattern, value)
        for pattern, headers in rules
        for name, value in headers
        if name == "strict-transport-security"
    ]
    if hsts != [("/*", "max-age=31536000")]:
        errors.append(
            f"{HEADERS}: the /* rule, and only it, must set"
            " Strict-Transport-Security: max-age=31536000"
        )

    cache = [
        (pattern, value)
        for pattern, headers in rules
        for name, value in headers
        if name == "cache-control"
    ]
    image_cache = [value for pattern, value in cache if pattern == "/img/*"]
    if len(image_cache) != 1 or "immutable" not in image_cache[0].lower():
        errors.append(f"{HEADERS}: /img/* must set one immutable Cache-Control")
    for pattern, value in cache:
        if pattern == "/img/*":
            continue
        if "immutable" in value.lower():
            errors.append(f"{HEADERS}: {pattern} sets an immutable Cache-Control")
        elif _pattern_matches(pattern, IMAGE_PROBE):
            errors.append(f"{HEADERS}: {pattern} also sets Cache-Control on /img/*")
    return errors


def collect_errors(repo_root: Path) -> list[str]:
    """Return every site-contract violation below *repo_root*."""

    errors: list[str] = []
    home = repo_root / HOME_PAGE
    # Every published page carries the shared chrome, so a new page cannot be
    # left out of the comparison by forgetting to list it.
    pages = [home] + sorted(
        page for page in (repo_root / "site/public").rglob("*.html") if page != home
    )

    try:
        expected_nav = _normalise_nav(_block(pages[0], NAV, "navigation"))
        expected_footer = _block(pages[0], FOOTER, "footer")
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    else:
        for page in pages[1:]:
            try:
                if _normalise_nav(_block(page, NAV, "navigation")) != expected_nav:
                    errors.append(
                        f"{page}: shared navigation differs beyond aria-current=page"
                    )
                if _block(page, FOOTER, "footer") != expected_footer:
                    errors.append(f"{page}: shared footer differs")
            except (OSError, ValueError) as exc:
                errors.append(str(exc))

    for public_path, canonical_path in ASSET_COPIES.items():
        public = repo_root / public_path
        canonical = repo_root / canonical_path
        try:
            if public.read_bytes() != canonical.read_bytes():
                errors.append(f"{public_path}: differs from {canonical_path}")
        except OSError as exc:
            errors.append(str(exc))

    image_dir = repo_root / "site/public/img"
    for image in sorted(path for path in image_dir.iterdir() if path.is_file()):
        if VERSIONED_IMAGE.fullmatch(image.name) is None:
            errors.append(f"site/public/img/{image.name}: filename is not versioned")

    public_dir = repo_root / "site/public"
    for source in sorted(public_dir.rglob("*")):
        if source.suffix not in {".css", ".html", ".txt"}:
            continue
        for match in IMAGE_REFERENCE.finditer(source.read_text(encoding="utf-8")):
            name = match.group("name")
            if VERSIONED_IMAGE.fullmatch(name) is None:
                errors.append(
                    f"{source.relative_to(repo_root)}: unversioned /img/{name}"
                )
            if not (image_dir / name).is_file():
                errors.append(f"{source.relative_to(repo_root)}: missing /img/{name}")

    errors.extend(_social_card_errors(repo_root, pages[0]))

    headers_path = repo_root / "site/public/_headers"
    try:
        headers = headers_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(str(exc))
    else:
        errors.extend(_headers_errors(headers))

    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    args = parser.parse_args()
    errors = collect_errors(args.repo_root.resolve())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("site contracts: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
