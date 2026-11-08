# Task: Recover or Fail Loud When the System Tap Stalls After Process Resume

**Status**: In Progress
**Component**: macos, transport, recorder
**Assigned to**: Codex
**Priority**: High
**Branch**: bug/capturer-system-tap-resume
**Created**: 2026-11-07
**Review Gates**: full

## Objective

Ensure the native system-audio capturer cannot remain apparently healthy after
its process was suspended long enough that the Core Audio tap generation is no
longer trustworthy. The capturer must terminate non-zero so the supervisor
rotates the partial session; a later Start creates a wholly fresh generation.

## Context

Signed-app manual testing reproduced a Core Audio process-tap failure after
`SIGSTOP`/`SIGCONT`: telemetry freshness and STT recovered, but the `them`
branch remained silent and emitted `zero-run-warning` without
`zero-run-clear`. The current all-zero detector is warning-only because a
rendering application can legitimately output digital silence. Audio content
therefore cannot be the fatal signal. The reproducible independent signal is
the multi-second gap in the capturer's own 20 ms pacer after the process resumes.

## Requirements

- Preserve never-mix: system tap audio can reach only the system socket and
  `them` STT session.
- Preserve legitimate idle playback and digital silence; no watchdog may infer
  a fatal tap failure merely from the absence of callbacks or all-zero samples.
- A multi-second process-suspension gap after capture activation terminates the
  capturer non-zero without waiting for audio-content heuristics.
- Preserve the supervisor as the sole owner of whole-generation restart; the
  native capturer must not reconnect sockets or silently create a second tap
  generation behind the recorder.
- Retain teardown ordering: stop device, destroy IOProc, destroy aggregate,
  destroy tap, then close sockets through the existing single exit path.
- Add a deterministic regression that fails against the pre-fix implementation
  and covers ordinary 20 ms wakes, the exact threshold, a multi-second gap,
  re-arming, clock regression, one-shot teardown, and a stop race.
- Verify the signed-app scenario with the supplied YouTube video as real
  system output after automated gates pass.

## Review Focus

- False-positive safety: paused playback, no callbacks, and intentional digital
  silence must not cause a restart; only a process heartbeat gap may escalate.
- Core Audio lifecycle: no duplicate IOProc, aggregate, tap, worker, or socket
  ownership during recovery.
- Process boundary: a failed recovery must be non-zero and bounded so the
  supervisor reports truncation and owns the next generation.
- Realtime safety: the IOProc remains copy-only; no logging, Core Audio API
  call, allocation-heavy recovery, or contended recovery lock runs there.

## Implementation Checklist

### Phase 1: Suspension Watchdog and Deterministic Harness

**Impl files:** `native/onoats-capturer/Sources/ProcessSuspensionWatchdog.swift, native/onoats-capturer/Sources/Resampler.swift, native/Makefile`
**Test files:** `native/onoats-capturer/Tests/ProcessSuspensionWatchdogHarness.swift, native/test_capturer_watchdog.sh, tests/test_capturer_mic_liveness.py`
**Test command:** `bash native/test_capturer_watchdog.sh && uv run pytest tests/test_capturer_mic_liveness.py -q`
**Goal:** Detect only a multi-second process heartbeat gap. Audio content, absent callbacks, and ordinary pacer cadence never trigger the watchdog.

- Add a pure monotonic-gap state machine with an explicit threshold, safe
  handling for a regressed clock, and one result per observed gap.
- Feed it from the existing pacer thread after capture activation.
- Invoke the suspension callback after releasing the chunker lock.
- Build a direct Swift harness for normal cadence, exact-boundary, over-boundary,
  re-arm, clock-regression, and one-shot callback cases.
- Add Python source-shape pins that fail if system suspension detection is
  removed, moved into the IOProc, or dispatched while the chunker lock is held.

### Phase 2: Fail-Loud Native-to-Supervisor Boundary

**Impl files:** `native/onoats-capturer/Sources/SystemCapture.swift, native/onoats-capturer/Sources/main.swift`
**Test files:** `tests/test_capturer_mic_liveness.py, tests/test_socket_supervisor.py, tests/test_native_contract_parity.py`
**Test command:** `uv run pytest tests/test_capturer_mic_liveness.py tests/test_socket_supervisor.py tests/test_native_contract_parity.py -q`
**Validation cmd:** `make -C native build`
**Goal:** A suspension gap invalidates the entire native capture generation and exits non-zero through the one ordered teardown owner; no socket reconnect or in-process tap rebuild occurs.

- Give production `SystemCapture` a terminal callback while leaving self-tests
  callback-free.
- Route the suspension callback into `Teardown.shared.trigger` with a stable
  non-zero code and diagnostic reason.
- Preserve idempotence when the watchdog races SIGTERM, socket failure, or a
  second branch failure.
- Reuse the existing supervisor early-exit contract tests to prove partial
  rotation, non-zero status, and no transport self-reconnect.

