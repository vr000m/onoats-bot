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

For terminal-driven use without the menu-bar app there is
`make -C native setup-cli`: cert → build + sign the **bare** capturer
(`build/onoats-capturer`, signed with the same identity + identifier, so the
designated requirement matches the bundle's) → `install-cli` → guided init.
It prints the `AUDIO_SOURCE=socket ONOATS_CAPTURER_BIN=… onoats bot` run line
at the end. Same idempotency as `setup`. Caveat: launched from a terminal,
TCC grants attribute to the terminal app, not to onoats (see "Launch from
`~/Applications`" below); and a later `make build`/`make setup` recompile
drops the bare binary's signature — re-run `setup-cli` after updates.

`install-cli` (and so `install`, `setup`, `setup-cli`) installs the `macos`
extra by default. `uv tool install` replaces the environment's extras, so a
plain `make install` removes any other extra you added, such as
`diarization`. Pass the full set to keep it:
`make -C native install EXTRAS=macos,diarization`.

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

Day-to-day usage (the window panel's Start/Stop/Seminar hierarchy, Flush,
Settings/Diagnostics, live display telemetry, per-run logs, and first-run TCC
prompts) lives in the [detailed menu-bar guide](../docs/menu-bar.md). The
internals behind that surface:

- **Launch from `~/Applications` via the GUI** — that's the point:
  LaunchServices makes `Onoats.app` its own TCC responsible process,
  restoring ~200 ms tap creation; terminal launches attribute grants to the
  terminal.
- **Start** runs `~/.local/bin/onoats bot` with `AUDIO_SOURCE=socket` and
  `ONOATS_CAPTURER_BIN` pointing at the embedded capturer. Override the CLI
  path with `defaults write net.varunsingh.onoats cliPath /abs/path`.
- **Stop** asks the supervisor it spawned for a graceful drain — never the
  capturer directly. A verified external session routes Stop and Flush through
  the recorder-owned, generation-authenticated control socket; neither command
  falls back to PID signalling. Control requests and direct compatibility
  signals enter one Python `RecorderLifecycleController`; the control server and
  signal handlers are adapters, not independent locks or rotation owners.
- Quit requests the same owned graceful stop during startup and recording,
  then waits for the recorder and any in-flight Flush/Stop helper to exit.
  A 15-second timeout keeps the app open with a cleanup error and retained
  process handles. Quitting an external session leaves its recorder running.
- The Settings "Mic (me)" picker sets the macOS **default input device** because the
  capturer binds the system default at start — the only selection that
  actually applies. Named device+STT profiles are a follow-up (need capturer
  `--mic-uid`).
- The lifecycle model reads the Phase-5a status file
  (`<data_dir>/.active/onoats.status.json`, schema-guarded) with the pid file
  as liveness backstop. Display-only live data comes from the separate,
  versioned `<data_dir>/.active/onoats.ui.json`: fresh per-branch levels,
  branch/aggregate STT state, locked category, and the optional bounded
  transcript preview. The consumer rejects stale ownership, freshness, schema,
  numeric, and aggregate mismatches; missing telemetry degrades the panel and
  never changes recorder liveness or success. Snapshot writes and cleanup use
  one daemon I/O worker with bounded coalescing, so a slow data volume cannot
  block capture or app-requested recorder shutdown. If initial running-status
  publication fails after PID/control publication, recording and control remain
  live, UI telemetry is disabled for that run, and the app derives the result
  from the owned process exit and run log.
- Native liveness caches use the complete recorder generation: PID, stored
  command fingerprint, and positive start epoch. Incomplete or changed
  generations miss the cache. A requested Stop does not turn a non-zero owned
  process exit into success; diagnostic status is consumed only when its
  generation, spawn floor, and active directory match.
- The recorder captures one immutable `RecorderSessionConfig` per Start. Both
  STT arms, banner, telemetry, recovery/kickstart policy, diarization, and
  shutdown diagnostics retain that snapshot until exit; configuration edits
  are next-session changes.
- Data dir resolves from `~/.config/onoats/config.toml` `[storage].data_dir`,
  else `~/.local/share/onoats` (a GUI app sees no shell env, so
  `ONOATS_DATA_DIR`/XDG exports don't apply here). The model snapshots this path
  at Start and uses it for launch, status/PID/telemetry reads, Stop/Flush, and
  exit handling until that run ends. A mid-run config change is shown as the
  next recording directory rather than silently moving the active run.
- **First Start after a fresh install / permission reset:** the system-audio
  TCC prompt fires at tap creation. Since release-plan Phase 7 the capturer
  creates the tap **before** its sockets exist (announced by `ONOATS-EVENT
  waiting-for-permission`), so the supervisor extends its socket wait
  (+120 s) while the dialog is unanswered and the menu bar shows
  "waiting for the system-audio permission prompt". Answer the prompt at
  human speed and the session proceeds — no restart needed.
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
- **Diagnostics and logs:** every app-owned Start reserves one
  `~/Library/Logs/Onoats/onoats-bot-<spawnEpoch>.log` with exclusive creation;
  the recorder and app-owned Flush helpers receive duplicates of that retained
  append descriptor and the newest ten are kept. App-owned Stop terminates the
  retained supervisor and does not spawn a Stop writer. The descriptor, not the
  replaceable pathname, remains the output and diagnostic-read authority until
  the recorder and every duplicate writer have exited.
  Open Log resolves the current app process's latest run first; Show Logs Folder
  always reveals the containing directory. External-session Stop and Flush
  output is not written to an app-owned run log, and no log tail is displayed
  in the panel.
- **Brand resources:** `../assets/brand/` owns the canonical grain-wave SVG mark,
  wordmark, palette, spacing, and export map. `onoats-menubar/Resources/` contains
  only the required native exports: header art, `.icns`, and state-specific
  monochrome template icons. `make app` starts from a clean bundle and copies
  each named resource explicitly; `make verify-resources` equality-checks that
  allowlist before `make sign`. After the gauntlet, run
  `make -C native app-tree-hash`
  and smoke the already-signed staged bundle before installation:

  ```sh
  SMOKE_DATA_DIR="$(mktemp -d)"
  trap 'rm -rf "$SMOKE_DATA_DIR"' EXIT
  ```

  First create the mandatory release identity from the signed staged bundle and
  the clean CLI checkout revision:

  ```sh
  python3 native/install_staged.py release-identity native/Onoats.app \
    --cli-git-identity "$(git rev-parse HEAD)" \
    --output /tmp/onoats-release-identity.json
  ```

  Pass that same immutable record to smoke. It contains the canonical bundle
  identifier, designated requirement, cdhash, tree hash, main-executable hash,
  and CLI git identity. The smoke re-derives it before and after recording and
  verifies the running app PID's designated requirement and cdhash through
  Security.framework; static path/signature checks alone are insufficient.

  ```sh
  python3 native/installed_recording_smoke.py \
    --app native/Onoats.app --data-dir "$SMOKE_DATA_DIR" \
    --release-identity /tmp/onoats-release-identity.json \
    --smoke-receipt /tmp/onoats-smoke-receipt.json
  ```

  The smoke is interactive: speak when prompted. It passes only after fresh Me
  telemetry and a durable Me utterance from the attested recorder run. Its
  TERM/KILL cleanup rechecks the captured macOS process birth immediately before
  delivery, but the userspace check and signal are not generation-atomic; that
  residual race is accepted. For an explicit clean install from the repository
  root, run:

  ```sh
  ONOATS_INSTALL_EXPECTED_IDENTITY=<app-tree-hash-output> \
    ONOATS_INSTALL_RELEASE_IDENTITY=/tmp/onoats-release-identity.json \
    ONOATS_INSTALL_SMOKE_RECEIPT=/tmp/onoats-smoke-receipt.json \
    ONOATS_INSTALL_ROLLBACK_APPROVED=0 \
    make -C native install-staged
  ```

  This installs an immutable verified snapshot without rebuilding or
  re-signing it. The canonical identity
  includes paths, entry types, executable modes, and file bytes. Private install
  and rollback copies reject unsupported entry types and group/other-writable
  modes, strip ACLs, retain code-signing metadata, and require every allowlisted
  resource to be a regular non-symlink file. Do not fix
  or otherwise mutate the bundle after signing; any fix requires rebuilding,
  re-running the gauntlet, and signing again.

Rollback-capable publication is a separate, fail-closed command. Before using
it, verify and record the prior app's designated-requirement hash and cdhash,
plus the prior CLI SHA, branch/detached state, exact `install-cli` command and
`EXTRAS`, shim path, and checkout-owned import path. Then, from the repository
root, run:

```sh
ONOATS_INSTALL_EXPECTED_IDENTITY=<smoked-app-tree-hash> \
  ONOATS_INSTALL_RELEASE_IDENTITY=/tmp/onoats-release-identity.json \
  ONOATS_INSTALL_SMOKE_RECEIPT=/tmp/onoats-smoke-receipt.json \
  ONOATS_INSTALL_ROLLBACK_APPROVED=1 \
  ONOATS_INSTALL_BASELINE_DR_HASH=<verified-sha256> \
  ONOATS_INSTALL_BASELINE_CDHASH=<verified-cdhash> \
  make -C native install-staged
```

Only that approved mode publishes a rollback record. Restore it with
`make -C native rollback`; rollback copies and extracts one immutable
descriptor-backed archive snapshot and verifies it before atomically publishing
the canonical `~/Applications/Onoats.app`. Restore verification uses the
archive's recorded tree hash, designated-requirement hash, cdhash, and archive
hash rather than the current checkout's resource allowlist, so an approved older
bundle remains restorable after the allowlist changes. The current release is a clean-install
run because its pre-change CLI baseline was incomplete, so it must use
`ONOATS_INSTALL_ROLLBACK_APPROVED=0` and must not invoke rollback.

All staged-installer operator input is environment-only data read directly by
`install_staged.py`: `ONOATS_INSTALL_APP`, `ONOATS_INSTALL_DIR`,
`ONOATS_INSTALL_BACKUP_DIR`, `ONOATS_INSTALL_ROLLBACK_RECORD`,
`ONOATS_INSTALL_EXPECTED_IDENTITY`, `ONOATS_INSTALL_ROLLBACK_APPROVED`,
`ONOATS_INSTALL_BASELINE_DR_HASH`, `ONOATS_INSTALL_BASELINE_CDHASH`, and
`ONOATS_INSTALL_BUNDLE_ID`, plus mandatory
`ONOATS_INSTALL_RELEASE_IDENTITY` and mandatory
`ONOATS_INSTALL_SMOKE_RECEIPT`. The smoke invalidates any prior receipt before
launch and atomically publishes a receipt only after recording, identity
re-verification, and cleanup succeed. The installer binds that receipt to the
canonical release-identity digest before any LaunchServices query or mutation.
`ONOATS_INSTALL_BUNDLE_ID`, when supplied, may only redundantly name the
canonical identifier; it is rejected before any LaunchServices query or
installation mutation otherwise.
Python owns defaults and derives the app name. The former Make-variable names
are rejected for `app-tree-hash`, `install-staged`, and `rollback`; staged
inputs must use the `NAME=value make ...` environment-prefix form shown above.
Their values are never expanded by Make.

Release CI runs the full suite on Python 3.12 and a separate recorder-contract
lane on Python 3.13. PyPI publication depends on both jobs.

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

- **Startup order is load-bearing:** mic TCC grant → emit
  `waiting-for-permission` → start the mic engine → start the
  system-tap/aggregate/IOProc chain (the system-audio TCC-prompting call) →
  create both sockets → accept both → write both handshakes → attach both
  writers. Starting the mic before creating the process tap avoids a known
  `AudioDeviceStart` stall; pre-socket frames from either branch are dropped as
  pre-session audio.
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
