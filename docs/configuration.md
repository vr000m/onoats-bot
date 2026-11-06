# Configuration

[Back to the project README](../README.md)

`onoats init` creates the configuration directory under
`$XDG_CONFIG_HOME/onoats` (normally `~/.config/onoats`).

## Files

### `config.toml`

The main configuration contains:

- `[storage]`: `data_dir`;
- `[devices]`: PortAudio device names;
- `[audio]`: capture source and manual socket paths;
- `[stt]`: service, model, language, websocket endpoint, launchd label, and
  diarization;
- `[speakers]`: render-time display labels;
- `[categories]`: allowed and default categories;
- `[tuning]`: recorder timing and buffering values; and
- `[app]`: native app settings such as `launch_at_login`.

### `secrets.env`

This file has mode `0600` and stores STT credentials only:

~~~dotenv
DEEPGRAM_API_KEY=...
STT_WS_TOKEN=...
~~~

Onoats does not need or store LLM keys.

### `dictionary.txt`

Each `wrong: correct` line defines a conversion substitution. Other terms
become STT vocabulary hints.

## Precedence

The general order is:

~~~text
CLI flag > process environment > config.toml or secrets.env > built-in default
~~~

An automation driver can inject `ONOATS_DATA_DIR`, `STT_SERVICE`, and
other values without editing the files. A non-empty but invalid environment
value still wins where the relevant option documents fail-closed or fallback
behaviour.

## Speech to text

`[stt].service` supports:

- `websocket`: local `pipecat-local-stt-server`;
- `deepgram`: hosted Deepgram; and
- `whisper`: local Whisper-MLX on Apple Silicon.

`[stt].language` defaults to `"en"`. Use `"auto"` for detection on
Whisper and websocket backends.

### Websocket endpoint

Configure a Unix socket:

~~~toml
[stt]
service = "websocket"
ws_socket = "~/Library/Caches/pipecat-stt/stt.sock"
~~~

Or loopback TCP:

~~~toml
[stt]
service = "websocket"
ws_host = "127.0.0.1"
ws_port = 9565
~~~

`ws_socket` outranks the host/port pair. `STT_WS_SOCKET`,
`STT_WS_HOST`, and `STT_WS_PORT` outrank the config file. With no explicit
endpoint, onoats uses a live legacy cache socket when available, otherwise
`127.0.0.1:9565`.

### launchd self-healing

Set `[stt].launchd_label`, or `STT_LAUNCHD_LABEL`, to let onoats request a
restart when the websocket server is unreachable at startup or drops during a
session.

The label must match:

~~~text
[A-Za-z0-9][A-Za-z0-9._-]{0,127}
~~~

Invalid labels are logged and treated as absent.

Onoats can bootstrap an unloaded matching user agent only from:

~~~text
~/Library/LaunchAgents/<launchd_label>.plist
~~~

The plist must:

- be a regular, user-owned file;
- not be group- or world-writable;
- contain the exact configured `Label`; and
- set `RunAtLoad=true`.

Onoats validates a descriptor-backed snapshot of the file. It never infers a
launch agent from the endpoint port and never accepts an arbitrary plist path.
A successful bootstrap is the launch request; onoats does not immediately
kickstart a job that is already booting.

Recovery is reported after a successful STT handshake. The process-wide,
label-keyed kickstart cooldown resets only after a confirmed transcript event.

## Speaker diarization (opt-in)

Diarization labels speakers on the **them** channel:

~~~markdown
**Them (S1):** ...
**Them (S1 / S2, overlapping):** ...
~~~

The **me** channel is never diarized. Labels are per-run model slots, not
identities, and a downstream consumer must map them to names.

### Enable

Set:

~~~toml
[stt]
service = "websocket"
diarization = true
~~~

Or export `ONOATS_DIARIZATION=1`. Accepted values are
`1/true/yes/on` and `0/false/no/off`, case-insensitively. A blank
environment value falls through to `config.toml`; any other non-empty value
wins, logs one warning, and disables diarization.

Diarization requires websocket STT. Other backends exit non-zero at startup,
while `onoats bot-single` ignores the flag with a warning.

### Install and cache the model

From a checkout:

~~~bash
uv sync --extra diarization
~~~

