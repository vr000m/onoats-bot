# Release plan: 0.9.x series → 1.0.0

**Status**: Not Started
**Component**: packaging, macos, recorder
**Assignee**: Varun Singh
**Priority**: High
**Branch**: `docs/release-plan-0.9-to-1.0` (plan only — each phase gets its own branch/PR, named in the phase contract)
**Created**: 2026-06-11
**Objective**: Take onoats from the just-shipped Milestone B state (version `0.0.0`,
no LICENSE, no CHANGELOG, no version tags, README Quickstart pointing at a
nonexistent PyPI package) to a tagged, licensed, documented `v1.0.0` — via a
`v0.9.0` tag for Milestone B and a 0.9.x series that closes the recorded
post-ship follow-ups.

## Context

Milestone B (native macOS Core Audio capturer + SwiftUI menu-bar app) shipped in
PR #5 (merge `16da012`, 2026-06-11). The repo has accumulated seven merged PRs
and a substantial pre-PR history but has never cut a release: `pyproject.toml`
says `version = "0.0.0"`, the only tag is `stt-extraction-base`, there is no
`LICENSE` file (README's License section says "See the repository for license
details" — circular), no `CHANGELOG.md`, and **onoats is not on PyPI** — the
README Quickstart's `uv tool install onoats` returns 404 (verified live
2026-06-11: `curl https://pypi.org/pypi/onoats/json` → HTTP 404 — re-verify
in Phase 3 before rewriting the Quickstart, since index state is external).
The only working install path is from-source (`git clone` +
`make -C native install`).

The Milestone B plan recorded post-ship follow-ups (see its `## Findings`):
menu-bar surfacing of the all-zero WARNING, named device+STT profiles,
`native/spike/` deletion, pre-socket tap preflight, ConfigStore TOML-subset
parity tests, and public distribution. User decisions (2026-06-11):

- WARNING surfacing and tap preflight land **before 1.0.0**.
- BlackHole configs get pruned once the menu bar is trusted (they can be
  recreated any time; git history preserves everything).
- `native/spike/` is deleted from the tree after tagging `spike-archive`
  (history retrieval covers the future-debugging case).
- README must be overhauled for users/devs; BlackHole/PortAudio fallback
  details move to an external doc.
- Public distribution (Developer ID notarization + Homebrew cask) is **on
  hold** — out of scope here.
- Named device+STT profiles are **not** a 1.0.0 gate — deferred past this plan
  (still gated on capturer `--mic-uid` support).

Versioning decision: the menu-bar era is the **0.9.x series**; `v0.9.0` tags
the current (Milestone B) state; pre-0.9 PortAudio-era versions are
reconstructed as **untagged** CHANGELOG entries (nothing was ever published, so
backdated tags buy nothing); `v1.0.0` is cut when the 1.0.0-gate phases are
done. Licensing decision: BSD-2-Clause (same as Pipecat), copyright
"2025–2026 Varun Singh"; the CHANGELOG notes that all listed versions
(including the reconstructed 0.x era) are BSD-2-Clause, which covers the
"license from 0.1.0" intent without rewriting history.

**Plan/PR structure**: one plan, multiple PRs — one branch + PR per phase,
each running the full review gauntlet (`/update-docs`, `/review`,
`/security-review`, `/deep-review` as appropriate to size). Phases 1–3 are
docs/metadata and can land quickly; phases 4 and 7 are native Swift work and
are **not `/conduct`-runnable** (no Python test command for Swift; TCC/audio
smokes need the user's interactive terminal or GUI — see the Milestone B
lesson).

## Requirements

1. `LICENSE` file at repo root: BSD-2-Clause, "Copyright (c) 2025–2026 Varun
   Singh"; `license = "BSD-2-Clause"` in `pyproject.toml [project]`; README
   License section states the license and links the file.
2. `CHANGELOG.md` (Keep a Changelog format): reconstructed 0.x entries from
   the merged-PR history (untagged), `0.9.0` = Milestone B, and a note that
   all listed versions are BSD-2-Clause. `pyproject.toml` version bumped to
   `0.9.0`; annotated tag `v0.9.0` created on the merge commit of the
   changelog PR. Tags exist only from 0.9.0 forward.
3. README overhaul: Quickstart is clone-based (no PyPI claim); macOS leads
   with the menu-bar app story (cert → `make -C native install` → `onoats
   init` → launch `Onoats.app`; Start/Flush/Stop; in-menu mic picker, STT
   picker, data-dir chooser); BlackHole/PortAudio loopback fallback details
   move to `docs/blackhole-fallback.md` with a link + matrix row remaining.
4. The capturer's all-zero-input WARNING (30 s zero-run detector in
   `FrameChunker`) is surfaced in the menu-bar UI, not just a stderr log line,
   with the branch-specific hint (system → check the Screen & System Audio
   Recording grant; mic → check hardware mute/device). Warning, not failure —
   the paused-player digital-silence false positive stays benign.
5. `onoats status` shows the capture device(s) in use; `onoats devices`
   states it enumerates PortAudio devices only and notes the socket path
   captures system defaults when the configured source is `socket`.
6. Install is one command after clone: `make -C native setup` chains
   cert → install → `onoats init` (skipping init if config already exists).
   Menu-bar launch before `onoats init` ever ran is verified and handled
   gracefully (no absurd status, a clear "run setup" affordance).
   `native/spike/` is deleted after `spike-archive` is tagged.
7. First Start after a fresh install / permission reset does not die at ~10 s
   while the system-audio TCC prompt is unanswered: a pre-socket tap
   preflight surfaces the prompt before the recorder's read clock starts.
8. BlackHole-specific config/test surface is pruned to the minimum that keeps
   the documented PortAudio fallback working (13.x–14.3 / off-mac).
9. ConfigStore TOML-subset parity tests extended beyond the existing
   escaping round-trip (`tests/test_native_contract_parity.py:156`).
10. `v1.0.0` cut: CHANGELOG entry, version bump, annotated tag.

**Constraints**

- Never squash-merge; regular merges preserve phase commit history.
- Native Swift phases (4, 7, parts of 6) are not `/conduct`-runnable; their
  Python-side pieces are. TCC smokes run only from the user's interactive
  terminal or the GUI app (agent shells lack the audio/TCC context).
- The self-signed cert identity must never be regenerated (new cert = new
  designated requirement = all TCC grants invalidated) — install streamlining
  must keep `make cert`'s refuse-to-regenerate behavior.
- Phase 2's `v0.9.0` tag must point at a commit whose `pyproject.toml` says
  `0.9.0` — tag after the phase-2 PR merges, on the merge commit.
