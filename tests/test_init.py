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
    # Simulate a hand-edited (or wizard-written) language, then re-run with no
    # flags (non-TTY) — must keep the prior categories + me-name + language.
    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(
        path.read_text().replace(
            '[stt]\nservice = "', '[stt]\nlanguage = "auto"\nservice = "'
        )
    )
    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path())
    assert "work" in cfg["categories"]["set"]
    assert cfg["speakers"]["me"] == "Ann"
    assert cfg["stt"]["language"] == "auto"


def test_rerun_preserves_launchd_label_and_app_section(_isolate_env, monkeypatch):
    """Round-2 finding 12: `onoats init` regenerates config.toml from only the
    field set it prompts for, so a re-run silently DROPPED a hand-configured
    `[stt].launchd_label` and the whole `[app]` section — disabling STT
    self-healing and launch-at-login on a routine rerun."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(
        path.read_text().replace(
            "[stt]\n",
            '[stt]\nlaunchd_label = "pipecat.stt-server.nemotron"\n',
        )
        + "\n[app]\nlaunch_at_login = true\n"
    )

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path())
    assert cfg["stt"]["launchd_label"] == "pipecat.stt-server.nemotron"
    assert cfg["app"]["launch_at_login"] is True


def test_rerun_preserves_a_quoted_launch_at_login_value(_isolate_env, monkeypatch):
    """Round-3 finding 7 (superseded by the deep-review verbatim-preservation
    fix): a `launch_at_login = "false"` (a spelling `ConfigStore.readValue`
    accepts, since it strips surrounding quotes, and which `tomllib` hands
    back as a string) must survive a rerun rather than being dropped —
    silently RE-ENABLING a login item the user had explicitly disabled, since
    absent means "take no action" and leaves an existing registration alone.
    `[app]` is now round-tripped as the original source text verbatim (see
    `_extract_raw_section`), so the quoted spelling is preserved exactly
    rather than normalized to a bare boolean — Python never reads this
    section at runtime, so there is nothing to normalize it FOR.
    """
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(path.read_text() + '\n[app]\nlaunch_at_login = "false"\n')

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path())
    # Preserved verbatim, quotes and all — still present, still resolves to
    # the same (Swift-side) meaning of FALSE either way.
    assert cfg["app"]["launch_at_login"] == "false"


def test_rerun_preserves_the_app_section_byte_for_byte(_isolate_env, monkeypatch):
    """Deep-review finding: Python re-rendering `[app]` from the parsed dict
    had to model `ConfigStore.readValue`'s (the Swift reader) exact quote-
    and whitespace-trimming semantics — an unversioned, undocumented
    contract shared between two independent parsers with no shared schema
    or cross-language test. `[app]` is now round-tripped as the literal
    source text (`_extract_raw_section`), so it survives a rerun byte for
    byte, INCLUDING a key Python's renderer has never heard of — proving
    the fix no longer needs to know anything about Swift's parsing rules at
    all."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    app_block = '[app]\nlaunch_at_login = "false"\nsome_future_swift_only_key = 42\n'
    path.write_text(path.read_text() + "\n" + app_block)

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    raw = path.read_text()
    assert app_block.strip() in raw

    cfg = _load_toml(config_toml_path())
    assert cfg["app"]["launch_at_login"] == "false"
    assert cfg["app"]["some_future_swift_only_key"] == 42


