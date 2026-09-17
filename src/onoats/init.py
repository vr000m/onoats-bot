"""``onoats init`` — guided first-run setup.

Writes a consolidated ``$XDG_CONFIG_HOME/onoats/config.toml`` plus a
``0600 secrets.env`` (STT secrets only — never the repo). Idempotent: re-running
re-reads the existing config and offers the current values as defaults.

Interactive flow (a TTY with no scripted flags):
  1. Devices    — enumerate + pick Me (mic) and Them (system/loopback) input
                  devices, reusing ``onoats.config.audio_devices``. Validates
                  16 kHz support, rejects the same device for both, warns when
                  no loopback-looking device is present.
  2. STT        — choose **local vs hosted FIRST**, then configure:
                  local  → Whisper-MLX OR the stt_server websocket socket;
                  hosted → Deepgram + API key.
                  Then run the existing reachability/preflight.
  3. Categories — define the set (default ``uncategorized``).
  4. Speakers   — Me name / Them label (render-only display labels).
  5. Secrets    — capture STT secrets → ``0600 secrets.env`` (NO LLM keys).
  6. Dictionary — seed ``dictionary.txt`` (import existing or start empty).
  7. Write      — ``config.toml`` with [devices] [stt] [speakers] [categories]
                  [tuning].

Non-interactive flow (``--categories`` / ``--mic`` / ``--system`` / ``--stt`` /
``--me-name`` … supplied, or a non-TTY stdin) writes a valid config headlessly
and never blocks on input.

Env vars still override the written file at runtime (precedence unchanged):
process env > config.toml/secrets.env > built-in default.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

from onoats.config import (
    config_toml_path,
    load_config,
    normalize_launchd_label,
    secrets_env_path,
)

# --- STT backend identifiers written into config.toml [stt].service ---------
# "local" splits into "whisper" (MLX/CPU) or "websocket" (stt_server socket);
# "hosted" maps to "deepgram".
_LOCAL_WHISPER = "whisper"
_LOCAL_WEBSOCKET = "websocket"
_HOSTED_DEEPGRAM = "deepgram"


# ---------------------------------------------------------------------------
# Small IO helpers (interactive) — all guarded by an explicit interactive flag
# ---------------------------------------------------------------------------


def _prompt(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{text}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raw = ""
    return raw or (default or "")


def _confirm(text: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    raw = _prompt(f"{text} ({d})")
    if not raw:
        return default
    return raw.lower().startswith("y")


# ---------------------------------------------------------------------------
# Device enumeration (reuses onoats.config.audio_devices)
# ---------------------------------------------------------------------------


def _enumerate_inputs() -> list[tuple[int, str, int]]:
    """Return ``[(index, name, default_rate), ...]`` for every input device.

    Imported lazily so ``onoats init --help`` never imports pyaudio.
    """
    import pyaudio

    from onoats.config.audio_devices import _enumerate_input_devices

    pa = pyaudio.PyAudio()
    try:
        return _enumerate_input_devices(pa)
    finally:
        pa.terminate()


_LOOPBACK_HINTS = ("blackhole", "loopback", "soundflower", "aggregate", "vb-cable")


def _looks_like_loopback(name: str) -> bool:
    low = name.casefold()
    return any(h in low for h in _LOOPBACK_HINTS)


def _resolve_device_by_name(name: str) -> str | None:
    """Best-effort validate a device name → return the validated name or None.

    Uses the picker's validator so a 16 kHz-incapable device is rejected.
    """
    if not name:
        return None
    from onoats.config.audio_devices import validate_audio_device

    idx = validate_audio_device(name, "device", need_input=True)
    return name if idx is not None else None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def _pick_devices_interactive(
    inputs: list[tuple[int, str, int]],
    default_mic: str | None,
    default_system: str | None,
) -> tuple[str | None, str | None]:
    """Interactive Me/Them device selection. Returns (mic_name, system_name)."""
    print("\n--- Audio devices ---")
    if not inputs:
        print("  (no input devices found)")
        return default_mic, default_system
    for idx, name, rate in inputs:
        flag = " [loopback?]" if _looks_like_loopback(name) else ""
        print(f"  [{idx}] {name} ({rate} Hz){flag}")

    if not any(_looks_like_loopback(n) for _, n, _ in inputs):
        # Device pickers configure the PortAudio path only; the native socket
        # path (macOS 14.4+) captures system audio without any loopback driver.
        print(
            "  NOTE: no system-loopback device detected (e.g. BlackHole). "
            "On the PortAudio path, 'Them' capture needs one. On macOS 14.4+ "
            "prefer the native capture path instead — AUDIO_SOURCE=socket, "
            "no loopback driver required (see native/README.md)."
        )

    def _pick(label: str, default: str | None) -> str | None:
        choice = _prompt(f"Select {label} device (index or name)", default)
        if not choice:
            return default
        # numeric index → resolve to that device's name
        try:
            i = int(choice)
            match = next((n for idx2, n, _ in inputs if idx2 == i), None)
            if match is None:
                print(f"  invalid index {i}, keeping {default!r}")
                return default
            return match
        except ValueError:
            return choice

    mic = _pick("Me (microphone)", default_mic)
    system = _pick("Them (system/loopback)", default_system)
    if mic and system and mic == system:
        print(
            f"  ERROR: Me and Them resolved to the same device ({mic!r}). Choose separate devices."
        )
        system = _pick("Them (system/loopback) — pick a DIFFERENT device", None)
    return mic, system


def _configure_stt_interactive(existing: dict) -> tuple[dict, dict]:
    """Interactive STT setup. Returns (stt_table, secrets_to_write)."""
    print("\n--- Speech-to-text ---")
    local = _confirm(
        "Use LOCAL speech-to-text (no cloud)? Yes = Whisper/stt_server, No = hosted Deepgram",
        default=True,
    )
    stt: dict = {}
    secrets: dict = {}
    if local:
        use_ws = _confirm(
            "Use the local stt_server websocket socket (vs in-process Whisper-MLX)?",
            default=False,
        )
        if use_ws:
            stt["service"] = _LOCAL_WEBSOCKET
            sock = _prompt(
                "stt_server socket path (blank to use STT_WS_* env at runtime)",
                existing.get("ws_socket"),
            )
            if sock:
                stt["ws_socket"] = sock
        else:
            stt["service"] = _LOCAL_WHISPER
            model = _prompt(
                "Whisper model (blank = large-v3-turbo on MLX / base on CPU)",
                existing.get("model"),
            )
            if model:
                stt["model"] = model
        # Consumed by both local backends (whisper + websocket); Deepgram
        # ignores it, so the prompt lives in the `local` branch only.
        lang = _prompt(
            "STT language (blank = en, 'auto' = detect)",
            existing.get("language"),
        )
        if lang:
            stt["language"] = lang
    else:
        stt["service"] = _HOSTED_DEEPGRAM
        model = _prompt(
            "Deepgram model (blank = Deepgram default)", existing.get("model")
        )
        if model:
            stt["model"] = model
        key = _prompt("Deepgram API key (stored 0600 in secrets.env)")
        if key:
            secrets["DEEPGRAM_API_KEY"] = key
        # Deepgram doesn't consume the language, but carry an existing value
        # forward so switching backends and back doesn't silently drop it.
        if existing.get("language"):
            stt["language"] = existing["language"]
    return stt, secrets


def _run_preflight(stt: dict, secrets: dict) -> None:
    """Run the existing STT reachability/preflight (best-effort, non-fatal).

    Only the websocket backend has a real network preflight; whisper/deepgram
    have no startup handshake. A preflight failure is reported but does not
    abort init — the user can fix the endpoint and re-run.
    """
    service = stt.get("service")
    if service != _LOCAL_WEBSOCKET:
        return
    import asyncio

    from onoats.runtime import (
        SttPreflightError,
        _display_target,
        _preflight_stt_ws,
        _resolve_stt_ws_target,
    )

    env = dict(os.environ)
    if stt.get("ws_socket"):
        env["STT_WS_SOCKET"] = stt["ws_socket"]
    kwargs = _resolve_stt_ws_target(env)
    target = _display_target(kwargs)
    print(f"  preflight: probing stt_server at {target} …")
    try:
        asyncio.run(_preflight_stt_ws(kwargs, target))
        print("  preflight: OK")
    except SttPreflightError as exc:
        print(f"  preflight: FAILED — {exc}", file=sys.stderr)


def _seed_dictionary(import_path: str | None) -> Path:
    """Seed ``dictionary.txt`` — import an existing file or create empty."""
    from onoats._vendor.dictionary import Dictionary, resolve_dictionary_path

    dest = resolve_dictionary_path()
    if dest.exists():
        return dest
    if import_path:
        src = Path(import_path).expanduser()
        if src.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            return dest
    Dictionary(path=dest).ensure_exists()
    return dest


# ---------------------------------------------------------------------------
# config.toml / secrets.env writers
# ---------------------------------------------------------------------------


_TOML_CONTROL_ESCAPES = {
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


def _toml_escape(value: str) -> str:
    """Escape ``value`` for embedding in a TOML basic (quoted) string.

    Must escape every character TOML's basic-string grammar forbids literal,
    not just backslash and quote: `tomllib` hands back an *actual* control
    character (e.g. a real newline) for any TOML string that itself used a
    `\\n`/`\\t`/etc. escape. Round-tripping that character back out unescaped
    (the previous behavior here) emits a raw newline into `config.toml`,
    producing a file `tomllib` then refuses to parse on the next load —
    silently losing the user's settings. Escape backslash first so later
    replacements don't double-escape the backslashes they introduce, then
    quote, then the named single-character TOML escapes, then any remaining
    control character as `\\uXXXX` (covers e.g. NUL, ESC — TOML forbids them
    literal but has no dedicated short escape).
    """
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    for ch, escape in _TOML_CONTROL_ESCAPES.items():
        out = out.replace(ch, escape)
    # TOML's basic-string grammar forbids U+0000-U+0008, U+000A-U+001F, AND
    # U+007F (DEL) literal — not just the < 0x20 range. A DEL byte slipping
    # through here round-trips into a `config.toml` `tomllib` then refuses to
    # parse, the exact silent-settings-loss failure this helper exists to
    # prevent, just for one more code point than the range check below names.
    return "".join(
        f"\\u{ord(c):04x}" if ord(c) < 0x20 or ord(c) == 0x7F else c for c in out
    )


_SECTION_HEADER_RE = re.compile(r"^\[[^\]]+\]\s*$")
# Captures the bracket interior so a header's *name* can be compared with
# surrounding whitespace ignored — both TOML (`tomllib.loads("[ app ]\n...")`
# parses fine) and the Swift `ConfigStore` reader (`readValue`/`writeValue`
# in native/onoats-menubar/Sources/ConfigStore.swift both
# `.trimmingCharacters` the bracket interior, comment: "tolerate hand-edited
# [ stt ]") accept a hand-edited `[ app ]`. An exact-string header match
# here disagreed with both and silently treated such a file as having no
# `[app]` section at all, dropping the block (including `launch_at_login`)
# on the next `onoats init` regeneration.
_SECTION_HEADER_NAME_RE = re.compile(r"^\[\s*([^\]]*?)\s*\]\s*$")


def _section_header_name(line: str) -> str | None:
    """Return a stripped line's section name if it is a ``[...]`` header.

    Whitespace-tolerant around the name (``[ app ]`` -> ``"app"``), matching
    ``ConfigStore``'s own header parsing. Returns ``None`` for a non-header
    line.
    """
    m = _SECTION_HEADER_NAME_RE.match(line)
    return m.group(1) if m else None


def _extract_raw_section(text: str, section: str) -> str | None:
    """Return ``[section]``'s literal source lines from ``text`` verbatim
    (header through the line before the next ``[...]`` header, or EOF), or
    ``None`` if the section is absent.

    Used for ``[app]`` (deep-review finding): it is Swift-only, and Python
    re-rendering it from the parsed dict had to reason about
    ``ConfigStore.readValue``'s (the Swift reader) exact quote- and
    whitespace-trimming semantics to avoid corrupting a value Python does
    not own — two independent parsers sharing an unversioned contract with
    no shared schema. Round-tripping the original text instead removes that
    cross-language coupling entirely: whatever the user's file said, byte
    for byte (including any key besides ``launch_at_login`` a future Swift
    version adds), survives every ``onoats init`` regeneration.

    This is a lexical approximation of TOML (it ends the section at the
    first line that merely *looks* like ``[...]``), not a real TOML parser —
    only valid for a section holding single-line scalar assignments, which
    is the only shape ``ConfigStore`` (the section's sole writer) ever
    produces. A multi-line value (an array or triple-quoted string) whose
    continuation line happens to look like a section header would end the
    block early and splice a truncated fragment into the regenerated file.
    Bounded here rather than left as a silent trap: the extracted block is
    validated as a standalone TOML document before being returned, so a
    future value shape this scanner can't handle degrades to "treat the
    section as absent" (the caller re-renders `[app]` from the parsed dict
    instead, or omits it) rather than silently corrupting `config.toml`.
    """
    lines = text.splitlines()
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if _section_header_name(line.strip()) == section
        ),
        None,
    )
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _SECTION_HEADER_RE.match(lines[j].strip()):
            end = j
            break
    # Trailing blank lines are re-added by the caller's own section spacing
    # convention (one `lines.append("")` after every section) — stripping
    # them here avoids doubling up.
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    block = "\n".join(lines[start:end])
    try:
        tomllib.loads(block)
    except tomllib.TOMLDecodeError:
        # The line-scan's section boundary doesn't line up with a real TOML
        # section boundary (e.g. a multi-line value's continuation line was
        # mistaken for the next header) — the extracted text is not
        # trustworthy to splice verbatim. Report absence; the caller falls
        # back to its own handling of a missing `[app]` section.
        return None
    return block


def _render_config_toml(
    *,
    mic: str | None,
    system: str | None,
    stt: dict,
    speakers: dict,
    categories: list[str],
    tuning: dict,
    data_dir: str | None = None,
    app_raw_block: str | None = None,
) -> str:
    lines: list[str] = [
        "# onoats configuration — written by `onoats init`.",
        "# Env vars override these values at runtime (process env > config.toml > default).",
        "",
    ]
    if data_dir:
        lines.append("[storage]")
        lines.append(f'data_dir = "{_toml_escape(data_dir)}"')
        lines.append("")
    lines.append("[devices]")
    lines.append(f'mic = "{_toml_escape(mic)}"' if mic else '# mic = "..."')
    lines.append(f'system = "{_toml_escape(system)}"' if system else '# system = "..."')
    lines.append("")
    lines.append("[stt]")
    lines.append(f'service = "{_toml_escape(stt.get("service", "whisper"))}"')
    if stt.get("model"):
        lines.append(f'model = "{_toml_escape(stt["model"])}"')
    if stt.get("ws_socket"):
        lines.append(f'ws_socket = "{_toml_escape(stt["ws_socket"])}"')
    if stt.get("language"):
        lines.append(f'language = "{_toml_escape(stt["language"])}"')
    if stt.get("launchd_label"):
        lines.append(f'launchd_label = "{_toml_escape(stt["launchd_label"])}"')
    lines.append("")
    # `[app]` is Swift-only (the Python config reader never reads it), but it
    # still has to survive a regeneration — see the carry-over in `main()`,
    # which extracts it via `_extract_raw_section`. Preserved as the
    # original source text verbatim rather than re-rendered from a parsed
    # dict: re-rendering used to require Python to model
    # `ConfigStore.readValue`'s (the Swift reader) exact quote- and
    # whitespace-trimming semantics, which is exactly the two-parsers-one-
    # unversioned-contract coupling that once silently RE-ENABLED a login
    # item the user had explicitly disabled (a `launch_at_login = "false"`
    # spelling `tomllib` hands back as the string `"false"`, which an
    # `isinstance(..., bool)` check rejected, dropping the whole section).
    # Round-tripping the literal text needs no such model at all.
    if app_raw_block is not None:
        lines.extend(app_raw_block.split("\n"))
        lines.append("")
    lines.append("[speakers]")
    lines.append(f'me = "{_toml_escape(speakers.get("me", "Me"))}"')
    lines.append(f'them = "{_toml_escape(speakers.get("them", "Them"))}"')
    lines.append("")
    lines.append("[categories]")
    rendered = ", ".join(f'"{_toml_escape(c)}"' for c in categories)
    lines.append(f"set = [{rendered}]")
    lines.append("")
    lines.append("[tuning]")
    for key in ("silence_timeout_sec", "segment_hint_threshold", "audio_heartbeat_sec"):
        lines.append(f"{key} = {float(tuning[key])}")
    return "\n".join(lines).rstrip() + "\n"


def _write_config_toml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_secrets_env(path: Path, secrets: dict, *, merge_existing: bool) -> None:
    """Write ``secrets.env`` with mode 0600. Merges with existing values."""
    path.parent.mkdir(parents=True, exist_ok=True)
    merged: dict[str, str] = {}
    if merge_existing and path.exists():
        from dotenv import dotenv_values

        merged.update({k: v for k, v in dotenv_values(path).items() if v is not None})
    merged.update({k: v for k, v in secrets.items() if v})
    # The file is always (re)created below with 0600 perms, even when empty,
    # so the path exists with correct perms for later edits.
    lines = [
        "# onoats STT secrets — 0600. NEVER commit. STT secrets only, NO LLM keys.",
        *[f"{k}={v}" for k, v in merged.items()],
    ]
    # Create with restrictive perms from the start (avoid a readable window).
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, ("\n".join(lines).rstrip() + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="onoats init",
        description="Guided first-run setup: writes config.toml + 0600 secrets.env.",
    )
    parser.add_argument(
        "--categories",
        default=None,
        help="Comma-separated category set (non-interactive). e.g. work,personal",
    )
    parser.add_argument(
        "--mic", default=None, help="Me (microphone) device name (non-interactive)."
    )
    parser.add_argument(
        "--system",
        default=None,
        help="Them (system/loopback) device name (non-interactive).",
    )
    parser.add_argument(
        "--stt",
        default=None,
        choices=["local", "hosted", "whisper", "websocket", "deepgram"],
        help="STT backend (non-interactive). local|whisper, websocket, hosted|deepgram.",
    )
    parser.add_argument(
        "--stt-model", default=None, help="STT model override (non-interactive)."
    )
    parser.add_argument(
        "--ws-socket", default=None, help="stt_server websocket socket path."
    )
    parser.add_argument(
        "--deepgram-key",
        default=None,
        help="Deepgram API key → secrets.env (non-interactive).",
    )
    parser.add_argument(
        "--me-name", default=None, help="Render-only display label for 'me'."
    )
    parser.add_argument(
        "--them-name", default=None, help="Render-only display label for 'them'."
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        help=(
            "Recorder data root (non-interactive). Point at e.g. ~/koda-data so a "
            "downstream worker drains the same queue. Default: XDG "
            "($XDG_DATA_HOME/onoats)."
        ),
    )
    parser.add_argument(
        "--import-dictionary",
        default=None,
        help="Seed dictionary.txt from this existing file.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; write a valid config from flags + defaults only.",
    )
    parser.add_argument(
        "--config-path", default=None, help="Override config.toml path (testing)."
    )
    parser.add_argument(
        "--secrets-path", default=None, help="Override secrets.env path (testing)."
    )
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="Skip the STT reachability preflight.",
    )
    return parser


def _normalize_stt_flag(flag: str | None) -> str | None:
    if flag is None:
        return None
    mapping = {
        "local": _LOCAL_WHISPER,
        "whisper": _LOCAL_WHISPER,
        "websocket": _LOCAL_WEBSOCKET,
        "hosted": _HOSTED_DEEPGRAM,
        "deepgram": _HOSTED_DEEPGRAM,
    }
    return mapping.get(flag, flag)


def _any_scripted_flag(args: argparse.Namespace) -> bool:
    return any(
        v is not None
        for v in (
            args.categories,
            args.mic,
            args.system,
            args.stt,
            args.me_name,
            args.them_name,
            args.deepgram_key,
            args.stt_model,
            args.ws_socket,
            args.data_dir,
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    config_path = Path(args.config_path) if args.config_path else config_toml_path()
    secrets_path = Path(args.secrets_path) if args.secrets_path else secrets_env_path()

    # Load any existing config so re-running is idempotent (offer current values).
    existing = load_config(config_path=config_path, secrets_path=secrets_path)

    # Interactive only when on a TTY AND no scripted flags AND not forced off.
    interactive = (
        not args.non_interactive and not _any_scripted_flag(args) and sys.stdin.isatty()
    )

    if interactive:
        print("onoats init — guided setup\n")

    # ---- 1. devices ----
    default_mic = args.mic or existing.mic_device
    default_system = args.system or existing.system_device
    if interactive:
        inputs = _enumerate_inputs()
        mic, system = _pick_devices_interactive(inputs, default_mic, default_system)
    else:
        mic = _resolve_device_by_name(default_mic) if default_mic else default_mic
        system = (
            _resolve_device_by_name(default_system)
            if default_system
            else default_system
        )
        if mic and system and mic == system:
            print(
                f"Error: --mic and --system are the same device ({mic!r}).",
                file=sys.stderr,
            )
            return 1

    # ---- 2. STT (local vs hosted FIRST) ----
    existing_stt = existing.raw.get("stt", {})
    secrets: dict = {}
    if interactive:
        stt, secrets = _configure_stt_interactive(existing_stt)
    else:
        service = _normalize_stt_flag(args.stt) or (
            existing_stt.get("service") or _LOCAL_WHISPER
        )
        stt = {"service": service}
        model = args.stt_model or existing_stt.get("model")
        if model:
            stt["model"] = model
        ws_socket = args.ws_socket or existing_stt.get("ws_socket")
        if ws_socket:
            stt["ws_socket"] = ws_socket
        language = existing_stt.get("language")
        if language:
            stt["language"] = language
        if args.deepgram_key:
            secrets["DEEPGRAM_API_KEY"] = args.deepgram_key

    # Carry over config.toml keys `onoats init` never prompts for. The
    # renderer rebuilds the file from scratch out of the prompted field set
    # only, so re-running `onoats init` on an already-configured install used
    # to silently DROP `[stt].launchd_label` and the whole `[app]` section —
    # disabling STT self-healing and launch-at-login for a user who had set
    # them up by hand. Applies to both the interactive and flag paths, neither
    # of which offers these keys.
    #
    # Two carry-over mechanisms coexist here on purpose, not by accident.
    # `[app]` (below) is copied as opaque raw text because Python never
    # manages any key inside that section — Swift/`ConfigStore` owns it
    # entirely, so this run never produces a "prompted" `[app]` value to
    # merge against, and verbatim byte-for-byte round-tripping is both safe
    # and the simplest correct thing (see `_extract_raw_section`'s
    # docstring). `[stt]` is deliberately NOT copied the same way: Python
    # actively re-renders several of its keys every run (`service`, `model`,
    # `ws_socket`, `language`) from this run's prompts/flags, so a
    # whole-section raw copy would silently discard those answers. Only
    # `launchd_label` — the one `[stt]` key `onoats init` never prompts
    # for — needs a carry-over, and it has to merge into the freshly-built
    # `stt` dict below rather than overwrite the section, which the generic
    # raw-block mechanism has no per-key granularity to do. So the generic
    # mechanism cannot subsume this bespoke one; both stay.
    #
    # The carried value is re-validated, not copied blind: the runtime reader
    # (`OnoatsConfig.stt_launchd_label`) already runs every label through
    # `normalize_launchd_label` (strip -> empty-as-None -> allowlist-validate)
    # and treats a non-conforming one as absent, so a schema-invalid value
    # stored in config.toml is *already* inert at runtime. Copying it through
    # unchecked let it reach `_toml_escape` (TypeError on a non-string — a
    # TOML integer or array) or, for a string carrying a raw newline, corrupt
    # the regenerated config.toml by breaking out of its own line. Calling
    # the SAME shared helper the runtime reader uses (rather than hand-
    # rolling the strip/empty/validate sequence here again) is what keeps
    # this path from disagreeing with the runtime reader on "absent" vs
    # "malformed" — the two independently drifted on that twice before
    # (review-gauntlet rounds 5 and 6 on this branch).
    carried_label = existing_stt.get("launchd_label")
    if carried_label is not None and not stt.get("launchd_label"):
        carried_str = carried_label if isinstance(carried_label, str) else None
        validated_label = normalize_launchd_label(carried_str)
        if validated_label:
            stt["launchd_label"] = validated_label
        elif carried_str and carried_str.strip():
            print(
                f"  note: ignoring malformed [stt].launchd_label "
                f"{carried_label!r} — it is already inert at runtime and is "
                "not carried into the regenerated config.toml."
            )
    # Verbatim, not re-rendered from `existing.raw["app"]` — see
    # `_extract_raw_section`'s docstring, `_render_config_toml`'s `[app]`
    # handling, and the comment above for why this section uses a different
    # carry-over mechanism than `launchd_label`.
    app_raw_block: str | None = None
    if config_path.exists():
        try:
            app_raw_block = _extract_raw_section(
                config_path.read_text(encoding="utf-8"), "app"
            )
        except OSError:
            pass

    if not args.no_preflight:
        _run_preflight(stt, secrets)

    # ---- 3. categories (default uncategorized) ----
    if args.categories is not None:
        cats = [c.strip().lower() for c in args.categories.split(",") if c.strip()]
    elif interactive:
        existing_cats = sorted(existing.category_set - {"uncategorized"})
        raw = _prompt(
            "Categories (comma-separated; 'uncategorized' always included)",
            ",".join(existing_cats) or None,
        )
        cats = [c.strip().lower() for c in raw.split(",") if c.strip()]
    else:
        cats = sorted(existing.category_set - {"uncategorized"})
    categories = sorted({*cats, "uncategorized"})

    # ---- 4. speaker identity (render-only) ----
    if interactive:
        me = _prompt("Your display name ('me' label)", existing.speaker_label_me)
        them = _prompt(
            "Their display label ('them' label)", existing.speaker_label_them
        )
    else:
        me = args.me_name or existing.speaker_label_me
        them = args.them_name or existing.speaker_label_them
    speakers = {"me": me, "them": them}

    # ---- storage (optional non-default data root) ----
    existing_data_dir = existing.raw.get("storage", {}).get("data_dir")
    if args.data_dir is not None:
        data_dir = args.data_dir.strip() or None
    elif interactive:
        data_dir = (
            _prompt(
                "Data dir (recordings + queue; blank = default ~/.local/share/onoats; "
                "set ~/koda-data to feed koda)",
                existing_data_dir,
            ).strip()
            or None
        )
    else:
        data_dir = existing_data_dir

    # ---- 5/6. tuning + dictionary ----
    tuning = {
        "silence_timeout_sec": existing.silence_timeout_sec,
        "segment_hint_threshold": existing.segment_hint_threshold,
        "audio_heartbeat_sec": existing.audio_heartbeat_sec,
    }
    dict_path = _seed_dictionary(args.import_dictionary)

    # ---- 7. write config.toml + secrets.env ----
    content = _render_config_toml(
        mic=mic,
        system=system,
        stt=stt,
        speakers=speakers,
        categories=categories,
        tuning=tuning,
        data_dir=data_dir,
        app_raw_block=app_raw_block,
    )
    _write_config_toml(config_path, content)
    _write_secrets_env(secrets_path, secrets, merge_existing=True)

    print(f"\nWrote {config_path}")
    print(f"Wrote {secrets_path} (0600)")
    print(f"Dictionary: {dict_path}")
    print(f"STT backend: {stt.get('service')}")
    print(f"Categories: {', '.join(categories)}")
    print(f"Speakers: me={me!r} them={them!r}")
    if data_dir:
        print(f"Data dir: {data_dir}  (recordings + queue; a worker here drains it)")
    print("\nNext: `onoats bot` to record, `onoats convert` to render transcripts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
