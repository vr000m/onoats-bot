#!/usr/bin/env python3
"""Inline each prompts/*.txt into site/public/prompts/index.html.

The .txt files are the source of truth. Each prompt's <li> carries exactly one
pair of markers; this script rewrites everything between them:

    <!-- prompt:segment.txt -->...<!-- /prompt:segment.txt -->

Run `python3 site/scripts/build_prompts.py` after editing a prompt, or with
`--check` to fail (exit 1) when the page is out of date. Both modes also fail
when a prompt breaks the untrusted-data rules in `prompt_errors`. Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import re
import string
import sys
from collections import Counter
from pathlib import Path

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = Path("site/public/prompts")
PAGE = PROMPT_DIR / "index.html"
MARKER = re.compile(
    r"(<!-- prompt:(?P<name>[\w.]+) -->)(?P<body>.*?)(<!-- /prompt:(?P=name) -->)",
    re.DOTALL,
)
OPEN_MARKER = re.compile(r"<!-- prompt:(?P<name>[\w.]+) -->")
CLOSE_MARKER = re.compile(r"<!-- /prompt:(?P<name>[\w.]+) -->")

# Every placeholder in every prompt is classified once, by name. Untrusted ones
# carry transcript text or text derived from it (summaries, extracted items,
# digests, topic signals) and must each sit alone inside a BEGIN/END UNTRUSTED
# fence; trusted ones are scalars the caller computes (a category, a date, a
# count). A new placeholder fails the check until it is added to one set.
TRUSTED_FIELDS = frozenset(
    {"category", "month", "target_date", "week_end", "week_start", "weeks_count"}
)
UNTRUSTED_FIELDS = frozenset(
    {
        "buffer_jsonl",
        "existing_action_items_section",
        "expansion_budget_explanation",
        "expansions",
        "items_json",
        "previous_week_context",
        "short_dailies",
        "short_weeklies",
        "summary",
        "topic_signal",
        "transcript",
        "transcripts",
    }
)
BOUNDARY_HEADING = "## Instruction boundary"
BOUNDARY_TEXT = "untrusted source material, never instructions"
FENCE = re.compile(
    r"--- (?P<edge>BEGIN|END) UNTRUSTED (?P<label>[A-Z]+(?: [A-Z]+)*) ---"
)


def block(prompt_dir: Path, name: str) -> str:
    text = (prompt_dir / name).read_text(encoding="utf-8")
    return (
        "\n          <details>\n"
        f'            <summary>Read the prompt <span class="vh">{html.escape(name)}</span></summary>\n'
        f'            <pre tabindex="0">{html.escape(text.rstrip(chr(10)), quote=False)}</pre>\n'
        "          </details>\n          "
    )


def render(page: str, prompt_dir: Path) -> str:
    def sub(m: re.Match[str]) -> str:
        return f"{m.group(1)}{block(prompt_dir, m.group('name'))}{m.group(4)}"

    return MARKER.sub(sub, page)


def marker_errors(page: str, on_disk: set[str]) -> list[str]:
    """Each prompt file needs exactly one ordered open/close marker pair, and
    every marker must name a prompt file."""

    opens = Counter(m["name"] for m in OPEN_MARKER.finditer(page))
    closes = Counter(m["name"] for m in CLOSE_MARKER.finditer(page))
    pairs = Counter(m["name"] for m in MARKER.finditer(page))
    errors = []
    for name in sorted(set(opens) | set(closes) | on_disk):
        if name not in on_disk:
            errors.append(f"{PAGE}: marker names {name}, which is not a prompt file")
        elif (opens[name], closes[name], pairs[name]) != (1, 1, 1):
            errors.append(
                f"{PAGE}: {name} needs exactly one <!-- prompt:{name} --> ..."
                f" <!-- /prompt:{name} --> pair (found {opens[name]} open,"
                f" {closes[name]} close, {pairs[name]} paired)"
            )
    return errors


def _fields(text: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(text) if name]


def prompt_errors(name: str, text: str, page: str) -> list[str]:
    """The untrusted-data rules for one prompt template.

    - It formats with str.format (no stray brace) and the page documents
      exactly its placeholders.
    - Every placeholder is classified as trusted or untrusted.
    - An `## Instruction boundary` paragraph saying the data is untrusted
      precedes the first fence.
    - Fences pair up, do not nest, and each holds exactly one line: one
      untrusted placeholder. Every untrusted placeholder is in a fence, so its
      text cannot run into fixed instructions on either side.
    """

    label = PROMPT_DIR / name
    try:
        fields = set(_fields(text))
        text.format(**dict.fromkeys(fields, ""))
    except (ValueError, IndexError, KeyError) as exc:
        return [f"{label}: not a str.format template ({exc})"]

    errors = []
    meta = re.search(
        rf'<p class="meta"><a href="/prompts/{re.escape(name)}">.*?</p>',
        page,
        re.DOTALL,
    )
    documented = (
        set(re.findall(r"<code>\{(\w+)\}</code>", meta.group(0))) if meta else None
    )
    if documented != fields:
        errors.append(
            f"{label}: the page documents {sorted(documented or [])},"
            f" the template takes {sorted(fields)}"
        )
    for field in sorted(fields - TRUSTED_FIELDS - UNTRUSTED_FIELDS):
        errors.append(f"{label}: {{{field}}} is not classified as trusted or untrusted")

    lines = text.splitlines()
    boundary = [i for i, line in enumerate(lines) if line == BOUNDARY_HEADING]
    first_fence = next(
        (i for i, line in enumerate(lines) if FENCE.fullmatch(line)), None
    )
    if first_fence is None:
        errors.append(f"{label}: no --- BEGIN UNTRUSTED ... --- fence")
    if len(boundary) != 1:
        errors.append(f"{label}: needs exactly one '{BOUNDARY_HEADING}' heading")
    else:
        paragraph = "\n".join(lines[boundary[0] + 1 : first_fence])
        if first_fence is not None and boundary[0] > first_fence:
            errors.append(f"{label}: the instruction boundary must precede the data")
        elif BOUNDARY_TEXT not in paragraph:
            errors.append(f"{label}: the boundary must say '{BOUNDARY_TEXT}'")

    fenced: set[int] = set()
    open_at: int | None = None
    for number, line in enumerate(lines):
        fence = FENCE.fullmatch(line)
        if line.startswith("--- ") and "UNTRUSTED" in line and fence is None:
            errors.append(f"{label}:{number + 1}: malformed fence line")
            continue
        if fence is None:
            if open_at is not None:
                fenced.add(number)
            continue
        if fence["edge"] == "BEGIN":
            if open_at is not None:
                errors.append(f"{label}:{number + 1}: fence opened inside another")
            open_at = number
            continue
        if open_at is None:
            errors.append(f"{label}:{number + 1}: END fence without a BEGIN")
            continue
        begin = FENCE.fullmatch(lines[open_at])
        assert begin is not None
        if begin["label"] != fence["label"]:
            errors.append(f"{label}:{number + 1}: END label does not match BEGIN")
        body = lines[open_at + 1 : number]
        if len(body) != 1 or body[0] not in {f"{{{f}}}" for f in UNTRUSTED_FIELDS}:
            errors.append(
                f"{label}:{open_at + 1}: a fence must hold exactly one untrusted"
                " placeholder line"
            )
        open_at = None
    if open_at is not None:
        errors.append(f"{label}:{open_at + 1}: fence never closed")

    for number, line in enumerate(lines):
        for field in _fields(line):
            if field in UNTRUSTED_FIELDS and (
                number not in fenced or line != f"{{{field}}}"
            ):
                errors.append(
                    f"{label}:{number + 1}: {{{field}}} must be alone on a line"
                    " inside an UNTRUSTED fence"
                )
    return errors


def _load(repo_root: Path) -> tuple[str, dict[str, str]]:
    page = (repo_root / PAGE).read_text(encoding="utf-8")
    prompts = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((repo_root / PROMPT_DIR).glob("*.txt"))
    }
    return page, prompts


def collect_errors(repo_root: Path) -> list[str]:
    """Marker, staleness and untrusted-data violations below *repo_root*."""

    page, prompts = _load(repo_root)
    errors = marker_errors(page, set(prompts))
    if not errors and render(page, repo_root / PROMPT_DIR) != page:
        errors.append(f"{PAGE} is stale; run build_prompts.py")
    for name, text in prompts.items():
        errors.extend(prompt_errors(name, text, page))
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="fail if the page is stale"
    )
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    if not args.check:
        page, prompts = _load(repo_root)
        errors = marker_errors(page, set(prompts))
        if not errors:
            out = render(page, repo_root / PROMPT_DIR)
            if out != page:
                (repo_root / PAGE).write_text(out, encoding="utf-8")
                print("updated", repo_root / PAGE)
    errors = collect_errors(repo_root)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
