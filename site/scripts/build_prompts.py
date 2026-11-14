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
when:

- a prompt breaks the untrusted-data rules in `prompt_errors`;
- a `FACTS` entry fails on either side: the prompt's item on the page no
  longer contains its text, or the prompt no longer backs it (when the item's
  shape is broken, `marker_errors` reports it and the page side is skipped);
- a `CAPTION_FACTS` entry fails on either side for the one diagram
  `<figcaption>`; the caption numbers prompts by their place in the one
  `<ol class="steps">`, so each step-numbered claim has its own entry, read in
  that order, and a short or broken list fails every entry;
- an item uses its `UNBACKED_WORDS` word, or the caption a `CAPTION_UNBACKED`
  word, in any letter case;
- a key of a `PROMPT_TABLES` table (the only tables keyed by prompt file name)
  names no prompt file;
- the coding-agent prompt (`AGENT_PROMPT`, a task for an agent, not a
  `str.format` template, so the template rules above skip it) lists the
  prompts out of the page's step order, with other placeholders than the
  prompts take, or with a claim an `AGENT_FACTS` entry finds the prompts no
  longer back.

These tables are the only place the page's claims about the prompts are
checked; tests drive them but add no claim of their own. Stdlib only.
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


def _structure(text: str) -> str:
    """The prompt's ``### Structure`` block, up to ``### Constraints``."""

    return _between(text, "### Structure", "### Constraints")


def _weekly_sections(text: str) -> list[str]:
    """The weekly Structure list's section names, in order."""

    return re.findall(r"^\d+\. \*\*(.+?)\*\*", _structure(text), re.MULTILINE)


def _weekly_fixed_headings(text: str) -> list[str]:
    """The ``##`` headings the weekly prompt asks for verbatim, in order."""

    line = next(
        (ln for ln in text.splitlines() if "level-2 headings verbatim" in ln), ""
    )
    return re.findall(r"`## ([^`]+)`", line)


def _monthly_headings(text: str) -> list[str]:
    """The monthly Structure block's ``##`` headings, in order."""

    return re.findall(r"^- `## ([^`]+)`", _structure(text), re.MULTILINE)


def _runs(names: list[str], count: int, first: str, last: str) -> bool:
    return len(names) == count and names[0] == first and names[-1] == last


# What `/prompts/` says about each prompt, checked on both sides: the first
# element is text the prompt's item must contain (between its <li> and the
# embedded prompt), the second checks that the prompt still backs it. Without
# the page side, editing the page would leave this table checking a claim the
# page no longer makes. When one fails, change the page and this table together.
FACTS: dict[str, tuple[tuple[str, Callable[[str], bool]], ...]] = {
    "segment.txt": (
        (
            '<code>{"segments": [{"start_time", "end_time", "reason"}]}</code>',
            lambda t: all(
                f'"{k}"' in t for k in ("segments", "start_time", "end_time", "reason")
            ),
        ),
    ),
    "classify_topics.txt": (
        (
            "<code>category</code>, <code>confidence</code>, <code>summary</code>"
            " and <code>tags</code>",
            lambda t: all(
                f'"{k}":' in t for k in ("category", "confidence", "summary", "tags")
            ),
        ),
    ),
    "classify_ap.txt": (
        (
            '<code>{"action_items": [{"text", "attribution"}]}</code>',
            lambda t: all(
                k in t for k in ('"action_items"', '"text"', '"attribution"')
            ),
        ),
    ),
    "consolidate_action_items.txt": (
        (
            '<code>{"consolidated_items": [{"text", "attribution"}]}</code>',
            lambda t: all(
                k in t for k in ('"consolidated_items"', '"text"', '"attribution"')
            ),
        ),
        ("at most 15.", lambda t: re.search(r"length ≤ 15\b", t) is not None),
    ),
    "digest_daily.txt": (
        (
            "<code>categories_active</code>, <code>transcript_ids</code>,"
            " <code>duration_by_category</code>, <code>topic_density</code> and"
            " <code>top_topics</code>",
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
            "eight sections, from the week overview to persistent blockers",
            lambda t: _runs(
                _weekly_sections(t), 8, "Week overview", "Persistent Blockers"
            ),
        ),
        (
            "the last three under fixed <code>##</code> headings",
            lambda t: (
                len(_weekly_fixed_headings(t)) == 3
                and _weekly_fixed_headings(t) == _weekly_sections(t)[-3:]
            ),
        ),
    ),
    "digest_monthly.txt": (
        (
            "seven fixed <code>##</code> headings, from Month overview to Outlook",
            lambda t: _runs(_monthly_headings(t), 7, "Month overview", "Outlook"),
        ),
    ),
}


