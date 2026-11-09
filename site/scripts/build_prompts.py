#!/usr/bin/env python3
"""Inline each prompts/*.txt into site/public/prompts/index.html.

The .txt files are the source of truth. Each prompt's <li> carries a pair of
markers; this script rewrites everything between them:

    <!-- prompt:segment.txt -->...<!-- /prompt:segment.txt -->

Run `python3 site/scripts/build_prompts.py` after editing a prompt, or with
`--check` to fail (exit 1) when the page is out of date. Stdlib only.
"""

from __future__ import annotations

import html
import re
import sys
from pathlib import Path

PUBLIC = Path(__file__).resolve().parent.parent / "public"
PAGE = PUBLIC / "prompts" / "index.html"
MARKER = re.compile(
    r"(<!-- prompt:(?P<name>[\w.]+) -->)(?P<body>.*?)(<!-- /prompt:(?P=name) -->)",
    re.S,
)


def block(name: str) -> str:
    text = (PUBLIC / "prompts" / name).read_text(encoding="utf-8")
    return (
        "\n          <details>\n"
        f'            <summary>Read the prompt <span class="vh">{html.escape(name)}</span></summary>\n'
        f'            <pre tabindex="0">{html.escape(text.rstrip(chr(10)), quote=False)}</pre>\n'
        "          </details>\n          "
    )


def render(page: str) -> str:
    def sub(m: re.Match[str]) -> str:
        return f"{m.group(1)}{block(m.group('name'))}{m.group(4)}"

    return MARKER.sub(sub, page)


def main() -> int:
    page = PAGE.read_text(encoding="utf-8")
    names = {m.group("name") for m in MARKER.finditer(page)}
    on_disk = {p.name for p in (PUBLIC / "prompts").glob("*.txt")}
    if names != on_disk:
        print(f"markers {sorted(names)} != files {sorted(on_disk)}", file=sys.stderr)
        return 1
    out = render(page)
    if "--check" in sys.argv:
        if out != page:
            print("prompts/index.html is stale; run build_prompts.py", file=sys.stderr)
            return 1
        return 0
    if out != page:
        PAGE.write_text(out, encoding="utf-8")
        print("updated", PAGE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
