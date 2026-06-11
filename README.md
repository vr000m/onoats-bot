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

```bash
# Linux: PortAudio dev headers are needed to build pyaudio
#   sudo apt-get install -y portaudio19-dev   # (macOS Homebrew ships them)

uv tool install onoats            # baseline (PortAudio + Deepgram/TCP STT)
# uv tool install 'onoats[macos]' # Apple Silicon: Whisper-MLX + Kokoro

onoats init                       # guided setup → config.toml + 0600 secrets.env
onoats bot                        # dual-input recorder (mic + system loopback)
onoats convert                    # render pending sessions → markdown transcripts
```

Other subcommands:

```bash
onoats bot-single   # legacy mic-only recorder
onoats flush        # tell the running recorder to rotate its buffer now
onoats devices      # list audio input/output devices
onoats status       # recorder pid / running state + data dir
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
(`AUDIO_SOURCE=socket` — Core Audio process tap, no virtual-audio driver; built
from source, see [`native/README.md`](native/README.md) and
[Audio source](#audio-source) below). On macOS 13.x–14.3 (below the Core Audio
tap API floor) and on other platforms, the fallback is a loopback driver
(e.g. BlackHole) on the default PortAudio path.

The baseline ships **MLX-free**: `mlx-whisper` is only in the `[macos]` extra
and its imports are lazy, so `onoats bot` runs off-mac with PortAudio plus
either Deepgram or a TCP-reachable `pipecat-local-stt-server`.

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
  driver (e.g. BlackHole). This is the path for other platforms and for macOS
  below 14.4.

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
> for the one-time self-signed-cert setup and the `make -C native install`
> flow (it also installs the CLI and wires `ONOATS_CAPTURER_BIN`). On other
> platforms, or below macOS 14.4, keep the default `portaudio` source —
> BlackHole remains the system-loopback fallback there.

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