### Phase 3: Contract Documentation and Live Verification

**Impl files:** `AGENTS.md, docs/audio-socket-contract.md, native/README.md, docs/dev_plans/20261028-feature-menubar-control-panel.md, docs/dev_plans/20261107-bug-capturer-system-tap-resume.md`
**Test files:** `scripts/check_review_markers.py`
**Test command:** `python3 scripts/check_review_markers.py && uv run ruff format && uv run ruff check && uv run pytest`
**Validation cmd:** `bash native/test_capturer_watchdog.sh && make -C native build`
**Goal:** Documentation and live behavior state the same boundary: process suspension fails the current generation; only a later explicit Start creates a new one.

- Update the native and socket contracts plus the root maintainer invariant.
- Append a dated correction to the historic control-panel plan without
  rewriting its original finding.
- Re-run the full local gates and inspect explicitly staged paths for secrets,
  PII, private transcripts, audio, credentials, and unintended binaries.
- Build and sign the app, play the supplied YouTube video, start recording,
  confirm `them` activity, apply four seconds of STOP/CONT to the exact owned
  capturer generation, and verify bounded non-zero failure plus partial rotation.
- Start a new recording and confirm the fresh tap again captures non-zero
  `them` audio from the same video.

## Technical Specifications

### Files to Modify

- `native/onoats-capturer/Sources/Resampler.swift` — feed the existing 20 ms
  pacer heartbeat into a process-suspension watchdog and invoke its callback
  outside the chunker lock.
- `native/onoats-capturer/Sources/SystemCapture.swift` — accept a terminal
  callback for a suspension-detected, no-longer-trustworthy tap generation.
- `native/onoats-capturer/Sources/main.swift` — connect the system-capture
  terminal callback to the existing `Teardown.shared.trigger` non-zero path;
  keep self-tests callback-free.
- `native/Makefile` — expose the direct Swift harness as a repeatable native
  test target while retaining wildcard production compilation.
- `tests/test_capturer_mic_liveness.py` — replace the old source pin requiring
  no system stale hook with shape checks for content-independent suspension
  detection and off-lock terminal dispatch.
- `docs/audio-socket-contract.md` — define suspension detection and the
  resulting fail-loud supervisor boundary.
- `native/README.md` — document operator-visible failure and manual
  STOP/CONT verification.
- `AGENTS.md` — record the load-bearing no-in-process-reconnect invariant.
- `docs/dev_plans/20261028-feature-menubar-control-panel.md` — append a dated
  correction below its review marker and cross-link this plan.

### New Files to Create

- `docs/dev_plans/20261107-bug-capturer-system-tap-resume.md` — reviewed bug-fix contract and evidence workspace.
- `native/onoats-capturer/Sources/ProcessSuspensionWatchdog.swift` — pure,
  clock-injected state machine that detects a pacer wake gap without examining
  audio samples or calling Core Audio.
- `native/onoats-capturer/Tests/ProcessSuspensionWatchdogHarness.swift` — direct
  Swift state-space harness for normal cadence, boundary, resume gap, re-arm,
  and clock-regression cases.
- `native/test_capturer_watchdog.sh` — compiles and runs the standalone watchdog
  harness without introducing a Swift package.

### Architecture Decisions

- Detect process suspension, not silence. `FrameChunker` already owns a 20 ms
  pacer and observes a multi-second cursor jump after the process resumes;
  bit-exact audio content remains warning-only.
- Fail the generation instead of rebuilding Core Audio in place. The process
  tap is created once per session, socket transports never reconnect, and the
  Python supervisor already treats every premature capturer exit as non-zero,
  rotates the partial session, and tears down both branches.
- Keep detection pure and testable. A small watchdog consumes injected
  monotonic values and reports gaps; it owns no thread, timer, audio resource,
  socket, or teardown action.
- Dispatch terminal action only after releasing the chunker lock so teardown
  cannot reverse the chunker-to-writer lock order or stop the chunker while its
  pacer still holds that lock.

### Dependencies

- No new runtime dependency is expected; the native capturer uses Apple
  CoreAudio/AVFoundation APIs and the existing Python supervisor contract.

Verified create-time facts: package version `1.5.1` and Python `>=3.12` are
declared in `pyproject.toml`; `native/Makefile` targets macOS 14.4 and links
AVFoundation, CoreAudio, and AudioToolbox directly. No `Package.swift` exists.
These path, pattern, and dependency facts are part of the plan contract; a
later correction invalidates review and requires `/review-plan` again. Git refs
are point-in-time facts only: branch `bug/capturer-system-tap-resume` resolved
to `c233efefa335dd90ff97e3f1b94d220701fbe078` at creation, and PR #34's merge
commit `2e1c7b6cc647b8e9cd1b8c9002fcf9c1411bd17b` is an ancestor of that base.

### Integration Seams

