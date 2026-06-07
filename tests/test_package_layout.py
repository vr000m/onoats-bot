"""Filesystem-only checks for the Phase 1 src-layout move.

Deliberately does NOT `import onoats` — internal imports are still broken
until the P2 import-severing phase. These assertions only verify the package
files and directories landed where the src layout expects them.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG = REPO_ROOT / "src" / "onoats"


def test_package_init_is_file():
    assert (PKG / "__init__.py").is_file()


def test_top_level_modules_exist():
    for name in ("runtime.py", "dual.py", "__main__.py"):
        assert (PKG / name).is_file(), f"missing {name}"


def test_subpackage_dirs_exist():
    for name in ("agents", "frames", "processors", "stt"):
        assert (PKG / name).is_dir(), f"missing dir {name}"


def test_old_bot_dir_removed():
    assert not (REPO_ROOT / "bot").exists()
