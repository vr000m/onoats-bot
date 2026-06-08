"""Audio device selection and validation for local transport.

Provides interactive device picker with last-used memory, env var
overrides, and device validation.

onoats is a silent recorder — it only needs a mic (input) device in
normal operation. Output devices are only required in interactive mode.

Usage — silent recorder mode (input only)::

    from onoats.config.audio_devices import select_input_device

    mic = select_input_device(input_device_env=os.getenv("INPUT_DEVICE"))

Usage — interactive mode (input + output)::

    from onoats.config.audio_devices import select_audio_devices

    mic, speaker = select_audio_devices(
        input_device_env=os.getenv("INPUT_DEVICE"),
        output_device_env=os.getenv("OUTPUT_DEVICE"),
    )
"""

import os
import sys
from pathlib import Path

from loguru import logger


def _device_state_dir() -> Path:
    """Last-used device files live under the XDG config dir, not the repo."""
    raw = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".config"
    return base / "onoats"


LAST_DEVICE_FILE = _device_state_dir() / ".last_audio_devices"
LAST_DUAL_DEVICE_FILE = _device_state_dir() / ".last_dual_audio_devices"

# Pipeline sample rate — 16kHz for Silero VAD compatibility.
# Silero VAD requires 8kHz or 16kHz. All tested devices support 16kHz.
PIPELINE_SAMPLE_RATE = 16000


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _coerce_device_query(device_query):
    """Normalize a device query into ``int`` index, ``str`` name, or ``None``."""
    if device_query is None:
        return None
    if isinstance(device_query, int):
        return device_query if device_query >= 0 else None
    raw = str(device_query).strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return raw
    return value if value >= 0 else None


def _device_supports_sample_rate(pa, device_index, *, need_input: bool) -> bool:
    import pyaudio

    try:
        if need_input:
            pa.is_format_supported(
                PIPELINE_SAMPLE_RATE,
                input_device=device_index,
                input_channels=1,
                input_format=pyaudio.paInt16,
            )
        else:
            pa.is_format_supported(
                PIPELINE_SAMPLE_RATE,
                output_device=device_index,
                output_channels=1,
                output_format=pyaudio.paInt16,
            )
    except ValueError:
        return False
    return True


def _find_device_by_name(pa, query: str, *, need_input: bool) -> int | None:
    query_norm = query.casefold()
    exact_matches: list[int] = []
    partial_matches: list[int] = []
    for i in range(pa.get_device_count()):
        info = pa.get_device_info_by_index(i)
        channels = info["maxInputChannels"] if need_input else info["maxOutputChannels"]
        if channels == 0:
            continue
        if not _device_supports_sample_rate(pa, i, need_input=need_input):
            continue
        name = str(info["name"])
        name_norm = name.casefold()
        if name_norm == query_norm:
            exact_matches.append(i)
        elif query_norm in name_norm:
            partial_matches.append(i)

    matches = exact_matches or partial_matches
    if not matches:
        return None
    if len(matches) > 1:
        names = ", ".join(
            str(pa.get_device_info_by_index(idx)["name"]) for idx in matches[:5]
        )
        logger.warning(
            f"Device query '{query}' matched multiple devices for "
            f"{'input' if need_input else 'output'} selection: {names}. Using [{matches[0]}]."
        )
    return matches[0]


def validate_audio_device(device_query, label, need_input=False):
    """Resolve a device query and verify it supports the pipeline sample rate.

    ``device_query`` may be:
    - an integer device index
    - a numeric string device index
    - a case-insensitive exact or substring device name
    """
    query = _coerce_device_query(device_query)
    if query is None:
        return None
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        if isinstance(query, int):
            device_index = query
            if device_index >= pa.get_device_count():
                logger.warning(
                    f"{label}={device_index} not found, using system default"
                )
                return None
        else:
            device_index = _find_device_by_name(pa, query, need_input=need_input)
            if device_index is None:
                logger.warning(
                    f"{label}={query!r} not found at {PIPELINE_SAMPLE_RATE}Hz, using system default"
                )
                return None

        info = pa.get_device_info_by_index(device_index)
        channels = info["maxInputChannels"] if need_input else info["maxOutputChannels"]
        if channels == 0:
            logger.warning(
                f"{label}={device_query!r} ({info['name']}) has no "
                f"{'input' if need_input else 'output'} channels, using system default"
            )
            return None

        if not _device_supports_sample_rate(pa, device_index, need_input=need_input):
            logger.warning(
                f"{label}={device_query!r} ({info['name']}) does not support "
                f"{PIPELINE_SAMPLE_RATE}Hz, using system default"
            )
            return None
        return device_index
    finally:
        pa.terminate()


