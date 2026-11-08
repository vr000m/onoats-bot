# Menu-bar app

[Back to the project README](../README.md)

`make -C native setup` installs **Onoats.app** in `~/Applications`.
Launch it through Finder, Spotlight, or LaunchServices so macOS treats Onoats
as the microphone and system-audio permission subject. The app has no Dock icon;
its stateful menu-bar icon opens a compact control panel.

## Primary controls

### Start, Stop, and Flush

Start launches `onoats bot` through the socket supervisor and native
capturer. Stop asks the recorder to drain in-flight audio and rotate its final
buffer. Flush rotates the current buffer without ending the session.

Normal Stop and Flush requests use the recorder-owned Unix control socket, not
a PID signal. Every request names the complete PID and start-epoch generation,
and the recorder accepts only same-user requests for its current generation.
Missing, legacy, replaced, or recycled endpoints fail closed.

Direct `SIGUSR1` and `SIGTERM` remain compatibility inputs for terminals
and service managers. All control and signal requests enter one lifecycle
controller, so a Stop admitted first rejects later Flush requests, a Flush
admitted first completes before Stop, and a Flush is also rejected once a
termination signal has latched even if its Stop has not yet been processed. See the
[control-socket contract](control-socket-contract.md).

### Seminar

Before Start, Seminar adds `--category seminars` to the next app-owned
recording. During that session it becomes a read-only lock badge and resets
after exit.

Add `seminars` to `[categories] set` in `config.toml` first. If it is
missing, the failed-Start panel explains the problem and offers **Open Config**.

## Settings

The gear opens:

- the microphone picker;
- the configured STT service;
- the data-directory chooser;
- the latest-transcript preview preference;
- **Open Config**; and
- diagnostics, the current run log, and the logs folder.

Selecting a microphone changes the macOS system default input. The native
capturer binds the system default at Start. Settings displays both the default
input and output so the next recording does not silently use the wrong device.

The app edits the same `~/.config/onoats/config.toml` as the CLI, except for
the UI-only transcript-preview preference. A running session keeps its captured
settings; edits apply at the next Start.

The app reads `config.toml` through a bounded, non-blocking read (1 MiB limit,
regular files only). It never overwrites a config it could not read: Settings
shows the reason and leaves the file as it was. A symlinked `config.toml` is
written through to its target, so the link survives an edit.

## Session and data-directory authority

Each Start captures one immutable session configuration. Both STT branches,
status text, recovery policy, telemetry, diarization, and shutdown diagnostics
use that snapshot.

The data directory is also fixed for the run. If the configured path changes
while recording, the panel shows both **Recording to** and **Next recording**
until the current session ends.

Choose a data directory whose path is at most 75 bytes: a longer one makes
Start fail because the recorder's control-socket path would exceed macOS's
Unix-socket limit (see [configuration](configuration.md#data-location)).

## Status and activity

The lifecycle status file is authoritative for recorder identity, liveness,
warnings, and failures. A separate, display-only telemetry snapshot supplies
fresh audio levels, per-branch STT health, the locked category, and the optional
transcript preview.

Audio activity states mean:

| State | Meaning |
|---|---|
| Signal | a fresh positive audio level |
| Silent | a fresh zero audio level |
| No signal | no fresh observation for that branch |
| Unavailable | telemetry is missing, stale, malformed, unsupported, or from another run |

Websocket STT is ready only when both branches report ready. Whisper and
Deepgram display **status not reported** because they do not publish the same
health signal.

Telemetry is fail-open. A telemetry or rich-status publication failure never
stops capture, control, or transcript persistence. The app falls back to the
owned process exit and run log.

## Privacy

The latest-transcript preview is off by default. When enabled, it is bounded
and display-only; complete utterances remain in the canonical session JSONL.
The app never renders the run log in the panel.

## Warnings

### Capture warning

If a stream produces only silence for about 30 seconds, the icon gains a
warning badge and the panel names the likely cause, such as denied system-audio
permission or a hardware-muted microphone. Recording continues, and the warning
clears when real audio arrives.

### Automatic recovery from a stalled capture

If the native system-audio pacer stalls for more than two seconds of process
uptime, the current capture generation is no longer trusted. The supervisor
rotates the partial session and exits with a matching
`capture-heartbeat-stalled` status. The menu-bar app then shows **Recovering…**
and starts a wholly fresh recorder/capturer generation automatically.

Recovery preserves the recording's pinned data directory and Seminar choice,
but it creates a new session and run log. **Cancel Recovery** stops a pending or
starting replacement. Stop and Quit remain authoritative and never trigger a
replacement.

The app permits one automatic restart per failure cluster. The replacement must
remain authoritatively running for 60 seconds before another automatic recovery
is allowed. Missing or replaced PID authority restarts that full window, and a
live external successor cancels the app replacement. If the replacement stalls
earlier, the panel remains visibly failed instead of entering a restart loop. A
manual Start begins a new cluster. Direct CLI and externally started recordings
do not use this app-owned recovery path.

### Login item warning

`[app].launch_at_login` accepts TOML `true` or `false`. Invalid values,
registration failures, and unregister failures appear as non-blocking warnings.
Omitting the key performs no action and never silently opts the user in.

The app synchronises the requested state at launch. macOS may still require
approval in **System Settings → Login Items**.

## Logs

Every app-owned Start creates:

~~~text
~/Library/Logs/Onoats/onoats-bot-<spawnEpoch>.log
~~~

The app keeps the newest ten logs. Failure surfaces can open the current log;
**Settings → Diagnostics** can also reveal the logs folder. External sessions
are labelled as not captured by the menu bar.

## First-run permissions

The first Start requests:

1. **Microphone** permission; and
2. **Screen & System Audio Recording** permission.

The native capturer triggers the system-audio prompt before binding its sockets.
When the prompt is visible, the supervisor extends startup once by 120 seconds
and the panel reports that it is waiting. Answer the prompt without restarting
the session.

Grants persist across rebuilds because they key on the signing identity. See the
[native guide](../native/README.md) for TCC and signing details.

## Brand assets

The canonical grain-wave mark and wordmark live in
[`assets/brand`](../assets/brand). Its README defines palette, spacing, and
export rules. The app icon, header, and state-specific template icons under
`native/onoats-menubar/Resources` are native exports; other surfaces should
reuse the canonical SVGs rather than upscale those raster files.
