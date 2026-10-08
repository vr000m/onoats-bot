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
    # specifier must admit 1.27 and exclude everything below it (the
    # high patch sentinel catches a lower bound inside the 1.26 series,
    # e.g. >=1.26.6, which a single low sample like 1.26.5 would miss).
    spec = hatchling[0].specifier
    assert spec.contains("1.27.0"), f"hatchling pin excludes 1.27 itself: {spec}"
    assert not spec.contains("1.26.999"), (
        f"hatchling pin must exclude <1.27 (PEP 639 support), got {spec}"
    )


def test_changelog_has_keep_a_changelog_structure():
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert text.startswith("# Changelog")
    assert "keepachangelog.com" in text
    assert "BSD-2-Clause" in text, "license-coverage note missing from header"
    headings = re.findall(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}$", text, re.M)
    assert headings, "no '## [X.Y.Z] - YYYY-MM-DD' version headings"
    # Newest-first ordering, as Keep a Changelog prescribes.
    parsed = [tuple(int(p) for p in v.split(".")) for v in headings]
    assert parsed == sorted(parsed, reverse=True), (
        f"versions not newest-first: {headings}"
    )


def test_changelog_has_entry_for_current_version():
    version = _load_pyproject()["project"]["version"]
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", text, re.M
    ), f"CHANGELOG has no entry for pyproject version {version}"


def test_version_surfaces_agree():
    """pyproject, uv.lock (editable onoats entry), and the menu-bar
    Info.plist must all carry the same version — release_check.sh enforces
    this at tag time; this test catches drift between releases."""
    version = _load_pyproject()["project"]["version"]

    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    m = re.search(r'^name = "onoats"\nversion = "([^"]+)"$', lock, re.M)
    assert m, "onoats package entry missing from uv.lock"
    assert m.group(1) == version, f"uv.lock has {m.group(1)}, pyproject has {version}"

    plist = (REPO_ROOT / "native/onoats-menubar/Info.plist").read_text(encoding="utf-8")
    m = re.search(
        r"<key>CFBundleShortVersionString</key>\s*<string>([^<]+)</string>", plist
    )
    assert m, "CFBundleShortVersionString missing from Info.plist"
    assert m.group(1) == version, (
        f"Info.plist has {m.group(1)}, pyproject has {version}"
    )


# --- diarization contract wiring (Phase 8) ---------------------------------

CONTRACT_TEST = "tests/test_diarization_contract.py"
CONTRACT_ENV = "ONOATS_REQUIRE_DIARIZATION_CONTRACT"


def test_release_check_runs_diarization_contract_hard_fail():
    script = (REPO_ROOT / "scripts/release_check.sh").read_text(encoding="utf-8")
    assert CONTRACT_TEST in script
    assert CONTRACT_ENV in script


def _run_contract_with_mlx_blocked(tmp_path: Path, env_value: str | None):
    import os
    import subprocess
    import sys

    (tmp_path / "sitecustomize.py").write_text(
        "import sys\nsys.modules['mlx_audio'] = None\n", encoding="utf-8"
    )
    env = {k: v for k, v in os.environ.items() if k not in (CONTRACT_ENV, "PYTHONPATH")}
    env["PYTHONPATH"] = str(tmp_path)
    if env_value is not None:
        env[CONTRACT_ENV] = env_value
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            CONTRACT_TEST,
            "-p",
            "no:cacheprovider",
            "-rs",
            "-q",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_contract_hard_fails_when_required_and_mlx_missing(tmp_path):
    proc = _run_contract_with_mlx_blocked(tmp_path, "1")
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out
    assert "failed" in out or "error" in out, out


def test_contract_skips_when_not_required_and_mlx_missing(tmp_path):
    proc = _run_contract_with_mlx_blocked(tmp_path, None)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "skipped" in out, out
    assert "failed" not in out, out


# --- release_check.sh check 5 runs against the working tree ----------------
# Checks 1-4 read <commit-ish> via `git show`; check 5 imports the installed
# package, so it must refuse to vouch for <commit-ish> unless the working tree
# IS a clean checkout of it. Driven end-to-end in a throwaway repo with a fake
# `uv` on PATH that records whether the contract run was attempted.

RELEASE_VERSION = "9.8.7"


def _git(repo: Path, *args: str) -> str:
    import os
    import subprocess

    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _release_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "native/onoats-menubar").mkdir(parents=True)
    script = (REPO_ROOT / "scripts/release_check.sh").read_text(encoding="utf-8")
    (repo / "scripts/release_check.sh").write_text(script, encoding="utf-8")
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "onoats"\nversion = "{RELEASE_VERSION}"\n', encoding="utf-8"
    )
    (repo / "uv.lock").write_text(
        f'[[package]]\nname = "onoats"\nversion = "{RELEASE_VERSION}"\n',
        encoding="utf-8",
    )
    (repo / "native/onoats-menubar/Info.plist").write_text(
        f"<key>CFBundleShortVersionString</key>\n<string>{RELEASE_VERSION}</string>\n",
        encoding="utf-8",
    )
    (repo / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{RELEASE_VERSION}] - 2026-10-08\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "release")
    return repo


def _run_release_check(tmp_path: Path, repo: Path, *commit: str):
    import os
    import subprocess

    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    uv_log = tmp_path / "uv.log"
    fake_uv = bindir / "uv"
    fake_uv.write_text('#!/bin/sh\necho "$@" >> "$UV_LOG"\nexit 0\n', encoding="utf-8")
    fake_uv.chmod(0o755)
    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}",
        UV_LOG=str(uv_log),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    proc = subprocess.run(
        ["bash", "scripts/release_check.sh", f"v{RELEASE_VERSION}", *commit],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    ran_contract = uv_log.exists() and "test_diarization_contract" in uv_log.read_text()
    return proc, ran_contract


def test_release_check_runs_contract_from_clean_checkout_of_commit(tmp_path):
    repo = _release_repo(tmp_path)
    proc, ran_contract = _run_release_check(tmp_path, repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ran_contract
    assert "ok:   mlx_audio diarization contract" in proc.stdout


def test_release_check_contract_fails_when_head_is_not_commit(tmp_path):
    repo = _release_repo(tmp_path)
    target = _git(repo, "rev-parse", "HEAD")
    (repo / "later.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-q", "-m", "later")
    proc, ran_contract = _run_release_check(tmp_path, repo, target)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert not ran_contract
    # Checks 1-4 still read <commit-ish> and pass; only check 5 refuses.
    assert proc.stdout.count("ok:   ") == 4, proc.stdout
    assert "FAIL: mlx_audio diarization contract" in proc.stderr
    assert "clean checkout" in proc.stderr


def test_release_check_contract_fails_on_dirty_tree(tmp_path):
    repo = _release_repo(tmp_path)
    (repo / "CHANGELOG.md").write_text("edited\n", encoding="utf-8")
    proc, ran_contract = _run_release_check(tmp_path, repo)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert not ran_contract
    assert proc.stdout.count("ok:   ") == 4, proc.stdout
    assert "FAIL: mlx_audio diarization contract" in proc.stderr
    assert "clean checkout" in proc.stderr


def test_release_check_header_documents_contract_weights_waiver():
    script = (REPO_ROOT / "scripts/release_check.sh").read_text(encoding="utf-8")
    header = script.split("set -euo pipefail", 1)[0]
    assert "HF cache" in header
    assert "hf download mlx-community/Nemotron-3-Diarization" in header