# ---------------------------------------------------------------------------
# Silent listener mode — input only
# ---------------------------------------------------------------------------


def select_input_device(input_device_env=None, *, source="from env"):
    """Select an audio input (mic) device. No output device required.

    Use this in silent recorder mode. onoats only needs a mic.

    Args:
        input_device_env: INPUT_DEVICE env var value (int or None).
        source: Provenance label for ``input_device_env`` (e.g. "from env",
            "from config"), used only for the log line. The single-input
            recorder consults only the INPUT_DEVICE env var today, so the
            default is accurate; the param keeps the label honest if a
            config-derived value is ever threaded in.

    Returns:
        Input device index (int), or None for system default.
    """
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        # Check env var override
        env_in = validate_audio_device(
            input_device_env, "INPUT_DEVICE", need_input=True
        )
        if env_in is not None:
            in_info = pa.get_device_info_by_index(env_in)
            logger.info(f"Audio input:  [{env_in}] {in_info['name']} ({source})")
            return env_in

        # Build input device list
        inputs = _enumerate_input_devices(pa)

        # Load last-used input device
        last_in = _load_last_input()
        last_in = validate_audio_device(last_in, "last_input", need_input=True)

        # Interactive or non-interactive selection
        if sys.stdin.isatty():
            picked_in = _pick_input_device(inputs, last_in)
        else:
            picked_in = last_in

        input_dev = picked_in

        # Fall back to system default
        if input_dev is None:
            input_dev = int(pa.get_default_input_device_info()["index"])

        # Validate default supports PIPELINE_SAMPLE_RATE
        try:
            pa.is_format_supported(
                PIPELINE_SAMPLE_RATE,
                input_device=input_dev,
                input_channels=1,
                input_format=pyaudio.paInt16,
            )
        except ValueError:
            dev_name = pa.get_device_info_by_index(input_dev)["name"]
            raise RuntimeError(
                f"Audio input device [{input_dev}] {dev_name} does not support "
                f"{PIPELINE_SAMPLE_RATE}Hz. Set INPUT_DEVICE to a compatible device, "
                f"or change PIPELINE_SAMPLE_RATE."
            )

        in_info = pa.get_device_info_by_index(input_dev)
        logger.info(f"Audio input:  [{input_dev}] {in_info['name']}")

        # Save for next time (input only)
        _save_last_input(input_dev)

        return input_dev
    finally:
        pa.terminate()


