"""Configurable recorder data root: env > config.toml [storage].data_dir > XDG.

Lets onoats write its queue into another tree (e.g. ~/koda-data) so a
downstream worker drains the same sessions/ — without per-invocation env vars.
"""

from __future__ import annotations

import os

from onoats import cli
from onoats.config import OnoatsConfig


def test_data_dir_from_config(monkeypatch):
    monkeypatch.delenv("ONOATS_DATA_DIR", raising=False)
    cfg = OnoatsConfig(raw={"storage": {"data_dir": "~/koda-data"}})
    assert cfg.data_dir == "~/koda-data"


def test_env_overrides_config_data_dir(monkeypatch):
    monkeypatch.setenv("ONOATS_DATA_DIR", "/explicit/root")
    cfg = OnoatsConfig(raw={"storage": {"data_dir": "~/koda-data"}})
    assert cfg.data_dir == "/explicit/root"


def test_data_dir_none_when_unset(monkeypatch):
    monkeypatch.delenv("ONOATS_DATA_DIR", raising=False)
    assert OnoatsConfig(raw={}).data_dir is None


def test_cli_exports_data_dir_from_config(monkeypatch):
    """_apply_config_data_dir exports ONOATS_DATA_DIR (expanduser-ed) from config
    so every downstream resolver sees it via the existing env path."""
    monkeypatch.delenv("ONOATS_DATA_DIR", raising=False)
    # _apply_config_data_dir does `from onoats.config import load_config`, so
    # patch it on the source module.
    import onoats.config as cfgmod

    monkeypatch.setattr(
        cfgmod,
        "load_config",
        lambda *a, **k: OnoatsConfig(raw={"storage": {"data_dir": "~/koda-data"}}),
    )
    cli._apply_config_data_dir()
    assert os.environ["ONOATS_DATA_DIR"] == os.path.expanduser("~/koda-data")


def test_cli_env_wins_over_config(monkeypatch):
    monkeypatch.setenv("ONOATS_DATA_DIR", "/already/set")
    import onoats.config as cfgmod

    monkeypatch.setattr(
        cfgmod,
        "load_config",
        lambda *a, **k: OnoatsConfig(raw={"storage": {"data_dir": "~/koda-data"}}),
    )
    cli._apply_config_data_dir()
    assert os.environ["ONOATS_DATA_DIR"] == "/already/set"


def test_init_writes_storage_section(tmp_path, monkeypatch):
    """Non-interactive `onoats init --data-dir ...` writes [storage] data_dir."""
    monkeypatch.delenv("ONOATS_DATA_DIR", raising=False)
    from onoats.init import main as init_main

    cfg_path = tmp_path / "config.toml"
    secrets_path = tmp_path / "secrets.env"
    rc = init_main(
        [
            "--non-interactive",
            "--data-dir",
            "~/koda-data",
            "--stt",
            "deepgram",
            "--config-path",
            str(cfg_path),
            "--secrets-path",
            str(secrets_path),
            "--no-preflight",
        ]
    )
    assert rc == 0
    text = cfg_path.read_text()
    assert "[storage]" in text
    assert 'data_dir = "~/koda-data"' in text
