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
| System loopback (BlackHole/etc.)  | driver-dependent            | BlackHole         |

The baseline ships **MLX-free**: `mlx-whisper` is only in the `[macos]` extra
and its imports are lazy, so `onoats bot` runs off-mac with PortAudio plus
either Deepgram or a TCP-reachable `pipecat-local-stt-server`.

## Configuration

`onoats init` writes:

- `$XDG_CONFIG_HOME/onoats/config.toml` — `[devices]` (by name), `[stt]`,
  `[speakers]` (render-only display labels), `[categories]`, `[tuning]`.
- `$XDG_CONFIG_HOME/onoats/secrets.env` — `0600`, STT secrets only
  (`DEEPGRAM_API_KEY` / `STT_WS_TOKEN`). **No LLM keys.**
- `$XDG_CONFIG_HOME/onoats/dictionary.txt` — `wrong: correct` substitutions
  (applied by `convert`) + vocabulary terms (fed to STT as recognition bias).

Precedence: **process env var > config.toml / secrets.env > built-in default**.
So an automation driver can env-inject `ONOATS_DATA_DIR`, `STT_SERVICE`, etc.
without editing the file.

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

See the repository for license details.