def select_dual_input_devices(
    mic_input_env=None,
    system_input_env=None,
    *,
    mic_source="from env",
    system_source="from env",
):
    """Select separate microphone and system-loopback input devices.

    ``mic_input_env`` and ``system_input_env`` may be indices or stable device
    names. The dual-input picker persists the last-used device names so the
    selection remains stable across host-side PortAudio reindexing.

    ``mic_source`` / ``system_source`` are provenance labels (e.g. "from env",
    "from config") describing where each resolved value came from, used only
    for the log/picker line so it reads accurately instead of always "from env".
    """
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        env_mic = validate_audio_device(
            mic_input_env, "MIC_INPUT_DEVICE", need_input=True
        )
        env_system = validate_audio_device(
            system_input_env, "SYSTEM_INPUT_DEVICE", need_input=True
        )
        if env_mic is not None and env_system is not None:
            _ensure_distinct_dual_inputs(env_mic, env_system, pa)
            _log_dual_inputs(
                pa,
                env_mic,
                env_system,
                mic_label=mic_source,
                system_label=system_source,
            )
            return env_mic, env_system

        inputs = _enumerate_input_devices(pa)

        last_mic_name, last_system_name = _load_last_dual_inputs()
        last_mic = validate_audio_device(
            last_mic_name, "last_mic_input", need_input=True
        )
        last_system = validate_audio_device(
            last_system_name, "last_system_input", need_input=True
        )

        if sys.stdin.isatty():
            picked_mic, picked_system = _pick_dual_input_devices(
                inputs,
                env_mic if env_mic is not None else last_mic,
                env_system if env_system is not None else last_system,
                skip_mic=env_mic is not None,
                skip_system=env_system is not None,
                mic_source=mic_source,
                system_source=system_source,
            )
        else:
            picked_mic = env_mic if env_mic is not None else last_mic
            picked_system = env_system if env_system is not None else last_system

        mic_dev = env_mic if env_mic is not None else picked_mic
        system_dev = env_system if env_system is not None else picked_system

        if mic_dev is None:
            mic_dev = int(pa.get_default_input_device_info()["index"])
        if system_dev is None:
            raise RuntimeError(
                "No system loopback input selected. Set SYSTEM_INPUT_DEVICE or run "
                "`onoats bot` in an interactive terminal to choose one."
            )

        for dev_idx, label in [
            (mic_dev, "microphone"),
            (system_dev, "system"),
        ]:
            if not _device_supports_sample_rate(pa, dev_idx, need_input=True):
                dev_name = pa.get_device_info_by_index(dev_idx)["name"]
                raise RuntimeError(
                    f"Audio {label} device [{dev_idx}] {dev_name} does not support "
                    f"{PIPELINE_SAMPLE_RATE}Hz. Set MIC_INPUT_DEVICE/SYSTEM_INPUT_DEVICE "
                    f"to compatible devices."
                )

        _ensure_distinct_dual_inputs(mic_dev, system_dev, pa)
        _log_dual_inputs(pa, mic_dev, system_dev)
        _save_last_dual_inputs(pa, mic_dev, system_dev)
        return mic_dev, system_dev
    finally:
        pa.terminate()


def _enumerate_input_devices(pa) -> list[tuple[int, str, int]]:
    """Return ``[(index, name, default_sample_rate), ...]`` for every input device."""
    inputs: list[tuple[int, str, int]] = []
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d["maxInputChannels"] > 0:
            inputs.append((i, d["name"], int(d["defaultSampleRate"])))
    return inputs


# ---------------------------------------------------------------------------
# Interactive mode — input + output (Phase 2)
# ---------------------------------------------------------------------------


