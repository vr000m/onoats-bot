# onoats

**Always-on Organized Audio Transcript System** — [onoats.dev](https://onoats.dev)

onoats is a standalone, local-first voice **recorder** plus a self-contained
**converter** that turns recordings into readable markdown transcripts. It
captures your microphone (*you*) and system/loopback audio (*them*) on separate
streams, transcribes each, and writes one chronological transcript per session.

No API keys are required for the converter, and **no database** is ever opened —
onoats emits plain JSONL session files to a filesystem queue and renders them to
markdown. A downstream consumer can subscribe to the same filesystem queue for
analysis; onoats itself stays files-only.

## Quickstart

onoats installs **from source** (it is not published on PyPI).

### macOS 14.4+ — menu bar + native capture (recommended)

Prerequisites on a fresh machine (everything else is handled by `setup`):

```bash
xcode-select --install    # Xcode Command Line Tools (swiftc, git, codesign)
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv (installs the CLI)
# then open a new shell (or follow the installer's PATH note) so `uv` resolves
```

```bash
git clone https://github.com/vr000m/onoats-bot.git
cd onoats-bot
make -C native setup     # cert + build + sign Onoats.app → ~/Applications,
                         # CLI → ~/.local/bin/onoats, then guided `onoats init`
```

`setup` walks you through configuration (`onoats init`: data dir, STT
service, secrets — see [Configuration](#configuration)) and ends with
**Onoats.app** installed. Launch it from `~/Applications` and record from
the [menu bar](#menu-bar-macos); the first Start triggers the macOS
permission prompts (microphone + system audio) — see the menu-bar section.
`setup` is safe to re-run at every step (it never regenerates the signing
cert or touches an existing config); reconfigure any time with `onoats init`
or the menu bar's Settings, and update after a `git pull` with
`make -C native install`. Details in [`native/README.md`](native/README.md).

### Other platforms (and macOS below 14.4)

```bash
# Linux: PortAudio dev headers are needed to build pyaudio
#   sudo apt-get install -y portaudio19-dev   # (macOS Homebrew ships them)

git clone https://github.com/vr000m/onoats-bot.git
cd onoats-bot
uv tool install --editable .            # baseline (PortAudio + Deepgram/TCP STT)
# uv tool install --editable '.[macos]' # Apple Silicon: adds Whisper-MLX + Kokoro

onoats init                       # guided setup → config.toml + 0600 secrets.env
onoats bot                        # dual-input recorder (mic + system loopback)
onoats convert                    # render pending sessions → markdown transcripts
```

On this path, capturing system audio ("them") needs a loopback driver — see
[docs/blackhole-fallback.md](docs/blackhole-fallback.md).

Other subcommands:

```bash
onoats bot-single   # legacy mic-only recorder
onoats flush        # tell the running recorder to rotate its buffer now
onoats devices      # list audio input/output devices (PortAudio's view; under
                    # the socket path it adds a note — the native capturer binds
                    # the system default input / default-output tap instead)
onoats status       # recorder pid / running state + data dir; names the capture
                    # devices (socket: what the running session bound; PortAudio:
                    # the configured [devices] names) and any live capture warning
```

## Cross-platform matrix

| Capability                        | Linux / Windows / Intel mac | Apple Silicon mac |
|-----------------------------------|-----------------------------|-------------------|
| Audio capture (PortAudio)         | ✅ baseline                  | ✅ baseline        |
| Hosted STT (Deepgram)             | ✅ baseline                  | ✅ baseline        |
| Local STT over TCP (stt_server)   | ✅ baseline                  | ✅ baseline        |
| Local Whisper-MLX (on-device)     | —                           | ✅ `[macos]` extra |
| Kokoro TTS                        | —                           | ✅ `[macos]` extra |
| System audio ("them") capture     | loopback driver-dependent   | ✅ native capturer ⁺ |

⁺ On **macOS 14.4+** the default system-audio story is the **native capturer**
(`AUDIO_SOURCE=socket` — Core Audio process tap, no virtual-audio driver; see
[Menu bar (macOS)](#menu-bar-macos) and [Audio source](#audio-source) below).
On macOS 13.x–14.3 (below the Core Audio tap API floor) and on other
platforms, the fallback is a loopback driver on the default PortAudio path —
setup in [docs/blackhole-fallback.md](docs/blackhole-fallback.md).

The baseline ships **MLX-free**: `mlx-whisper` is only in the `[macos]` extra
and its imports are lazy, so `onoats bot` runs off-mac with PortAudio plus
either Deepgram or a TCP-reachable `pipecat-local-stt-server`.

## Menu bar (macOS)

`make -C native install` puts **Onoats.app** in `~/Applications`. Launch it
from there — GUI launch matters: LaunchServices makes the app its own TCC
permission subject (a terminal launch would attribute the permission grants to
the terminal instead). It lives in the menu bar with no Dock icon.

- **Start / Stop / Flush** — Start runs the recorder (`onoats bot` with the
  native capturer); Stop ends the session gracefully (the recorder drains
  in-flight audio before rotating the buffer into the queue); Flush rotates
  the current buffer into the queue mid-session.
- **Mic (me) picker** — the submenu lists input devices; selecting one sets
  the macOS **default input device** (system-wide — disclosed in the submenu),
  because the capturer binds the system default at Start. A running session
  keeps its device; changes apply on the next Start.
- **Devices line** — the menu shows the current system default input/output,
  i.e. the devices the capturer will actually bind (a guard against silently
  recording the wrong device).
- **Settings** — STT service picker (whisper / websocket / deepgram), data-dir
  chooser, and an "Open config.toml…" escape hatch. These edit the same
  `~/.config/onoats/config.toml` the CLI reads — one source of truth; every
  other line of the file is left byte-identical. Changes apply on the next
  Start.
- **Status** — a running indicator backed by the recorder's status file;
  failed starts surface the exit reason / last error in the menu. Sessions
  started from a terminal show as "external" and are not signalled from the
  GUI.
- **Capture warning** — if a stream delivers only silence for ~30 s (e.g. the
  system-audio permission was denied, or the mic is hardware-muted), the icon
  gains a warning badge and the menu shows a hint naming the likely cause.
  The session keeps recording; the warning clears on its own once real audio
  arrives.
- **Logs** — recorder output lands in `~/Library/Logs/Onoats/onoats-bot.log`.
- **First run (TCC prompts)** — the first Start prompts for **Microphone**
  and records a **Screen & System Audio Recording** grant ("Onoats" appears
  in both panes of System Settings ▸ Privacy & Security). The system-audio
  prompt fires before the capture session starts streaming: the supervisor
  extends its startup wait (+120 s) while the dialog is unanswered and the
  menu bar shows "waiting for the system-audio permission prompt" — answer
  at human speed and the session proceeds, no restart needed.
  Grants persist across rebuilds and reinstalls (they key on the
  signing identity, not the binary — see
  [`native/README.md`](native/README.md)).

## Configuration

`onoats init` writes:

- `$XDG_CONFIG_HOME/onoats/config.toml` — `[storage]` (`data_dir`), `[devices]`
  (by name), `[stt]`, `[speakers]` (render-only display labels), `[categories]`,
  `[tuning]`.
- `$XDG_CONFIG_HOME/onoats/secrets.env` — `0600`, STT secrets only
  (`DEEPGRAM_API_KEY` / `STT_WS_TOKEN`). **No LLM keys.**
- `$XDG_CONFIG_HOME/onoats/dictionary.txt` — `wrong: correct` substitutions
  (applied by `convert`) + vocabulary terms (fed to STT as recognition bias).

Precedence: **process env var > config.toml / secrets.env > built-in default**.
So an automation driver can env-inject `ONOATS_DATA_DIR`, `STT_SERVICE`, etc.
without editing the file. A few runtime-only knobs are env-only (no `config.toml`
key) — notably the shutdown timers: on Ctrl+C the recorder drains the pipeline
(up to `SHUTDOWN_DRAIN_TIMEOUT_SEC`, default `8.0`) so a final in-flight
transcript lands before the flush, then hard-cancels (capped at
`SHUTDOWN_CANCEL_TIMEOUT_SEC`, default `2.0`) if the drain stalls.

### Audio source

Two capture backends:

- **`socket`** — the recommended macOS (14.4+) path: framed PCM16 from two
  per-branch unix sockets (mic → `me`, system → `them`) written by the native
  capturer. No loopback driver, no PortAudio device enumeration.
- **`portaudio`** (default) — PortAudio devices; system audio needs a loopback
  driver. This is the path for other platforms and for macOS below 14.4 —
  driver install and device selection in
  [docs/blackhole-fallback.md](docs/blackhole-fallback.md).

Select via env `AUDIO_SOURCE` or `config.toml`:

```toml
[audio]
source = "socket"                 # "portaudio" (default) | "socket"
mic_socket = "~/onoats/mic.sock"      # or env ONOATS_MIC_SOCKET
system_socket = "~/onoats/system.sock"  # or env ONOATS_SYSTEM_SOCKET
capturer_nonce = ""                # or env ONOATS_CAPTURER_NONCE (usually supervisor-set)
```

When `AUDIO_SOURCE=socket`, `onoats bot` runs a supervisor that mints a private
socket directory + generation nonce and spawns the capturer named by
`ONOATS_CAPTURER_BIN`. The capturer↔recorder wire format (framing, handshake,
endianness, backpressure, versioning) is pinned in
[`docs/audio-socket-contract.md`](docs/audio-socket-contract.md).

A one-off override is available on the command line — `onoats bot --source
socket` (or `--source portaudio`) — which sits at the top of the usual
precedence (CLI flag > env `AUDIO_SOURCE` > `config.toml` > default).

> **Status:** runnable end-to-end on macOS 14.4+. The native capturer +
> menu-bar app build from source — see [`native/README.md`](native/README.md)
> for the `make -C native setup` flow (one command: signing cert, app + CLI
> install, guided init; the app wires `ONOATS_CAPTURER_BIN`). On other
> platforms, or below macOS 14.4, keep the default `portaudio` source — a
> loopback driver remains the system-audio fallback there
> ([docs/blackhole-fallback.md](docs/blackhole-fallback.md)).

### Data location

By default onoats stores everything under `$XDG_DATA_HOME/onoats`
(`~/.local/share/onoats`): `sessions/{pending,claimed,done,failed}/` (the queue),
`.active/` (live recording), `transcripts/{category}/{date}/` (converter output).

Point it elsewhere with `[storage] data_dir` (or `ONOATS_DATA_DIR`):

```bash
onoats init --data-dir ~/some/other/root
```

**Feeding another worker the same queue.** Because the queue layout is shared,
setting `data_dir` to a tree another tool drains makes onoats a drop-in
recorder for it:

```bash
onoats init --data-dir ~/koda-data   # write into the consumer's queue root
onoats bot                           # record → ~/koda-data/sessions/pending/
```

In this mode, let the **downstream worker** drain and render the queue — do
**not** also run `onoats convert` against the same root (the two would race for
the same `pending/` files). Run only one recorder against a given root at a time.

## The queue contract

onoats writes one type-discriminated JSONL file per session under
`queue/pending/{session_id}.jsonl`. This is the versioned inter-repo interface
a consumer drains.

| Line type      | Shape                                                              |
|----------------|--------------------------------------------------------------------|
| `session_meta` | `{"type":"session_meta","category":"<cat>"}` — optional FIRST line |
| `utterance`    | `{"type":"utterance","time":...,"text":...,"source":"me"\|"them"}` |
| `silence_gap`  | `{"type":"silence_gap","time":...,"duration_seconds":N}`           |

- The `source` field is the **canonical `me`/`them` enum** — the frozen wire
  contract. Configurable speaker display labels (`[speakers]`) are applied
  **only at render time**, never written into the queue.
- The chosen `--category` rides on the `session_meta` first line (not the
  filename); the `{session_id}` stem stays load-bearing for a consumer's
  back-fill keying.
- `active → pending` is an atomic `rename(2)`; a partial file is never visible
  in `pending/`.

## License

BSD-2-Clause — see [LICENSE](LICENSE).