For a tool install:

~~~bash
uv tool install 'onoats[macos,diarization]'
~~~

For the native app:

~~~bash
make -C native install EXTRAS=macos,diarization
~~~

A later plain `make -C native install` resets the installed extras to
`macos`.

Download the weights explicitly; onoats never downloads them:

~~~bash
uvx --from huggingface_hub hf download mlx-community/Nemotron-3-Diarization
~~~

If the extra, weights, or required STT ordering contract is missing, startup
fails before audio capture and prints the remediation.

### Labels and limitations

- The more active speaker appears first in an overlap label.
- Labels remain unique within one recorder run and can exceed `S8`.
- A slot idle for more than 20 minutes receives a new number when it returns.
  Tune this with `[stt].diarization_idle_remint_sec` or
  `ONOATS_DIARIZATION_IDLE_REMINT_SEC`.
- A speaker may split across slots, and quiet or overlapped speech may remain
  unlabelled.
- Runtime failure is fail-open: onoats logs once and continues transcription
  without labels.
- Labelling can delay a them-channel line by about 1.5 seconds.
- A continuation flush can move a line still awaiting a label into the next
  session file.

The [queue contract](queue-contract.md) defines persisted `speakers`,
`overlap`, and diarization provenance.

## Audio source

Onoats supports two capture backends:

- `socket`: recommended on macOS 14.4+. The native capturer writes framed
  PCM16 from microphone and system audio to separate Unix sockets. This path
  does not import or enumerate PortAudio devices.
- `portaudio`: the cross-platform default. System audio needs a loopback
  device such as BlackHole.

Select a source in `config.toml`:

~~~toml
[audio]
source = "socket"                        # "portaudio" | "socket"
mic_socket = "~/onoats/mic.sock"         # or ONOATS_MIC_SOCKET
system_socket = "~/onoats/system.sock"   # or ONOATS_SYSTEM_SOCKET
capturer_nonce = ""                      # usually supervisor-managed
~~~

Or use `AUDIO_SOURCE` and `onoats bot --source socket`. The CLI flag has
the highest precedence.

In normal socket mode the supervisor creates a private socket directory and
generation nonce, exports both socket paths, and launches
`ONOATS_CAPTURER_BIN`. The transport never reconnects itself; EOF, framing
failure, read-idle timeout, staging failure, and consumer stall are fatal to
that session.

The [audio-socket contract](audio-socket-contract.md) defines framing,
handshake, endianness, backpressure, and versioning. Use the
[BlackHole guide](blackhole-fallback.md) for the PortAudio fallback.

## Data location

The default data root is:

~~~text
$XDG_DATA_HOME/onoats
~/.local/share/onoats
~~~

Its main paths are:

~~~text
sessions/{pending,claimed,done,failed}/
.active/
transcripts/{category}/{date}/
~~~

Set `[storage] data_dir`, `ONOATS_DATA_DIR`, or:

~~~bash
onoats init --data-dir ~/some/other/root
~~~

### Sharing a queue

Point `data_dir` at another worker's queue root:

~~~bash
onoats init --data-dir ~/koda-data
onoats bot
~~~

Onoats then writes sessions to `~/koda-data/sessions/pending/`. Let only
one consumer drain that queue. Do not run `onoats convert` against the same
root while another worker claims pending files.

Run only one recorder against a given data root. The recorder holds an atomic
single-instance lock for its entire lifetime.

## Native app

`[app].launch_at_login` is read by Onoats.app, not Python. Accepted TOML
values are `true` and `false`; quoted equivalents normalise to the same
value. Omission performs no action. Invalid values appear as warnings.

The app checks the setting at launch. Explicit `false` unregisters the login
item. There is no menu toggle; edit `config.toml`, relaunch the app, and
approve it in **System Settings → Login Items** if macOS requires approval.

## Shutdown timing

Two runtime-only environment values bound graceful shutdown:

- `SHUTDOWN_DRAIN_TIMEOUT_SEC` defaults to `8.0`; and
- `SHUTDOWN_CANCEL_TIMEOUT_SEC` defaults to `2.0`.

The first signal drains the pipeline so a final in-flight transcript can land
before rotation. If draining stalls, onoats cancels within the second bound.