def select_audio_devices(input_device_env=None, output_device_env=None):
    """Select audio input and output devices interactively.

    Use this in interactive mode when onoats responds with voice.

    Args:
        input_device_env:  INPUT_DEVICE env var value (int or None).
        output_device_env: OUTPUT_DEVICE env var value (int or None).

    Returns:
        Tuple of (input_device_index, output_device_index). Either may be
        None for system default.
    """
    import pyaudio

    pa = pyaudio.PyAudio()
    try:
        # Check env var overrides
        env_in = validate_audio_device(
            input_device_env, "INPUT_DEVICE", need_input=True
        )
        env_out = validate_audio_device(
            output_device_env, "OUTPUT_DEVICE", need_input=False
        )

        # If both env vars are set, skip interactive selection entirely
        if env_in is not None and env_out is not None:
            in_info = pa.get_device_info_by_index(env_in)
            out_info = pa.get_device_info_by_index(env_out)
            logger.info(f"Audio input:  [{env_in}] {in_info['name']}")
            logger.info(f"Audio output: [{env_out}] {out_info['name']}")
            return env_in, env_out

        # Build device lists
        inputs = []
        outputs = []
        for i in range(pa.get_device_count()):
            d = pa.get_device_info_by_index(i)
            if d["maxInputChannels"] > 0:
                inputs.append((i, d["name"], int(d["defaultSampleRate"])))
            if d["maxOutputChannels"] > 0:
                outputs.append((i, d["name"], int(d["defaultSampleRate"])))

        # Load last-used devices
        last_in, last_out = _load_last_devices()
        last_in = validate_audio_device(last_in, "last_input", need_input=True)
        last_out = validate_audio_device(last_out, "last_output", need_input=False)

        # Use env overrides where available, pick the rest interactively
        if sys.stdin.isatty():
            picked_in, picked_out = _pick_devices(
                inputs,
                outputs,
                env_in if env_in is not None else last_in,
                env_out if env_out is not None else last_out,
                skip_input=env_in is not None,
                skip_output=env_out is not None,
            )
        else:
            picked_in = last_in
            picked_out = last_out

        input_dev = env_in if env_in is not None else picked_in
        output_dev = env_out if env_out is not None else picked_out

        # Fall back to system defaults
        if input_dev is None:
            input_dev = int(pa.get_default_input_device_info()["index"])
        if output_dev is None:
            output_dev = int(pa.get_default_output_device_info()["index"])

        # Validate both defaults support PIPELINE_SAMPLE_RATE
        for dev_idx, direction, label in [
            (input_dev, True, "default input"),
            (output_dev, False, "default output"),
        ]:
            try:
                if direction:
                    pa.is_format_supported(
                        PIPELINE_SAMPLE_RATE,
                        input_device=dev_idx,
                        input_channels=1,
                        input_format=pyaudio.paInt16,
                    )
                else:
                    pa.is_format_supported(
                        PIPELINE_SAMPLE_RATE,
                        output_device=dev_idx,
                        output_channels=1,
                        output_format=pyaudio.paInt16,
                    )
            except ValueError:
                dev_name = pa.get_device_info_by_index(dev_idx)["name"]
                raise RuntimeError(
                    f"Audio {label} device [{dev_idx}] {dev_name} does not support "
                    f"{PIPELINE_SAMPLE_RATE}Hz. Set INPUT_DEVICE/OUTPUT_DEVICE to a "
                    f"compatible device, or change PIPELINE_SAMPLE_RATE."
                )

        in_info = pa.get_device_info_by_index(input_dev)
        out_info = pa.get_device_info_by_index(output_dev)
        logger.info(f"Audio input:  [{input_dev}] {in_info['name']}")
        logger.info(f"Audio output: [{output_dev}] {out_info['name']}")

        # Save for next time
        _save_last_devices(input_dev, output_dev)

        return input_dev, output_dev
    finally:
        pa.terminate()


# ---------------------------------------------------------------------------
# Device tag (for filenames)
# ---------------------------------------------------------------------------


def get_device_tag(input_dev, output_dev=None):
    """Build a short device tag for recording filenames.

    In silent listener mode, output_dev is omitted.

    Examples:
        get_device_tag(3) → "in3-jabra"
        get_device_tag(3, 2) → "in3-jabra_out2-jabra"
    """
    import pyaudio

    pa = pyaudio.PyAudio()
    try:

        def _short_name(idx):
            if idx is None:
                return "default"
            name = pa.get_device_info_by_index(idx)["name"]
            return name.split()[0].lower()

        in_label = input_dev if input_dev is not None else "D"
        tag = f"in{in_label}-{_short_name(input_dev)}"
        if output_dev is not None:
            out_label = output_dev
            tag += f"_out{out_label}-{_short_name(output_dev)}"
        return tag
    finally:
        pa.terminate()


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _load_last_input():
    """Read the last-used input device index from disk (input-only format)."""
    if not LAST_DEVICE_FILE.exists():
        return None
    try:
        parts = LAST_DEVICE_FILE.read_text().strip().split("\n")
        # Support both single-line (input-only) and two-line (input+output) formats
        if len(parts) >= 1 and parts[0].strip():
            return int(parts[0])
    except (ValueError, IndexError):
        pass
    return None


def _save_last_input(input_dev):
    """Write the last-used input device index to disk."""
    try:
        LAST_DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Preserve existing output device line if present
        existing_out = None
        if LAST_DEVICE_FILE.exists():
            try:
                parts = LAST_DEVICE_FILE.read_text().strip().split("\n")
                if len(parts) == 2 and parts[1].strip():
                    existing_out = parts[1].strip()
            except (ValueError, IndexError):
                pass
        if existing_out is not None:
            LAST_DEVICE_FILE.write_text(f"{input_dev}\n{existing_out}\n")
        else:
            LAST_DEVICE_FILE.write_text(f"{input_dev}\n")
    except OSError:
        pass


