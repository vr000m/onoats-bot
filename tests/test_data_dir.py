"""Data-dir resolution: XDG + ONOATS_DATA_DIR + legacy deprecation.

Asserts the precedence chain and the legacy-env-var carve-outs required by the
plan: (a) a DeprecationWarning fires when only the legacy var is set, and
(b) ONOATS_DATA_DIR wins when both are set.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from onoats._vendor.store import onoats_data_dir

_LEGACY = "KODA_DATA_DIR"


def _clear(monkeypatch):
    for var in ("ONOATS_DATA_DIR", _LEGACY, "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)


def test_onoats_data_dir_env_wins(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("ONOATS_DATA_DIR", str(tmp_path / "explicit"))
    assert onoats_data_dir() == (tmp_path / "explicit")


def test_xdg_default(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert onoats_data_dir() == (tmp_path / "xdg" / "onoats")


def test_xdg_fallback_to_home(monkeypatch):
    _clear(monkeypatch)
    assert onoats_data_dir() == (Path.home() / ".local" / "share" / "onoats")


def test_legacy_env_fires_deprecation_warning(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv(_LEGACY, str(tmp_path / "legacy"))
    with pytest.warns(DeprecationWarning):
        resolved = onoats_data_dir()
    assert resolved == (tmp_path / "legacy")


def test_onoats_env_wins_over_legacy(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("ONOATS_DATA_DIR", str(tmp_path / "new"))
    monkeypatch.setenv(_LEGACY, str(tmp_path / "old"))
    # No deprecation warning should fire when ONOATS_DATA_DIR is set — the
    # legacy var is never consulted.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        resolved = onoats_data_dir()
    assert resolved == (tmp_path / "new")
