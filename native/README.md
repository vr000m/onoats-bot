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

```sh
# Real system-output stream from other apps, without muting them:
uv run python supervisor-exec.py tap --seconds 10
#   While it runs, play music / a video in another app and KEEP LISTENING — the
#   other app must keep playing (tap is .unmuted). Expect: TAP frames=… peak>0 PASS.

# Concurrent mic + system capture (no clock-domain conflict):
uv run python supervisor-exec.py concurrent --seconds 10
#   Speak into the mic AND play system audio. Expect: CONCURRENT mic=PASS system=PASS.

# Residue: start/kill/start ×3 must leave no stale aggregate/tap.
for i in 1 2 3; do
  uv run python supervisor-exec.py tap --seconds 3 &
  pid=$!; sleep 1; kill -9 $pid; wait $pid 2>/dev/null
done
uv run python supervisor-exec.py list-aggregates   # expect: RESIDUE: none
```

**PASS:** `tap` yields a real stream (peak > 0) while other apps keep playing,
`concurrent` streams both, and after start/kill/start ×3 `list-aggregates` reports
`RESIDUE: none`.
**FAIL → STOP:** Open Question 1 (system-audio API) reopens; Phase 4 must be built
on a different primitive. Record it in `## Findings`.

> **Note on the two TCC services.** The plan declares both
> `NSMicrophoneUsageDescription` and `NSAudioCaptureUsageDescription`. It is not
> certain Core Audio taps consult the latter (tap authorization has historically
> ridden on the mic/"Audio Recording" grant). Watch which prompt(s) actually fire
> during the first `tcc` run and record it — if only one appears, that's a finding,
> not a bug.

## After the spikes

Record both spike outcomes (DR stability, grant persistence, tap PASS, residue
none) in the dev plan's `## Findings` and tick the two Pre-req acceptance boxes.
The throwaway `native/spike/` tree can be deleted once Phase 4 (`native/onoats-capturer/`)
is built from its proven recipe.
