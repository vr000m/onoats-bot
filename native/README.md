# onoats native (macOS) — build, sign, and TCC internals

This directory holds the native macOS half of the socket-audio path (dev plan
`docs/dev_plans/20260609-feature-milestone-b-macos-capture-menubar.md`). The pip
wheel ships **no** native code; it discovers the capturer via `ONOATS_CAPTURER_BIN`.

This README covers **build, signing, and TCC internals**. For the install
commands and day-to-day menu-bar usage, see the top-level
[README Quickstart](../README.md#quickstart) and
[Menu bar (macOS)](../README.md#menu-bar-macos).

> **Build-from-source + self-signed only.** No notarization, no Homebrew. The
> bundle is signed with a **stable self-signed "Code Signing" certificate** so the
> mic + system-audio TCC grants survive rebuilds. Public distribution (Developer ID
> + notarize) is out of scope here.

## Step 0 (one-time): create the stable self-signed "Code Signing" certificate

TCC persistence is keyed to the bundle's **designated requirement**, which derives
from the signing certificate. An **ad-hoc** signature (`codesign -s -`) gets a new
identity every build and re-prompts forever — so we use a stable self-signed cert.

**Scripted (recommended):**

```sh
make -C native cert     # idempotent; no-op if the identity already exists
```

This runs `make_cert.sh`: openssl generates a self-signed cert with the
code-signing EKU and imports it into your login keychain with codesign
pre-authorized — no password prompts (verified: codesign needs neither keychain
trust nor a partition-list fix). **Do not regenerate the cert once you have TCC
grants** — a new cert means a new designated requirement and macOS re-prompts
for everything; the script refuses to overwrite an existing identity for this
reason.

**Manual fallback (Keychain Access GUI):**

1. Open **Keychain Access** → menu **Keychain Access ▸ Certificate Assistant ▸
   Create a Certificate…**
2. Set:
   - **Name:** `Code Signing`  ← must match `IDENTITY` in the Makefile exactly
   - **Identity Type:** Self Signed Root
   - **Certificate Type:** **Code Signing**
   - (optional) tick *Let me override defaults* and extend validity to a few years.
3. Create it; it lands in the **login** keychain.
4. On the override pages, set **Certificate Type / Extended Key Usage = Code
   Signing** (the assistant defaults to *SSL Server* — if you leave it, the cert is
   not a codesigning identity). A long validity (e.g. 3650 days) saves re-doing it.
5. Verify the tool can see it — **without `-v`**:
   ```sh
   security find-identity -p codesigning
   ```
   You should see one line containing `"Code Signing"`. Note: `find-identity -v`
   (valid-only) will report `0 valid identities` for a self-signed cert because it
   is not in the system trust store — **that is expected and fine**. codesign signs
   with it regardless, and TCC keys on the cert *identity* (the designated
   requirement), not on keychain trust.

You only do this once. The same cert signs the capturer and the menu-bar `.app`.

## Install / update (the whole story)

```sh
make -C native setup     # fresh clone, one command: cert (no-op if the
                         # identity exists) → build + sign Onoats.app →
                         # ~/Applications + CLI → ~/.local/bin/onoats →
                         # `onoats init` (only when no config.toml yet)
```

`setup` is safe to re-run: the cert is never regenerated (that would
invalidate the TCC grants — `make_cert.sh` refuses), and an existing
`config.toml` is never touched. Updating after a `git pull` is
`make -C native install` (everything `setup` does minus cert + init).
What each kind of change needs:

| Change                     | Needed action                                  |
|----------------------------|------------------------------------------------|
| Python source edits        | nothing (editable install picks them up)       |
| Python dependency changes  | `make -C native install-cli` (or full install) |
| Swift / bundle changes     | `make -C native install`                       |

The CLI is installed with `uv tool install --editable`, giving an isolated venv
under `~/.local/share/uv/tools/onoats/` and a stable shim at
`~/.local/bin/onoats` — this fixed path is what the menu-bar app invokes (a
LaunchServices-launched GUI app inherits no shell PATH). Reinstalling the app
bundle does **not** re-prompt TCC: grants key on the designated requirement
(bundle id + cert), not the filesystem path or cdhash.

## Phase 5b: menu-bar launcher (`onoats-menubar/`)

`Onoats.app` is the single native bundle: the SwiftUI menu-bar app
(`Contents/MacOS/Onoats`, `LSUIElement` — no Dock icon) with the capturer
embedded at `Contents/MacOS/onoats-capturer`. One bundle id ⇒ one TCC identity;
both binaries are signed with the same identity + identifier (capturer first,
then the bundle), so the DR is unchanged from the capturer-only bundle and
existing TCC grants carry over.

Day-to-day usage (Start/Flush/Stop, mic picker, Settings, status, log
location, first-run TCC prompts) lives in the top-level
[Menu bar (macOS)](../README.md#menu-bar-macos) section. The internals behind
that surface:

- **Launch from `~/Applications` via the GUI** — that's the point:
  LaunchServices makes `Onoats.app` its own TCC responsible process,
  restoring ~200 ms tap creation; terminal launches attribute grants to the
  terminal.
- **Start** runs `~/.local/bin/onoats bot` with `AUDIO_SOURCE=socket` and
  `ONOATS_CAPTURER_BIN` pointing at the embedded capturer. Override the CLI
  path with `defaults write net.varunsingh.onoats cliPath /abs/path`.
- **Stop** SIGTERMs the supervisor it spawned (graceful drain) — never the
  capturer directly.
- The "Mic (me)" picker sets the macOS **default input device** because the
  capturer binds the system default at start — the only selection that
  actually applies. Named device+STT profiles are a follow-up (need capturer
  `--mic-uid`).
- The running indicator reads the Phase-5a status file
  (`<data_dir>/.active/onoats.status.json`, schema-guarded) with the pid file
  as liveness backstop. Data dir resolves from `~/.config/onoats/config.toml`
  `[storage].data_dir`, else `~/.local/share/onoats` (a GUI app sees no shell
  env, so `ONOATS_DATA_DIR`/XDG exports don't apply here).
- **First Start after a fresh install / permission reset:** the system-audio
  TCC prompt fires at tap creation, which *blocks* while the dialog is
  unanswered — if you take longer than ~10 s to click, the recorder's
  read-idle watchdog fails the session loud (by design). Answer the prompt,
  then Start again.
- **All-zero watchdog:** if a branch's capture callbacks deliver only zero
  samples for 30 s, the capturer emits a machine-parseable
  `ONOATS-EVENT zero-run-warning` stderr line naming the likely cause
  (system → the Screen & System Audio Recording grant is denied — a denied
  tap still fires callbacks, all-zero; mic → hardware mute / wrong device).
  The supervisor's stderr reader reflects it into the status file's `warning`
  field, so it shows in the menu bar (badge icon + hint line) and
  `onoats status`; real audio re-arms the detector and clears it
  (`zero-run-clear`). Warning only, never a failure: an app rendering digital
  silence (paused player) triggers it benignly. Event format:
  `docs/audio-socket-contract.md` "Capturer event lines".
- **Settings** writes go through a surgical single-key TOML editor — every
  other line of `config.toml`, comments included, stays byte-identical.

## Phase 4: production capturer (`onoats-capturer/`)

Build + sign from `native/`:

```sh
cd native
make sign        # build → assemble Onoats.app → codesign (stable cert) → print DR
make print-bin   # the path to export as ONOATS_CAPTURER_BIN
```

Run the real session:

```sh
AUDIO_SOURCE=socket ONOATS_CAPTURER_BIN="$(make -s print-bin)" onoats bot
```

Design notes baked into the capturer (each learned the hard way — see the dev
plan `## Findings` for the evidence):

- **Startup order is load-bearing:** mic TCC grant → create both sockets →
  accept both → write both handshakes → only then start captures (tap first,
  then mic engine).
- **Copy-only IOProc.** Doing AVAudioConverter work in the Core Audio realtime
  callback makes the HAL silently stop calling it after ~5 cycles. The IOProc
  memcpys into a bounded queue; a worker thread resamples and chunks.
- **The tap delivers data only while something renders audio.** Each branch
  runs a 20 ms silence pacer so a quiet system doesn't trip the recorder's
  read-idle watchdog and the me/them timeline stays continuous.
- **`AudioHardwareCreateProcessTap` is intermittently flaky** (instant
  `noErr` + `kAudioObjectUnknown`) even signed — retried ×3, 500 ms apart.
- Full teardown on every exit path: `AudioDeviceStop` → IOProc → aggregate →
  tap; one socket closing (or `EPIPE`) tears down BOTH branches.

### Wire-contract checker

`wire_check.py` plays the recorder's role against a running capturer and
asserts the v1 contract (handshake incl. nonce echo, BE length prefix, 640-byte
whole-sample frames, monotonic `seq`/`captured_monotonic_ns`):

```sh
S=$(mktemp -d) && ./Onoats.app/Contents/MacOS/onoats-capturer \
  --mic-socket $S/mic.sock --system-socket $S/system.sock --nonce cafef00d &
python3 wire_check.py --mic-socket $S/mic.sock --system-socket $S/system.sock \
  --nonce cafef00d --seconds 10   # play audio + speak during this
```

The binary also has socket-less debug modes: `--selftest-tap` and
`--selftest-concurrent` (`--seconds N`).

### Residue check (manual-smoke step 8)

`residue_check.sh` automates the start → `kill -9` → ×3 loop against the
**production** binary with live socket clients (the tap + aggregate only exist
once a client connects), then asserts via the capturer's own maintenance
subcommands (`list-aggregates` / `list-taps`; `clean-taps` force-sweeps
leftover taps) that no `onoats-*` aggregate or process tap survived:

```sh
./residue_check.sh        # default 3 rounds; expect "residue check PASS"
```

(PASSED 2026-06-10 against the spike-based checker, re-PASSED 2026-06-11
against the production binary's own subcommands — see the dev plans'
manual-smoke checklists.)

## History: Phase-4 Pre-req spikes (archived)

A throwaway spike kit in `native/spike/` de-risked Phase 4 before any
production Swift was written: it proved TCC grant persistence across rebuilds
(byte-stable designated requirement from the stable self-signed cert) and the
Core Audio process-tap recipe, including kill-residue behavior. The tree was
deleted in Phase 6 of the 0.9→1.0 plan after its residue-enumeration commands
were ported into the production capturer (`Maintenance.swift`); the full kit —
sources, Makefile, and run-books — is preserved at the `spike-archive` git
tag, and the outcomes are recorded in the `## Findings` of
`docs/dev_plans/20260609-feature-milestone-b-macos-capture-menubar.md`.

Durable conclusions baked into this directory:

- A stable self-signed cert ⇒ byte-stable DR ⇒ mic + system-audio TCC grants
  survive rebuilds (Step 0; the whole reason `make cert` exists).
- **Microphone** and **Screen & System Audio Recording** are two distinct TCC
  services; only the mic prompt is interactive (confirmed by spike 3).
- Private aggregate devices are auto-reclaimed on process death, but process
  taps **survive SIGKILL** — the reason `residue_check.sh` exists.
- Unsigned binaries block ~4 s per `AudioHardwareCreateProcessTap` call in
  security verification; signed builds take ~200 ms.