- The zero-run WARNING surfacing must not touch the realtime IOProc — the
  detector already runs on the worker thread (`FrameChunker.append`); any new
  signalling stays off the RT path.

## Implementation Checklist

### Phase 1 — LICENSE + license metadata
**Branch:** `chore/license-bsd2`
**Impl files:** `LICENSE`, `pyproject.toml`, `README.md`
**Test files:** `tests/test_release_meta.py` (new)
**Test command:** `uv run pytest tests/test_release_meta.py -q`

- [ ] Add `LICENSE` (BSD-2-Clause, "Copyright (c) 2025–2026 Varun Singh")
- [ ] Add `license = "BSD-2-Clause"` to `pyproject.toml [project]` (line ~12
  area; no `license`/`classifiers` fields exist today)
- [ ] Pin `requires = ["hatchling>=1.27"]` in `[build-system]` — the build
  backend is **hatchling** (`pyproject.toml:7-8`), not setuptools, and the
  PEP 639 SPDX-string `license` form needs hatchling ≥ 1.27; today the
  requirement is unpinned
- [ ] Run `uv build` and assert the wheel METADATA emits
  `License-Expression: BSD-2-Clause`
- [ ] Add `tests/test_release_meta.py`: LICENSE body matches the canonical
  BSD-2-Clause template (placeholders filled), pyproject `license` field
  present
- [ ] README `## License` (lines 164–166): replace placeholder with
  "BSD-2-Clause — see [LICENSE](LICENSE)."

### Phase 2 — CHANGELOG + version 0.9.0 + tag
**Branch:** `chore/changelog-v0.9.0`
**Impl files:** `CHANGELOG.md`, `pyproject.toml`, `scripts/release_check.sh` (new)
**Test files:** `tests/test_release_meta.py`
**Test command:** `uv run pytest tests/test_release_meta.py -q`

- [ ] Create `CHANGELOG.md` (Keep a Changelog). Reconstruct the pre-0.9 era
  as untagged entries from history; anchor commits/PRs:
  - pre-PR-#1 era is **long** (the tag `stt-extraction-base` sits on a
    "Merge pull request #89 … feat/bot-harness-decoupling" commit, so an
    earlier PR series predates the current #1–#7 numbering) — derive
    entries from `git log --oneline` + actual diffs, not from tag or
    branch names
  - PR #1 `7f33e72` (STT RSS probe/config env fix), PR #2 `645d90b`
    (flush process-identity fix) — the dual-input PortAudio recorder +
    converter + queue contract era → `0.1.0`–`0.x` entries as the history
    warrants (entries may be coarse)
  - PR #3 `8e6c165` polish items
  - tag `stt-extraction-base` (`2b3a9ee`) — read the commit's diff to
    describe it (bot-harness decoupling), don't trust the tag name
  - PR #4 `1f9dfdc` Milestone A: socket transport + supervisor hardening → `0.8.0`
  - PR #5 `16da012` Milestone B: native capturer + menu bar → `0.9.0`
    (PRs #6 `6aa0025` / #7 `8db8840` are docs-only ride-alongs)
- [ ] Verify every anchor SHA and its attributed feature set against
  `git log --oneline` / `git show --stat` before writing its entry
- [ ] Note in the CHANGELOG header that all listed versions are BSD-2-Clause
- [ ] Extend `tests/test_release_meta.py`: CHANGELOG carries the
  Keep-a-Changelog structural headings and an entry matching the current
  pyproject version
- [ ] Add `scripts/release_check.sh`: for a given `vX.Y.Z`, assert the
  target commit's pyproject `version == X.Y.Z` and a matching CHANGELOG
  entry exists — run before pushing any release tag (Phases 2 and 10)
- [ ] Bump **all three version surfaces** (Codex finding, 2026-06-11):
  `pyproject.toml` `0.0.0` → `0.9.0`; regenerate `uv.lock` (`uv lock` — the
  editable-package entry at `uv.lock:2109` records the version); and
  `native/onoats-menubar/Info.plist` `CFBundleShortVersionString`
  `0.1.0` → `0.9.0` (the Makefile copies this plist into the assembled
  `Onoats.app`). `scripts/release_check.sh` asserts all three match the tag
- [ ] Acknowledge the git-pinned runtime dependency in the CHANGELOG/release
  notes: `pipecat-local-stt-server @ git+…@5062b98` (`pyproject.toml:18`)
  is intentional for the from-source install story; revisit only if PyPI
  publishing is ever taken up
- [ ] After PR merge: run `scripts/release_check.sh v0.9.0 <merge-commit>`,
  then `git tag -a v0.9.0 <merge-commit> && git push origin v0.9.0`

### Phase 3 — README overhaul + BlackHole fallback doc
**Branch:** `docs/readme-menubar-first`
**Impl files:** `README.md`, `docs/blackhole-fallback.md`, `native/README.md` (cross-links)
**Test files:** —
**Test command:** `uv run pytest tests/test_init.py -q`

- [ ] Quickstart: replace `uv tool install onoats` (404 — not on PyPI) with
  the clone-based flow; macOS: `git clone` → `make -C native setup` (Phase 6;
  until then `make -C native cert && make -C native install && onoats init`) →
  launch `Onoats.app`; other platforms: `uv tool install --editable .`
- [ ] New "Menu bar (macOS)" section: Start/Flush/Stop, mic picker, STT
  picker, data-dir chooser, status display, log location
  (`~/Library/Logs/Onoats/onoats-bot.log`), first-run TCC prompts
- [ ] Create `docs/blackhole-fallback.md`: move loopback-driver details
  (driver install, device selection, `portaudio` source config) out of the
  README; README keeps one matrix row + link
- [ ] Reconcile `native/README.md` (install story, first-run UX) with the new
  top-level README — single source for each fact, cross-links not duplication

### Phase 4 — Menu-bar surfacing of the all-zero WARNING (native; NOT /conduct-runnable)
**Branch:** `feat/menubar-zero-run-warning`
**Impl files:** `native/onoats-capturer/Sources/Resampler.swift`,
`native/onoats-capturer/Sources/main.swift`, `src/onoats/cli.py`,
`src/onoats/status.py`, `native/onoats-menubar/Sources/RecorderModel.swift`,
`native/onoats-menubar/Sources/OnoatsMenuBarApp.swift`
**Test files:** `tests/test_status_file.py`, `tests/test_native_contract_parity.py`, `tests/test_socket_supervisor.py`
**Test command:** `uv run pytest tests/test_status_file.py tests/test_native_contract_parity.py tests/test_socket_supervisor.py -q`

- [ ] **Build the supervisor stderr reader (new plumbing — it does not exist
  today).** The capturer is spawned with no `stderr=` argument
  (`cli.py:398`), so its stderr is *inherited* and flows straight to the
  supervisor's own stderr (→ the menu bar's log redirect at
  `RecorderModel.swift:268`) — nothing in `src/onoats/` reads or parses it.
  Add `stderr=asyncio.subprocess.PIPE` + a concurrent always-drain reader
  task in `_supervise_socket_session`: parse `ONOATS-EVENT`-prefixed lines,
  tee every line to the supervisor's own stderr so the existing log-file
  destination is preserved, never block the capturer on a full pipe, and
  define the reader's lifecycle against the existing recorder/capturer race
  in `_run_recorder_with_capturer`. Extend
  `tests/test_socket_supervisor.py` (the existing lifecycle/no-hang suite
  for `_supervise_socket_session`) with pipe-drain, tee, and
  reader-shutdown cases — the reader must never extend or hang the
  bounded waits that suite pins
