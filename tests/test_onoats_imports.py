"""Import-leak guard: onoats entrypoints must not pull koda / heavy deps.

Importing the recorder entrypoints (``onoats.runtime`` and ``onoats.__main__``)
must NOT drag ``shared.*`` (koda), ``anthropic``, ``fastapi``, ``google.genai``,
``aiosqlite``, or ``mlx_whisper`` into ``sys.modules``. These would re-couple
onoats to koda or load the SQLite / LLM / MLX stacks the recorder never uses.
"""

from __future__ import annotations

import subprocess
import sys

_FORBIDDEN = ["shared", "anthropic", "fastapi", "aiosqlite", "mlx_whisper"]


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