def test_rerun_preserves_an_unrecognized_launch_at_login_value(
    _isolate_env, monkeypatch
):
    """Same root cause as above: an unrecognized spelling (Python-style
    `True`) is treated as absent by the Swift reader, but deleting the user's
    line is strictly worse than round-tripping it — the value is preserved
    verbatim so a typo is still visible and fixable in the file."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(path.read_text() + '\n[app]\nlaunch_at_login = "True"\n')

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path())
    assert cfg["app"]["launch_at_login"] == "True"


def test_rerun_keeps_a_whitespace_padded_launch_at_login_inert(
    _isolate_env, monkeypatch
):
    """Round-4 finding 2: a quoted, whitespace-padded value like
    `launch_at_login = " false "` reads back on the Swift side as the
    literal string " false " (`ConfigStore.readValue` trims OUTSIDE a quoted
    value but preserves whitespace INSIDE one) — which its exact "true"/
    "false" check treats as unrecognized, i.e. absent/inert, same as any
    other typo. The carry-over used to `.strip()` before comparing, so it
    normalized this to bare `false` on rerun — a spelling the Swift side
    DOES recognize, turning a previously-inert value into a real unregister
    on next app launch. It must round-trip verbatim (still inert) instead.
    """
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(path.read_text() + '\n[app]\nlaunch_at_login = " false "\n')

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path())
    # Preserved verbatim (still the padded string, not normalized to the
    # bare boolean `false`) — exactly as inert on the Swift side as before.
    assert cfg["app"]["launch_at_login"] == " false "


def test_rerun_escapes_a_control_character_in_launch_at_login(
    _isolate_env, monkeypatch
):
    """Round-4 finding 3: an existing `[app].launch_at_login` string that
    contains a raw control character (as `tomllib` hands back for a TOML
    string that itself used a `\\n` escape) must be re-escaped, not emitted
    literally — `_toml_escape` previously only escaped backslash and quote,
    so a raw newline broke out of its own quoted string, producing a
    config.toml that `tomllib` then refuses to parse on the next load."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    # A TOML basic string containing an escaped newline — tomllib hands
    # this back to Python as the two-character... actually one-character
    # (U+000A) real newline, not the two-character sequence "\n".
    path.write_text(path.read_text() + '\n[app]\nlaunch_at_login = "bad\\nvalue"\n')

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    # The regenerated file must still be valid TOML (this alone would raise
    # tomllib.TOMLDecodeError before the fix, since a raw newline inside a
    # basic string is a syntax error).
    cfg = _load_toml(config_toml_path())
    assert cfg["app"]["launch_at_login"] == "bad\nvalue"
    # And the raw file must not contain a literal, unescaped newline inside
    # the quoted value.
    raw = path.read_text()
    line = next(ln for ln in raw.splitlines() if ln.startswith("launch_at_login"))
    assert "\\n" in line


def test_rerun_escapes_a_del_character_in_launch_at_login(_isolate_env, monkeypatch):
    """Round-5 finding 1: TOML's basic-string grammar forbids U+007F (DEL)
    literal, in addition to the U+0000-U+001F range — `_toml_escape`'s final
    escape condition only checked `ord(c) < 0x20`, so a stored value
    containing a DEL byte (e.g. `tomllib`'s decode of a source string that
    itself used a `\\u007f` escape) round-tripped straight through unescaped,
    producing a `config.toml` that is itself invalid TOML on the next
    `tomllib` parse."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(path.read_text() + '\n[app]\nlaunch_at_login = "bad\\u007fvalue"\n')

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    # The regenerated file must still be valid TOML (this alone would raise
    # tomllib.TOMLDecodeError before the fix, since a raw DEL byte inside a
    # basic string is forbidden).
    cfg = _load_toml(config_toml_path())
    assert cfg["app"]["launch_at_login"] == "bad\x7fvalue"
    raw = path.read_text()
    line = next(ln for ln in raw.splitlines() if ln.startswith("launch_at_login"))
    assert "\x7f" not in line
    assert "\\u007f" in line


def test_rerun_carries_a_whitespace_padded_launchd_label(_isolate_env, monkeypatch):
    """Round-5 finding 2: `OnoatsConfig.stt_launchd_label` (the runtime
    reader) strips whitespace before allowlist-validating a label, so a
    stored `" pipecat.stt-server "` resolves and self-heals correctly at
    runtime. The `onoats init` carry-over validated the RAW, unstripped
    string instead — disagreeing with the runtime reader — so it rejected
    and silently DROPPED the same value on every `onoats init` rerun,
    disabling self-healing the user had working."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(
        path.read_text().replace(
            "[stt]\n",
            '[stt]\nlaunchd_label = " pipecat.stt-server.nemotron "\n',
        )
    )

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path())
    assert cfg["stt"]["launchd_label"] == "pipecat.stt-server.nemotron"


