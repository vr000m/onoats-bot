"""Release metadata checks: LICENSE file and pyproject license field.

Pins the Phase-1 licensing contract from the 0.9→1.0 release plan: the LICENSE
body is the canonical BSD-2-Clause template (placeholders filled, text
otherwise verbatim) and pyproject declares the matching PEP 639 SPDX string.
"""

import re
import hashlib
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

REPO_ROOT = Path(__file__).resolve().parent.parent
NATIVE_MAKEFILE = REPO_ROOT / "native" / "Makefile"
MENUBAR_PLIST = REPO_ROOT / "native" / "onoats-menubar" / "Info.plist"
RECORDER_HARNESS_SCRIPT = REPO_ROOT / "native" / "test_recorder_model.sh"

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
    # The wheel is an allowlist of one package, so nothing else can ride along.
    assert build["wheel"]["packages"] == ["src/onoats"]


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
    return subprocess.run(
        ["make", "-s", "verify-resources", f"APP={app}"],
        cwd=REPO_ROOT / "native",
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
                "CFBundleIdentifier": "net.varunsingh.onoats.tests",
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


def _run_install_staged(
    work: Path,
    *,
    approved: bool = False,
    designated_requirement_sha256: str | None = None,
    cdhash: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        "make",
        "-s",
        "install-staged",
        "APP=Onoats.app",
        f"INSTALL_DIR={work / 'installed'}",
        f"ROLLBACK_BASELINE_APPROVED={int(approved)}",
    ]
    if designated_requirement_sha256 is not None:
        command.append(f"ROLLBACK_BASELINE_DR_HASH={designated_requirement_sha256}")
    if cdhash is not None:
        command.append(f"ROLLBACK_BASELINE_CDHASH={cdhash}")
    env = os.environ.copy()
    env["HOME"] = str(work / "home")
    return subprocess.run(
        command,
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture
def staged_install_workflow(tmp_path: Path) -> Path:
    if sys.platform != "darwin":
        pytest.skip("native codesign regression requires macOS")
    missing = [tool for tool in ("codesign", "ditto", "make") if not shutil.which(tool)]
    if missing:
        pytest.skip(f"native install tools unavailable: {', '.join(missing)}")
    shutil.copy2(NATIVE_MAKEFILE, tmp_path / "Makefile")
    _make_adhoc_app(tmp_path / "Onoats.app")
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


def test_designated_requirement_extraction_is_path_independent_and_fail_closed():
    makefile = NATIVE_MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"\^designated => ", makefile), (
        "codesign output must be reduced to the designated requirement line"
    )
    assert re.search(r"(?:!=|-(?:eq|ne))\s*1|count\s*==\s*1", makefile), (
        "designated-requirement extraction must reject zero or multiple matches"
    )
    assert not re.search(
        r"printf\s+'%s(?:\\n)?'\s+\"\$\$dr_output\"\s*\|\s*shasum",
        makefile,
    ), "the full path-bearing codesign stderr must never be hashed"


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
    for variable in (
        "ROLLBACK_BASELINE_APPROVED",
        "ROLLBACK_BASELINE_DR_HASH",
        "ROLLBACK_BASELINE_CDHASH",
    ):
        assert re.search(rf"(?m)^{variable}\s*\?=", makefile)
        assert f"$({variable})" in body
    assert "ditto -c -k --keepParent" in body
    assert "app-backups" in body and "Onoats-" in body and ".zip" in body
    assert re.search(r"sha(?:sum|256sum)\b[^\n]*256|shasum\s+-a\s+256", body)
    assert "mktemp -d" in body
    assert "ditto -x -k" in body

    remove = body.find("rm -rf $(INSTALL_DIR)/$(APP)")
    copy = body.find("ditto $(APP) $(INSTALL_DIR)/$(APP)")
    assert -1 not in (remove, copy) and remove < copy
    for evidence in (
        "codesign --verify --deep --strict",
        "codesign -dr -",
        "cdhash",
    ):
        position = body.find(evidence)
        assert position != -1 and position < remove, (
            f"installed baseline {evidence!r} verification must precede removal"
        )

    # The approved archive path and its digest must be persisted together for
    # rollback, rather than rediscovered later from a directory glob.
    before_remove = body[:remove]
    assert re.search(r"(?:printf|echo)[^\n]*(?:archive|ARCHIVE)", before_remove)
    assert re.search(r"(?:printf|echo)[^\n]*(?:hash|HASH|sha)", before_remove)


def test_clean_install_ignores_invalid_existing_app_and_clears_stale_approval(
    staged_install_workflow: Path,
):
    work = staged_install_workflow
    installed = work / "installed" / "Onoats.app"
    installed.mkdir()
    (installed / "invalid-baseline").write_text("not signed", encoding="utf-8")
    backup_dir = work / "home" / "Library/Application Support/Onoats/app-backups"
    backup_dir.mkdir(parents=True)
    record = backup_dir / "approved-rollback.txt"
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
    backup_dir = work / "home" / "Library/Application Support/Onoats/app-backups"
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
    backup_dir = work / "home" / "Library/Application Support/Onoats/app-backups"
    archives = list(backup_dir.glob("Onoats-*.zip"))
    assert len(archives) == 1
    record = backup_dir / "approved-rollback.txt"
    assert record.is_file()
    record_text = record.read_text(encoding="utf-8")
    assert str(archives[0]) in record_text
    assert hashlib.sha256(archives[0].read_bytes()).hexdigest() in record_text


def test_rollback_uses_only_recorded_archive_then_reverifies_restored_app():
    makefile = NATIVE_MAKEFILE.read_text(encoding="utf-8")
    _, install_body = _make_target(makefile, "install-staged")
    _, body = _make_target(makefile, "rollback")
    assert "app-backups" in body
    assert re.search(r"sha(?:sum|256sum)\b[^\n]*256|shasum\s+-a\s+256", body)
    assert "ditto -x -k" in body
    assert "codesign --verify --deep --strict" in body
    assert "codesign -dr -" in body
    assert "cdhash" in body
    assert not re.search(r"app-backups/[^\n]*[*?]", body), (
        "rollback must not select an archive by glob"
    )
    approval_vars = set(
        re.findall(r"\$\(([A-Z_]*(?:APPROV|MANIFEST|RECORD)[A-Z_]*)\)", install_body)
    ) & set(re.findall(r"\$\(([A-Z_]*(?:APPROV|MANIFEST|RECORD)[A-Z_]*)\)", body))
    assert approval_vars, (
        "install-staged and rollback must share one persisted approval record; "
        "rollback may not accept an arbitrary archive path"
    )

    remove = body.find("rm -rf $(INSTALL_DIR)/$(APP)")
    unpack = body.find("ditto -x -k")
    assert -1 not in (remove, unpack) and remove < unpack
    assert body.find("codesign --verify --deep --strict", unpack) > unpack
    assert body.find("codesign -dr -", unpack) > unpack
    assert body.find("cdhash", unpack) > unpack


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
