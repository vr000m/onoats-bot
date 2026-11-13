#!/usr/bin/env python3
"""Inline each prompts/*.txt into site/public/prompts/index.html.

The .txt files are the source of truth. Each prompt's <li> carries exactly one
meta paragraph, ``<p class="meta"><a href="/prompts/NAME">``, followed in the
same <li> by exactly one pair of markers; this script rewrites everything
between them:

    <!-- prompt:segment.txt -->...<!-- /prompt:segment.txt -->

Prompt file names use only letters, digits, ``_``, ``.`` and ``-``.

Run `python3 site/scripts/build_prompts.py` after editing a prompt, or with
`--check` to fail (exit 1) when the page is out of date. Both modes also fail
when a prompt breaks the untrusted-data rules in `prompt_errors` or stops
backing something the page says about it (`FACTS`). Stdlib only.
"""

from __future__ import annotations

import argparse
import html
import re
import string
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = Path("site/public/prompts")
PAGE = PROMPT_DIR / "index.html"
NAME = r"[\w.-]+"  # one grammar for file names and all three marker patterns
PROMPT_NAME = re.compile(NAME)
MARKER = re.compile(
    rf"(<!-- prompt:(?P<name>{NAME}) -->)(?P<body>.*?)(<!-- /prompt:(?P=name) -->)",
    re.DOTALL,
)
OPEN_MARKER = re.compile(rf"<!-- prompt:(?P<name>{NAME}) -->")
CLOSE_MARKER = re.compile(rf"<!-- /prompt:(?P<name>{NAME}) -->")
ITEM_OPEN = re.compile(r"<li\b")
ITEM_CLOSE = "</li>"

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


def meta_paragraphs(page: str, name: str) -> list[re.Match[str]]:
    """Every ``<p class="meta">`` paragraph that introduces prompt *name*."""

    pattern = rf'<p class="meta"><a href="/prompts/{re.escape(name)}">.*?</p>'
    return list(re.finditer(pattern, page, re.DOTALL))


def _item_end(page: str, meta: re.Match[str]) -> int | None:
    """Where the <li> holding *meta* closes, or None if *meta* is in no <li>."""

    opens = [m.start() for m in ITEM_OPEN.finditer(page, 0, meta.start())]
    if not opens or page.find(ITEM_CLOSE, opens[-1], meta.start()) != -1:
        return None
    end = page.find(ITEM_CLOSE, meta.end())
    return None if end == -1 else end