STEPS = re.compile(r'<ol class="steps">(.*?)</ol>', re.DOTALL)


def step_names(page: str) -> list[str | None] | None:
    """The prompt each ``<li>`` of the one steps list embeds, in page order.

    Prompt N on the page is item N of ``<ol class="steps">``; an item with no
    marker is None. None when the page has no single steps list.
    """

    lists = STEPS.findall(page)
    if len(lists) != 1:
        return None
    items = ITEM_OPEN.split(lists[0])[1:]
    return [m["name"] if (m := OPEN_MARKER.search(item)) else None for item in items]


Steps = list[str | None]


def _step(steps: Steps, prompts: dict[str, str], number: int) -> str | None:
    """The text of prompt *number* (1-based) on the page; None when the list is
    too short, the item has no marker or the marker names no prompt file."""

    name = steps[number - 1] if 0 < number <= len(steps) else None
    return prompts.get(name) if name is not None else None


def _takes(steps: Steps, prompts: dict[str, str], number: int, *fields: str) -> bool:
    text = _step(steps, prompts, number)
    return text is not None and all(f"{{{field}}}" in text for field in fields)


def _sole_producer(
    steps: Steps, prompts: dict[str, str], number: int, key: str
) -> bool:
    """Prompt *number* is the one prompt whose text contains *key*."""

    producers = [name for name, text in prompts.items() if key in text]
    return _step(steps, prompts, number) is not None and producers == [
        steps[number - 1]
    ]


# Each check below backs one step-numbered claim of the caption. Data flow is
# checkable from the prompts (who returns a key, who takes a placeholder);
# what the caller passes between steps, and when it calls them, is not.


def _session_chain(steps: Steps, prompts: dict[str, str]) -> bool:
    """Step 1 takes the session buffer; steps 2 and 3 take a transcript.

    That step 1's segments become the {transcript} of steps 2 and 3 is caller
    wiring; the order 2 -> 3 -> 4 is backed by the category and summary/items
    checks. This also backs the segment item's "through the next step".
    """

    return _takes(steps, prompts, 1, "buffer_jsonl") and all(
        _takes(steps, prompts, n, "transcript") for n in (2, 3)
    )


def _category_flow(steps: Steps, prompts: dict[str, str]) -> bool:
    """Step 2 is the one prompt returning a category; steps 3 and 4 take it.

    This also backs the classify item's "feeds the next two prompts".
    """

    return _sole_producer(steps, prompts, 2, '"category":') and all(
        _takes(steps, prompts, n, "category") for n in (3, 4)
    )


def _summary_and_items_flow(steps: Steps, prompts: dict[str, str]) -> bool:
    """Step 4 takes the summary only step 2 returns and the items only step 3
    returns."""

    return (
        _sole_producer(steps, prompts, 2, '"summary":')
        and _sole_producer(steps, prompts, 3, '"action_items"')
        and _takes(steps, prompts, 4, "summary", "items_json")
    )


def _daily_input(steps: Steps, prompts: dict[str, str]) -> bool:
    """Step 5 reads one day's transcripts. That they are the rendered Markdown
    files is the caller's choice; no prompt names a file format for them."""

    text = _step(steps, prompts, 5)
    return _takes(steps, prompts, 5, "transcripts") and "a day's worth" in (text or "")


def _digest_inputs(steps: Steps, prompts: dict[str, str]) -> bool:
    """Step 6 takes the daily digests and step 7 the weekly ones."""

    return _takes(steps, prompts, 6, "short_dailies") and _takes(
        steps, prompts, 7, "short_weeklies"
    )


