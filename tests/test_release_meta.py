"""Release metadata checks: LICENSE file and pyproject license field.

Pins the Phase-1 licensing contract from the 0.9→1.0 release plan: the LICENSE
body is the canonical BSD-2-Clause template (placeholders filled, text
otherwise verbatim) and pyproject declares the matching PEP 639 SPDX string.
"""

import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_pyproject() -> dict:
    with (REPO_ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


COPYRIGHT_LINE = "Copyright (c) 2025–2026 Varun Singh"

# Canonical BSD-2-Clause template (SPDX), placeholders filled in.
CANONICAL_BODY = """\
Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""


def _normalize(text: str) -> str:
    """Collapse runs of whitespace so wrapping differences don't matter."""
    return re.sub(r"\s+", " ", text).strip()


def test_license_file_is_canonical_bsd_2_clause():
    license_path = REPO_ROOT / "LICENSE"
    assert license_path.is_file(), "LICENSE missing at repo root"
    text = license_path.read_text(encoding="utf-8")

    lines = text.splitlines()
    assert lines[0] == "BSD 2-Clause License"
    assert COPYRIGHT_LINE in text, "copyright holder/years line missing or altered"

    assert _normalize(CANONICAL_BODY) in _normalize(text), (
        "LICENSE body diverges from the canonical BSD-2-Clause template"
    )


def test_pyproject_declares_bsd_2_clause_spdx_license():
    assert _load_pyproject()["project"]["license"] == "BSD-2-Clause"


def test_build_backend_pins_hatchling_with_pep639_support():
    requires = [Requirement(r) for r in _load_pyproject()["build-system"]["requires"]]
    hatchling = [r for r in requires if r.name == "hatchling"]
    assert hatchling, "hatchling missing from [build-system] requires"
    # PEP 639 License-Expression emission needs hatchling >= 1.27: the
    # specifier must exclude everything below that.
    assert not hatchling[0].specifier.contains("1.26.5"), (
        f"hatchling pin must exclude <1.27 (PEP 639 support), got {hatchling[0]}"
    )
