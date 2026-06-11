# System-audio capture with a loopback driver (PortAudio fallback)

The recommended macOS path captures system audio natively (Core Audio process
tap, `AUDIO_SOURCE=socket` — see the [README Quickstart](../README.md#quickstart)
and [Menu bar](../README.md#menu-bar-macos)). That path needs **macOS 14.4+**.
Everywhere else, the "them" stream comes from a **loopback driver** exposed as
a PortAudio input device:

- macOS 13.x–14.3 (below the Core Audio tap API floor)
- Linux / Windows / Intel macs without the native capturer
- any setup where you prefer not to build the native capturer

This is the default `portaudio` audio source — no native build required, just
`uv tool install --editable .` plus the driver below.

## 1. Install a loopback driver

A loopback driver presents the system's audio **output** as a recordable
**input** device.

**macOS — [BlackHole](https://github.com/ExistentialAudio/BlackHole)** (the
driver this doc assumes):

```bash
brew install blackhole-2ch
```

**Other platforms:** any virtual loopback device that shows up as a PortAudio
input works the same way — e.g. VB-Cable on Windows, or a PulseAudio/PipeWire
monitor source on Linux (monitor sources usually exist out of the box; check
`onoats devices` for a `Monitor of …` entry before installing anything).

## 2. Route system audio through the driver (macOS)

BlackHole only carries what is played **into** it, and a Mac can only play to
one output device — so to record system audio *and* keep hearing it, create a
**Multi-Output Device**:

1. Open **Audio MIDI Setup** (Applications ▸ Utilities).
2. Click **+** (bottom-left) ▸ **Create Multi-Output Device**.
3. Tick both your normal output (speakers/headphones) and **BlackHole 2ch**.
4. Select that Multi-Output Device as the system **output** (Sound settings,
   or option-click the menu-bar volume icon).

Audio now plays to your speakers and is simultaneously recordable from the
"BlackHole 2ch" input device. (Caveat of the multi-output approach: macOS
volume keys don't adjust a Multi-Output Device — set the speaker volume on
the underlying device.)

## 3. Point onoats at the devices

`onoats init` enumerates input devices and flags likely loopback candidates
with `[loopback?]` (it matches names containing *blackhole*, *loopback*,
*soundflower*, *aggregate*, or *vb-cable* — Linux monitor sources aren't
flagged, but work fine when selected). Pick your microphone as **Me** and the
loopback device as **Them**. The choices land in
`~/.config/onoats/config.toml`:

```toml
[audio]
source = "portaudio"        # the default — only needed if you set "socket" earlier

[devices]
mic = "MacBook Pro Microphone"   # "me" — your voice
system = "BlackHole 2ch"         # "them" — system/loopback audio
```

Devices are matched **by stable name** (not index — PortAudio indices reshuffle
when devices come and go). One-off overrides via env: `MIC_INPUT_DEVICE` /
`SYSTEM_INPUT_DEVICE` (index or name) take precedence over `config.toml`.

Verify with:

```bash
onoats devices    # the loopback device must appear as an INPUT
onoats bot        # then play audio + speak; both streams should transcribe
```

## Troubleshooting

- **"Them" transcript is empty** — system output isn't routed into the
  loopback device: re-check step 2 (is the Multi-Output Device actually
  selected as the system output?).
- **You can't hear anything** — the system output is set to the bare loopback
  device instead of the Multi-Output Device.
- **`onoats init` prints "no system-loopback device detected"** — the driver
  isn't installed, or needs a reboot/re-login to register with Core Audio.
- **Loopback device missing from `onoats devices`** — same as above; on
  Linux, ensure the PulseAudio/PipeWire monitor source is enabled.
