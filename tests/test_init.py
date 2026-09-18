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


def test_extract_raw_section_rejects_a_boundary_it_cannot_trust():
    """Round-2 review-gauntlet finding: `_extract_raw_section` is a lexical
    line-scanner, not a real TOML parser — it ends a section at the first
    line that merely looks like `[...]`. A multi-line value's continuation
    line that happens to match that shape would end the section early,
    splicing a truncated fragment into the regenerated file. The extracted
    block must now be validated as standalone TOML before being trusted;
    when it isn't (as here, since the scanner cuts the triple-quoted string
    off mid-value, leaving it unterminated), the section reports as absent
    rather than corrupt."""
    text = '[app]\nlaunch_at_login = true\nweird = """\n[oops]\n"""\n'
    assert init_mod._extract_raw_section(text, "app") is None


def test_extract_raw_section_accepts_a_normal_single_line_section():
    text = '[app]\nlaunch_at_login = "false"\nsome_future_swift_only_key = 42\n'
    assert init_mod._extract_raw_section(text, "app") == text.rstrip("\n")


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


def test_section_header_name_tolerates_whitespace_variant():
    """Codex finding (round 4): both TOML (`tomllib.loads("[ app ]\\n...")`
    parses fine) and the Swift `ConfigStore` reader trim whitespace inside
    the brackets of a section header (comment in ConfigStore.swift:
    "tolerate hand-edited [ stt ]"). `_section_header_name` must agree,
    matching `[app]` and `[ app ]` (and other interior-whitespace spellings)
    identically."""
    assert init_mod._section_header_name("[app]") == "app"
    assert init_mod._section_header_name("[ app ]") == "app"
    assert init_mod._section_header_name("[  app  ]") == "app"
    assert init_mod._section_header_name("[stt]") == "stt"
    assert init_mod._section_header_name("not a header") is None


