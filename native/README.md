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
4. Verify the tool can see it:
   ```sh
   security find-identity -v -p codesigning
   ```
   You should see one line containing `"Code Signing"`.

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

The persistence test must run on the **real launch topology**: the Python
supervisor exec-ing the embedded helper (not double-clicking the `.app`). The
`supervisor-exec.py` harness reuses the real `_build_capturer_env` +
`start_new_session=True` exec from `cli.py`.

```sh
make sign                        # assemble Onoats.app + codesign + print the DR
make dr  > /tmp/onoats-dr-1.txt  # capture the designated requirement (run 1)
make cdhash                      # note the cdhash (it WILL change — that's fine)

# Launch via the supervisor exec path. First run prompts for BOTH grants — accept.
uv run python ../../native/spike/supervisor-exec.py tcc
#   → expect: TCC mic=PASS system=PASS
```

Then **rebuild + re-sign 3×** and confirm macOS does **not** re-prompt and both
grants persist:

```sh
make rebuild && make dr > /tmp/onoats-dr-2.txt
uv run python supervisor-exec.py tcc        # must NOT re-prompt; still PASS/PASS

make rebuild && make dr > /tmp/onoats-dr-3.txt
uv run python supervisor-exec.py tcc        # must NOT re-prompt; still PASS/PASS

make rebuild && make dr > /tmp/onoats-dr-4.txt
uv run python supervisor-exec.py tcc        # must NOT re-prompt; still PASS/PASS

# The designated requirement MUST be byte-identical across rebuilds:
diff /tmp/onoats-dr-1.txt /tmp/onoats-dr-2.txt && \
diff /tmp/onoats-dr-1.txt /tmp/onoats-dr-3.txt && \
diff /tmp/onoats-dr-1.txt /tmp/onoats-dr-4.txt && echo "DR STABLE ✓"
```

**PASS:** all four `tcc` runs report `mic=PASS system=PASS`, no re-prompt after the
first, and the four DR files are byte-identical (the cdhash differing is expected).
**FAIL → STOP:** the `$0` / no-notarization conclusion (Open Question 2) reopens and
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