def marker_errors(page: str, on_disk: set[str]) -> list[str]:
    """Bind each prompt file to exactly one marker pair in its own <li>.

    Every file name fits ``NAME``; every marker names a prompt file; each file
    has exactly one ordered open/close pair and exactly one meta paragraph, and
    the pair follows that paragraph inside the same <li>, so swapping two
    items' markers cannot embed the wrong prompt under a description.
    """

    opens = Counter(m["name"] for m in OPEN_MARKER.finditer(page))
    closes = Counter(m["name"] for m in CLOSE_MARKER.finditer(page))
    pairs = {m["name"]: m for m in MARKER.finditer(page)}
    paired = Counter(m["name"] for m in MARKER.finditer(page))
    errors = []
    for name in sorted(set(opens) | set(closes) | on_disk):
        if name not in on_disk:
            errors.append(f"{PAGE}: marker names {name}, which is not a prompt file")
            continue
        if PROMPT_NAME.fullmatch(name) is None:
            errors.append(
                f"{PROMPT_DIR / name}: a prompt file name may use only letters,"
                " digits, '_', '.' and '-'"
            )
            continue
        if (opens[name], closes[name], paired[name]) != (1, 1, 1):
            errors.append(
                f"{PAGE}: {name} needs exactly one <!-- prompt:{name} --> ..."
                f" <!-- /prompt:{name} --> pair (found {opens[name]} open,"
                f" {closes[name]} close, {paired[name]} paired)"
            )
            continue
        metas = meta_paragraphs(page, name)
        if len(metas) != 1:
            errors.append(
                f'{PAGE}: {name} needs exactly one <p class="meta"><a'
                f' href="/prompts/{name}"> paragraph (found {len(metas)})'
            )
            continue
        end, pair = _item_end(page, metas[0]), pairs[name]
        if end is None or not metas[0].end() <= pair.start() < pair.end() <= end:
            errors.append(
                f"{PAGE}: the {name} markers must follow its meta paragraph"
                " inside the same <li>"
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
    metas = meta_paragraphs(page, name)  # marker_errors reports a count other than 1
    documented = (
        set(re.findall(r"<code>\{(\w+)\}</code>", metas[0].group(0)))
        if len(metas) == 1
        else None
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


def _between(text: str, start: str, end: str) -> str:
    head, _, rest = text.partition(start)
    return rest.partition(end)[0] if rest else ""


def _metadata_keys(text: str) -> set[str]:
    block = _between(text, "```metadata\n", "```")
    return set(re.findall(r"^([a-z_]+):", block, re.MULTILINE))


# What `/prompts/` says about each prompt's output, checked against the prompt
# itself. The page's "Returns" lines and prose restate these, and nothing else
# compares them, so a prompt edit would leave the page wrong without failing CI.
# When one of these fails, change the page text and this table together.
FACTS: dict[str, tuple[tuple[str, Callable[[str], bool]], ...]] = {
    "segment.txt": (
        (
            'returns {"segments": [{start_time, end_time, reason}]}',
            lambda t: all(
                f'"{k}"' in t for k in ("segments", "start_time", "end_time", "reason")
            ),
        ),
    ),
    "classify_topics.txt": (
        (
            "returns category, confidence, summary and tags",
            lambda t: all(
                f'"{k}":' in t for k in ("category", "confidence", "summary", "tags")
            ),
        ),
    ),
    "classify_ap.txt": (
        (
            'returns {"action_items": [{text, attribution}]}',
            lambda t: all(
                k in t for k in ('"action_items"', '"text"', '"attribution"')
            ),
        ),
    ),
    "consolidate_action_items.txt": (
        (
            'returns {"consolidated_items": [{text, attribution}]}',
            lambda t: all(
                k in t for k in ('"consolidated_items"', '"text"', '"attribution"')
            ),
        ),
        ("capped at 15 items", lambda t: "length ≤ 15" in t),
    ),
    "digest_daily.txt": (
        (
            "ends in a metadata block with categories_active, transcript_ids,"
            " duration_by_category, topic_density and top_topics",
            lambda t: (
                _metadata_keys(t)
                == {
                    "categories_active",
                    "transcript_ids",
                    "duration_by_category",
                    "topic_density",
                    "top_topics",
                }
            ),
        ),
        ("under 800 words", lambda t: "under 800 words" in t),
    ),
    "digest_weekly.txt": (
        (
            "eight numbered sections, week overview first, persistent blockers last",
            lambda t: (
                len(
                    re.findall(
                        r"^\d+\. \*\*",
                        _between(t, "### Structure", "### Constraints"),
                        re.MULTILINE,
                    )
                )
                == 8
                and "**Week overview**" in t
                and "**Persistent Blockers**" in t
            ),
        ),
    ),
    "digest_monthly.txt": (
        (
            "seven ## headings, Month overview first, Outlook last",
            lambda t: (
                len(re.findall(r"^- `## ", t, re.MULTILINE)) == 7
                and "`## Month overview`" in t
                and "`## Outlook`" in t
            ),
        ),
    ),
}


def fact_errors(name: str, text: str) -> list[str]:
    """Statements the page makes about *name* that the prompt no longer backs."""

    return [
        f"{PROMPT_DIR / name}: the page says it {claim}; update {PAGE} and FACTS"
        for claim, holds in FACTS.get(name, ())
        if not holds(text)
    ]


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
        errors.extend(fact_errors(name, text))
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
