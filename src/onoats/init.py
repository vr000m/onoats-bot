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
import sys
from pathlib import Path

from onoats.config import config_toml_path, load_config, secrets_env_path

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


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_config_toml(
    *,
    mic: str | None,
    system: str | None,
    stt: dict,
    speakers: dict,
    categories: list[str],
    tuning: dict,
    data_dir: str | None = None,
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
