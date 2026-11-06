"""Release metadata checks: LICENSE file and pyproject license field.

Pins the Phase-1 licensing contract from the 0.9→1.0 release plan: the LICENSE
body is the canonical BSD-2-Clause template (placeholders filled, text
otherwise verbatim) and pyproject declares the matching PEP 639 SPDX string.
"""

import re
import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tomllib
import warnings
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent
NATIVE_MAKEFILE = REPO_ROOT / "native" / "Makefile"
NATIVE_INSTALL_SCRIPT = REPO_ROOT / "native" / "install_staged.py"
NATIVE_RELEASE_IDENTITY = REPO_ROOT / "native" / "release_identity.py"
MENUBAR_PLIST = REPO_ROOT / "native" / "onoats-menubar" / "Info.plist"
RECORDER_HARNESS_SCRIPT = REPO_ROOT / "native" / "test_recorder_model.sh"

_RELEASE_SPEC = importlib.util.spec_from_file_location(
    "release_identity", NATIVE_RELEASE_IDENTITY
)
assert _RELEASE_SPEC is not None and _RELEASE_SPEC.loader is not None
RELEASE_IDENTITY = importlib.util.module_from_spec(_RELEASE_SPEC)
sys.modules["release_identity"] = RELEASE_IDENTITY
_RELEASE_SPEC.loader.exec_module(RELEASE_IDENTITY)

_INSTALL_SPEC = importlib.util.spec_from_file_location(
    "onoats_native_install_staged", NATIVE_INSTALL_SCRIPT
)
assert _INSTALL_SPEC is not None and _INSTALL_SPEC.loader is not None
INSTALL_STAGED = importlib.util.module_from_spec(_INSTALL_SPEC)
_INSTALL_SPEC.loader.exec_module(INSTALL_STAGED)


def test_installed_smoke_imports_release_contract_from_neutral_module():
    source = (REPO_ROOT / "native" / "installed_recording_smoke.py").read_text(
        encoding="utf-8"
    )
    assert "from install_staged import" not in source
    assert "from release_identity import" in source


def test_release_contract_has_one_neutral_owner():
    installer = NATIVE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    neutral = NATIVE_RELEASE_IDENTITY.read_text(encoding="utf-8")
    assert "class ReleaseIdentity" not in installer
    assert "class SmokeReceipt" not in installer
    assert "def release_identity_digest" not in installer
    assert "def write_smoke_receipt" not in installer
    assert "class ReleaseIdentity" in neutral
    assert "class SmokeReceipt" in neutral
    assert "from install_staged import" not in neutral


NATIVE_RESOURCES = (
    "Onoats.icns",
    "OnoatsHeader.png",
    "OnoatsHeader@2x.png",
    "OnoatsMenuBarTemplateIdle.png",
    "OnoatsMenuBarTemplateIdle@2x.png",
    "OnoatsMenuBarTemplateRecording.png",
    "OnoatsMenuBarTemplateRecording@2x.png",
    "OnoatsMenuBarTemplateWarning.png",
    "OnoatsMenuBarTemplateWarning@2x.png",
    "OnoatsMenuBarTemplateBusy.png",
    "OnoatsMenuBarTemplateBusy@2x.png",
    "OnoatsMenuBarTemplateFailed.png",
    "OnoatsMenuBarTemplateFailed@2x.png",
)


def _make_target(text: str, target: str) -> tuple[str, str]:
    """Return one literal Make target's prerequisites and recipe/body."""
    match = re.search(
        rf"(?m)^{re.escape(target)}\s*:(?P<prereqs>[^\n]*)\n"
        rf"(?P<body>(?:(?:\t|#|\s*$).*\n)*)",
        text,
    )
    assert match, f"native/Makefile has no {target!r} target"
    return match.group("prereqs").strip(), match.group("body")


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


def test_experiments_are_excluded_from_the_distributions():
    """Scratch experiments stay in the repo but never ship (sdist) or install (wheel)."""
    build = _load_pyproject()["tool"]["hatch"]["build"]["targets"]
    assert "/experiments" in build["sdist"]["exclude"]
    # The wheel is an exact allowlist of the application and its neutral
    # dependency-free contract package, so nothing else can ride along.
    assert build["wheel"]["packages"] == ["src/onoats", "src/onoats_contracts"]


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


# --- native bundle resources and staged installation (menu-bar Phase 5) ---


def test_native_resource_source_set_is_exact():
    resource_dir = REPO_ROOT / "native" / "onoats-menubar" / "Resources"
    actual = tuple(
        sorted(path.name for path in resource_dir.iterdir() if path.is_file())
    )
    assert actual == tuple(sorted(NATIVE_RESOURCES))


def test_native_app_recreates_bundle_and_copies_each_resource_explicitly():
    makefile = NATIVE_MAKEFILE.read_text(encoding="utf-8")
    _, app = _make_target(makefile, "app")

    remove = app.find("rm -rf $(APP)")
    create = app.find("mkdir -p $(APP)/Contents/MacOS")
    assert -1 not in (remove, create) and remove < create, (
        "app assembly must remove the entire staging bundle before recreating it"
    )
    assert "Contents/Resources" in app
    for name in NATIVE_RESOURCES:
        assert re.search(rf"(?m)^\tcp\s+[^\n]*\b{re.escape(name)}\b[^\n]*$", app), (
            f"app target must copy {name} explicitly"
        )
    assert not re.search(r"(?m)^\tcp\s+[^\n]*Resources/[^\s]*[*?]", app), (
        "resource assembly must not use a wildcard copy"
    )


def test_native_build_and_model_harness_include_phase6_panel_sources():
    """The app build picks up both panel destinations, while the model harness
    remains an isolated model test and compiles every model dependency."""
    makefile = NATIVE_MAKEFILE.read_text(encoding="utf-8")
    assert (
        "MENUBAR_SOURCES := $(wildcard $(MENUBAR_SRC_DIR)/Sources/*.swift)" in makefile
    )
    for name in ("RecordingPanelView.swift", "SettingsPanelView.swift"):
        assert (REPO_ROOT / "native/onoats-menubar/Sources" / name).is_file(), name

    harness = RECORDER_HARNESS_SCRIPT.read_text(encoding="utf-8")
    for name in (
        "AudioDevices.swift",
        "ConfigStore.swift",
        "LoginItemManager.swift",
        "RecorderModel.swift",
        "UiTelemetry.swift",
    ):
        assert name in harness
    assert "CFFIXED_USER_HOME=" in harness and "HOME=" in harness