def _load_last_devices():
    """Read the last-used input and output device indices from disk."""
    if not LAST_DEVICE_FILE.exists():
        return None, None
    try:
        parts = LAST_DEVICE_FILE.read_text().strip().split("\n")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
        if len(parts) == 1 and parts[0].strip():
            return int(parts[0]), None
    except (ValueError, IndexError):
        pass
    return None, None


def _save_last_devices(input_dev, output_dev):
    """Write the last-used input and output device indices to disk."""
    try:
        LAST_DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        LAST_DEVICE_FILE.write_text(f"{input_dev}\n{output_dev}\n")
    except OSError:
        pass


def _load_last_dual_inputs():
    """Read last-used dual-input device names from disk."""
    if not LAST_DUAL_DEVICE_FILE.exists():
        return None, None
    try:
        parts = LAST_DUAL_DEVICE_FILE.read_text().strip().split("\n")
        if len(parts) >= 2:
            mic = parts[0].strip() or None
            system = parts[1].strip() or None
            return mic, system
    except OSError:
        pass
    return None, None


def _save_last_dual_inputs(pa, mic_dev, system_dev):
    """Persist dual-input device names so selection survives index drift."""
    try:
        LAST_DUAL_DEVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        mic_name = str(pa.get_device_info_by_index(mic_dev)["name"]).strip()
        system_name = str(pa.get_device_info_by_index(system_dev)["name"]).strip()
        LAST_DUAL_DEVICE_FILE.write_text(f"{mic_name}\n{system_name}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Interactive pickers
# ---------------------------------------------------------------------------


def _pick_input_device(inputs, last_in):
    """Interactive input-only device picker."""
    last_in_name = (
        next((n for i, n, _ in inputs if i == last_in), None)
        if last_in is not None
        else None
    )

    print("\n--- Mic Device Selection ---")
    if last_in_name:
        print(f"Last used: IN=[{last_in}] {last_in_name}")
        print("Press Enter to reuse, or pick a new device.\n")

    print("Input devices:")
    for idx, name, rate in inputs:
        marker = " *" if idx == last_in else ""
        print(f"  [{idx}] {name}{marker}")

    try:
        default_label = f" [{last_in}]" if last_in is not None else ""
        choice = input(f"Select input device{default_label}: ").strip()
        if choice:
            input_dev = int(choice)
            if not any(d[0] == input_dev for d in inputs):
                print(f"  Invalid device {input_dev}, using last/default")
                input_dev = last_in
        else:
            input_dev = last_in
    except (ValueError, EOFError, KeyboardInterrupt):
        input_dev = last_in

    print()
    return input_dev


def _pick_devices(
    inputs, outputs, last_in, last_out, *, skip_input=False, skip_output=False
):
    """Interactive device picker showing both input and output."""
    last_in_name = (
        next((n for i, n, _ in inputs if i == last_in), None)
        if last_in is not None
        else None
    )
    last_out_name = (
        next((n for i, n, _ in outputs if i == last_out), None)
        if last_out is not None
        else None
    )

    print("\n--- Audio Device Selection ---")
    if last_in_name and last_out_name and not skip_input and not skip_output:
        print(
            f"Last used: IN=[{last_in}] {last_in_name}, OUT=[{last_out}] {last_out_name}"
        )
        print("Press Enter to reuse, or pick new devices.\n")

    # Input device
    if skip_input:
        input_dev = last_in
        in_name = (
            next((n for i, n, _ in inputs if i == last_in), "?")
            if last_in is not None
            else "?"
        )
        print(f"Input device: [{last_in}] {in_name} (from env)")
    else:
        print("Input devices:")
        for idx, name, rate in inputs:
            marker = " *" if idx == last_in else ""
            print(f"  [{idx}] {name}{marker}")
        try:
            default_label = f" [{last_in}]" if last_in is not None else ""
            choice = input(f"Select input device{default_label}: ").strip()
            if choice:
                input_dev = int(choice)
                if not any(d[0] == input_dev for d in inputs):
                    print(f"  Invalid device {input_dev}, using last/default")
                    input_dev = last_in
            else:
                input_dev = last_in
        except (ValueError, EOFError, KeyboardInterrupt):
            input_dev = last_in

    # Output device
    if skip_output:
        output_dev = last_out
        out_name = (
            next((n for i, n, _ in outputs if i == last_out), "?")
            if last_out is not None
            else "?"
        )
        print(f"Output device: [{last_out}] {out_name} (from env)")
    else:
        print("\nOutput devices:")
        for idx, name, rate in outputs:
            marker = " *" if idx == last_out else ""
            print(f"  [{idx}] {name}{marker}")
        try:
            default_label = f" [{last_out}]" if last_out is not None else ""
            choice = input(f"Select output device{default_label}: ").strip()
            if choice:
                output_dev = int(choice)
                if not any(d[0] == output_dev for d in outputs):
                    print(f"  Invalid device {output_dev}, using last/default")
                    output_dev = last_out
            else:
                output_dev = last_out
        except (ValueError, EOFError, KeyboardInterrupt):
            output_dev = last_out

    print()
    return input_dev, output_dev


def _pick_dual_input_devices(
    inputs,
    last_mic,
    last_system,
    *,
    skip_mic: bool = False,
    skip_system: bool = False,
    mic_source: str = "from env",
    system_source: str = "from env",
):
    """Interactive picker for separate mic and loopback input devices."""

    def _pick(label, default_idx, *, skip: bool = False, source: str = "from env"):
        if skip:
            name = next((n for i, n, _ in inputs if i == default_idx), "?")
            print(f"{label}: [{default_idx}] {name} ({source})")
            return default_idx

        print(f"{label} input devices:")
        for idx, name, _rate in inputs:
            marker = " *" if idx == default_idx else ""
            print(f"  [{idx}] {name}{marker}")
        try:
            default_label = f" [{default_idx}]" if default_idx is not None else ""
            choice = input(
                f"Select {label.lower()} input device{default_label}: "
            ).strip()
            if choice:
                picked = int(choice)
                if not any(d[0] == picked for d in inputs):
                    print(f"  Invalid device {picked}, using last/default")
                    return default_idx
                return picked
            return default_idx
        except (ValueError, EOFError, KeyboardInterrupt):
            return default_idx

    print("\n--- Dual Input Device Selection ---")
    if last_mic is not None:
        mic_name = next((n for i, n, _ in inputs if i == last_mic), None)
        if mic_name:
            print(f"Last microphone: [{last_mic}] {mic_name}")
    if last_system is not None:
        system_name = next((n for i, n, _ in inputs if i == last_system), None)
        if system_name:
            print(f"Last system:     [{last_system}] {system_name}")
    if last_mic is not None or last_system is not None:
        print("Press Enter to reuse a highlighted device.\n")

    mic_dev = _pick("Microphone", last_mic, skip=skip_mic, source=mic_source)
    print()
    system_dev = _pick("System", last_system, skip=skip_system, source=system_source)
    print()
    return mic_dev, system_dev


def _ensure_distinct_dual_inputs(mic_dev: int, system_dev: int, pa) -> None:
    if mic_dev == system_dev:
        name = pa.get_device_info_by_index(mic_dev)["name"]
        raise RuntimeError(
            "MIC_INPUT_DEVICE and SYSTEM_INPUT_DEVICE resolved to the same input "
            f"device [{mic_dev}] {name}. Choose separate devices for bot-dual."
        )


def _log_dual_inputs(
    pa,
    mic_dev: int,
    system_dev: int,
    *,
    mic_label: str | None = None,
    system_label: str | None = None,
) -> None:
    mic_info = pa.get_device_info_by_index(mic_dev)
    system_info = pa.get_device_info_by_index(system_dev)
    mic_suffix = f" ({mic_label})" if mic_label else ""
    system_suffix = f" ({system_label})" if system_label else ""
    logger.info(f"Mic input:    [{mic_dev}] {mic_info['name']}{mic_suffix}")
    logger.info(f"System input: [{system_dev}] {system_info['name']}{system_suffix}")
