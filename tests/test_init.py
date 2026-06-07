"""Guided + non-interactive `onoats init`."""

from __future__ import annotations

import stat
import tomllib

import pytest

from onoats import init as init_mod


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ONOATS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("KODA_DATA_DIR", raising=False)
    for var in (
        "ONOATS_SPEAKER_ME",
        "ONOATS_SPEAKER_THEM",
        "ONOATS_CATEGORIES",
        "STT_SERVICE",
        "DEEPGRAM_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _load_toml(path):
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _patch_inputs(monkeypatch, answers):
    """Feed a queue of answers to input(); raise if exhausted."""
    it = iter(answers)

    def fake_input(prompt=""):
        try:
            return next(it)
        except StopIteration:  # pragma: no cover - test bug guard
            raise AssertionError(f"unexpected extra prompt: {prompt!r}")

    monkeypatch.setattr("builtins.input", fake_input)


def _force_tty(monkeypatch, value=True):
    monkeypatch.setattr("sys.stdin.isatty", lambda: value)


def _patch_devices(monkeypatch, inputs):
    monkeypatch.setattr(init_mod, "_enumerate_inputs", lambda: inputs)


# ---------------------------------------------------------------------------
# Non-interactive
# ---------------------------------------------------------------------------


def test_non_interactive_writes_valid_config(_isolate_env, monkeypatch):
    # No prompts must fire.
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("blocked on input")),
    )
    rc = init_mod.main(
        [
            "--categories",
            "work,personal",
            "--me-name",
            "Varun",
            "--stt",
            "deepgram",
            "--deepgram-key",
            "x" * 40,
            "--no-preflight",
        ]
    )
    assert rc == 0

    from onoats.config import config_toml_path, secrets_env_path

    cfg = _load_toml(config_toml_path())
    assert cfg["stt"]["service"] == "deepgram"
    assert cfg["speakers"]["me"] == "Varun"
    assert set(cfg["categories"]["set"]) == {"work", "personal", "uncategorized"}

    # secrets.env written 0600 with the key
    spath = secrets_env_path()
    assert spath.exists()
    mode = stat.S_IMODE(spath.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    assert "DEEPGRAM_API_KEY" in spath.read_text()

    # dictionary seeded
    from onoats._vendor.dictionary import resolve_dictionary_path

    assert resolve_dictionary_path().exists()


def test_non_interactive_no_tty_does_not_block(_isolate_env, monkeypatch):
    """A non-TTY stdin with no flags must still write a default config."""
    _force_tty(monkeypatch, value=False)
    monkeypatch.setattr(
        "builtins.input",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("blocked on input")),
    )
    rc = init_mod.main(["--no-preflight"])
    assert rc == 0
    from onoats.config import config_toml_path

    cfg = _load_toml(config_toml_path())
    # default STT backend + default category
    assert cfg["stt"]["service"] == "whisper"
    assert cfg["categories"]["set"] == ["uncategorized"]


def test_non_interactive_rejects_same_device(_isolate_env, monkeypatch):
    # validate_audio_device returns a truthy index → name resolves to itself
    monkeypatch.setattr(
        "onoats.config.audio_devices.validate_audio_device",
        lambda q, label, need_input=False: 0,
    )
    rc = init_mod.main(
        ["--mic", "Same Device", "--system", "Same Device", "--no-preflight"]
    )
    assert rc == 1


def test_idempotent_rerun_preserves_values(_isolate_env, monkeypatch):
    assert (
        init_mod.main(["--categories", "work", "--me-name", "Ann", "--no-preflight"])
        == 0
    )
    # Re-run with no flags (non-TTY) — must keep the prior categories + me-name.
    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0
    from onoats.config import config_toml_path

    cfg = _load_toml(config_toml_path())
    assert "work" in cfg["categories"]["set"]
    assert cfg["speakers"]["me"] == "Ann"


# ---------------------------------------------------------------------------
# Interactive — local vs hosted branch
# ---------------------------------------------------------------------------


