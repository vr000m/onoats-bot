#!/usr/bin/env python3
"""Validate dev-plan review markers against their above-marker content.

A reviewed dev plan carries a marker line written by ``/review-plan``::

    <!-- reviewed: YYYY-MM-DD @ <40-hex-sha1> -->

The marker is a **contract / workspace divider**: everything ABOVE it is the
reviewed contract (objective, requirements, phases), everything below is editable
workspace (Progress, Findings). The recorded sha1 is
``git hash-object`` of the plan with the marker line and everything after it
stripped — i.e. the above-marker bytes. Editing the contract section without
refreshing the marker silently invalidates the review, with nothing to catch it
(this happened on the milestone-A plan: a header edit went unrefreshed for ~5
commits, and rode alongside a flatly-false "PR #4 merged" status line).

This check recomputes that hash and fails on any mismatch. It is deliberately
narrow: it does NOT validate below-marker edits (workspace is meant to change),
and it SKIPS plans with no real marker (an unreviewed plan is a valid state) and
the template placeholder (``@ <hash>``).

Usage::

    check_review_markers.py [path ...]     # default: docs/dev_plans/*.md

Exit status is non-zero if any plan's marker is stale/invalid.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# Column-zero marker with a real date + 40-hex sha1. The template placeholder
# (`YYYY-MM-DD @ <hash>`) does not match, so an un-reviewed plan is skipped.
_MARKER_RE = re.compile(r"^<!-- reviewed: (\d{4}-\d{2}-\d{2}) @ ([0-9a-f]{40}) -->\s*$")


def _git_hash_object(data: bytes) -> str:
    """Return ``git hash-object --stdin`` for ``data`` (the blob sha1)."""
    out = subprocess.run(
        ["git", "hash-object", "--stdin"],
        input=data,
        stdout=subprocess.PIPE,
        check=True,
    )
    return out.stdout.decode().strip()


def _find_marker(text: str) -> tuple[int, str] | None:
    """Return ``(byte_offset_of_marker_line_start, recorded_sha1)`` for the LAST
    column-zero real marker, or ``None`` if the plan has no real marker.

    Marker-shaped lines inside ``` / ~~~ fenced code blocks are ignored, matching
    the skein ``conduct/marker.py`` convention (a plan documenting marker syntax
    in a fence must not be mistaken for a real review marker)."""
    found: tuple[int, str] | None = None
    offset = 0
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
        elif not in_fence:
            m = _MARKER_RE.match(line)
            if m:
                found = (offset, m.group(2))
        offset += len(line.encode("utf-8"))
    return found


def check_plan(path: Path) -> str | None:
    """Validate one plan. Return an error string on mismatch, else ``None``
    (also ``None`` when the plan has no real marker — that is a valid state)."""
    raw = path.read_bytes()
    marker = _find_marker(raw.decode("utf-8"))
    if marker is None:
        return None
    marker_offset, recorded = marker
    # Above-marker bytes == everything before the marker line begins. This is
    # byte-identical to `head -n <marker_line-1> | git hash-object --stdin`.
    computed = _git_hash_object(raw[:marker_offset])
    if computed != recorded:
        return (
            f"{path}: STALE review marker — recorded {recorded[:12]} but "
            f"above-marker content hashes to {computed[:12]}. The reviewed "
            "contract section changed without refreshing the marker. Re-run "
            "/review-plan, or recompute the hash if the edit was administrative."
        )
    return None


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv] or sorted(Path("docs/dev_plans").glob("*.md"))
    errors = [err for p in paths if p.is_file() if (err := check_plan(p))]
    if errors:
        print("Dev-plan review-marker check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