def _digest_cadence(steps: Steps, prompts: dict[str, str]) -> bool:
    """Steps 5, 6 and 7 each take one day, week and month. Which boxes are
    dashed is the hand-drawn SVG, and "not once per call" is when the caller
    runs them; neither is checked."""

    return (
        _takes(steps, prompts, 5, "target_date")
        and _takes(steps, prompts, 6, "week_start", "week_end")
        and _takes(steps, prompts, 7, "month")
    )


# What the pipeline diagram's <figcaption> says about the prompts as a set, in
# the same (page text, check) shape as FACTS: one entry per step-numbered claim,
# each reading the prompts in step order (from step_names) and every prompt's
# text. A short or broken steps list fails every check.
CAPTION_FACTS: tuple[tuple[str, Callable[[Steps, dict[str, str]], bool]], ...] = (
    ("Prompts 1 to 4 run in order on each session file", _session_chain),
    ("Prompts 3 and 4 take the category from 2", _category_flow),
    ("4 also takes the summary from 2 and the items from 3", _summary_and_items_flow),
    ("Prompt 5 reads a day's Markdown files", _daily_input),
    ("6 reads the daily digests and 7 the weekly ones", _digest_inputs),
    ("Dashed boxes run once per day, week or month", _digest_cadence),
)
CAPTION = "the diagram caption"
FIGCAPTION = re.compile(r"<figcaption>(.*?)</figcaption>", re.DOTALL)

# Words a prompt's item must not use, in any letter case, because no prompt
# backs the claim: the weekly Structure list numbers instructions, not output
# sections. CAPTION_UNBACKED holds the caption's: no prompt runs on a schedule,
# the caller decides when.
UNBACKED_WORDS: dict[str, tuple[str, ...]] = {"digest_weekly.txt": ("numbered",)}
CAPTION_UNBACKED: tuple[str, ...] = ("schedule",)

# The coding-agent prompt is not a template: it is a task for an agent, which
# fetches the seven prompts by URL. It lists them as "N. URL takes {fields}. It
# returns ...", numbered by the page's steps list, and its claims about how they
# connect are bound to the same checks as the diagram caption's.
AGENT_PROMPT = "build_workflow.txt"
PROMPT_URL = "https://onoats.dev/prompts/"
AGENT_LINE = re.compile(
    rf"^(\d+)\. {re.escape(PROMPT_URL)}({NAME}) takes (.*?)\. It returns",
    re.MULTILINE,
)
AGENT_URL = re.compile(rf"{re.escape(PROMPT_URL)}({NAME})")
AGENT = "the coding-agent prompt"


def _all_fenced(steps: Steps, prompts: dict[str, str]) -> bool:
    return bool(prompts) and all(
        any(FENCE.fullmatch(line) for line in text.splitlines())
        for text in prompts.values()
    )


AGENT_FACTS: tuple[tuple[str, Callable[[Steps, dict[str, str]], bool]], ...] = (
    ("Prompts 1 to 4 run in order on each session file", _session_chain),
    ("Prompts 3 and 4 take the category from 2", _category_flow),
    (
        "Prompt 4 also takes the summary from 2 and the items from 3",
        _summary_and_items_flow,
    ),
    ("Prompt 5 reads one day's Markdown files", _daily_input),
    ("Prompt 6 reads the daily digests and prompt 7 the weekly ones", _digest_inputs),
    ("Prompts 5 to 7 run once per day, week or month", _digest_cadence),
    ("wrap each such placeholder in a fixed `--- BEGIN UNTRUSTED", _all_fenced),
)

# What the coding-agent prompt says each prompt returns, keyed by prompt file:
# each text must sit in that prompt's line and be the excerpt of a FACTS entry
# for the same prompt (without its <code> tags), so FACTS backs it.
AGENT_RETURNS: dict[str, tuple[str, ...]] = {
    "segment.txt": ('{"segments": [{"start_time", "end_time", "reason"}]}',),
    "classify_topics.txt": ("category, confidence, summary and tags",),
    "classify_ap.txt": ('{"action_items": [{"text", "attribution"}]}',),
    "consolidate_action_items.txt": (
        '{"consolidated_items": [{"text", "attribution"}]}',
        "at most 15.",
    ),
    "digest_daily.txt": (
        "categories_active, transcript_ids, duration_by_category,"
        " topic_density and top_topics",
    ),
}


