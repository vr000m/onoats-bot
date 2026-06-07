"""Filesystem-only converter: ``pending/*.jsonl`` -> rendered markdown.

The converter is the standalone-onoats post-record step. For each session file
in ``sessions/pending/`` it:

  1. reads the type-discriminated JSONL (``jsonl.read_session_file``);
  2. applies ``Dictionary`` substitutions to each utterance's text;
  3. applies the (default-empty) ``pipeline`` steps;
  4. renders ONE chronological markdown transcript using the configured
     speaker display labels (``OnoatsConfig.speaker_labels()``);
  5. atomically writes it to ``transcripts/{category}/{date}/{session_id}.md``;
  6. atomically moves the source file ``pending/ -> done/``.

No DB, no network, no SQLite — purely files on disk. A per-session error routes
that file to ``failed/`` and the batch continues.

Idempotency: only ``pending/`` files are processed; a session already in
``done/`` is never touched. Running ``convert_once`` twice is a clean no-op —
the second run leaves ``done/`` byte-and-mtime-identical and ``pending/`` empty.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from loguru import logger

from onoats._vendor.dictionary import Dictionary
from onoats._vendor.session_queue import ensure_queue_dirs, queue_dir
from onoats._vendor.store import onoats_data_dir
from onoats.config import load_config
from onoats.jsonl import Session, read_session_file
from onoats.pipeline import Step, apply_steps, default_steps
from onoats.render import render_session, session_date


def _apply_dictionary(session: Session, dictionary: Dictionary) -> None:
    """Apply substitution pairs to each utterance's text in place."""
    for utt in session.utterances:
        utt.text = dictionary.apply(utt.text)


def _write_markdown_atomic(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically (temp file + os.replace)."""
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


def _convert_one(
    pending_path: Path,
    *,
    data_dir: Path,
    dictionary: Dictionary,
    speaker_labels: dict[str, str],
    steps: list[Step],
) -> Path:
    """Convert one pending file, returning the written markdown path.

    Raises on any error so the caller can route the source to ``failed/``.
    """
    session = read_session_file(pending_path)
    _apply_dictionary(session, dictionary)
    session = apply_steps(session, steps)

    date = session_date(session)
    markdown = render_session(session, speaker_labels=speaker_labels)

    out_path = (
        data_dir / "transcripts" / session.category / date / f"{session.session_id}.md"
    )
    _write_markdown_atomic(out_path, markdown)
    return out_path


def convert_once(data_dir: Path | str | None = None) -> dict[str, int]:
    """Convert every ``pending/*.jsonl`` once; return a small result summary.

    ``data_dir`` defaults to :func:`onoats_data_dir`. Returns
    ``{"converted": N, "failed": M}``. Idempotent: only ``pending/`` files are
    processed, so a second call with an already-drained queue is a clean no-op.
    """
    base = Path(data_dir) if data_dir is not None else onoats_data_dir()
    ensure_queue_dirs(base)

    pending = queue_dir("pending", base)
    done = queue_dir("done", base)
    failed = queue_dir("failed", base)

    config = load_config()
    speaker_labels = config.speaker_labels()
    dictionary = Dictionary()
    steps = default_steps()

    converted = 0
    failed_count = 0

    for pending_path in sorted(pending.glob("*.jsonl")):
        session_id = pending_path.stem
        try:
            out_path = _convert_one(
                pending_path,
                data_dir=base,
                dictionary=dictionary,
                speaker_labels=speaker_labels,
                steps=steps,
            )
            os.rename(pending_path, done / pending_path.name)
            converted += 1
            logger.info(f"convert: {session_id} -> {out_path} (pending -> done)")
        except Exception as exc:
            logger.error(f"convert: {session_id} failed: {exc}")
            try:
                os.rename(pending_path, failed / pending_path.name)
            except OSError as move_exc:
                logger.error(
                    f"convert: could not move {session_id} to failed/: {move_exc}"
                )
            failed_count += 1

    return {"converted": converted, "failed": failed_count}


def main(argv: list[str] | None = None) -> int:
    """Minimal P4-independent CLI: ``python -m onoats.convert --once``."""
    parser = argparse.ArgumentParser(
        prog="onoats.convert",
        description="Render onoats pending/*.jsonl session files to markdown.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Convert every pending session once, then exit.",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help="Data dir override (else $ONOATS_DATA_DIR / XDG default).",
    )
    args = parser.parse_args(argv)

    if not args.once:
        parser.error("nothing to do: pass --once")

    result = convert_once(args.data_dir)
    logger.info(
        f"convert --once: converted={result['converted']} failed={result['failed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