| Seam | Writer (task) | Caller (task) | Contract |
|------|---------------|---------------|----------|
| Pacer heartbeat to watchdog | `FrameChunker` | `ProcessSuspensionWatchdog` | A normal 20 ms wake and audio content never trip; only a monotonic gap above the reviewed threshold trips. |
| Watchdog to native teardown | `SystemCapture` | `main.swift` / `Teardown` | Callback runs outside the chunker lock, is one-shot per process generation, and exits with a non-zero capturer code. |
| Capturer exit to session failure | native capturer | Python socket supervisor | Existing early-exit path stops both branches, rotates partial output, reports failure, and never reconnects the transport. |
| New generation | menu-bar app or CLI Start | Python supervisor | A later explicit Start mints a new socket directory and nonce and creates a new tap; no in-process tap replacement is attempted. |

## Architecture & Call Flow

Component graph — which process triggers which:

```mermaid
graph LR
    U[User starts recording] --> A[Menu-bar app or CLI]
    A --> S[Python socket supervisor]
    S --> C[Native capturer process]
    S --> R[Recorder pipelines]
    C -->|mic socket| R
    C -->|system socket| R
    C -->|non-zero exit after suspension gap| S
    S -->|stop and rotate partial session| R
```

Trigger order — one reproduced suspension failure:

```mermaid
sequenceDiagram
    participant U as User or OS
    participant C as Native capturer
    participant W as Suspension watchdog
    participant S as Python supervisor
    participant R as Recorder
    U->>C: SIGSTOP then SIGCONT
    C->>W: first pacer wake after multi-second gap
    W-->>C: generation untrustworthy
    C->>C: ordered teardown and non-zero exit
    C-->>S: process exit plus socket EOF
    S->>R: cancel, finalise, rotate partial session
    S-->>U: failed recording; next Start required
```

Context lifecycle — what each process owns:

| Step | Trigger | Enters context | Cleared/persisted | Turn boundary |
|------|---------|----------------|-------------------|---------------|
| 1 | Start action | data directory, fresh socket paths, nonce | Persisted for one supervisor generation | Capturer and recorder launched |
| 2 | Capturer activation | tap, aggregate, IOProc, pacer baseline | Process-local until ordered teardown | Both sockets streaming |
| 3 | Pacer wake | current monotonic tick and prior tick | Tick replaces prior tick; no audio samples retained | Watchdog returns normal or gap |
| 4 | Gap detected | fixed failure reason and non-zero exit code | Log/status failure persists; native resources cleared | Capturer exits |
| 5 | Supervisor observes exit | immutable generation identity and partial session | Session rotates to `pending/`; sockets and nonce discarded | Process returns non-zero |
| 6 | Later explicit Start | entirely new paths, nonce, tap, aggregate, IOProc | New generation; no old native context reused | New recording begins |

## Testing Notes

### Test Approach

- [ ] Deterministic native/source contract regression for the watchdog state space.
- [ ] Focused Python/native contract and supervisor tests.
- [ ] Native build and full Python validation.
- [ ] Signed-app STOP/CONT manual verification using the supplied system audio.

### Test Results

- [ ] Pre-fix regression fails.
- [ ] Focused tests pass.
- [ ] Full validation passes.
- [ ] Manual verification passes or records a bounded fail-loud restart.

### Edge Cases Tested

- [ ] No rendered audio / no tap callbacks.
- [ ] Active callbacks with deliberate digital silence.
- [ ] Non-zero audio returns before escalation.
- [ ] Recovery attempt succeeds.
- [ ] Recovery attempt fails or blocks.
- [ ] Stop races the watchdog or recovery path.

## Acceptance Criteria

- After a four-second capturer suspension, active system playback either returns
  as non-zero `them` audio or the capturer exits non-zero within the documented
  bound; indefinite warning-only silence is impossible.
- A normal paused player or digital-silence stream does not force a restart.
- The supervisor remains the only whole-generation restart owner, and every
  failed recovery rotates the partial session through existing fail-loud logic.
- No resource residue, duplicate tap chain, branch mixing, or unbounded wait is
  introduced.
- Focused tests, full tests, Ruff format/check, review-marker validation, and
  native build pass.
- Documentation, the plan, and PR description reflect the final failure and
  recovery semantics.

<!-- reviewed: YYYY-MM-DD @ <hash> -->

## Progress

- [ ] Phase 1: Suspension Watchdog and Deterministic Harness
- [ ] Phase 2: Fail-Loud Native-to-Supervisor Boundary
- [ ] Phase 3: Contract Documentation and Live Verification

## Findings

- 2026-11-07: live checkout was clean `main` at `c233efe`, the regular merge of
  PR #36. The older feature branch recorded the manual finding in `a721480`,
  but that commit was not part of the merged PR.

## Issues & Solutions

- (append as implementation proceeds)

## Final Results

### Summary

- (fill when complete)

### Outcomes

- (fill when complete)

### Learnings

- (fill when complete)

### Follow-up Work

- (fill when complete)