# Every table keyed by prompt file name; orphan_errors checks each key.
PROMPT_TABLES: dict[str, dict[str, tuple[object, ...]]] = {
    "FACTS": FACTS,
    "UNBACKED_WORDS": UNBACKED_WORDS,
    "AGENT_RETURNS": AGENT_RETURNS,
}


def _returns_errors(label: Path, text: str, prompts: dict[str, str]) -> list[str]:
    lines = {
        m["name"]: m.group(0)
        for m in re.finditer(
            rf"^\d+\. {re.escape(PROMPT_URL)}(?P<name>{NAME}) takes .*$",
            text,
            re.MULTILINE,
        )
    }
    errors = []
    for name, plains in AGENT_RETURNS.items():
        for plain in plains:
            cells = [
                holds
                for excerpt, holds in FACTS.get(name, ())
                if re.sub(r"</?code>", "", excerpt) == plain
            ]
            if plain not in lines.get(name, ""):
                errors.append(
                    f"{label}: the {name} line no longer says {plain!r};"
                    " update the prompt and AGENT_RETURNS together"
                )
            if len(cells) != 1:
                errors.append(
                    f"site/scripts/build_prompts.py: AGENT_RETURNS says {plain!r}"
                    f" for {name}, which no one FACTS entry backs"
                )
            elif name in prompts and not cells[0](prompts[name]):
                errors.append(
                    f"{PROMPT_DIR}: {AGENT} says {plain!r} for {name}, which the"
                    " prompt no longer backs; update the prompt and AGENT_RETURNS"
                    " together"
                )
    return errors


def agent_errors(page: str, prompts: dict[str, str], text: str) -> list[str]:
    """The coding-agent prompt against the page's steps and the prompts."""

    label = PROMPT_DIR / AGENT_PROMPT
    steps = step_names(page)
    if steps is None:  # caption_errors reports the missing or repeated list
        return []
    errors = []
    listed = AGENT_LINE.findall(text)
    if [(int(n), name) for n, name, _ in listed] != list(enumerate(steps, 1)):
        errors.append(
            f"{label}: lists its prompts as {[(int(n), name) for n, name, _ in listed]},"
            f" but the page's steps embed {list(enumerate(steps, 1))}"
        )
    for _, name, taken in listed:
        said = set(re.findall(r"\{(\w+)\}", taken))
        try:  # a prompt that is not a template is prompt_errors' to report
            takes = set(_fields(prompts[name])) if name in prompts else said
        except ValueError:
            continue
        if said != takes:
            errors.append(f"{label}: {name} takes {sorted(takes)}, not {sorted(said)}")
    urls = set(AGENT_URL.findall(text))
    if urls != set(prompts):
        errors.append(
            f"{label}: links {sorted(urls)}, but the prompts are {sorted(prompts)}"
        )
    errors.extend(_returns_errors(label, text, prompts))
    for excerpt, holds in AGENT_FACTS:
        if excerpt not in text:
            errors.append(
                f"{label}: no longer says {excerpt!r};"
                " update the prompt and AGENT_FACTS together"
            )
        if not holds(steps, prompts):
            errors.append(
                f"{PROMPT_DIR}: {AGENT} says {excerpt!r}, which the prompts no"
                " longer back; update the prompt and AGENT_FACTS together"
            )
    errors.extend(_unbacked_errors(AGENT, text, CAPTION_UNBACKED))
    return errors


def item_prose(page: str, name: str) -> tuple[int, int] | None:
    """The span of *name*'s item text, from its ``<li`` to the embedded prompt.

    None when the item has no single meta paragraph inside an ``<li>`` followed
    by its open marker; ``marker_errors`` reports that shape.
    """

    metas = meta_paragraphs(page, name)
    if len(metas) != 1 or _item_end(page, metas[0]) is None:
        return None
    start = [m.start() for m in ITEM_OPEN.finditer(page, 0, metas[0].start())][-1]
    marker = page.find(f"<!-- prompt:{name} -->", metas[0].end())
    return None if marker == -1 else (start, marker)