def _run_resource_verifier(app: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["ONOATS_VERIFY_RESOURCES_APP"] = str(app)
    return subprocess.run(
        ["make", "-s", "verify-resources"],
        cwd=REPO_ROOT / "native",
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _codesign_output(app: Path, *flags: str) -> str:
    result = subprocess.run(
        ["codesign", *flags, str(app)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout + result.stderr


def _designated_requirement(app: Path) -> tuple[str, str]:
    output = _codesign_output(app, "-dr", "-")
    requirements = [
        re.sub(r"^(?:# )?designated => ", "", line)
        for line in output.splitlines()
        if re.match(r"^(?:# )?designated => ", line)
    ]
    assert len(requirements) == 1, output
    return requirements[0], output


def _cdhash(app: Path) -> str:
    output = _codesign_output(app, "-dvvv")
    matches = re.findall(r"(?mi)^cdhash=(\S+)$", output)
    assert len(matches) == 1, output
    return matches[0]


def _make_adhoc_app(path: Path) -> None:
    contents = path / "Contents"
    executable = contents / "MacOS" / "Onoats"
    resources = contents / "Resources"
    executable.parent.mkdir(parents=True)
    resources.mkdir()
    shutil.copyfile("/usr/bin/true", executable)
    os.chmod(executable, 0o755)
    with (contents / "Info.plist").open("wb") as fh:
        plistlib.dump(
            {
                "CFBundleExecutable": "Onoats",
                "CFBundleIdentifier": "net.varunsingh.onoats",
                "CFBundleName": "Onoats",
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1",
            },
            fh,
        )
    for name in NATIVE_RESOURCES:
        (resources / name).write_bytes(f"test resource: {name}\n".encode())
    result = subprocess.run(
        ["codesign", "--force", "--deep", "--sign", "-", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _copy_file_without_flags(source: str, destination: str) -> str:
    shutil.copyfile(source, destination)
    os.chmod(destination, stat.S_IMODE(os.stat(source).st_mode))
    return destination


def _app_tree_hash(app: Path) -> str:
    return INSTALL_STAGED.app_identity(app)


@pytest.mark.parametrize(
    "mutation",
    ["file-bytes", "file-mode", "directory-mode", "entry-type", "symlink-target"],
)
def test_app_identity_covers_complete_bundle_state_space(tmp_path: Path, mutation: str):
    baseline = tmp_path / "baseline.app"
    (baseline / "Contents/MacOS").mkdir(parents=True)
    executable = baseline / "Contents/MacOS/Onoats"
    executable.write_bytes(b"signed executable")
    executable.chmod(0o755)
    (baseline / "target-a").write_text("a", encoding="utf-8")
    (baseline / "target-b").write_text("b", encoding="utf-8")
    (baseline / "link").symlink_to("target-a")
    candidate = tmp_path / "candidate.app"
    shutil.copytree(baseline, candidate, symlinks=True)
    assert _app_tree_hash(candidate) == _app_tree_hash(baseline)

    if mutation == "file-bytes":
        (candidate / "Contents/MacOS/Onoats").write_bytes(b"mutated")
    elif mutation == "file-mode":
        (candidate / "Contents/MacOS/Onoats").chmod(0o644)
    elif mutation == "directory-mode":
        (candidate / "Contents").chmod(0o700)
    elif mutation == "entry-type":
        path = candidate / "target-a"
        path.unlink()
        path.mkdir()
    else:
        link = candidate / "link"
        link.unlink()
        link.symlink_to("target-b")

    assert _app_tree_hash(candidate) != _app_tree_hash(baseline)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("post_verify_succeeds", [False, True])
@pytest.mark.parametrize("commit_succeeds", [False, True])
def test_verified_replacement_is_failure_atomic_over_full_state_space(
    tmp_path: Path,
    monkeypatch,
    existing: bool,
    post_verify_succeeds: bool,
    commit_succeeds: bool,
):
    installed = tmp_path / "Onoats.app"
    candidate = tmp_path / "candidate.app"
    candidate.mkdir()
    (candidate / "identity").write_text("new", encoding="utf-8")
    if existing:
        installed.mkdir()
        (installed / "identity").write_text("old", encoding="utf-8")

    def exchange(left: Path, right: Path) -> None:
        temporary = tmp_path / "exchange"
        left.rename(temporary)
        right.rename(left)
        temporary.rename(right)

    monkeypatch.setattr(INSTALL_STAGED, "_exchange", exchange)

    def verify(path: Path) -> None:
        assert (path / "identity").read_text(encoding="utf-8") == "new"
        if not post_verify_succeeds:
            raise INSTALL_STAGED.InstallError("injected post-publish failure")

    def commit() -> None:
        if not commit_succeeds:
            raise INSTALL_STAGED.InstallError("injected record commit failure")

    if post_verify_succeeds and commit_succeeds:
        INSTALL_STAGED._replace_verified(candidate, installed, verify, commit)
        assert (installed / "identity").read_text(encoding="utf-8") == "new"
        assert not candidate.exists()
    else:
        with pytest.raises(INSTALL_STAGED.InstallError):
            INSTALL_STAGED._replace_verified(candidate, installed, verify, commit)
        if existing:
            assert (installed / "identity").read_text(encoding="utf-8") == "old"
        else:
            assert not installed.exists()
        assert (candidate / "identity").read_text(encoding="utf-8") == "new"


def test_post_commit_old_tree_cleanup_failure_is_warning_only(tmp_path, monkeypatch):
    installed = tmp_path / "Onoats.app"
    candidate = tmp_path / "exchange-candidate"
    for path, value in ((installed, "old"), (candidate, "new")):
        path.mkdir()
        (path / "identity").write_text(value, encoding="utf-8")

    def exchange(left: Path, right: Path) -> None:
        temporary = tmp_path / "exchange"
        left.rename(temporary)
        right.rename(left)
        temporary.rename(right)

    monkeypatch.setattr(INSTALL_STAGED, "_exchange", exchange)
    monkeypatch.setattr(
        INSTALL_STAGED,
        "_remove_path",
        lambda _path: (_ for _ in ()).throw(OSError("injected cleanup failure")),
    )
    INSTALL_STAGED._replace_verified(
        candidate,
        installed,
        lambda path: (path / "identity").read_text(encoding="utf-8") == "new",
        lambda: None,
    )
    assert (installed / "identity").read_text(encoding="utf-8") == "new"
    assert (candidate / "identity").read_text(encoding="utf-8") == "old"


def test_exchange_candidate_is_not_a_launchservices_app_bundle(tmp_path, monkeypatch):
    source = tmp_path / "Onoats.app"
    source.mkdir()
    install_dir = tmp_path / "installed"
    install_dir.mkdir()

    def fake_run(*argv: str, cwd=None):
        shutil.copytree(Path(argv[-2]), Path(argv[-1]))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(INSTALL_STAGED, "_run", fake_run)
    parent, candidate = INSTALL_STAGED._copy_snapshot(source, install_dir, source.name)
    try:
        assert candidate.suffix != ".app"
        assert candidate.is_dir()
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def test_launchservices_uniqueness_rejects_any_second_registered_bundle(
    tmp_path, monkeypatch
):
    installed = tmp_path / "Onoats.app"
    duplicate = tmp_path / "another-volume" / "leftover.app"
    monkeypatch.setattr(
        INSTALL_STAGED,
        "_launchservices_paths",
        lambda _bundle_id: {installed.resolve(), duplicate.resolve()},
    )
    with pytest.raises(INSTALL_STAGED.InstallError, match="duplicate LaunchServices"):
        INSTALL_STAGED._verify_single_registration(installed, "net.varunsingh.onoats")


@pytest.mark.parametrize(
    ("initial", "should_fail"),
    [
        ("none", False),
        ("staged", False),
        ("installed-and-staged", False),
        ("foreign-and-staged", True),
    ],
)
def test_installer_clears_only_verified_staged_registration_then_fails_closed(
    tmp_path, monkeypatch, initial, should_fail
):
    staged = (tmp_path / "smoke output" / "Onoats.app").resolve()
    installed = (tmp_path / "Applications" / "Onoats.app").resolve()
    foreign = (tmp_path / "Elsewhere" / "Onoats.app").resolve()
    registrations = {
        "none": set(),
        "staged": {staged},
        "installed-and-staged": {installed, staged},
        "foreign-and-staged": {foreign, staged},
    }[initial]
    verified: list[Path] = []
    unregistered: list[Path] = []

    monkeypatch.setattr(
        INSTALL_STAGED, "_launchservices_paths", lambda _bundle_id: registrations
    )
    monkeypatch.setattr(
        INSTALL_STAGED,
        "_verify_app",
        lambda app, **_kwargs: verified.append(app) or ("1" * 64, "2" * 64, "3" * 40),
    )

    def fake_run(*argv: str, cwd=None):
        assert argv[:2] == (str(INSTALL_STAGED.LSREGISTER), "-u")
        target = Path(argv[2]).resolve()
        registrations.remove(target)
        unregistered.append(target)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(INSTALL_STAGED, "_run", fake_run)
    INSTALL_STAGED._clear_verified_staged_registration(
        staged,
        installed,
        "net.varunsingh.onoats",
        "1" * 64,
        tmp_path,
    )
    if should_fail:
        with pytest.raises(
            INSTALL_STAGED.InstallError, match="duplicate LaunchServices"
        ):
            INSTALL_STAGED._verify_single_registration(
                installed, "net.varunsingh.onoats"
            )
    else:
        INSTALL_STAGED._verify_single_registration(installed, "net.varunsingh.onoats")
    assert unregistered == ([staged] if "staged" in initial else [])
    assert verified == ([staged] if "staged" in initial else [])
    assert foreign in registrations if should_fail else foreign not in registrations


def test_post_publication_launchservices_failure_restores_prior_app_before_commit(
    tmp_path, monkeypatch
):
    installed = tmp_path / "Onoats.app"
    candidate = tmp_path / "candidate"
    for path, identity in ((installed, "old"), (candidate, "new")):
        path.mkdir()
        (path / "identity").write_text(identity, encoding="utf-8")

    def exchange(left: Path, right: Path) -> None:
        temporary = tmp_path / "exchange"
        left.rename(temporary)
        right.rename(left)
        temporary.rename(right)

    committed = False

    def reject_duplicate_registration() -> None:
        nonlocal committed
        assert (installed / "identity").read_text(encoding="utf-8") == "new"
        raise INSTALL_STAGED.InstallError(
            "duplicate LaunchServices bundle registration: /Elsewhere/Onoats.app"
        )

    def commit() -> None:
        nonlocal committed
        reject_duplicate_registration()
        committed = True

    monkeypatch.setattr(INSTALL_STAGED, "_exchange", exchange)
    with pytest.raises(INSTALL_STAGED.InstallError, match="LaunchServices"):
        INSTALL_STAGED._replace_verified(
            candidate,
            installed,
            lambda path: (path / "identity").read_text(encoding="utf-8") == "new",
            commit,
        )

    assert committed is False
    assert (installed / "identity").read_text(encoding="utf-8") == "old"
    assert (candidate / "identity").read_text(encoding="utf-8") == "new"


def test_install_keeps_final_launchservices_uniqueness_as_fatal_commit_gate(
    tmp_path, monkeypatch
):
    staged = tmp_path / "staged" / "Onoats.app"
    staged.mkdir(parents=True)
    install_dir = tmp_path / "installed"
    installed = install_dir / "Onoats.app"
    installed.mkdir(parents=True)
    (installed / "identity").write_text("old", encoding="utf-8")
    parent = install_dir / ".onoats-install-test"
    candidate = parent / "candidate"
    candidate.mkdir(parents=True)
    (candidate / "identity").write_text("new", encoding="utf-8")
    expected_identity = "1" * 64
    checks: list[str] = []

    def verify_registration(path: Path, _bundle_id: str) -> None:
        checks.append((path / "identity").read_text(encoding="utf-8"))
        if checks == ["old", "new"]:
            raise INSTALL_STAGED.InstallError(
                "duplicate LaunchServices bundle registration: /Elsewhere/Onoats.app"
            )

    def exchange(left: Path, right: Path) -> None:
        temporary = tmp_path / "exchange"
        left.rename(temporary)
        right.rename(left)
        temporary.rename(right)

    monkeypatch.setattr(
        INSTALL_STAGED, "_verify_single_registration", verify_registration
    )
    monkeypatch.setattr(
        INSTALL_STAGED, "_copy_snapshot", lambda *_args: (parent, candidate)
    )
    monkeypatch.setattr(
        INSTALL_STAGED,
        "_verify_app",
        lambda *_args, **_kwargs: (expected_identity, "2" * 64, "3" * 40),
    )
    monkeypatch.setattr(INSTALL_STAGED, "_verify_resources", lambda *_args: None)
    monkeypatch.setattr(INSTALL_STAGED, "_exchange", exchange)

    args = argparse.Namespace(
        app=str(staged),
        install_dir=str(install_dir),
        backup_dir=str(tmp_path / "backups"),
        record=str(tmp_path / "backups/approved-rollback.txt"),
        expected_identity=expected_identity,
        approved=0,
        baseline_dr_hash="",
        baseline_cdhash="",
        bundle_id="net.varunsingh.onoats",
        makefile_dir=str(tmp_path),
    )
    with pytest.raises(INSTALL_STAGED.InstallError, match="LaunchServices"):
        INSTALL_STAGED.install(args)

    assert checks == ["old", "new"]
    assert (installed / "identity").read_text(encoding="utf-8") == "old"
    assert not parent.exists()


@pytest.mark.parametrize("after_commit", [False, True])
def test_temporary_cleanup_warns_only_after_install_commit(
    tmp_path, monkeypatch, after_commit
):
    staged = tmp_path / "staged" / "Onoats.app"
    staged.mkdir(parents=True)
    install_dir = tmp_path / "installed"
    install_dir.mkdir()
    parent = install_dir / ".onoats-install-test"
    candidate = parent / "candidate"
    candidate.mkdir(parents=True)
    backup_dir = tmp_path / "backups"
    record = backup_dir / "approved-rollback.txt"
    expected_identity = "1" * 64

    monkeypatch.setattr(INSTALL_STAGED, "_verify_single_registration", lambda *_: None)
    monkeypatch.setattr(
        INSTALL_STAGED, "_copy_snapshot", lambda *_args: (parent, candidate)
    )
    monkeypatch.setattr(
        INSTALL_STAGED,
        "_verify_app",
        lambda *_args, **_kwargs: (expected_identity, "2" * 64, "3" * 40),
    )
    monkeypatch.setattr(INSTALL_STAGED, "_verify_resources", lambda *_args: None)

    def replace(_candidate, _installed, _verify, commit):
        if not after_commit:
            raise INSTALL_STAGED.InstallError("injected pre-commit failure")
        commit()

    monkeypatch.setattr(INSTALL_STAGED, "_replace_verified", replace)
    monkeypatch.setattr(
        INSTALL_STAGED.shutil,
        "rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("injected temp cleanup failure")),
    )
    args = argparse.Namespace(
        app=str(staged),
        install_dir=str(install_dir),
        backup_dir=str(backup_dir),
        record=str(record),
        expected_identity=expected_identity,
        approved=0,
        baseline_dr_hash="",
        baseline_cdhash="",
        bundle_id="net.varunsingh.onoats",
        makefile_dir=str(tmp_path),
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if after_commit:
            INSTALL_STAGED.install(args)
        else:
            with pytest.raises(OSError, match="temp cleanup"):
                INSTALL_STAGED.install(args)

    if after_commit:
        assert [str(item.message) for item in caught] == [
            "installed app committed; temporary cleanup failed: "
            "injected temp cleanup failure"
        ]
    else:
        assert caught == []


@pytest.mark.parametrize("kind", ["symlink", "fifo", "group-writable"])
def test_private_tree_metadata_policy_rejects_unsafe_entries(tmp_path, kind):
    app = tmp_path / "candidate"
    app.mkdir(mode=0o700)
    unsafe = app / "unsafe"
    if kind == "symlink":
        unsafe.symlink_to(app)
    elif kind == "fifo":
        os.mkfifo(unsafe)
    else:
        unsafe.write_bytes(b"x")
        unsafe.chmod(0o620)
    with pytest.raises(INSTALL_STAGED.InstallError):
        INSTALL_STAGED._normalise_private_tree(app)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
def test_archive_snapshot_rejects_non_regular_input_boundedly(tmp_path, kind):
    archive = tmp_path / "approved.zip"
    if kind == "symlink":
        target = tmp_path / "target.zip"
        target.write_bytes(b"zip")
        archive.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(archive)
    else:
        archive.mkdir()
    with pytest.raises((INSTALL_STAGED.InstallError, OSError)):
        INSTALL_STAGED._copy_archive_snapshot(archive, tmp_path / "snapshot", "0" * 64)


def test_same_second_archive_allocation_never_overwrites_an_approved_archive(
    tmp_path: Path, monkeypatch
):
    installed = tmp_path / "Onoats.app"
    installed.mkdir()
    backup_dir = tmp_path / "backups"
    identity = ("1" * 64, "2" * 64, "3" * 40)
    monkeypatch.setattr(INSTALL_STAGED, "_verify_app", lambda _path: identity)
    monkeypatch.setattr(
        INSTALL_STAGED.time, "strftime", lambda *_args: "20260101T000000Z"
    )

    def fake_run(*argv: str, cwd=None):
        if argv[1:5] == ("-c", "-k", "--keepParent", str(installed)):
            Path(argv[5]).write_bytes(f"archive:{Path(argv[5]).name}".encode())
        elif argv[1:4] == ("-x", "-k", argv[3]):
            (Path(argv[4]) / installed.name).mkdir()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(INSTALL_STAGED, "_run", fake_run)
    first, first_path = INSTALL_STAGED._archive_baseline(
        installed, backup_dir, identity[1], identity[2]
    )
    first_bytes = first_path.read_bytes()
    second, second_path = INSTALL_STAGED._archive_baseline(
        installed, backup_dir, identity[1], identity[2]
    )
    assert first["archive"] != second["archive"]
    assert first_path.read_bytes() == first_bytes
    assert second_path.is_file()


def _run_install_staged(
    work: Path,
    *,
    approved: bool = False,
    designated_requirement_sha256: str | None = None,
    cdhash: str | None = None,
    staged_app_tree_hash: str | None = None,
    install_dir: Path | None = None,
    bundle_id: str = "net.varunsingh.onoats",
    release_identity: Path | None = None,
    smoke_receipt: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    staged_app_tree_hash = staged_app_tree_hash or _app_tree_hash(work / "Onoats.app")
    install_dir = install_dir or work / "installed"
    release_identity = release_identity or work / "release-identity.json"
    smoke_receipt = smoke_receipt or work / "smoke-receipt.json"
    command = ["make", "-s", "install-staged"]
    env = os.environ.copy()
    env["HOME"] = str(work / "home")
    env.update(
        {
            "ONOATS_INSTALL_APP": str(work / "Onoats.app"),
            "ONOATS_INSTALL_DIR": str(install_dir),
            "ONOATS_INSTALL_BACKUP_DIR": str(work / "backups"),
            "ONOATS_INSTALL_ROLLBACK_RECORD": str(work / "approved-rollback.txt"),
            "ONOATS_INSTALL_EXPECTED_IDENTITY": staged_app_tree_hash,
            "ONOATS_INSTALL_ROLLBACK_APPROVED": str(int(approved)),
            "ONOATS_INSTALL_BASELINE_DR_HASH": designated_requirement_sha256 or "",
            "ONOATS_INSTALL_BASELINE_CDHASH": cdhash or "",
            "ONOATS_INSTALL_BUNDLE_ID": bundle_id,
            "ONOATS_INSTALL_RELEASE_IDENTITY": str(release_identity),
            "ONOATS_INSTALL_SMOKE_RECEIPT": str(smoke_receipt),
        }
    )
    return subprocess.run(
        command,
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _configure_test_cli_authority(work: Path) -> str:
    """Create a clean checkout and independently observable CLI authority."""
    authority = work / "test-authority"
    interpreter = authority / "python"
    cli_entry = authority / "onoats"
    module = work / "src/onoats/__init__.py"
    module.parent.mkdir(parents=True)
    module.write_text("", encoding="utf-8")
    authority.mkdir()

    observed = {
        "python": str(interpreter),
        "module": str(module),
        "direct_url": json.dumps(
            {"url": work.as_uri(), "dir_info": {"editable": True}}
        ),
    }
    interpreter.write_text(
        f"#!/usr/bin/python3\nimport json\nprint({json.dumps(json.dumps(observed))})\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    cli_entry.write_text(f"#!{interpreter}\n", encoding="utf-8")
    cli_entry.chmod(0o755)
    shim = work / "home/.local/bin/onoats"
    shim.parent.mkdir(parents=True)
    shim.symlink_to(cli_entry)

    (work / ".gitignore").write_text(
        "/Onoats.app/\n"
        "/approved-rollback.txt\n"
        "/backups/\n"
        "/home/\n"
        "/installed*/\n"
        "/native/__pycache__/\n"
        "/release-identity*.json\n"
        "/smoke-receipt*.json\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=work, check=True)
    subprocess.run(
        [
            "git",
            "add",
            ".gitignore",
            "Makefile",
            "install_staged.py",
            "native/Makefile",
            "native/install_staged.py",
            "native/release_identity.py",
            "src/onoats/__init__.py",
            "test-authority/onoats",
            "test-authority/python",
        ],
        cwd=work,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Onoats Test",
            "-c",
            "user.email=onoats-test@example.invalid",
            "commit",
            "-q",
            "-m",
            "test fixture",
        ],
        cwd=work,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=work,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_install_staged_rejects_conflicting_bundle_id_before_mutation(
    staged_install_workflow: Path,
):
    work = staged_install_workflow
    installed = work / "installed" / "Onoats.app"

    result = _run_install_staged(
        work, bundle_id="net.varunsingh.onoats.conflicting-override"
    )

    assert result.returncode != 0
    assert "may only redundantly name the canonical bundle" in (
        result.stdout + result.stderr
    )
    assert not installed.exists()


@pytest.fixture
def staged_install_workflow(tmp_path: Path) -> Path:
    if sys.platform != "darwin":
        pytest.skip("native codesign regression requires macOS")
    missing = [tool for tool in ("codesign", "ditto", "make") if not shutil.which(tool)]
    if missing:
        pytest.skip(f"native install tools unavailable: {', '.join(missing)}")
    native = tmp_path / "native"
    native.mkdir()
    shutil.copy2(NATIVE_MAKEFILE, native / "Makefile")
    (tmp_path / "Makefile").symlink_to("native/Makefile")
    installer = native / "install_staged.py"
    shutil.copy2(NATIVE_INSTALL_SCRIPT, installer)
    shutil.copy2(NATIVE_RELEASE_IDENTITY, native / "release_identity.py")
    (tmp_path / "install_staged.py").symlink_to(installer.relative_to(tmp_path))
    source = installer.read_text(encoding="utf-8")
    source = source.replace(
        "LSREGISTER = Path(\n"
        '    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"\n'
        '    "LaunchServices.framework/Support/lsregister"\n'
        ")",
        'LSREGISTER = Path("/usr/bin/true")',
    )
    installer.write_text(source, encoding="utf-8")
    _make_adhoc_app(tmp_path / "Onoats.app")
    _configure_test_cli_authority(tmp_path)
    cli_authority = RELEASE_IDENTITY.observed_cli_authority(
        checkout=tmp_path, shim=tmp_path / "home/.local/bin/onoats"
    )
    identity = INSTALL_STAGED.release_identity(
        tmp_path / "Onoats.app", cli_authority=cli_authority
    )
    (tmp_path / "release-identity.json").write_text(
        identity.to_json() + "\n", encoding="utf-8"
    )
    INSTALL_STAGED.write_smoke_receipt(tmp_path / "smoke-receipt.json", identity)
    (tmp_path / "installed").mkdir()
    return tmp_path


def test_resource_allowlist_rejects_missing_and_unexpected_files(tmp_path: Path):
    """The verifier is an equality check, not merely an existence check."""
    app = tmp_path / "Onoats.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    source = REPO_ROOT / "native" / "onoats-menubar" / "Resources"
    for name in NATIVE_RESOURCES:
        (resources / name).write_bytes((source / name).read_bytes())

    complete = _run_resource_verifier(app)
    assert complete.returncode == 0, complete.stdout + complete.stderr

    (resources / "unexpected.txt").write_text("stale", encoding="utf-8")
    unexpected = _run_resource_verifier(app)
    assert unexpected.returncode != 0, "unexpected bundle resource was accepted"
    (resources / "unexpected.txt").unlink()

    (resources / NATIVE_RESOURCES[0]).unlink()
    missing = _run_resource_verifier(app)
    assert missing.returncode != 0, "missing bundle resource was accepted"


@pytest.mark.parametrize("wrong_type", ["directory", "symlink"])
def test_resource_allowlist_rejects_expected_name_with_wrong_type(
    tmp_path: Path, wrong_type: str
):
    app = tmp_path / "Onoats.app"
    resources = app / "Contents" / "Resources"
    resources.mkdir(parents=True)
    source = REPO_ROOT / "native" / "onoats-menubar" / "Resources"
    for name in NATIVE_RESOURCES:
        (resources / name).write_bytes((source / name).read_bytes())
    victim = resources / NATIVE_RESOURCES[0]
    victim.unlink()
    if wrong_type == "directory":
        victim.mkdir()
    else:
        victim.symlink_to(resources / NATIVE_RESOURCES[1])

    result = _run_resource_verifier(app)
    assert result.returncode != 0, (
        f"resource verifier accepted {wrong_type} at expected file name"
    )


def test_resource_verification_precedes_codesigning():
    makefile = NATIVE_MAKEFILE.read_text(encoding="utf-8")
    _, sign = _make_target(makefile, "sign")
    verify = sign.find("verify-resources")
    first_codesign = sign.find("codesign --force")
    post_sign_verify = sign.find("codesign --verify --deep --strict")
    assert -1 not in (verify, first_codesign, post_sign_verify)
    assert verify < first_codesign < post_sign_verify, (
        "resource allowlist must be checked before signing and the signed bundle "
        "must receive strict verification"
    )


def test_staged_installer_never_resolves_trust_executables_through_path():
    """All install trust/copy tools have fixed platform-owned authority."""
    source = NATIVE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert INSTALL_STAGED.CHMOD == "/bin/chmod"
    assert INSTALL_STAGED.CODESIGN == "/usr/bin/codesign"
    assert INSTALL_STAGED.DITTO == "/usr/bin/ditto"
    assert INSTALL_STAGED.MAKE == "/usr/bin/make"
    assert '_run("' not in source
    assert '(CHMOD, "-RN"' in source


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("CHMOD", "/bin/chmod"),
        ("CODESIGN", "/usr/bin/codesign"),
        ("DITTO", "/usr/bin/ditto"),
        ("MAKE", "/usr/bin/make"),
    ],
)
def test_staged_installer_executable_authority_table(name, expected):
    assert getattr(INSTALL_STAGED, name) == expected


def test_make_resource_verifier_environment_is_deny_by_default(
    tmp_path: Path, monkeypatch
):
    makefile = tmp_path / "Makefile"
    makefile.write_text(
        "verify-resources:\n"
        "\t@command -v sh sort find basename diff mktemp rm >/dev/null\n"
        '\t@test -n "$$ONOATS_VERIFY_RESOURCES_APP"\n'
        '\t@test -z "$$PLANTED_API_SECRET"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PLANTED_API_SECRET", "must-not-cross")
    app = tmp_path / "Onoats.app"
    app.mkdir()

    INSTALL_STAGED._verify_resources(app, tmp_path)

    child = INSTALL_STAGED._make_verifier_env(
        app,
        environ={"PLANTED_API_SECRET": "must-not-cross", "TMPDIR": str(tmp_path)},
    )
    assert child == {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LC_ALL": "C",
        "ONOATS_VERIFY_RESOURCES_APP": str(app),
        "TMPDIR": str(tmp_path),
    }


def test_smoke_receipt_state_space_is_atomic_and_identity_bound(
    tmp_path: Path, monkeypatch
):
    identity = INSTALL_STAGED.ReleaseIdentity(
        INSTALL_STAGED.BUNDLE_ID,
        "identifier net.varunsingh.onoats",
        "a" * 40,
        "b" * 64,
        "c" * 64,
        "/tmp/onoats-test-cli",
        "d" * 64,
        "/tmp/onoats-test-python",
        "e" * 64,
        "d" * 40,
    )
    other = identity._replace(tree_hash="e" * 64)
    receipt_path = tmp_path / "smoke-receipt.json"

    INSTALL_STAGED.write_smoke_receipt(receipt_path, identity)
    receipt = INSTALL_STAGED._read_smoke_receipt(receipt_path)
    assert receipt.release_identity_sha256 == INSTALL_STAGED.release_identity_digest(
        identity
    )
    assert receipt.release_identity_sha256 != INSTALL_STAGED.release_identity_digest(
        other
    )
    with pytest.raises(INSTALL_STAGED.InstallError, match="does not match"):
        INSTALL_STAGED._require_smoke_receipt(receipt_path, other)

    INSTALL_STAGED.invalidate_smoke_receipt(receipt_path)
    with pytest.raises(INSTALL_STAGED.InstallError, match="fields"):
        INSTALL_STAGED._read_smoke_receipt(receipt_path)

    INSTALL_STAGED.write_smoke_receipt(receipt_path, identity)
    replacement = tmp_path / "replacement.json"
    INSTALL_STAGED.write_smoke_receipt(replacement, other)
    real_read = INSTALL_STAGED.os.read

    def replace_after_read(descriptor, size):
        payload = real_read(descriptor, size)
        os.replace(replacement, receipt_path)
        return payload

    monkeypatch.setattr(INSTALL_STAGED.os, "read", replace_after_read)
    with pytest.raises(INSTALL_STAGED.InstallError, match="changed while being read"):
        INSTALL_STAGED._read_smoke_receipt(receipt_path)


@pytest.mark.parametrize(
    ("state", "accepted"),
    [("absent", True), ("regular", True), ("symlink", False), ("replacement", False)],
)
def test_smoke_receipt_output_leaf_state_space(
    tmp_path: Path, state: str, accepted: bool
):
    receipt = tmp_path / "shared-smoke-receipt.json"
    target = tmp_path / "attacker-target"
    target.write_text("unchanged", encoding="utf-8")
    if state in {"regular", "replacement"}:
        receipt.write_text("old", encoding="utf-8")
    if state == "replacement":
        receipt.unlink()
    if state in {"symlink", "replacement"}:
        receipt.symlink_to(target)

    if accepted:
        RELEASE_IDENTITY.invalidate_smoke_receipt(receipt)
        assert receipt.is_file() and not receipt.is_symlink()
    else:
        with pytest.raises(RELEASE_IDENTITY.InstallError, match="non-symlink"):
            RELEASE_IDENTITY.invalidate_smoke_receipt(receipt)
        assert target.read_text(encoding="utf-8") == "unchanged"


def test_smoke_receipt_path_resolution_preserves_hostile_final_symlink(tmp_path: Path):
    target = tmp_path / "attacker-target"
    target.write_text("unchanged", encoding="utf-8")
    planted = tmp_path / "receipt.json"
    planted.symlink_to(target)

    safe = RELEASE_IDENTITY.preserve_final_component(planted)

    assert safe == planted
    assert safe.is_symlink()
    with pytest.raises(RELEASE_IDENTITY.InstallError, match="non-symlink"):
        RELEASE_IDENTITY.invalidate_smoke_receipt(safe)
    assert target.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize(
    ("cell", "accepted"),
    [
        ("canonical", True),
        ("override", False),
        ("wrapper", False),
        ("replacement", False),
    ],
)
def test_release_cli_authority_state_space(tmp_path: Path, cell: str, accepted: bool):
    interpreter = tmp_path / "python"
    interpreter.write_bytes(b"verified interpreter")
    interpreter.chmod(0o755)
    entrypoint = tmp_path / "onoats-entry"
    entrypoint.write_text(f"#!{interpreter}\n", encoding="utf-8")
    entrypoint.chmod(0o755)
    shim = tmp_path / "onoats"
    shim.symlink_to(entrypoint)
    identity = RELEASE_IDENTITY.ReleaseIdentity(
        RELEASE_IDENTITY.BUNDLE_ID,
        "identifier net.varunsingh.onoats",
        "a" * 40,
        "b" * 64,
        "c" * 64,
        str(shim),
        hashlib.sha256(entrypoint.read_bytes()).hexdigest(),
        str(interpreter),
        hashlib.sha256(interpreter.read_bytes()).hexdigest(),
        "d" * 40,
    )
    selected = shim
    if cell == "override":
        selected = tmp_path / "override"
        selected.symlink_to(entrypoint)
    elif cell == "wrapper":
        shim.unlink()
        shim.write_text(f"#!{interpreter}\n", encoding="utf-8")
    elif cell == "replacement":
        entrypoint.write_text(f"#!{interpreter}\n# replaced\n", encoding="utf-8")

    if accepted:
        assert RELEASE_IDENTITY.verify_cli_authority(identity, selected) == interpreter
    else:
        with pytest.raises(RELEASE_IDENTITY.InstallError):
            RELEASE_IDENTITY.verify_cli_authority(identity, selected)


def test_designated_requirement_extraction_is_path_independent_and_fail_closed():
    contract = NATIVE_RELEASE_IDENTITY.read_text(encoding="utf-8")
    assert 'line.startswith("designated => ")' in contract, (
        "codesign output must be reduced to the designated requirement line"
    )
    assert "len(lines) != 1" in contract, (
        "designated-requirement extraction must reject zero or multiple matches"
    )
    installer = NATIVE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "hashlib.sha256(requirement.encode())" in installer


def test_adhoc_designated_requirement_normalizes_across_bundle_paths(tmp_path: Path):
    if sys.platform != "darwin" or not shutil.which("codesign"):
        pytest.skip("native codesign regression requires macOS")
    original = tmp_path / "staged" / "Onoats.app"
    original.parent.mkdir()
    _make_adhoc_app(original)
    copies = [
        tmp_path / "installed" / "Onoats.app",
        tmp_path / "extracted" / "nested" / "Onoats.app",
    ]
    for copy in copies:
        copy.parent.mkdir(parents=True)
        shutil.copytree(original, copy, copy_function=_copy_file_without_flags)

    observations = [_designated_requirement(app) for app in [original, *copies]]
    assert len({requirement for requirement, _ in observations}) == 1
    executable_lines = [
        next(line for line in output.splitlines() if line.startswith("Executable="))
        for _, output in observations
    ]
    assert len(set(executable_lines)) == len(executable_lines), (
        "the regression needs distinct absolute paths to prove normalization"
    )


def test_info_plist_adds_icon_without_identity_or_tcc_drift():
    with MENUBAR_PLIST.open("rb") as fh:
        plist = plistlib.load(fh)
    assert plist["CFBundleIconFile"] == "Onoats.icns"
    assert plist["CFBundleIdentifier"] == "net.varunsingh.onoats"
    assert plist["CFBundleExecutable"] == "Onoats"
    assert plist["CFBundleName"] == "Onoats"
    assert plist["LSMinimumSystemVersion"] == "14.4"
    assert plist["NSMicrophoneUsageDescription"] == (
        'onoats records your microphone ("me") for transcription.'
    )
    assert plist["NSAudioCaptureUsageDescription"] == (
        'onoats records system audio ("them") for transcription.'
    )


def test_install_staged_requires_explicit_rollback_approval_before_archiving():
    makefile = NATIVE_MAKEFILE.read_text(encoding="utf-8")
    prerequisites, body = _make_target(makefile, "install-staged")
    assert prerequisites == "", (
        "install-staged must not rebuild or re-sign staged bytes"
    )
    recipe = next(line.strip() for line in body.splitlines() if line.startswith("\t"))
    assert recipe == "@python3 install_staged.py env-install"
    assert "$(" not in recipe, "operator values must never become shell source"
    installer = NATIVE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    for variable in (
        "ONOATS_INSTALL_APP",
        "ONOATS_INSTALL_DIR",
        "ONOATS_INSTALL_BACKUP_DIR",
        "ONOATS_INSTALL_ROLLBACK_RECORD",
        "ONOATS_INSTALL_EXPECTED_IDENTITY",
        "ONOATS_INSTALL_ROLLBACK_APPROVED",
        "ONOATS_INSTALL_BASELINE_DR_HASH",
        "ONOATS_INSTALL_BASELINE_CDHASH",
    ):
        assert f'"{variable}"' in installer
        assert variable not in recipe
    assert '_run(DITTO, "-c", "-k", "--keepParent"' in installer
    assert "tempfile.mkstemp(" in installer and 'suffix=".zip"' in installer
    assert "_verify_app(candidate" in installer
    assert "_replace_verified(candidate, installed, verify, commit)" in installer
    assert "_write_record(record, record_fields)" in installer
    assert "commit()" in installer


def test_staged_make_recipes_treat_hostile_paths_as_data(tmp_path: Path):
    """Environment-only operator bytes reach Python without Make evaluation."""
    hostile = tmp_path / "checkout `touch CHECKOUT_PWNED` $(touch ALSO_PWNED)"
    hostile.mkdir()
    shutil.copy2(NATIVE_MAKEFILE, hostile / "Makefile")
    (hostile / "install_staged.py").write_text(
        "import os, pathlib, sys\n"
        "pathlib.Path('captured.txt').write_text("
        "sys.argv[1] + '\\n' + os.environ['ONOATS_INSTALL_APP'] + '\\n' + "
        "os.environ['ONOATS_INSTALL_DIR'], encoding='utf-8')\n",
        encoding="utf-8",
    )
    hostile_app = "Onoats$(touch APP_PWNED)`touch ALSO_APP_PWNED`.app"
    hostile_install = str(tmp_path / "Install; touch INSTALL_PWNED; # space")
    for target, subcommand in (
        ("app-tree-hash", "make-identity"),
        ("install-staged", "make-install"),
        ("rollback", "make-rollback"),
    ):
        env = os.environ.copy()
        env["ONOATS_INSTALL_APP"] = hostile_app
        env["ONOATS_INSTALL_DIR"] = hostile_install
        result = subprocess.run(
            ["make", "-s", target],
            cwd=hostile,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        for marker in (
            "CHECKOUT_PWNED",
            "ALSO_PWNED",
            "APP_PWNED",
            "ALSO_APP_PWNED",
            "INSTALL_PWNED",
        ):
            assert not (hostile / marker).exists()
            assert not (tmp_path / marker).exists()
        assert (hostile / "captured.txt").read_text(encoding="utf-8") == (
            f"{subcommand.replace('make-', 'env-')}\n{hostile_app}\n{hostile_install}"
        )


STAGED_INSTALL_ENV_INPUTS = (
    "ONOATS_INSTALL_APP",
    "ONOATS_INSTALL_DIR",
    "ONOATS_INSTALL_BACKUP_DIR",
    "ONOATS_INSTALL_ROLLBACK_RECORD",
    "ONOATS_INSTALL_EXPECTED_IDENTITY",
    "ONOATS_INSTALL_ROLLBACK_APPROVED",
    "ONOATS_INSTALL_BASELINE_DR_HASH",
    "ONOATS_INSTALL_BASELINE_CDHASH",
    "ONOATS_INSTALL_BUNDLE_ID",
)


@pytest.mark.parametrize("variable", STAGED_INSTALL_ENV_INPUTS)
def test_every_staged_installer_input_rejects_make_command_line_evaluation(
    tmp_path: Path, variable: str
):
    """Make command-line variables are program text, never installer data."""
    shutil.copy2(NATIVE_MAKEFILE, tmp_path / "Makefile")
    (tmp_path / "install_staged.py").write_text(
        "from pathlib import Path\nPath('PYTHON_STARTED').write_text('bad')\n",
        encoding="utf-8",
    )
    sentinel = tmp_path / "installed-sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")

    result = subprocess.run(
        [
            "make",
            "-s",
            "install-staged",
            f"{variable}=$(shell touch MAKE_EVALUATED)",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert not (tmp_path / "PYTHON_STARTED").exists()
    assert not (tmp_path / "MAKE_EVALUATED").exists()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.parametrize("variable", STAGED_INSTALL_ENV_INPUTS)
def test_every_staged_installer_input_accepts_literal_environment_prefix_bytes(
    tmp_path: Path, variable: str
):
    """A true environment-prefix assignment bypasses Make evaluation."""
    shutil.copy2(NATIVE_MAKEFILE, tmp_path / "Makefile")
    (tmp_path / "install_staged.py").write_text(
        "import os, pathlib\n"
        "name = os.environ['CAPTURE_NAME']\n"
        "pathlib.Path('captured.txt').write_text(os.environ[name], encoding='utf-8')\n",
        encoding="utf-8",
    )
    hostile = "$(shell touch ENVIRONMENT_EVALUATED)"
    env = os.environ.copy()
    env.update({"CAPTURE_NAME": variable, variable: hostile})

    result = subprocess.run(
        ["make", "-s", "install-staged"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "captured.txt").read_text(encoding="utf-8") == hostile
    assert not (tmp_path / "ENVIRONMENT_EVALUATED").exists()


@pytest.mark.parametrize(
    "legacy",
    [
        "APP",
        "APP_NAME",
        "INSTALL_DIR",
        "BACKUP_DIR",
        "ROLLBACK_RECORD",
        "STAGED_APP_TREE_HASH",
        "ROLLBACK_BASELINE_APPROVED",
        "ROLLBACK_BASELINE_DR_HASH",
        "ROLLBACK_BASELINE_CDHASH",
        "BUNDLE_ID",
    ],
)
def test_every_legacy_staged_installer_override_fails_before_python_or_mutation(
    tmp_path: Path, legacy: str
):
    shutil.copy2(NATIVE_MAKEFILE, tmp_path / "Makefile")
    (tmp_path / "install_staged.py").write_text(
        "from pathlib import Path\nPath('PYTHON_STARTED').write_text('bad')\n",
        encoding="utf-8",
    )
    sentinel = tmp_path / "installed-sentinel"
    sentinel.write_text("unchanged", encoding="utf-8")
    result = subprocess.run(
        ["make", "-s", "install-staged", f"{legacy}=$(shell touch MAKE_EVALUATED)"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert not (tmp_path / "PYTHON_STARTED").exists()
    assert not (tmp_path / "MAKE_EVALUATED").exists()
    assert sentinel.read_text(encoding="utf-8") == "unchanged"


def test_install_staged_binds_install_to_pre_smoke_tree_digest(
    staged_install_workflow: Path,
):
    work = staged_install_workflow
    installed = work / "installed" / "Onoats.app"
    shutil.copytree(
        work / "Onoats.app", installed, copy_function=_copy_file_without_flags
    )
    inode_before = installed.stat().st_ino
    expected = _app_tree_hash(work / "Onoats.app")
    resource = work / "Onoats.app/Contents/Resources/OnoatsHeader.png"
    resource.write_bytes(resource.read_bytes() + b"changed-after-smoke")

    result = _run_install_staged(work, staged_app_tree_hash=expected)

    assert result.returncode != 0
    assert "approved release identity" in result.stdout + result.stderr
    assert installed.stat().st_ino == inode_before


def test_install_staged_preserves_rollback_record_until_successful_install(
    staged_install_workflow: Path,
):
    work = staged_install_workflow
    backup_dir = work / "backups"
    backup_dir.mkdir(parents=True)
    record = backup_dir / "approved-rollback.txt"
    record.write_text("existing verified rollback\n", encoding="utf-8")

    result = _run_install_staged(work, staged_app_tree_hash="0" * 64)

    assert result.returncode != 0
    assert record.read_text(encoding="utf-8") == "existing verified rollback\n"


def test_install_staged_quotes_paths_with_spaces_and_glob_characters(
    staged_install_workflow: Path,
):
    work = staged_install_workflow
    install_dir = work / "installed target [literal]"
    install_dir.mkdir()

    result = _run_install_staged(work, install_dir=install_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (install_dir / "Onoats.app").is_dir()


def test_install_staged_never_expands_operator_path_as_make_syntax(
    staged_install_workflow: Path,
):
    work = staged_install_workflow
    install_dir = work / "installed $(shell touch MAKE_EXPANSION_RAN) [literal]"
    install_dir.mkdir()

    result = _run_install_staged(work, install_dir=install_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (install_dir / "Onoats.app").is_dir()
    assert not (work / "MAKE_EXPANSION_RAN").exists()


def test_clean_install_ignores_invalid_existing_app_and_clears_stale_approval(
    staged_install_workflow: Path,
):
    work = staged_install_workflow
    installed = work / "installed" / "Onoats.app"
    installed.mkdir()
    (installed / "invalid-baseline").write_text("not signed", encoding="utf-8")
    backup_dir = work / "backups"
    backup_dir.mkdir(parents=True)
    record = work / "approved-rollback.txt"
    record.write_text("stale approval", encoding="utf-8")

    result = _run_install_staged(work)
    assert result.returncode == 0, result.stdout + result.stderr
    _codesign_output(installed, "--verify", "--deep", "--strict")
    assert not record.exists(), "clean install retained stale rollback approval"
    assert not list(backup_dir.glob("Onoats-*.zip")), (
        "clean install created a rollback archive without approval"
    )


def test_clean_install_does_not_archive_an_unapproved_valid_app(
    staged_install_workflow: Path,
):
    work = staged_install_workflow
    installed = work / "installed" / "Onoats.app"
    shutil.copytree(
        work / "Onoats.app", installed, copy_function=_copy_file_without_flags
    )

    result = _run_install_staged(work)
    assert result.returncode == 0, result.stdout + result.stderr
    backup_dir = work / "backups"
    assert not list(backup_dir.glob("Onoats-*.zip"))
    assert not (backup_dir / "approved-rollback.txt").exists()


def _rollback_approval(app: Path, **overrides: str) -> dict[str, str]:
    requirement, _ = _designated_requirement(app)
    fields = {
        "designated_requirement_sha256": hashlib.sha256(
            requirement.encode()
        ).hexdigest(),
        "cdhash": _cdhash(app),
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize(
    ("approval_kind", "overrides"),
    [
        ("missing", None),
        ("designated requirement", {"designated_requirement_sha256": "0" * 64}),
        ("cdhash", {"cdhash": "0" * 40}),
    ],
)
def test_approved_mode_fails_closed_before_removal_when_evidence_is_invalid(
    staged_install_workflow: Path,
    approval_kind: str,
    overrides: dict[str, str] | None,
):
    work = staged_install_workflow
    installed = work / "installed" / "Onoats.app"
    shutil.copytree(
        work / "Onoats.app", installed, copy_function=_copy_file_without_flags
    )
    inode_before = installed.stat().st_ino
    evidence = _rollback_approval(installed, **(overrides or {}))
    if overrides is None:
        evidence = {}

    result = _run_install_staged(
        work,
        approved=True,
        designated_requirement_sha256=evidence.get("designated_requirement_sha256"),
        cdhash=evidence.get("cdhash"),
    )
    assert result.returncode != 0, (
        f"approved mode accepted invalid {approval_kind} evidence\n"
        f"{result.stdout}{result.stderr}"
    )
    expected_error = {
        "missing": "approved rollback requires",
        "designated requirement": "designated requirement differs",
        "cdhash": "cdhash differs",
    }[approval_kind]
    assert expected_error in result.stdout + result.stderr
    assert installed.stat().st_ino == inode_before, (
        "installed app was removed before approval evidence passed"
    )


def test_complete_explicit_approval_creates_verified_rollback_record(
    staged_install_workflow: Path,
):
    work = staged_install_workflow
    installed = work / "installed" / "Onoats.app"
    shutil.copytree(
        work / "Onoats.app", installed, copy_function=_copy_file_without_flags
    )
    evidence = _rollback_approval(installed)

    result = _run_install_staged(
        work,
        approved=True,
        designated_requirement_sha256=evidence["designated_requirement_sha256"],
        cdhash=evidence["cdhash"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    backup_dir = work / "backups"
    archives = list(backup_dir.glob("Onoats-*.zip"))
    assert len(archives) == 1
    record = work / "approved-rollback.txt"
    assert record.is_file()
    record_text = record.read_text(encoding="utf-8")
    assert str(archives[0]) in record_text
    assert hashlib.sha256(archives[0].read_bytes()).hexdigest() in record_text


@pytest.mark.parametrize(
    "field",
    [
        "bundle_id",
        "designated_requirement",
        "cdhash",
        "tree_hash",
        "executable_hash",
        "cli_path",
        "cli_executable_hash",
        "cli_interpreter_path",
        "cli_interpreter_hash",
        "cli_git_identity",
    ],
)
def test_install_requires_every_release_identity_field_before_mutation(
    staged_install_workflow: Path, field: str
):
    work = staged_install_workflow
    identity_path = work / "release-identity.json"
    value = json.loads(identity_path.read_text(encoding="utf-8"))
    value[field] = {
        "bundle_id": "net.varunsingh.onoats.impostor",
        "designated_requirement": "identifier impostor",
        "cdhash": "f" * 40,
        "tree_hash": "f" * 64,
        "executable_hash": "f" * 64,
        "cli_path": "/tmp/impostor-onoats",
        "cli_executable_hash": "f" * 64,
        "cli_interpreter_path": "/tmp/impostor-python",
        "cli_interpreter_hash": "f" * 64,
        "cli_git_identity": "f" * 40,
    }[field]
    mismatch = work / f"release-identity-{field}.json"
    mismatch.write_text(json.dumps(value), encoding="utf-8")

    result = _run_install_staged(work, release_identity=mismatch)

    assert result.returncode != 0
    assert not (work / "installed" / "Onoats.app").exists()


@pytest.mark.parametrize("receipt_state", ["missing", "invalidated", "wrong-identity"])
def test_install_requires_matching_successful_smoke_receipt_before_mutation(
    staged_install_workflow: Path, receipt_state: str
):
    work = staged_install_workflow
    receipt = work / f"receipt-{receipt_state}.json"
    identity = INSTALL_STAGED._read_release_identity(work / "release-identity.json")
    if receipt_state == "invalidated":
        INSTALL_STAGED.invalidate_smoke_receipt(receipt)
    elif receipt_state == "wrong-identity":
        INSTALL_STAGED.write_smoke_receipt(
            receipt, identity._replace(executable_hash="f" * 64)
        )

    result = _run_install_staged(work, smoke_receipt=receipt)

    assert result.returncode != 0
    assert not (work / "installed" / "Onoats.app").exists()


def test_release_identity_is_portable_across_a_distinct_bundle_copy(
    staged_install_workflow: Path, tmp_path: Path
):
    source = staged_install_workflow / "Onoats.app"
    copied = tmp_path / "portable" / "Onoats.app"
    copied.parent.mkdir()
    shutil.copytree(source, copied, copy_function=_copy_file_without_flags)

    approved = INSTALL_STAGED._read_release_identity(
        staged_install_workflow / "release-identity.json"
    )
    cli_authority = (
        approved.cli_path,
        approved.cli_executable_hash,
        approved.cli_interpreter_path,
        approved.cli_interpreter_hash,
        approved.cli_git_identity,
    )
    source_identity = INSTALL_STAGED.release_identity(
        source, cli_authority=cli_authority
    )
    copied_identity = INSTALL_STAGED.release_identity(
        copied, cli_authority=cli_authority
    )

    assert copied_identity == source_identity
    assert (source / "Contents/MacOS/Onoats").stat().st_ino != (
        copied / "Contents/MacOS/Onoats"
    ).stat().st_ino


def test_release_identity_rejects_source_path_replacement_during_snapshot(
    tmp_path: Path, monkeypatch
):
    app = tmp_path / "Onoats.app"
    executable = app / "Contents/MacOS/Onoats"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"first")

    def replace_during_copy(_ditto: str, source: str, destination: str) -> None:
        shutil.copytree(source, destination)
        replacement = executable.with_suffix(".replacement")
        replacement.write_bytes(b"second")
        os.replace(replacement, executable)

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def replace_during_copy_run(argv, **_kwargs):
        assert argv[0] == INSTALL_STAGED.DITTO
        replace_during_copy(*argv)
        return Result()

    monkeypatch.setattr(RELEASE_IDENTITY.subprocess, "run", replace_during_copy_run)
    cli_authority = (
        "/tmp/onoats",
        "a" * 64,
        "/tmp/python",
        "b" * 64,
        "c" * 40,
    )

    with pytest.raises(
        INSTALL_STAGED.InstallError, match="path/inode changed during identity snapshot"
    ):
        INSTALL_STAGED.release_identity(app, cli_authority=cli_authority)


def test_release_publish_waits_for_both_supported_runtime_gates():
    workflow = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    contracts = (REPO_ROOT / ".github/workflows/python-313-contracts.yml").read_text(
        encoding="utf-8"
    )
    publish = workflow.split("\n  publish:\n", 1)[1]
    assert "needs: [build, python-313-contracts]" in publish
    assert 'python-version: "3.12"' in workflow
    assert 'python-version: "3.13"' in contracts
    assert "uses: ./.github/workflows/python-313-contracts.yml" in workflow


def test_rollback_uses_only_recorded_archive_then_reverifies_restored_app():
    makefile = NATIVE_MAKEFILE.read_text(encoding="utf-8")
    _make_target(makefile, "install-staged")
    _, body = _make_target(makefile, "rollback")
    recipe = next(line.strip() for line in body.splitlines() if line.startswith("\t"))
    assert recipe == "@python3 install_staged.py env-rollback"
    assert "$(" not in recipe, "operator values must never become shell source"
    installer = NATIVE_INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "_read_record(Path(args.record), backup_dir)" in installer
    assert '_run(DITTO, "-x", "-k"' in installer
    assert '_verify_app(candidate, expected_identity=fields["tree_hash"])' in installer
    assert "_replace_verified(" in installer
    assert 'archive = Path(fields["archive"])' in installer
    assert "archive.relative_to(backup_dir)" in installer
    assert "ONOATS_INSTALL_ROLLBACK_RECORD" not in recipe
    assert '_env_value(\n        "ONOATS_INSTALL_ROLLBACK_RECORD"' in installer

    unpack = installer.index('_run(DITTO, "-x", "-k", str(archive), str(parent))')
    verify = installer.index(
        '_verify_app(candidate, expected_identity=fields["tree_hash"])', unpack
    )
    replace = installer.index("_replace_verified(", verify)
    assert unpack < verify < replace


def test_rollback_extracts_one_private_archive_snapshot_not_replaced_path(
    tmp_path, monkeypatch
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    archive = backup_dir / "Onoats-approved.zip"
    original = b"immutable-approved-archive"
    archive.write_bytes(original)
    record = backup_dir / "approved-rollback.txt"
    record.write_text(
        "\n".join(
            (
                f"archive={archive}",
                f"hash={hashlib.sha256(original).hexdigest()}",
                f"tree_hash={'1' * 64}",
                f"dr_hash={'2' * 64}",
                f"cdhash={'3' * 40}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    install_dir = tmp_path / "installed"
    extraction_sources: list[Path] = []

    def fake_run(*argv: str, cwd=None):
        if len(argv) >= 5 and argv[1:3] == ("-x", "-k"):
            source = Path(argv[3])
            extraction_sources.append(source)
            archive.write_bytes(b"attacker replacement")
            assert source != archive
            assert source.read_bytes() == original
            (Path(argv[4]) / "Onoats.app").mkdir()
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(INSTALL_STAGED, "_run", fake_run)
    monkeypatch.setattr(
        INSTALL_STAGED,
        "_verify_app",
        lambda *_args, **_kwargs: ("1" * 64, "2" * 64, "3" * 40),
    )
    monkeypatch.setattr(INSTALL_STAGED, "_replace_verified", lambda *_args: None)
    INSTALL_STAGED.rollback(
        argparse.Namespace(
            install_dir=str(install_dir),
            backup_dir=str(backup_dir),
            record=str(record),
            app_name="Onoats.app",
        )
    )
    assert len(extraction_sources) == 1


def test_rollback_uses_recorded_identity_not_current_resource_manifest(
    tmp_path, monkeypatch
):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    archive = backup_dir / "Onoats-approved.zip"
    payload = b"older-release-archive"
    archive.write_bytes(payload)
    record = backup_dir / "approved-rollback.txt"
    record.write_text(
        "\n".join(
            (
                f"archive={archive}",
                f"hash={hashlib.sha256(payload).hexdigest()}",
                f"tree_hash={'1' * 64}",
                f"dr_hash={'2' * 64}",
                f"cdhash={'3' * 40}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    install_dir = tmp_path / "Applications"
    verify_kwargs: list[dict[str, object]] = []

    def fake_run(*argv: str, cwd=None):
        if len(argv) >= 5 and argv[1:3] == ("-x", "-k"):
            (Path(argv[4]) / "Onoats.app").mkdir()
        return subprocess.CompletedProcess(argv, 0, "", "")

    def verify(_app, **kwargs):
        verify_kwargs.append(kwargs)
        return "1" * 64, "2" * 64, "3" * 40

    def replace(candidate, installed, verify_published, commit):
        installed.parent.mkdir(parents=True, exist_ok=True)
        candidate.rename(installed)
        verify_published(installed)
        commit()

    monkeypatch.setattr(INSTALL_STAGED, "_run", fake_run)
    monkeypatch.setattr(INSTALL_STAGED, "_verify_app", verify)
    monkeypatch.setattr(INSTALL_STAGED, "_replace_verified", replace)
    monkeypatch.setattr(INSTALL_STAGED, "_verify_single_registration", lambda *_: None)
    monkeypatch.setattr(
        INSTALL_STAGED,
        "_verify_resources",
        lambda *_: (_ for _ in ()).throw(AssertionError("current manifest used")),
    )
    INSTALL_STAGED.rollback(
        argparse.Namespace(
            install_dir=str(install_dir),
            backup_dir=str(backup_dir),
            record=str(record),
            app_name="Onoats.app",
            makefile_dir=str(tmp_path / "current-release"),
            bundle_id="net.varunsingh.onoats",
        )
    )
    assert len(verify_kwargs) == 2
    assert all("makefile_dir" not in kwargs for kwargs in verify_kwargs)


def test_existing_install_and_setup_contracts_are_unchanged():
    makefile = NATIVE_MAKEFILE.read_text(encoding="utf-8")
    install_prerequisites, install = _make_target(makefile, "install")
    assert install_prerequisites.split() == ["sign", "install-cli"]
    assert (
        install.find("mkdir -p $(INSTALL_DIR)")
        < install.find("rm -rf $(INSTALL_DIR)/$(APP)")
        < install.find("ditto $(APP) $(INSTALL_DIR)/$(APP)")
    )

    setup_prerequisites, setup = _make_target(makefile, "setup")
    assert setup_prerequisites == ""
    assert setup.find("$(MAKE) cert") < setup.find("$(MAKE) install")
    assert 'config.toml"' in setup
    assert "skipping 'onoats init'" in setup
    assert '"$$HOME/.local/bin/onoats" init' in setup


def test_python313_contract_lane_has_one_reusable_workflow_owner():
    workflow = REPO_ROOT / ".github/workflows/python-313-contracts.yml"
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    release = (REPO_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    authority = workflow.read_text(encoding="utf-8")
    call = "uses: ./.github/workflows/python-313-contracts.yml"

    assert call in ci
    assert call in release
    assert 'python-version: "3.13"' not in ci
    assert 'python-version: "3.13"' not in release
    for test_file in (
        "test_control_socket.py",
        "test_control_socket_contract_parity.py",
        "test_recorder_lifecycle.py",
        "test_socket_supervisor.py",
        "test_status_file.py",
        "test_stt_config_wiring.py",
        "test_runtime_preflight.py",
    ):
        assert authority.count(test_file) == 1
        assert test_file not in ci
        assert test_file not in release
    assert "needs: [build, python-313-contracts]" in release


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
