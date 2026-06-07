# vendored from koda shared/dictionary.py (seeds stripped; no shared.llm_client)
"""Custom dictionary service for onoats transcript quality.

Supports two entry types in a plain-text file:
  - vocabulary terms, one per line (optionally with context: ``Cekura -- voice AI company``)
  - substitution pairs in the form ``wrong: correct``

Vocabulary terms feed the STT backend as a recognition bias (Deepgram
keywords / Whisper ``initial_prompt``); substitution pairs are applied at
render time by the converter (Phase 3).

The dictionary ships EMPTY — the upstream seed vocabulary/substitutions were
project-specific and were stripped during extraction. The file is hot-reloaded
by mtime and updated atomically via temp-file + ``os.replace()``.

Differences from the upstream original (vendored from koda):
  - the seed lists (``_SEED_VOCABULARY`` / ``_SEED_SUBSTITUTIONS``) are
    removed — ``ensure_exists`` writes an empty template;
  - the ``shared.llm_client.dictionary_hash`` import (used only for
    prompt-cache keying) is dropped in favour of a local sha256;
  - the dictionary lives at ``$XDG_CONFIG_HOME/onoats/dictionary.txt``.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path

from loguru import logger

DEFAULT_FILENAME = "dictionary.txt"


def _local_dictionary_hash(parts: list[str]) -> str:
    """Stable content hash of dictionary parts (replaces shared.llm_client)."""
    joined = "\n".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _xdg_config_home() -> Path:
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".config"
    return base / "onoats"


def resolve_dictionary_path(*, config_dir: Path | None = None) -> Path:
    """Return the dictionary path under ``$XDG_CONFIG_HOME/onoats`` (or override)."""
    base_dir = Path(config_dir) if config_dir is not None else _xdg_config_home()
    return base_dir / DEFAULT_FILENAME


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path_str = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.stem}_tmp_",
        suffix=path.suffix,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path_str, path)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


def _render_file(
    vocabulary: list[tuple[str, str]], substitutions: list[tuple[str, str]]
) -> str:
    lines = [
        "# Known vocabulary — correct spellings for domain terms",
        "# Lines without ':' are vocabulary-only (fed to STT as recognition bias)",
        "# Optional context: Term -- description (e.g. Cekura -- voice AI company)",
    ]
    for term, context in vocabulary:
        lines.append(f"{term} -- {context}" if context else term)
    lines.extend(
        [
            "",
            "# Substitution pairs for display-time correction (applied by the converter)",
            "# Format: wrong: correct",
        ]
    )
    lines.extend(f"{wrong}: {correct}" for wrong, correct in substitutions)
    return "\n".join(lines).rstrip() + "\n"


class Dictionary:
    """Hot-reloaded vocabulary + substitution dictionary."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        config_dir: Path | None = None,
        auto_create: bool = False,
    ) -> None:
        self.path = (
            Path(path)
            if path is not None
            else resolve_dictionary_path(config_dir=config_dir)
        )
        self._lock = threading.RLock()
        self._last_mtime_ns: int | None = None
        self._vocabulary: list[tuple[str, str]] = []  # (term, context)
        self._substitutions: list[tuple[str, str]] = []
        self._replacement_map: dict[str, str] = {}
        self._pattern: re.Pattern[str] | None = None
        if auto_create:
            self.ensure_exists()

    def ensure_exists(self) -> None:
        with self._lock:
            if self.path.exists():
                return
            logger.info(f"Dictionary: creating empty file at {self.path}")
            # Ships empty — no project-specific seeds.
            _write_text_atomic(self.path, _render_file([], []))
            self._last_mtime_ns = None

    def _parse(
        self, content: str
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        vocabulary: list[tuple[str, str]] = []  # (term, context)
        substitutions: list[tuple[str, str]] = []
        seen_vocab: set[str] = set()
        seen_wrong: set[str] = set()

        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                wrong, correct = (part.strip() for part in line.split(":", 1))
                if not wrong or not correct:
                    continue
                wrong_key = wrong.casefold()
                if wrong_key in seen_wrong:
                    substitutions = [
                        (old_wrong, old_correct)
                        for old_wrong, old_correct in substitutions
                        if old_wrong.casefold() != wrong_key
                    ]
                seen_wrong.add(wrong_key)
                substitutions.append((wrong, correct))
                continue
            # Vocabulary term, optionally with context: "Term -- description"
            if " -- " in line:
                term, context = (part.strip() for part in line.split(" -- ", 1))
            else:
                term, context = line, ""
            vocab_key = term.casefold()
            if vocab_key in seen_vocab:
                continue
            seen_vocab.add(vocab_key)
            vocabulary.append((term, context))

        return vocabulary, substitutions

    def _compile_substitutions(self) -> None:
        if not self._substitutions:
            self._replacement_map = {}
            self._pattern = None
            return

        escaped = sorted(
            (re.escape(wrong) for wrong, _ in self._substitutions),
            key=len,
            reverse=True,
        )
        alternation = "|".join(escaped)
        self._pattern = re.compile(
            rf"(?<!\w)(?:{alternation})(?=\W|$)",
            re.IGNORECASE,
        )
        self._replacement_map = {
            wrong.casefold(): correct for wrong, correct in self._substitutions
        }

    def _reload_if_needed(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._vocabulary = []
                self._substitutions = []
                self._replacement_map = {}
                self._pattern = None
                self._last_mtime_ns = None
                return

            try:
                stat = self.path.stat()
            except OSError as exc:
                logger.warning(f"Dictionary: could not stat {self.path}: {exc}")
                return

            mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
            if self._last_mtime_ns == mtime_ns:
                return

            try:
                content = self.path.read_text(encoding="utf-8")
                vocabulary, substitutions = self._parse(content)
            except Exception as exc:
                logger.warning(
                    f"Dictionary: reload failed, keeping last good state: {exc}"
                )
                return

            self._vocabulary = vocabulary
            self._substitutions = substitutions
            self._compile_substitutions()
            self._last_mtime_ns = mtime_ns
            logger.debug(
                f"Dictionary: loaded {len(self._vocabulary)} vocabulary term(s), "
                f"{len(self._substitutions)} substitution(s)"
            )

    def apply(self, text: str) -> str:
        """Apply substitution pairs to text."""
        if not text:
            return text
        self._reload_if_needed()
        if self._pattern is None:
            return text

        def _replace(match: re.Match[str]) -> str:
            return self._replacement_map.get(match.group(0).casefold(), match.group(0))

        return self._pattern.sub(_replace, text)

    def get_vocabulary(self) -> list[str]:
        """Return vocabulary terms only (no context). Used for content_hash + STT bias."""
        self._reload_if_needed()
        return [term for term, _ in self._vocabulary]

    def get_vocabulary_with_context(self) -> list[tuple[str, str]]:
        """Return vocabulary as (term, context) pairs."""
        self._reload_if_needed()
        return list(self._vocabulary)

    def get_substitutions(self) -> list[tuple[str, str]]:
        self._reload_if_needed()
        return list(self._substitutions)

    def content_hash(self) -> str:
        """Hash of the full dictionary for staleness detection."""
        self._reload_if_needed()
        parts = [f"{term}--{ctx}" if ctx else term for term, ctx in self._vocabulary]
        parts.extend(f"{wrong}:{correct}" for wrong, correct in self._substitutions)
        return _local_dictionary_hash(parts)

    def add_vocabulary(self, term: str, context: str = "") -> None:
        cleaned = term.strip()
        ctx = context.strip()
        if not cleaned:
            raise ValueError("Vocabulary term must not be empty")
        if ":" in cleaned:
            raise ValueError(
                "Vocabulary terms must not contain ':' — use add_substitution() for pairs"
            )
        with self._lock:
            self._reload_if_needed()
            vocabulary = list(self._vocabulary)
            substitutions = list(self._substitutions)
            existing = {t.casefold() for t, _ in vocabulary}
            if cleaned.casefold() in existing:
                vocabulary = [
                    (t, ctx) if t.casefold() == cleaned.casefold() else (t, c)
                    for t, c in vocabulary
                ]
            else:
                vocabulary.append((cleaned, ctx))
            _write_text_atomic(self.path, _render_file(vocabulary, substitutions))
            self._last_mtime_ns = None
        self._reload_if_needed()

    def add_substitution(self, wrong: str, correct: str) -> None:
        wrong_clean = wrong.strip()
        correct_clean = correct.strip()
        if not wrong_clean or not correct_clean:
            raise ValueError("Both wrong and correct terms are required")
        with self._lock:
            self._reload_if_needed()
            vocabulary = list(self._vocabulary)
            substitutions = [
                (old_wrong, old_correct)
                for old_wrong, old_correct in self._substitutions
                if old_wrong.casefold() != wrong_clean.casefold()
            ]
            substitutions.append((wrong_clean, correct_clean))
            if correct_clean.casefold() not in {t.casefold() for t, _ in vocabulary}:
                vocabulary.append((correct_clean, ""))
            _write_text_atomic(self.path, _render_file(vocabulary, substitutions))
            self._last_mtime_ns = None
        self._reload_if_needed()

    def remove_entry(self, line: str) -> None:
        target = line.strip()
        if not target:
            return
        with self._lock:
            self._reload_if_needed()
            vocabulary = list(self._vocabulary)
            substitutions = list(self._substitutions)
            if ":" in target:
                wrong, correct = (part.strip() for part in target.split(":", 1))
                substitutions = [
                    (old_wrong, old_correct)
                    for old_wrong, old_correct in substitutions
                    if not (
                        old_wrong.casefold() == wrong.casefold()
                        and old_correct.casefold() == correct.casefold()
                    )
                ]
            else:
                vocabulary = [
                    (t, c) for t, c in vocabulary if t.casefold() != target.casefold()
                ]
            _write_text_atomic(self.path, _render_file(vocabulary, substitutions))
            self._last_mtime_ns = None
        self._reload_if_needed()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage onoats's dictionary file.")
    subparsers = parser.add_subparsers(dest="command")

    add_parser = subparsers.add_parser(
        "add", help="Add a vocabulary term or substitution pair."
    )
    add_parser.add_argument("entry", help="Either TERM or wrong:correct")
    add_parser.add_argument(
        "--context", "-c", default="", help="Context for vocabulary terms"
    )

    remove_parser = subparsers.add_parser(
        "remove", help="Remove a vocabulary term or substitution."
    )
    remove_parser.add_argument("entry", help="Either TERM or wrong:correct")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    dictionary = Dictionary(auto_create=True)

    if args.command == "add":
        if ":" in args.entry:
            wrong, correct = (part.strip() for part in args.entry.split(":", 1))
            dictionary.add_substitution(wrong, correct)
        else:
            dictionary.add_vocabulary(args.entry, context=args.context)
        return 0

    if args.command == "remove":
        dictionary.remove_entry(args.entry)
        return 0

    print("Vocabulary:")
    for term, context in dictionary.get_vocabulary_with_context():
        if context:
            print(f"  {term} -- {context}")
        else:
            print(f"  {term}")
    print("")
    print("Substitutions:")
    for wrong, correct in dictionary.get_substitutions():
        print(f"  {wrong}: {correct}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