def test_interactive_hosted_branch(_isolate_env, monkeypatch):
    _force_tty(monkeypatch)
    _patch_devices(
        monkeypatch,
        [(0, "Built-in Mic", 16000), (1, "BlackHole 2ch", 16000)],
    )
    _patch_inputs(
        monkeypatch,
        [
            "0",  # Me device (index)
            "1",  # Them device (index)
            "n",  # local STT? no → hosted Deepgram
            "",  # Deepgram model (default)
            "y" * 40,  # Deepgram API key
            "work,personal",  # categories
            "Varun",  # me name
            "Them",  # them label
        ],
    )
    rc = init_mod.main(["--no-preflight"])
    assert rc == 0

    from onoats.config import config_toml_path, secrets_env_path

    cfg = _load_toml(config_toml_path())
    assert cfg["stt"]["service"] == "deepgram"
    assert cfg["devices"]["mic"] == "Built-in Mic"
    assert cfg["devices"]["system"] == "BlackHole 2ch"
    assert cfg["speakers"]["me"] == "Varun"
    assert set(cfg["categories"]["set"]) == {"work", "personal", "uncategorized"}
    assert "DEEPGRAM_API_KEY" in secrets_env_path().read_text()


def test_interactive_local_websocket_branch_runs_preflight(_isolate_env, monkeypatch):
    _force_tty(monkeypatch)
    _patch_devices(monkeypatch, [(0, "Mic", 16000), (1, "BlackHole", 16000)])
    _patch_inputs(
        monkeypatch,
        [
            "Mic",  # Me by name
            "BlackHole",  # Them by name
            "y",  # local STT? yes
            "y",  # use websocket socket? yes
            "/tmp/stt.sock",  # socket path
            "",  # categories (none)
            "Me",  # me name
            "Them",  # them label
        ],
    )

    preflight_called = {}

    def fake_preflight(stt, secrets):
        preflight_called["stt"] = stt

    monkeypatch.setattr(init_mod, "_run_preflight", fake_preflight)
    rc = init_mod.main([])  # preflight path exercised via patched _run_preflight
    assert rc == 0
    assert preflight_called["stt"]["service"] == "websocket"
    assert preflight_called["stt"]["ws_socket"] == "/tmp/stt.sock"

    from onoats.config import config_toml_path

    cfg = _load_toml(config_toml_path())
    assert cfg["stt"]["service"] == "websocket"
    assert cfg["stt"]["ws_socket"] == "/tmp/stt.sock"


def test_interactive_warns_when_loopback_absent(_isolate_env, monkeypatch, capsys):
    _force_tty(monkeypatch)
    _patch_devices(monkeypatch, [(0, "Mic A", 16000), (1, "Mic B", 16000)])
    _patch_inputs(
        monkeypatch,
        [
            "0",  # Me
            "1",  # Them
            "n",  # hosted
            "",  # model
            "z" * 40,  # key
            "",  # categories
            "Me",  # me
            "Them",  # them
        ],
    )
    rc = init_mod.main(["--no-preflight"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "no system-loopback device detected" in out


def test_interactive_rejects_same_device(_isolate_env, monkeypatch):
    _force_tty(monkeypatch)
    _patch_devices(monkeypatch, [(0, "Mic", 16000), (1, "BlackHole", 16000)])
    _patch_inputs(
        monkeypatch,
        [
            "0",  # Me
            "0",  # Them == Me (rejected → re-prompt)
            "1",  # Them re-pick (different)
            "n",  # hosted
            "",  # model
            "q" * 40,  # key
            "",  # categories
            "Me",
            "Them",
        ],
    )
    rc = init_mod.main(["--no-preflight"])
    assert rc == 0
    from onoats.config import config_toml_path

    cfg = _load_toml(config_toml_path())
    assert cfg["devices"]["mic"] == "Mic"
    assert cfg["devices"]["system"] == "BlackHole"


def test_secrets_env_mode_0600_interactive(_isolate_env, monkeypatch):
    _force_tty(monkeypatch)
    _patch_devices(monkeypatch, [(0, "Mic", 16000), (1, "BlackHole", 16000)])
    _patch_inputs(
        monkeypatch,
        ["0", "1", "n", "", "k" * 40, "", "Me", "Them"],
    )
    assert init_mod.main(["--no-preflight"]) == 0
    from onoats.config import secrets_env_path

    mode = stat.S_IMODE(secrets_env_path().stat().st_mode)
    assert mode == 0o600