- [ ] Capturer: make the zero-run WARNING line machine-parseable (stable
  `ONOATS-EVENT` prefix + branch + hint), still once per run, re-armed by
  real audio
- [ ] `status.py`: **single schema bump 1→2 defining BOTH new optional
  fields** — `warning` (this phase) and the Phase-5 device fields — so
  Phase 5 adds no second bump (see sequencing constraint below); supervisor
  writes/clears `warning`
- [ ] Update the status-schema section of `docs/audio-socket-contract.md` in
  the same commit as the bump (the doc's own bump-together rule)
- [ ] Swift status reader + parity greps (`tests/test_native_contract_parity.py`)
  updated in lockstep; PR notes that a schema bump requires reinstalling
  the app and CLI together (`make -C native install`) — both readers
  hard-reject a mismatched schema (`status.py:142`,
  `RecorderModel.swift:133`), so a mixed-version window shows schemaDrift
  rather than data
- [ ] Menu bar: render the warning (icon change + menu line with the hint)
- [ ] Live smoke (user, GUI topology): deny system audio → WARNING appears in
  the menu ≈30 s in; re-allow → warning clears on next real audio

### Phase 5 — Device visibility in the CLI
**Branch:** `feat/cli-device-visibility`
**Impl files:** `src/onoats/cli.py`, `src/onoats/status.py`, possibly the
supervisor handshake path in `cli.py:311–482`
**Test files:** `tests/test_status_file.py`, new/extended CLI tests
**Test command:** `uv run pytest tests/test_status_file.py tests/ -q -k "status or devices"`

- [ ] Surface the capture device name/UID in `onoats status`: the capturer
  already logs it at bind (`MicCapture.swift:199`); emit it as an
  `ONOATS-EVENT device …` line consumed by the Phase-4 stderr reader.
  **Field shape decision:** two flat string scalars — `mic_device` and
  `system_device` (`"<name> (uid=<uid>)"`) — keeping `StatusRecord` a flat
  dataclass of scalars (`status.py:45-66`) so both decoders and the
  grep-style parity tests stay unchanged in kind. The fields are defined in
  Phase 4's single schema bump; this phase populates them. **Depends on
  Phase 4 (stderr reader + schema bump) having merged.**
- [ ] `onoats devices` (`cli.py:823–850`): when configured source is
  `socket`, print a note that the list is PortAudio-only and the socket path
  captures the system default input / default output tap
- [ ] PortAudio path: `onoats status` shows the configured `[devices]` names
  (the A/B-finding wrong-device guard for the fallback path)

### Phase 6 — Install streamlining + spike removal
**Branch:** `chore/install-setup-and-spike-removal`
**Impl files:** `native/Makefile`, `native/README.md`,
`native/onoats-menubar/Sources/RecorderModel.swift` (pre-init guard),
`README.md` (Quickstart flip to `make setup`)
**Test files:** `tests/test_native_contract_parity.py` (if guard logic is greppable)
**Test command:** `uv run pytest tests/ -q`

- [ ] Add `setup` target: `cert` → `install` → `onoats init` (init only when
  `~/.config/onoats/config.toml` is absent; never regenerate the cert)
- [ ] **Live-verify `make -C native setup` from a fresh clone with no prior
  Python install** yields a working `onoats init` — the macOS Quickstart
  has no separate editable-install step, so `install-cli` (inside
  `install: sign install-cli`) must fully bootstrap the `onoats`
  entry point on its own; if it doesn't, add the bootstrap to the target
  or the Quickstart
- [ ] Verify (live) what `Onoats.app` does when launched before `onoats init`
  ever ran — `RecorderModel.start()` currently spawns `onoats bot` with no
  config-existence check; add a graceful guard ("Run `make -C native setup`
  first" affordance) if the observed behavior is confusing
- [ ] **Rewire `native/residue_check.sh` off the spike tree first** — it
  currently builds and executes `spike/onoats-capturer`
  (`residue_check.sh:32-34`); point it at the production capturer build
  before any deletion, and re-run it to confirm the kill-×3 residue check
  still passes (Codex finding, 2026-06-11)
- [ ] Tag `spike-archive` on the last commit containing `native/spike/`, then
  delete `native/spike/`. Reference sweep must catch **relative** spike
  paths, not just `native/spike`: `rg -l "spike" native/ docs/ README.md`
  and clear every hit. **Tag timing:** push `spike-archive` after
  the PR merges — the mandated regular (non-rebase) merge preserves the
  intra-branch pre-deletion SHA — and record the SHA in `## Findings`
- [ ] Update README Quickstart + `native/README.md` to the one-command story

### Phase 7 — Pre-socket tap preflight (1.0.0 gate; native; NOT /conduct-runnable)
**Branch:** `feat/tap-preflight`
**Impl files:** `native/onoats-capturer/Sources/main.swift`,
`native/onoats-capturer/Sources/SystemCapture.swift`, `src/onoats/cli.py`
(supervisor wait), possibly `src/onoats/transports/socket_audio.py`
**Test files:** `tests/test_native_contract_parity.py`, supervisor tests in `tests/`
**Test command:** `uv run pytest tests/ -q -k "supervisor or capturer"`

- [ ] Design (Technical Specifications below): create the process tap (the
  TCC-prompting call) **before** binding/announcing the sockets, so the
  prompt-pending block happens while the recorder's `read_idle_timeout`
  (10 s, `socket_audio.py:232`) clock has not started. **This inverts the
  capturer's documented keystone startup order** (sockets-before-captures,
  `main.swift:8-14`, made structural by `registerAndStart` at
  `main.swift:336`) — the reorder is a coupled change across the Swift
  keystone, the supervisor's socket-appearance wait, and the
  rc=11/`socketFailed` error ordering, and each must be restated in the PR
- [ ] Supervisor wait budget: a prompt-blocked capturer with no sockets is
  currently indistinguishable from `capturer-start-timeout` in
  `_wait_for_sockets` (`cli.py:153-174`). Emit a
  `ONOATS-EVENT waiting-for-permission` stderr line from the capturer
  before the tap call; the supervisor (Phase 4's reader — **this phase
  depends on Phase 4**) extends the wait and surfaces
  "waiting for the system-audio permission prompt…" in the status file
- [ ] Keep the existing fail-loud semantics; pin the rc=11 meaning while
  here: the code reason string was `system-audio-denied` (`cli.py:187`
  pre-branch; renamed to `system-audio-failed` in PR #17 — see Findings) but
  a denied grant never produces rc=11 (denied taps succeed and deliver
  zeros — verified 2026-06-11); rc=11 fires only on genuine
  `AudioHardwareCreateProcessTap` API failure (e.g. retry exhaustion), and
  denial's sole observable is the zero-run WARNING. Rename the reason
  string or document the mismatch at both sites
- [ ] Add supervisor unit tests (extend the parametrized fake-capturer
  test): rc=10 → `mic-denied` and rc=11 mapping survive the reorder;
  `capturer-start-timeout` still fires when no socket and no
  waiting-for-permission event appear; the prompt-pending event extends
  the wait. (rc=11 cannot be live-smoked — the unit test is its only
  verification)
- [ ] Live smokes (user, GUI topology): `tccutil reset AudioCapture
  net.varunsingh.onoats` → Start → leave the prompt unanswered >10 s →
  answer Allow → session proceeds (no 10 s death); also Don't Allow →
  session starts, zero-run WARNING (Phase 4) fires ≈30 s in; mic-denial
  rc=10 fail-loud re-smoked once after the reorder

### Phase 8 — BlackHole config pruning (1.0.0 gate)
**Branch:** `chore/prune-blackhole-configs`
**Impl files:** `native/Makefile` (new `setup-cli` target),
`src/onoats/init.py` (lines ~91, ~135), `src/onoats/dual.py`
(help text ~665), `pyproject.toml` (comment ~31), `README.md`,
`docs/blackhole-fallback.md`
**Test files:** `tests/test_init.py`, `tests/test_stt_config_wiring.py`, `tests/test_config.py`
**Test command:** `uv run pytest tests/test_init.py tests/test_stt_config_wiring.py tests/test_config.py -q`

- [ ] Decide the keep-list with the user first (gate: user is "happy with the
  menubar" in daily use) — the documented fallback for 13.x–14.3/off-mac
  stays functional. **DECIDED 2026-06-11 (user): conservative keep-list.**
  PortAudio/BlackHole stays a supported path — the user has older Intel
  MacBooks that may be capped below macOS 14.4 (no process-tap API), where
  BlackHole is the only system-audio route. KEEP: the `_LOOPBACK_HINTS`
  auto-detection (`init.py:91`) and the no-loopback NOTE (`init.py:135`)
  everywhere they fire today; the `dual.py:665` backend help text; all
  config-wiring tests (BlackHole fixture names may stay). PRUNE only:
  redundant prose/comments that re-explain BlackHole where a link to
  `docs/blackhole-fallback.md` suffices (README mentions, `pyproject.toml`
  comment).
- [ ] Prune BlackHole-specific hints/branches/tests beyond that keep-list;
  point remaining mentions at `docs/blackhole-fallback.md`
- [ ] **Install-path branching (DECIDED 2026-06-12, user):** the install
  layer — not `onoats init` — encodes the menubar-vs-CLI choice; `init`
  stays untouched. Add `make -C native setup-cli` (cert → capturer build +
  sign → `install-cli` → init; skips the app bundle) alongside the existing
  `setup` (menubar, unchanged default). CLI/PortAudio keeps the old way —
  no make target (it must not require the native toolchain): documented
  two-liner in `docs/blackhole-fallback.md`. No justfile — make is the
  established entry point (PR #16 README story). Docs job: README explains
  the THREE paths side by side (menubar `setup` / CLI+native `setup-cli` /
  CLI+PortAudio for Intel / ≤14.3 / off-mac)
- [ ] Coverage bound (stated, not solved): there is no 13.x–14.3 hardware in
  CI or on the author's machine — the config-wiring tests
  (`test_init.py`/`test_config.py`/`test_stt_config_wiring.py`) are the
  **accepted verification bound** for the PortAudio fallback; functional
  proof on that matrix is explicitly out of scope

### Phase 9 — ConfigStore TOML-subset parity tests (1.0.0 gate)
**Branch:** `test/configstore-parity`
**Impl files:** `tests/test_native_contract_parity.py`; fixes (if found) in
`native/onoats-menubar/Sources/ConfigStore.swift`
**Test files:** `tests/test_native_contract_parity.py`
**Test command:** `uv run pytest tests/test_native_contract_parity.py -q`

- [ ] Extend beyond the existing 4-case escaping round-trip (line 156):
  comments on/after lines, whitespace variants, absent section, absent key,
  duplicate keys, non-string values adjacent to edited keys, untouched-line
  byte-identity, CRLF — each case round-trips Swift writer output through
  `tomllib` and asserts untouched bytes. **CRLF contract (pinned):**
  untouched lines keep their original bytes (CRLF preserved verbatim); the
  edited line is written with LF; the result must still parse under
  `tomllib`
- [ ] Fix any ConfigStore divergence the new cases expose (Swift), keeping
  the documented TOML-subset contract doc in sync

### Phase 10 — Cut v1.0.0
**Branch:** `chore/release-v1.0.0`
**Impl files:** `CHANGELOG.md`, `pyproject.toml`
**Test files:** —
**Test command:** `uv run pytest tests/ -q`

- [ ] Confirm gates: Phases 1–9 merged; soak/echo + drift ride-alongs have
  not surfaced blockers
- [ ] CHANGELOG `1.0.0` entry; bump all three version surfaces (pyproject,
  `uv.lock` regen, menu-bar Info.plist `CFBundleShortVersionString`);
  `scripts/release_check.sh v1.0.0`; after merge: annotated tag
  `v1.0.0` on the merge commit

## Technical Specifications

### Verified current state (Explore, 2026-06-11)

- `pyproject.toml`: `name = "onoats"`, `version = "0.0.0"` (line 12); **no
  `license` or `classifiers` fields**. Deps: `pipecat-ai>=1.0.0,<2.0.0` (+
  extras), `pipecat-local-stt-server @ git+…@5062b98` (git-pinned, line 18 —
  intentional for the from-source story), `websockets>=13.0`,
  `python-dotenv>=1.2.1`, `loguru>=0.7.0`;
  `[macos]` extra = `mlx-whisper>=0.4.0`, `kokoro-onnx>=0.4.0` (lines 33–36);
  dev: `pytest>=8`, `ruff>=0.15,<0.16`. PyAudio arrives transitively via
  `pipecat-ai[local]` — no direct dependency.
- `README.md:164-166` — License section is the circular placeholder.
- `LICENSE`, `CHANGELOG.md`, `docs/blackhole-fallback.md` — none exist.
- Tags: only `stt-extraction-base` (`2b3a9ee`). No `v0.9.0`/`v1.0.0`/
  `spike-archive`. PR merge commits: #1 `7f33e72`, #2 `645d90b`,
  #3 `8e6c165`, #4 `1f9dfdc` (Milestone A), #5 `16da012` (Milestone B),
  #6 `6aa0025`, #7 `8db8840` (docs).
- `native/Makefile` targets: `build`, `app`, `sign`, `dr`, `cdhash`,
  `rebuild`, `print-bin`, `cert`, `install-cli`, `install`, `clean`,
  `check-identity`; chaining pattern `install: sign install-cli`,
  `sign: app check-identity`. **No `setup` target.**
- Zero-run detector: `FrameChunker` in
  `native/onoats-capturer/Sources/Resampler.swift:71` — state lines 83–97,
  `zeroRunWarnSamples = 480_000` (30 s @ 16 kHz) line 94, WARNING emitted
  lines 146–147 via `logLine(...)` to **stderr only**; runs on the worker
  thread (never the RT IOProc).
- Device logging: `MicCapture.swift:47-70` `defaultInputDeviceDescription()`
  → logged at bind (`MicCapture.swift:199`). The normal streaming startup
  (`main.swift:368`) does not include device names.
- CLI: handler-dict dispatch (`cli.py:914-922`); `_cmd_devices`
  `cli.py:823-850` (unconditional `import pyaudio`, no source check);
  `_cmd_status` `cli.py:853-911` (prints `audio_source`, `stt_label`; no
  device names); supervisor `_run_socket_supervisor` `cli.py:279-308` →
  `_supervise_socket_session` `cli.py:311-482`; rc→reason map
  `_CAPTURER_RC_REASONS` `cli.py:185-188`.
- Status file: `StatusRecord` `src/onoats/status.py:45-66`
  (`STATUS_SCHEMA_VERSION = 1`, line 38; **no device or warning fields**);
  `write_prestart_failure()` line 257.
- Read-idle watchdog: `read_idle_timeout: float = 10.0` at
  `src/onoats/transports/socket_audio.py:232` (and 792) — **in the
  transport, not runtime.py**.
- Capturer exit codes: `Support.swift:23-30` (`ok=0`, `usage=2`,
  `micDenied=10`, `systemAudioFailed=11`, `socketFailed=12`,
  `captureFailed=13`).
- Menu bar: `OnoatsMenuBarApp.swift` — flat menu (Start/Stop/Flush, inline
  mic picker that sets the macOS default input, output-device text, STT
  picker, data-dir controls). `RecorderModel.swift` — `cliAvailable` checks
  the shim is executable (line 83); `start()` (line 253) spawns `onoats bot`
  with **no config.toml-existence guard**.
- ConfigStore: `native/onoats-menubar/Sources/ConfigStore.swift` (172 lines;
  `readValue` line 61, `writeValue` line 118). Existing parity test:
  `tests/test_native_contract_parity.py:156`
  (`test_configstore_escaping_round_trips_through_tomllib`, 4 cases). The
  parity-test pattern is Python `re.search` grep-assertions over Swift
  source plus round-trips through `tomllib` — no Swift test runner.
- BlackHole references: `src/onoats/init.py:91,135`, `src/onoats/dual.py:665`,
  `pyproject.toml:31` (comment), `README.md`, `tests/test_init.py`,
  `tests/test_stt_config_wiring.py`, `tests/test_config.py`, both Milestone
  dev plans.
- `native/spike/` exists: `Info.plist`, `main.swift`, `Makefile`,
  `onoats-capturer/`, `Onoats.app/`, `supervisor-exec.py`.

### Key design decisions

**Warning/device channel (Phases 4+5): capturer stderr → supervisor parse →
status file → menu bar.** The supervisor owns the status file and the menu
bar already polls it — but the stderr leg is **new plumbing, not reuse**:
today the capturer is spawned with no `stderr=` argument (`cli.py:398`), so
its stderr is inherited (flowing untouched into the supervisor's stderr and
on to the menu bar's log redirect, `RecorderModel.swift:268`) and nothing in
`src/onoats/` reads it. Phase 4 introduces `stderr=PIPE` + an always-drain
reader task that parses `ONOATS-EVENT`-prefixed lines (e.g. `ONOATS-EVENT
zero-run-warning branch=system`, `ONOATS-EVENT device …`,
`ONOATS-EVENT waiting-for-permission`) and tees everything to the
supervisor's stderr so the log destination is unchanged. Alternative
(rejected): menu bar tails the capturer log file — couples the GUI to log
format/location and duplicates parsing. Status schema: **one bump, 1→2, in
Phase 4, defining all new optional fields** (`warning`, `mic_device`,
`system_device` — flat string scalars to keep `StatusRecord` and the
grep-style parity tests structurally unchanged); Phase 5 populates the
device fields without a second bump. Both readers hard-reject a mismatched
schema (`status.py:142` returns None; `RecorderModel.swift:133` sets
schemaDrift), and the CLI and app are installed out-of-band of each other —
so the bump is a hard cross-version read break: the schema-bumping PR must
state that app + CLI are reinstalled together (`make -C native install`),
with schemaDrift as the visible mixed-version symptom. The status-schema
section of `docs/audio-socket-contract.md` updates in the same commit.

**Tap preflight (Phase 7): reorder capturer startup so the TCC-prompting tap
creation happens before the sockets are announced.** This **inverts the
documented keystone order** — `main.swift:8-14` pins
"create BOTH listening sockets … only then start the captures", enforced
structurally by `registerAndStart` (`main.swift:336`) — so the change is a
coupled redesign across three things, not a local Swift edit: (1) the Swift
keystone and its teardown registration order; (2) the supervisor's
socket-appearance wait (`_wait_for_sockets`, `cli.py:153-174`), which today
reads a pre-socket block as `capturer-start-timeout` and must learn the
`ONOATS-EVENT waiting-for-permission` signal (via Phase 4's stderr reader —
hard dependency) to extend its budget and surface prompt-pending state;
(3) the rc=11/`socketFailed` error ordering, which must be restated and
unit-tested post-reorder. The recorder's 10 s `read_idle_timeout` only
starts once the recorder connects, so a prompt answered at human speed costs
nothing once sockets appear after the tap. Constraints from Milestone B
findings: capture start order must remain tap+aggregate **before** mic
engine (tap creation while the engine runs correlated with flaky creation),
and the bounded tap-create retry (×3 @ 500 ms) must be preserved. rc=11
semantics pinned in-phase: fires on genuine tap API failure only — TCC
denial never produces it (denied taps deliver zeros).

**Versioning**: pre-0.9 entries are changelog-only (untagged) — nothing was
ever distributed, so backdated tags add maintenance surface without value.
Real annotated tags exist from `v0.9.0` forward, each on the merge commit of
the PR that bumped the version.

**Spike removal**: `git tag -a spike-archive` on the last commit containing
`native/spike/` (i.e. the commit just before the deletion lands), so
retrieval is `git checkout spike-archive -- native/spike`.

### Integration seams

| Seam | Contract | Phases |
|---|---|---|
| Capturer stderr → supervisor | **New in Phase 4**: `stderr=PIPE` + always-drain reader; `ONOATS-EVENT <type> k=v…` stable-prefix lines parsed, everything teed to supervisor stderr (log destination unchanged) | 4, 5, 7 |
| Status file schema | **One** `STATUS_SCHEMA_VERSION` bump (1→2, Phase 4) defining `warning`, `mic_device`, `system_device` (flat string scalars); Swift reader + parity greps + `docs/audio-socket-contract.md` updated in the same commit; schema-bumping PR mandates app+CLI reinstalled together | 4, 5 |
| `native/Makefile` | `setup: cert install` + conditional `onoats init`; `cert` stays refuse-to-regenerate; `install-cli` must bootstrap `onoats` from a bare clone | 6 |
| CHANGELOG ↔ tags | tag exists ⇔ changelog entry exists ⇔ **all three version surfaces** match (pyproject, `uv.lock` editable entry, menu-bar Info.plist `CFBundleShortVersionString`), from 0.9.0 forward — enforced by `scripts/release_check.sh` before every tag push | 2, 10 |
| README ↔ native/README | top-level README owns the user story; native/README owns build/sign internals; cross-links not copies. **Merge-order constraint: Phase 3 → Phase 6 → Phase 8** (all three edit the Quickstart/matrix) | 3, 6, 8 |
| Phase dependencies | Phase 5 and Phase 7 both require Phase 4's stderr reader + schema bump merged first | 4 → 5, 7 |

## Review Focus

- Phase 2: changelog reconstruction is history-derived — verify entry↔commit
  attribution against `git log`, don't invent features.
- Phases 4/5: status-file schema change crosses the Python↔Swift boundary;
  the contract-parity tests (`tests/test_native_contract_parity.py`) are the
  tripwire — confirm they pin the new fields and schema version on both sides.
- Phase 4: the supervisor stderr reader is new plumbing — review its
  lifecycle against the recorder/capturer race and confirm a full pipe can
  never block the capturer.
- Phase 7: regression risk to the verified fail-loud paths (rc=10
  mic-denied, rc=11 genuine tap-API failure — note: TCC denial does NOT
  produce rc=11, denied taps deliver zeros; capturer-start-timeout;
  kill-mid-session 4-part observable) — each needs a unit-test re-check
  after the startup reorder, plus a live rc=10 re-smoke.
- Phase 8: pruning must not break the documented PortAudio fallback for
  macOS 13.x–14.3 / off-mac (Cross-platform matrix promise; config-wiring
  tests are the accepted verification bound).
- Licensing: BSD-2-Clause text verbatim; `pyproject` uses the SPDX string
  form (`license = "BSD-2-Clause"`, PEP 639) — the build backend is
  **hatchling** (not setuptools); pin `hatchling>=1.27` and verify the
  built wheel emits `License-Expression: BSD-2-Clause`.

## Testing Notes

- Python phases: full suite (`uv run pytest tests/ -q`, 209 tests at
  Milestone B close; 247 after Phase 7) plus phase-specific files named in
  each contract block.
- Swift phases: no Swift test runner exists; coverage is (a) the Python
  grep/round-trip parity tests, (b) `make -C native rebuild` compile+sign,
  (c) live GUI-topology smokes run by the user (TCC denials, prompt-pending
  Start, WARNING surfacing). Agent shells cannot run audio smokes.
- Tagging steps (Phases 2, 6, 10) are operator actions after merge — record
  the tag SHAs in `## Findings`.

## Acceptance Criteria

- [ ] `LICENSE` (BSD-2-Clause) at root; `pyproject.toml` has `license`
  field and `hatchling>=1.27` pin; `uv build` wheel METADATA emits
  `License-Expression: BSD-2-Clause`; README License section is real;
  CHANGELOG notes license covers all listed versions;
  `tests/test_release_meta.py` green
- [ ] `CHANGELOG.md` covers the full history (reconstructed 0.x → 0.9.0 →
  …) and `v0.9.0` tag exists on a commit with `version = "0.9.0"`
- [ ] README Quickstart works as written on a clean machine (clone-based; no
  PyPI reference); menu-bar section exists; BlackHole details live in
  `docs/blackhole-fallback.md`
- [ ] Zero-run WARNING visible in the menu bar within ~35 s of an all-zero
  branch (live-smoked, denied-grant case), and clears/re-arms on real audio
- [ ] `onoats status` names the capture device(s); `onoats devices` carries
  the socket-path note
- [ ] Fresh-clone install is `make -C native setup` + launch; pre-init
  menu-bar launch is handled gracefully (live-verified)
- [ ] `native/spike/` gone from HEAD; `spike-archive` tag retrieves it
- [ ] First Start with the system-audio prompt left unanswered >10 s
  survives to a working session once Allowed (live-smoked); rc=10 re-smoked
  live; rc=11 + start-timeout mappings pinned by supervisor unit tests
- [ ] BlackHole surface pruned per the agreed keep-list; PortAudio fallback
  still documented and its tests green
- [ ] ConfigStore parity suite covers the agreed case matrix; all green
- [ ] `v1.0.0` tag on a merge commit with `version = "1.0.0"` and a 1.0.0
  changelog entry
- [ ] Every phase merged via its own reviewed PR (regular merge, no squash)

<!-- reviewed: 2026-06-11 @ 0d68b5a07007a22333d589a3524f2ef7b7710607 -->
## Issues & Solutions

*(populated during implementation)*

## Final Results

*(to be filled at completion)*

## Progress

- [x] Phase 1 — LICENSE + license metadata (PR #9 merged 2026-06-11, `66a93cd`)
- [x] Phase 2 — CHANGELOG + v0.9.0 (PR #10 merged 2026-06-11, `3a4e538`; tag `v0.9.0` pushed)
- [x] Phase 3 — README overhaul + blackhole-fallback doc (PR #13)
- [x] Phase 4 — Menu-bar zero-run WARNING surfacing (PR #14; live smoke
  passed 2026-06-11)
- [x] Phase 5 — CLI device visibility (PR #15)
- [x] Phase 6 — Install streamlining + spike removal (PR #16; kill-×3 re-run
  PASSED live 2026-06-11; fresh-clone + pre-init verifies DEFERRED to the
  next fresh-machine install — see Findings; post-merge: push `spike-archive`
  tag on `7ac0b2e`)
- [x] Phase 7 — Tap preflight (1.0.0 gate; PR #17 merged 2026-06-12,
  `46894fe` — implementation + unit tests + parity pins; all three live
  smokes PASSED 2026-06-11, see Findings)
- [x] Phase 8 — BlackHole pruning (1.0.0 gate; PR #18 merged 2026-06-12,
  `5f7a857` — conservative keep-list applied, `setup-cli` target added,
  three install paths documented)
- [x] Phase 9 — ConfigStore parity tests (1.0.0 gate; PR #19 merged
  2026-06-12, `101d368` — 12 new cases incl. pinned CRLF contract; real
  CRLF divergence found and fixed in ConfigStore.swift)
- [ ] Phase 10 — Cut v1.0.0

## Findings

- **`v0.9.0` tagged 2026-06-11** on PR #10's merge commit `3a4e538`
  (annotated; pushed to origin). `scripts/release_check.sh v0.9.0 3a4e538`
  passed all four surface checks before the tag was pushed. GitHub
  Release created from the CHANGELOG 0.9.0 section (user decision
  2026-06-11): **standard post-tag step from now on** — after pushing a
  release tag, run `gh release create vX.Y.Z --verify-tag --notes-file
  <extracted CHANGELOG section>`. CHANGELOG.md stays canonical; the GitHub
  Release is a rendered mirror. Applies to Phase 10 (v1.0.0). Only tagged
  versions get releases — the reconstructed 0.x era stays changelog-only.
- Phase 2 attribution audit: 23 CHANGELOG claims verified against history,
  zero refutations; one drafting misattribution caught pre-commit
  (`--category` came from `81afbc0`, not pre-extraction PR #34).
- The plan-file review marker blocks above-marker edits (including checkbox
  ticks) — phase progress is recorded here and in `## Progress` below the
  marker instead; recomputing the marker hash requires the user.
- Phase 3 (PR #13, 2026-06-11): PyPI 404 re-verified live before the rewrite
  (`curl https://pypi.org/pypi/onoats/json` → 404), so the Quickstart is
  clone-based as planned. macOS Quickstart documents the interim
  `cert && install && init` flow — flips to `make -C native setup` in
  Phase 6. Doc-ownership split settled: top-level README owns the user story
  (Quickstart, Menu bar section), `native/README.md` owns build/sign/TCC
  internals, `docs/blackhole-fallback.md` owns the loopback fallback;
  cross-links in all three directions, no duplicated facts.
- Phase 4 implementation (2026-06-11): event-line format settled as
  `ONOATS-EVENT <type> k=v …` with `hint=` as the trailing free-text field by
  contract (single split point, no quoting needed). Per-branch warnings merge
  `; `-joined in branch order; `zero-run-clear` (new event, emitted when real
  audio re-arms the detector) removes the branch and nulls the field when none
  remain. Reader lifecycle: starts immediately after spawn (before the
  socket wait, so a prestart stderr flood can't block the capturer), is NOT a
  participant in the recorder/capturer completion race, and retires in the
  session `finally` under a 2 s bound after `_stop_capturer`. Overlong stderr
  lines (>64 KiB) are dropped via `StreamReader.readline`'s documented
  ValueError path (buffer cleared, transport resumed) — the drain survives.
  Pipe-drain proof in tests: a 512 KiB pre-bind stderr flood would block an
  undrained capturer at the ~64 KiB pipe capacity before its sockets appear;
  the E2E test passes only because the reader drains from spawn.
- **Phase 4 live smoke PASSED (user, GUI topology, 2026-06-11).** Denied
  Screen & System Audio Recording → Start → menu showed the ⚠ system-branch
  hint within the 12:40 session (log shows the `ONOATS-EVENT
  zero-run-warning`); the log also shows a full in-session re-arm cycle
  (`zero-run-warning` → `zero-run-clear` → `zero-run-warning`), so the strict
  warning-clears-on-real-audio path was exercised live, not just
  across-session. Re-granted session (12:46) started clean (no warning).
  Incidentals: (1) **rc=10 mic-denied fail-loud re-smoked live** on the new
  stderr-reader code path (mic grant revoked → capturer exited rc=10
  pre-socket, fresh `mic-denied` status — relevant to Phase 7's acceptance);
  (2) revoking a TCC grant for a *running* app needs macOS's Quit & Reopen —
  smoke procedure note; (3) a stale schema-v1 status file from a pre-upgrade
  session shows the menu's drift line until the next session overwrites it —
  benign, self-heals on first Start, recurs at every future schema bump;
  (4) the ~200-char warning rendered as ONE native menu item stretched the
  menu to its width — fixed pre-merge by splitting on the hint's em-dash
  clauses into stacked caption lines (full text remains in `onoats status` +
  the log).
- Phase 5 implementation (2026-06-11): two decisions worth recording.
  (1) **The `device` event reuses the trailing `hint=` field** to carry
  `<name> (uid=<uid>)` — device names contain spaces, and the contract
  already defines exactly one free-text trailing field, so no parser change
  and no quoting scheme. (2) **Device events outrun `write_running`** (they
  fire within the capturer's first second; the recorder still has STT
  preflight + model load ahead of it, and `write_running` builds a FRESH
  record, clobbering any earlier stamp), so the live apply in the stderr
  reader alone cannot work — a deferred supervisor task
  (`_apply_device_fields_when_recorded`) polls for a running record whose
  `start_time` is at/after a session floor taken before recorder start,
  applies the shared `device_state`, and exits; the floor check is what
  keeps a stale (crashed-previous-session, `running=true`) record from
  being stamped, and `status.set_devices` is additionally gated on
  `running` (unlike `set_warning`). Mic re-emits per bind, so a mid-session
  default-input rebind updates `mic_device` live; the system branch is
  identified as `system-output tap (uid=<aggregate uid>)` — the tap is
  global (all processes' output), so naming the default output device
  would be dishonest and go stale on output switches.
- Phase 6 implementation (2026-06-11): the spike binary was the ONLY carrier
  of the residue-enumeration commands, so "rewire residue_check.sh off the
  spike" meant porting `list-aggregates`/`list-taps`/`clean-taps` into the
  production capturer (`Maintenance.swift`, socket-less subcommands dispatched
  before any TCC interaction; verdict lines on stdout, byte-compatible with
  the script's greps). **Kill-×3 residue check re-PASSED live (user,
  2026-06-11) against the production-binary checker** before any deletion.
  **`spike-archive` tag target: `7ac0b2e`** — the last commit containing
  `native/spike/` (parent of the deletion commit `9f4c15f`); user pushes the
  tag after the regular merge. **DONE 2026-06-12: annotated `spike-archive`
  tag created on `7ac0b2e` and pushed to origin.** Sweep interpretation: every dangling
  *path* reference cleared (native/README run-book sections → archived
  History note carrying the durable conclusions; source comments → tag
  pointer); pure historical prose with no path stays; dev plans untouched as
  historical records. Pre-init launch analysis: menu-bar Start sets both
  `AUDIO_SOURCE=socket` and `ONOATS_CAPTURER_BIN`, and STT defaults to
  `whisper` (installed by the `[macos]` extra), so a pre-init Start is
  expected to *work* with all-default settings (data → `~/.local/share/onoats`)
  rather than fail — guard decision deferred to the live verify.
- Phase 6 live-verify deferral (user decision 2026-06-11): no spare machine
  for the fresh-clone `make -C native setup` verify, and the pre-init
  app-launch verify is moot on a configured machine — both DEFERRED to the
  user's next fresh-machine install, not blocking the merge. Mitigation:
  README Quickstart now documents the full fresh-machine procedure including
  prerequisites (`xcode-select --install`, uv installer) so the deferred
  verify is a documented walk-through, not tribal knowledge. The
  `RecorderModel.start()` pre-init guard was NOT added (code-level analysis
  predicts a pre-init Start works with defaults rather than confusing the
  user); revisit if the deferred verify observes otherwise.
- Phase 7 implementation (PR #17, 2026-06-11): four decisions worth recording.
  (1) **The preflight starts the FULL system chain** (tap → aggregate →
  IOProc), not just the tap: the tap-created→IOProc-started window is the
  audible output dropout (~200 ms signed, SystemCapture header), and a
  tap-only preflight would stretch that dropout across the accept barrier
  (recorder connect comes after STT preflight + model load — seconds). Frames
  emitted before the system FrameWriter attaches are dropped by a
  `LateBoundWriter` (pre-session audio); streaming still starts at the accept
  barrier, exactly as pre-reorder. (2) **`waiting-for-permission` is emitted
  unconditionally** — there is no TCC preflight API, so the capturer cannot
  know whether the tap call will block. The supervisor extension is therefore
  armed on every start but applies only when the 10 s base budget expires
  (+120 s, once); the granted/fast path never pays it. Surfacing uses new
  `status.write_prestart_waiting` (fresh `running=true` record, note in the
  v2 `warning` field; replaced by the recorder's start write or the
  prestart-failure stamp). (3) **rc=11 reason renamed**
  `system-audio-denied` → `system-audio-failed` at both sites (cli map,
  status.py vocabulary; ExitCode + contract doc document the semantics):
  denial never exits the capturer. Post-reorder error ordering: rc=10 and
  rc=11 both pre-socket; rc=12 only after a healthy tap. (4) **Latent bug
  found by the new start-timeout test**: the prestart-failure stamp read
  `capturer_proc.returncode` AFTER `_stop_capturer` (which always reaps), so
  a hung-but-alive capturer was stamped `capturer-start-failed` with the
  SIGTERM exit code — `capturer-start-timeout` was unreachable end-to-end.
  Fixed by reading the code before stopping. Parity pin added:
  emit-before-tap-before-sockets order in main.swift + the supervisor's
  event handler (a one-sided rename or sockets-first reorder fails CI).
  Suite: 247 passed. Live smokes pending (user, GUI topology).
- Phase 7 live smoke, round 1 (2026-06-11 22:25–22:30, new binary): the
  preflight machinery worked as designed — `waiting-for-permission` emitted,
  supervisor extended the wait at exactly 10 s (+120 s), menu showed
  "starting…" instead of dying, tap created before sockets, and the
  user's Allow click (~42 s in) unblocked `AudioHardwareCreateProcessTap`
  (attempt 1 also reproduced the flaky `OSStatus 0, tapID=0` shape; the ×3
  retry recovered). **New bug exposed**: the mic branch had no silence pacer
  until `bind()` completed, and the post-Allow HAL bind on AirPods Pro 2
  blocked >10 s with the recorder already connected → mic read-idle →
  `fatal_error_frame`. The system branch survived the identical window only
  because its pacer was already pacing. Fixed in `9ea72ba`: mic
  `chunker.activate()` moved before `bind()` (paced silence from writer
  attach until the device delivers). Also recorded: the first smoke round
  (17:49–17:53) ran the pre-Phase-7 capturer binary (app not reinstalled)
  and faithfully reproduced both old failure modes — a useful baseline.
- **Phase 7 live smokes PASSED (user, GUI topology, 2026-06-11 22:38–22:47,
  binary with `9ea72ba` mic-pacer fix).** (1) Allow-after-pause: prompt left
  unanswered ~36 s → menu showed "starting…", supervisor extended at 10 s
  (+120 s), Allow → tap → sockets → recording with NO second Start; bonus
  zero-run warn/clear cycle observed. (2) Don't Allow: tap creation
  SUCCEEDED on denial (re-confirms denied-taps-deliver-zeros), session
  recorded since 22:42, system zero-run warning ≈30 s in and persisting, no
  fatal_error_frame; the mic pacer visibly saved the session (500 paced
  filler frames ≈10 s before "mic: capturing from AirPods Pro 2" — would
  have read-idled pre-fix). (3) Mic-denial: rc=10 in ~56 ms pre-socket,
  fresh `mic-denied` status ("capturer exited (rc=10) before creating its
  sockets"). All three observables verified against
  ~/Library/Logs/Onoats/onoats-bot.log timestamps.
