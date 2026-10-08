"""Import-leak guard: onoats entrypoints must not pull koda / heavy deps.

Importing the recorder entrypoints (``onoats.runtime`` and ``onoats.__main__``)
must NOT drag ``shared.*`` (koda), ``anthropic``, ``fastapi``, ``google.genai``,
``aiosqlite``, or ``mlx_whisper`` into ``sys.modules``. These would re-couple
onoats to koda or load the SQLite / LLM / MLX stacks the recorder never uses.
"""

from __future__ import annotations

import subprocess
import sys

_FORBIDDEN = ["shared", "anthropic", "fastapi", "aiosqlite", "mlx_whisper", "mlx_audio"]


def _import_in_subprocess(module: str, forbidden: list[str]) -> None:
    """Import ``module`` in a clean interpreter and assert no forbidden module loaded."""
    checks = "; ".join(
        f"assert not any(m=={f!r} or m.startswith({f + '.'!r}) for m in sys.modules), "
        f"{f!r} + ' leaked into sys.modules'"
        for f in forbidden
    )
    # google.genai is the LLM client; google.* (protobuf etc.) is pulled by
    # pipecat transitively, so only forbid the genai subpackage specifically.
    code = (
        f"import {module}, sys; {checks}; "
        "assert 'google.genai' not in sys.modules, 'google.genai leaked'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"importing {module} leaked a forbidden module:\n{result.stderr}"
    )


def test_runtime_import_is_clean():
    _import_in_subprocess("onoats.runtime", _FORBIDDEN)


def test_main_import_is_clean():
    _import_in_subprocess("onoats.__main__", _FORBIDDEN)


def test_config_and_categories_import_is_clean():
    _import_in_subprocess("onoats.config", _FORBIDDEN)
    _import_in_subprocess("onoats.categories", _FORBIDDEN)


def test_dual_and_diarization_modules_import_clean_and_build_flag_off():
    """Importing onoats.dual + the diarization processors, then building the dual
    pipeline with the flag OFF, must never load ``mlx_audio`` (production factory
    imports it lazily on the worker thread, and only with the flag on)."""
    code = """
import sys
import onoats.dual
import onoats.processors.diarization_feeder
import onoats.processors.diarization_labeler
import onoats.stt.diarization_timeline
from pipecat.processors.frame_processor import FrameProcessor
from onoats.dual import _build_dual_pipeline

class P(FrameProcessor):
    pass

class T:
    def input(self):
        return P()

_build_dual_pipeline(T(), T(), P(), P(), P(), P(), P(), P(), diarization=False)
bad = [m for m in sys.modules if m == 'mlx_audio' or m.startswith('mlx_audio.')]
assert not bad, f'mlx_audio leaked: {bad}'
"""
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