def fact_errors(name: str, text: str, page: str) -> list[str]:
    """Statements about *name* the page no longer makes or the prompt no longer backs."""

    span = item_prose(page, name)  # None: marker_errors reports the item shape
    prose = page[span[0] : span[1]] if span else None
    errors = []
    for excerpt, holds in FACTS.get(name, ()):
        if prose is not None and excerpt not in prose:
            errors.append(
                f"{PAGE}: the {name} item no longer says {excerpt!r};"
                " update the prompts page and FACTS together"
            )
        if not holds(text):
            errors.append(
                f"{PROMPT_DIR / name}: the page says {excerpt!r}, which the prompt"
                " no longer backs; update the prompts page and FACTS together"
            )
    errors.extend(_unbacked_errors(f"{name} item", prose, UNBACKED_WORDS.get(name)))
    return errors


def _unbacked_errors(
    where: str, text: str | None, words: tuple[str, ...] | None
) -> list[str]:
    folded = text.casefold() if text is not None else None
    return [
        f"{PAGE}: the {where} says {word!r}, a claim no prompt backs"
        for word in words or ()
        if folded is not None and word.casefold() in folded
    ]


def caption_errors(page: str, prompts: dict[str, str]) -> list[str]:
    """The diagram caption: exactly one, saying each backed CAPTION_FACTS text."""

    captions = FIGCAPTION.findall(page)
    if len(captions) != 1:
        return [f"{PAGE}: needs exactly one <figcaption> (found {len(captions)})"]
    steps = step_names(page)
    if steps is None:
        return [f'{PAGE}: needs exactly one <ol class="steps">']
    errors = []
    for excerpt, holds in CAPTION_FACTS:
        if excerpt not in captions[0]:
            errors.append(
                f"{PAGE}: {CAPTION} no longer says {excerpt!r};"
                " update the prompts page and CAPTION_FACTS together"
            )
        if not holds(steps, prompts):
            errors.append(
                f"{PROMPT_DIR}: {CAPTION} says {excerpt!r}, which the prompts no"
                " longer back; update the prompts page and CAPTION_FACTS together"
            )
    errors.extend(_unbacked_errors(CAPTION, captions[0], CAPTION_UNBACKED))
    return errors


def orphan_errors(prompts: dict[str, str]) -> list[str]:
    """Table keys that name no prompt file, so their claims would go unchecked."""

    return [
        f"site/scripts/build_prompts.py: {table} names {name},"
        " which is not a prompt file"
        for table, keys in PROMPT_TABLES.items()
        for name in sorted(set(keys) - set(prompts))
    ]


def _load(repo_root: Path) -> tuple[str, dict[str, str]]:
    page = (repo_root / PAGE).read_text(encoding="utf-8")
    prompts = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted((repo_root / PROMPT_DIR).glob("*.txt"))
        if path.name != AGENT_PROMPT
    }
    return page, prompts


def _load_agent(repo_root: Path) -> str | None:
    path = repo_root / PROMPT_DIR / AGENT_PROMPT
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _prompt_files(prompts: dict[str, str], agent: str | None) -> set[str]:
    return set(prompts) | ({AGENT_PROMPT} if agent is not None else set())


def collect_errors(repo_root: Path) -> list[str]:
    """Every violation below *repo_root*: markers, staleness, untrusted-data
    rules, page claims (FACTS, CAPTION_FACTS, UNBACKED_WORDS, CAPTION_UNBACKED)
    and PROMPT_TABLES keys that name no prompt file."""

    page, prompts = _load(repo_root)
    agent = _load_agent(repo_root)
    errors = marker_errors(page, _prompt_files(prompts, agent))
    if agent is None:
        errors.append(f"{PROMPT_DIR / AGENT_PROMPT} is missing")
    if not errors and render(page, repo_root / PROMPT_DIR) != page:
        errors.append(f"{PAGE} is stale; run build_prompts.py")
    for name, text in prompts.items():
        errors.extend(prompt_errors(name, text, page))
        errors.extend(fact_errors(name, text, page))
    errors.extend(caption_errors(page, prompts))
    if agent is not None:
        errors.extend(agent_errors(page, prompts, agent))
    errors.extend(orphan_errors(prompts))
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
        errors = marker_errors(page, _prompt_files(prompts, _load_agent(repo_root)))
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
