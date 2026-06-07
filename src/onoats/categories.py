"""User category set + ``--category`` validation + session_meta encoding.

Replaces a hardcoded ``VALID_CATEGORIES`` import with a config-driven set. The
default set is ``{"uncategorized"}``; users extend it via
``config.toml [categories] set`` (or ``ONOATS_CATEGORIES`` env /
``onoats init --categories``).

The validated ``--category`` is NOT written into the filename — the
``{session_id}`` stem stays load-bearing for a consumer's back-fill keying.
Instead it rides in the queue contract as a typed ``session_meta`` FIRST line::

    {"type": "session_meta", "category": "<cat>"}

so a downstream consumer (a queue worker, or the converter) can honor it.
"""

from __future__ import annotations

import json

from onoats.config import OnoatsConfig, load_config

DEFAULT_CATEGORY = "uncategorized"


def category_set(config: OnoatsConfig | None = None) -> set[str]:
    """Return the configured category set (always includes ``uncategorized``)."""
    cfg = config if config is not None else load_config()
    return cfg.category_set


class InvalidCategoryError(ValueError):
    """Raised when a ``--category`` value is not in the configured set."""


def validate_category(
    category: str | None,
    *,
    config: OnoatsConfig | None = None,
) -> str | None:
    """Validate ``--category`` against the configured set.

    Returns the normalised category (lower-stripped) when valid, ``None`` when
    ``category`` is ``None``/blank. Rejecting ``uncategorized`` is deliberate:
    locking to the default sentinel is a no-op, so it is treated as an explicit
    error to surface a likely mistake.

    Raises :class:`InvalidCategoryError` when the value is not in the set.
    """
    if category is None:
        return None
    cat = category.lower().strip()
    if not cat:
        return None
    valid = category_set(config)
    if cat == DEFAULT_CATEGORY or cat not in valid:
        choices = ", ".join(sorted(valid - {DEFAULT_CATEGORY})) or "(none configured)"
        raise InvalidCategoryError(
            f"--category must be one of: {choices} (got {category!r})"
        )
    return cat


def session_meta_line(category: str | None) -> str:
    """Return the JSONL ``session_meta`` first line for a session.

    ``category`` defaults to ``uncategorized`` when ``None`` so the consumer
    always sees an explicit value. The returned string has NO trailing newline
    — the writer appends it.
    """
    meta = {"type": "session_meta", "category": category or DEFAULT_CATEGORY}
    return json.dumps(meta, ensure_ascii=False)