def test_rerun_carries_an_app_section_with_whitespace_variant_header(
    _isolate_env, monkeypatch
):
    """Codex finding (round 4, unreconciled): `_extract_raw_section`'s
    exact-string header comparison (`line.strip() == "[app]"`) did not
    tolerate a hand-edited `[ app ]` header, even though both TOML and the
    Swift `ConfigStore` reader accept it — so re-running `onoats init`
    against a config using that spelling silently dropped the entire `[app]`
    block (including `launch_at_login`), reverting an installed launch-at-
    login preference with no warning. Verified this fails pre-fix: with the
    old exact-string check, `start` is never found for `[ app ]`, so
    `app_raw_block` is `None` and the whole section is omitted."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(path.read_text() + "\n[ app ]\nlaunch_at_login = true\n")

    _force_tty(monkeypatch, value=False)
    assert init_mod.main(["--no-preflight"]) == 0

    cfg = _load_toml(config_toml_path())
    assert cfg["app"]["launch_at_login"] is True


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


def test_section_header_grammar_is_shared_by_start_and_end_scans():
    """Round-5 finding 14: `_extract_raw_section`'s end-of-section scan and
    `_section_header_name`'s start-of-section lookup used two regexes that
    disagreed on the degenerate `[]` — the former required a non-empty
    interior, the latter did not. One grammar now serves both."""
    text = "[app]\nx = 1\n[]\n[stt]\ny = 2\n"
    # The `[]` line ends the `[app]` section under both readings now.
    assert init_mod._extract_raw_section(text, "app") == "[app]\nx = 1"
    assert init_mod._section_header_name("[]") == ""
    assert not hasattr(init_mod, "_SECTION_HEADER_RE")


# ---------------------------------------------------------------------------
# Round-6 security regression.
# ---------------------------------------------------------------------------


def test_secrets_env_values_cannot_inject_a_second_key(tmp_path):
    """Round-6 security finding: `_write_secrets_env` rendered each secret as
    a raw `f"{k}={v}"` line without ever checking that the value could BE one
    line. A newline in an argv-supplied value (`--deepgram-key`) therefore
    wrote a second, attacker-chosen `KEY=value` pair into the 0600 file that
    no prompt had accepted — and the next merge read it back as a real
    secret. Values are now written as dotenv double-quoted literals, so every
    value round-trips byte-for-byte and none of them can be a line of their
    own."""
    from dotenv import dotenv_values

    from onoats.init import _write_secrets_env

    path = tmp_path / "secrets.env"
    hostile = {
        "DEEPGRAM_API_KEY": "legit\nINJECTED_KEY=pwned",
        # Silently truncated at the comment marker before the fix.
        "OPENAI_API_KEY": "tail # not-a-comment",
        "ASSEMBLYAI_API_KEY": 'quote" and \\backslash',
    }
    _write_secrets_env(path, hostile, merge_existing=False)

    got = dict(dotenv_values(path))
    assert "INJECTED_KEY" not in got
    assert got == hostile
    assert oct(path.stat().st_mode)[-3:] == "600"

    # And the injected pair does not reappear through the merge path either.
    _write_secrets_env(path, {"STT_WS_URI": "ws://127.0.0.1:8765"}, merge_existing=True)
    merged = dict(dotenv_values(path))
    assert "INJECTED_KEY" not in merged
    assert merged["DEEPGRAM_API_KEY"] == "legit\nINJECTED_KEY=pwned"


def test_secrets_env_values_are_not_env_interpolated(tmp_path, monkeypatch):
    """Round-7 security finding: round 6's newline fix double-quotes every
    value (the one dotenv form that round-trips escapes), and double quotes
    are also the one form `dotenv_values` expands `${VAR}` inside by default.
    A secret containing `${INJECTED}` therefore read back as an unrelated env
    var's value at every read — and the next merge rewrote the file with that
    substitution baked in, so the real secret was destroyed on disk. Both
    readers (`config._load_secrets` and this module's merge) now pass
    `interpolate=False`."""
    from onoats.config import _load_secrets
    from onoats.init import _write_secrets_env

    monkeypatch.setenv("INJECTED", "PWNED")
    path = tmp_path / "secrets.env"
    secret = "sk-${INJECTED}-tail"
    _write_secrets_env(path, {"DEEPGRAM_API_KEY": secret}, merge_existing=False)

    # The config reader hands the client the stored bytes, not the env's.
    assert _load_secrets(path)["DEEPGRAM_API_KEY"] == secret

    # And the merge path does not rewrite the file with the substitution.
    _write_secrets_env(path, {"STT_WS_URI": "ws://127.0.0.1:8765"}, merge_existing=True)
    assert _load_secrets(path)["DEEPGRAM_API_KEY"] == secret
    assert "PWNED" not in path.read_text(encoding="utf-8")


def test_secrets_env_drops_a_malformed_key(tmp_path):
    """A key is a bare token on the left of the `=` — unlike a value it
    cannot be escaped into safety, so a malformed one is dropped."""
    from dotenv import dotenv_values

    from onoats.init import _write_secrets_env

    path = tmp_path / "secrets.env"
    _write_secrets_env(
        path, {"GOOD_KEY": "v", "BAD KEY=X": "v", "9BAD": "v"}, merge_existing=False
    )
    assert dict(dotenv_values(path)) == {"GOOD_KEY": "v"}


def test_secrets_env_refuses_to_write_an_unreadable_file(tmp_path, monkeypatch):
    """Round-9 finding: `_env_quote` was the sole guarantee that a rendered
    line is parseable — a claim about an escaping function, re-argued from
    scratch every time a new hostile character turns up. The blast radius of
    being wrong once is total and silent: `dotenv_values` abandons the file on
    a line it cannot parse, `config._load_secrets` returns `{}`, every STT
    credential vanishes at runtime with no error, and the next `onoats init`
    merge reads the same `{}` and rewrites the file *without* them.

    The invariant is now checked against the bytes, not asserted about the
    escaper. Simulated by breaking `_env_quote` itself, because no input
    defeats the real one (verified over a 3-character cross-product of every
    hostile character, including NUL, newline, backslash and quotes)."""
    from onoats import init as init_mod
    from onoats.init import _write_secrets_env

    path = tmp_path / "secrets.env"
    _write_secrets_env(path, {"STT_WS_TOKEN": "keepme"}, merge_existing=False)
    before = path.read_bytes()

    monkeypatch.setattr(init_mod, "_env_quote", lambda v: f'"{v}"')
    with pytest.raises(RuntimeError, match="refusing to write secrets.env"):
        _write_secrets_env(path, {"STT_WS_TOKEN": 'a"\nEVIL=1'}, merge_existing=False)
    # The existing secrets survive: the refusal happens before the truncate.
    assert path.read_bytes() == before


def test_env_quote_round_trips_every_hostile_character(tmp_path):
    """The property `_assert_secrets_round_trip` enforces, asserted directly
    against the real escaper so a regression in it is attributed here rather
    than surfacing as a mysterious `onoats init` failure."""
    import itertools

    from dotenv import dotenv_values

    from onoats.init import _write_secrets_env

    path = tmp_path / "secrets.env"
    alphabet = ("a", "\\", '"', "'", "\n", "\r", "#", " ", "=", "$", "{", "\t", "\x00")
    for combo in itertools.product(alphabet, repeat=2):
        value = "".join(combo)
        _write_secrets_env(path, {"STT_WS_TOKEN": value}, merge_existing=False)
        assert dotenv_values(path, interpolate=False)["STT_WS_TOKEN"] == value, value


@pytest.mark.parametrize("literal", ("42", "true", "[1, 2]"), ids=str)
def test_rerun_notes_a_non_string_launchd_label(
    _isolate_env, monkeypatch, capsys, literal
):
    """Round-9 finding: the "ignoring malformed [stt].launchd_label" note was
    guarded on `carried_str` — the string-only projection, which is `None` for
    exactly the typed-TOML case the note's own comment cites as its motivating
    example. So `launchd_label = 42` / `= true` was dropped in total silence:
    the one shape where the user is most likely to believe self-healing is
    configured and be wrong, and the shape a `true` makes especially plausible
    (it reads like an on switch). The value is still dropped — that half was
    always right — it is now also reported."""
    assert init_mod.main(["--no-preflight"]) == 0

    from onoats.config import config_toml_path

    path = config_toml_path()
    path.write_text(
        path.read_text().replace("[stt]\n", f"[stt]\nlaunchd_label = {literal}\n")
    )

    _force_tty(monkeypatch, value=False)
    capsys.readouterr()  # drain the first run's output
    assert init_mod.main(["--no-preflight"]) == 0

    out = capsys.readouterr().out
    assert "malformed [stt].launchd_label" in out, out
    cfg = _load_toml(config_toml_path())
    assert "launchd_label" not in cfg["stt"]
