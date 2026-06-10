# onoats native (macOS) — build, sign, and the Phase-4 Pre-req spikes

This directory holds the native macOS half of the socket-audio path (dev plan
`docs/dev_plans/20260609-feature-milestone-b-macos-capture-menubar.md`). The pip
wheel ships **no** native code; it discovers the capturer via `ONOATS_CAPTURER_BIN`.

> **Build-from-source + self-signed only.** No notarization, no Homebrew. The
> bundle is signed with a **stable self-signed "Code Signing" certificate** so the
> mic + system-audio TCC grants survive rebuilds. Public distribution (Developer ID
> + notarize) is out of scope here.

## Step 0 (one-time): create the stable self-signed "Code Signing" certificate

TCC persistence is keyed to the bundle's **designated requirement**, which derives
from the signing certificate. An **ad-hoc** signature (`codesign -s -`) gets a new
identity every build and re-prompts forever — so we use a stable self-signed cert.

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

## Phase 4: production capturer (`onoats-capturer/`)

Build + sign from `native/` (NOT from `spike/` — that Makefile builds the
throwaway spike):

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

## Phase-4 Pre-req spikes (BLOCKING — run before any production Swift)

Two unverified Apple-platform premises gate Phase 4. Resolve **both** here.

All commands run from `native/spike/`:

```sh
cd native/spike
```

### Pre-flight: OS floor + compile

```sh
sw_vers -productVersion          # must be ≥ 14.4 (Core Audio process-tap floor)
make build                       # compile-only; catches API errors before perms
```

### Spike 3 — TCC persistence (mic + system audio, across 3 rebuilds)

> **Attribution gotcha (learned the hard way, 2026-06-09).** TCC attributes a
> grant to the **responsible GUI process at the session root**. A helper
> `posix_spawn`'d from a **terminal** (`shell → uv → python → capturer`) is
> attributed to the **terminal**, and inherits *its* grants — so running this from
> a terminal is a **false-pass confound** (you'd be testing your terminal's grants,
> not the bundle's). The menu-bar topology's responsible process is `Onoats.app`
> itself, so the faithful test **GUI-launches the app** via `open` (LaunchServices),
> making `Onoats.app` its own responsible process. The tell: a **brand-new** bundle
> id must report `mic_pre=0` (notDetermined) and **prompt**, attributed to "Onoats".
> A non-zero `mic_pre` on first run means attribution went elsewhere.

The helper writes results to `/tmp/onoats-spike-result.txt` (a GUI launch has no
stdout). Run 1 — obtain the grants against the bundle identity:

```sh
make sign                        # rebuild + codesign + print the DR
make dr  > /tmp/onoats-dr-1.txt  # capture the designated requirement (run 1)
make cdhash                      # note the cdhash (it WILL change — that's fine)
: > /tmp/onoats-spike-result.txt # clear the result log

open Onoats.app                  # GUI launch → responsible process = Onoats.app
#   Expect TWO prompts attributed to "Onoats" (mic + system audio) — ACCEPT both.
cat /tmp/onoats-spike-result.txt #   → expect: mic_pre=0 mic=PASS system=PASS
```

Confirm **"Onoats"** now appears in System Settings ▸ Privacy & Security ▸
**Microphone** and ▸ **Screen & System Audio Recording**. Then **rebuild + re-sign
3×** and confirm macOS does **not** re-prompt and both grants persist (now
`mic_pre=3`, no prompt):

```sh
make rebuild && make dr > /tmp/onoats-dr-2.txt
open Onoats.app && sleep 6 && cat /tmp/onoats-spike-result.txt  # mic_pre=3, no prompt

make rebuild && make dr > /tmp/onoats-dr-3.txt
open Onoats.app && sleep 6 && cat /tmp/onoats-spike-result.txt  # mic_pre=3, no prompt

make rebuild && make dr > /tmp/onoats-dr-4.txt
open Onoats.app && sleep 6 && cat /tmp/onoats-spike-result.txt  # mic_pre=3, no prompt

# The designated requirement MUST be byte-identical across rebuilds:
diff /tmp/onoats-dr-1.txt /tmp/onoats-dr-2.txt && \
diff /tmp/onoats-dr-1.txt /tmp/onoats-dr-3.txt && \
diff /tmp/onoats-dr-1.txt /tmp/onoats-dr-4.txt && echo "DR STABLE ✓"
```

**PASS:** run 1 shows `mic_pre=0` + two "Onoats" prompts → `mic=PASS system=PASS`;
runs 2–4 show `mic_pre=3` with **no** re-prompt; the four DR files are byte-identical
(the cdhash differing is expected). The end-to-end menu-bar→supervisor→capturer path
is verified later in Phase 5b (the embedded capturer shares the bundle id, so this
grant covers it).
**FAIL → STOP:** if run 1 re-shows `mic_pre≠0` (attribution still wrong) or grants
don't persist, the `$0` / no-notarization conclusion (Open Question 2) reopens and
needs the paid Developer ID path. Record the failure in the plan's `## Findings`.

### Spike 4 — Core Audio tap recipe + residue

> **ALWAYS test the SIGNED app (`open Onoats.app --args …`), not the unsigned
> `./onoats-capturer`.** The unsigned standalone blocks **~3.9 s** inside
> `AudioHardwareCreateProcessTap` (per-call security verification) and is
> intermittently flaky; the **signed** bundle creates the tap in **~200 ms** with no
> audible dropout. Results land in `/tmp/onoats-spike-result.txt`. Use `--mute
> unmuted` (the default) — other apps stay audible. (Residue/leak checks below run
> the standalone deliberately, because signing is irrelevant to object lifecycle.)

```sh
make sign
: > /tmp/onoats-spike-result.txt

# Real system-output stream, other apps stay audible (signed → ~200ms, no stutter):
open Onoats.app --args tap --seconds 10        # play music in another app; KEEP LISTENING
# Concurrent mic + system (no clock-domain conflict):
open Onoats.app --args concurrent --seconds 10 # speak AND play audio
sleep 12; cat /tmp/onoats-spike-result.txt
#   expect: TAP … peak>0 PASS  and  CONCURRENT mic=PASS system=PASS

# Residue: start/kill -9 ×3 must leave no stale aggregate OR tap.
for i in 1 2 3; do
  ./onoats-capturer tap --seconds 30 & pid=$!; sleep 2; kill -9 $pid; wait $pid 2>/dev/null
done
./onoats-capturer list-aggregates   # expect: RESIDUE: none
./onoats-capturer list-taps         # expect: TAPS: none   (clean-taps to force-sweep)
```

**PASS:** `tap` yields a real stream (peak > 0) while other apps keep playing,
`concurrent` streams both, and after start/kill/start ×3 both `list-aggregates` and
`list-taps` report none.
**FAIL → STOP:** Open Question 1 (system-audio API) reopens; Phase 4 must be built
on a different primitive. Record it in `## Findings`.

> **Two TCC services — CONFIRMED.** Both `NSMicrophoneUsageDescription` and
> `NSAudioCaptureUsageDescription` are real, separate services: macOS lists
> **Microphone** and **System Audio Recording Only** as distinct panes and the tap
> added "Onoats" to the latter. Only the **mic** prompt is interactive; the
> system-audio grant is recorded without a visible separate prompt yet capture works.

## After the spikes

Record both spike outcomes (DR stability, grant persistence, tap PASS, residue
none) in the dev plan's `## Findings` and tick the two Pre-req acceptance boxes.
The throwaway `native/spike/` tree can be deleted once Phase 4 (`native/onoats-capturer/`)
is built from its proven recipe.
