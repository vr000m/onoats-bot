# Installation

[Back to the project README](../README.md)

Choose the path that matches the platform and interface you want:

| Path | Command after cloning | Works on |
|---|---|---|
| Menu bar + native capture (recommended) | `make -C native setup` | Apple Silicon, macOS 14.4+ |
| CLI + native capture | `make -C native setup-cli` | Apple Silicon, macOS 14.4+ |
| CLI + PortAudio | `uv tool install onoats` | Linux, Windows, Intel Mac, and older macOS |

The CLI is published on PyPI. The native macOS menu-bar app and capturer are
built from source and are not distributed through PyPI.

## Menu bar and native capture

### Prerequisites

Install the Xcode Command Line Tools and uv:

~~~bash
xcode-select --install
curl -LsSf https://astral.sh/uv/install.sh | sh
~~~

Open a new shell, or follow uv's PATH instructions, before continuing.

### Install

~~~bash
git clone https://github.com/vr000m/onoats-bot.git
cd onoats-bot
make -C native setup
~~~

`setup`:

1. creates or reuses the local signing certificate;
2. builds and signs the native capturer and **Onoats.app**;
3. installs the app in `~/Applications`;
4. installs the CLI in `~/.local/bin`; and
5. runs `onoats init` for guided configuration.

Launch `~/Applications/Onoats.app` through Finder, Spotlight, or
LaunchServices. A GUI launch matters because macOS grants microphone and system
audio permission to the app, not to the terminal.

The first Start requests **Microphone** and **Screen & System Audio Recording**
permission. Onoats triggers the system-audio prompt before opening the recorder
sockets and extends the startup wait while the prompt is visible.

### Update

After `git pull`, rebuild and reinstall with the same extras selected for the
current installation:

~~~bash
# Standard native installation
make -C native install EXTRAS=macos

# Native installation with speaker diarization
make -C native install EXTRAS=macos,diarization
~~~

`install-cli` replaces the managed tool environment, so omitting a previously
selected extra removes it.

The setup and install targets reuse the signing identity and preserve an
existing configuration. See the [native build and release guide](../native/README.md)
for signing, exact-byte staging, smoke, rollback, and TCC details.

## Native capture without the menu bar

Use the same prerequisites, then:

~~~bash
git clone https://github.com/vr000m/onoats-bot.git
cd onoats-bot
make -C native setup-cli
~~~

The target installs the CLI and signed capturer without installing the app. It
prints the exact command for a session, equivalent to:

~~~bash
AUDIO_SOURCE=socket \
ONOATS_CAPTURER_BIN=/path/to/onoats-bot/native/build/onoats-capturer \
onoats bot
~~~

When launched from a terminal, macOS attributes microphone and system-audio
permission to that terminal app.

## CLI and PortAudio

This is the cross-platform path and needs no Swift compiler, signing
certificate, or native app.

Linux needs the PortAudio development headers:

~~~bash
sudo apt-get install -y portaudio19-dev
uv tool install onoats
onoats init
~~~

From a development checkout:

~~~bash
git clone https://github.com/vr000m/onoats-bot.git
cd onoats-bot
uv tool install --editable .
onoats init
~~~

PortAudio can capture a microphone directly. Capturing system audio needs a
loopback device and routing; follow the [BlackHole fallback guide](blackhole-fallback.md)
on macOS.

## Optional extras

| Extra | Purpose | Platform |
|---|---|---|
| `macos` | Whisper-MLX and Kokoro | Apple Silicon |
| `diarization` | them-channel speaker diarization | Apple Silicon |

Install extras with the tool:

~~~bash
uv tool install 'onoats[macos]'
uv tool install 'onoats[macos,diarization]'
~~~

Or synchronise them in a checkout:

~~~bash
uv sync --extra macos
uv sync --extra macos --extra diarization
~~~

The baseline package is MLX-free. It supports Deepgram and
`pipecat-local-stt-server` over TCP without Apple-specific dependencies.

## Platform support

| Capability | Linux / Windows / Intel Mac | Apple Silicon Mac |
|---|---|---|
| PortAudio capture | ✅ baseline | ✅ baseline |
| Deepgram | ✅ baseline | ✅ baseline |
| Local STT over TCP | ✅ baseline | ✅ baseline |
| Local Whisper-MLX | — | ✅ `macos` extra |
| Kokoro TTS | — | ✅ `macos` extra |
| Native system audio | — | ✅ macOS 14.4+ |
| Speaker diarization | — | ✅ `diarization` extra |

On macOS 14.4+, native capture uses a Core Audio process tap and two Unix
sockets, so it needs no virtual-audio driver. On older macOS and other
platforms, use the PortAudio path with an appropriate loopback device.

## Next steps

- Configure STT, storage, and audio in the [configuration guide](configuration.md).
- Learn the app controls in the [menu-bar guide](menu-bar.md).
- Integrate another consumer through the [queue contract](queue-contract.md).
