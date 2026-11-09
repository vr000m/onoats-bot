#!/usr/bin/env python3
"""Validate onoats.dev source and generated-file invariants."""

from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
PAGE_PATHS = (
    Path("site/public/index.html"),
    Path("site/public/prompts/index.html"),
    Path("site/public/local-models/index.html"),
    Path("site/public/404.html"),
)
ASSET_COPIES = {
    Path("site/public/favicon.svg"): Path("assets/brand/onoats-mark.svg"),
    Path("site/public/img/onoats-mark-1.6.0.svg"): Path("assets/brand/onoats-mark.svg"),
    Path("site/public/img/onoats-1.6.0-1-ready.png"): Path(
        "assets/screenshots/onoats-1.6.0-1-ready.png"
    ),
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
SOCIAL_SOURCE = Path("site/scripts/render_social_card.swift")
META_IMAGE = re.compile(
    r'<meta (?:property|name)="(?P<key>og:image|twitter:image)" content="(?P<url>[^"]*)"'
)
META_SIZE = re.compile(
    r'<meta property="og:image:(?P<axis>width|height)" content="(?P<pixels>\d+)"'
)
NAV = re.compile(r'<nav aria-label="Primary">.*?</nav>', re.S)
FOOTER = re.compile(r"<footer>.*?</footer>", re.S)


def _block(page: Path, pattern: re.Pattern[str], label: str) -> str:
    match = pattern.search(page.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"{page}: missing shared {label}")
    return match.group(0)


def _normalise_nav(block: str) -> str:
    return block.replace(' aria-current="page"', "")


def _png_size(path: Path) -> tuple[int, int] | None:
    data = path.read_bytes()
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", data[16:24])


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
    reference = IMAGE_REFERENCE.search(urls["og:image"])
    if reference is None:
        return [f"{label}: the social card must live under /img/"]

    name = reference["name"]
    errors: list[str] = []
    card = repo_root / "site/public/img" / name
    if card.is_file():  # a missing card is already reported by the reference scan
        declared = {m["axis"]: int(m["pixels"]) for m in META_SIZE.finditer(text)}
        if _png_size(card) != (1200, 630):
            errors.append(f"site/public/img/{name}: must be a 1200x630 PNG")
        if declared != {"width": 1200, "height": 630}:
            errors.append(f"{label}: og:image:width/height must be 1200 and 630")
    source = repo_root / SOCIAL_SOURCE
    if not source.is_file():
        errors.append(f"{SOCIAL_SOURCE}: missing social-card source")
    elif name not in source.read_text(encoding="utf-8"):
        errors.append(f"{SOCIAL_SOURCE}: does not write {name}")
    return errors


def collect_errors(repo_root: Path) -> list[str]:
    """Return every site-contract violation below *repo_root*."""

    errors: list[str] = []
    pages = [repo_root / path for path in PAGE_PATHS]

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
        if "  Strict-Transport-Security: max-age=31536000\n" not in headers:
            errors.append("site/public/_headers: missing one-year HSTS")
        image_rule = re.search(r"(?ms)^/img/\*\n(?P<body>(?:  .+\n?)+)", headers)
        if image_rule is None or "immutable" not in image_rule.group("body"):
            errors.append("site/public/_headers: /img/* must remain immutable")

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