def test_rerun_drops_a_schema_invalid_launchd_label(_isolate_env, monkeypatch):
    """Round-3 finding 8: the carry-over copied `[stt].launchd_label` into the
    regenerated file WITHOUT running it through `validate_launchd_label`,
    unlike the runtime reader (`OnoatsConfig.stt_launchd_label`), which treats
    a non-conforming value as absent. Such a value is therefore already inert
    at runtime; copying it through unchecked could corrupt the regenerated
    config.toml (a raw newline breaks out of its own line) or crash
    `_toml_escape` (a non-string TOML value)."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    # A label with a shell-ish character the allowlist rejects.
    path.write_text(
        path.read_text().replace("[stt]\n", '[stt]\nlaunchd_label = "bad label;rm"\n')
    )

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path())
    assert "launchd_label" not in cfg["stt"]


def test_rerun_drops_a_non_string_launchd_label(_isolate_env, monkeypatch):
    """Same fix, the shape that used to raise: `_toml_escape` calls
    `str.replace`, so a TOML integer stored under `launchd_label` crashed
    `onoats init` outright once the round-2 carry-over started copying it."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(path.read_text().replace("[stt]\n", "[stt]\nlaunchd_label = 42\n"))

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path())
    assert "launchd_label" not in cfg["stt"]


def test_rerun_carries_an_empty_launchd_label_silently(
    _isolate_env, monkeypatch, capsys
):
    """Round-6 finding 2: an empty/whitespace-only `[stt].launchd_label` is
    "absent", not "malformed" — `OnoatsConfig.stt_launchd_label` (the runtime
    reader) normalizes it via `val or None` *before* calling
    `validate_launchd_label`, so it never warns for this value. The carry-over
    used to call `validate_launchd_label(carried_label.strip())` directly,
    which fails the label regex on `""` and both logs a spurious "malformed"
    warning and prints a misleading note here, on every `onoats init` re-run
    of an install that simply never set a label."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(
        path.read_text().replace("[stt]\n", '[stt]\nlaunchd_label = "   "\n')
    )

    _force_tty(monkeypatch, value=False)
    capsys.readouterr()  # drain output from the first run above
    assert init_mod.main(["--no-preflight"]) == 0

    out = capsys.readouterr().out.lower()
    assert "malformed" not in out
    assert "launchd_label" not in out

    cfg = _load_toml(config_toml_path())
    assert "launchd_label" not in cfg["stt"]


def test_rerun_without_the_new_keys_writes_no_empty_app_section(
    _isolate_env, monkeypatch
):
    assert init_mod.main(["--no-preflight"]) == 0
    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path_for_test())
    assert "app" not in cfg
    assert "launchd_label" not in cfg["stt"]


def config_toml_path_for_test():
    from onoats.config import config_toml_path

    return config_toml_path()


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
            "",  # data dir (default XDG)
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
            "auto",  # STT language → [stt].language
            "",  # categories (none)
            "Me",  # me name
            "Them",  # them label
            "",  # data dir (default XDG)
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
    assert cfg["stt"]["language"] == "auto"


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
            "",  # data dir (default XDG)
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
            "",  # data dir (default XDG)
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
        ["0", "1", "n", "", "k" * 40, "", "Me", "Them", ""],
    )
    assert init_mod.main(["--no-preflight"]) == 0
    from onoats.config import secrets_env_path

    mode = stat.S_IMODE(secrets_env_path().stat().st_mode)
    assert mode == 0o600
