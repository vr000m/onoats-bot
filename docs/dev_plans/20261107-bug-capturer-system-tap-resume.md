# Task: Recover or Fail Loud When the System Tap Stalls After Process Resume

**Status**: In Progress
**Component**: macos, transport, recorder
**Assigned to**: Codex
**Priority**: High
**Branch**: bug/capturer-system-tap-resume
**Created**: 2026-11-07
**Review Gates**: full

## Objective

Ensure the native system-audio capturer cannot remain apparently healthy while
the process tap delivers permanent silence after a process suspension. The tap
must either recover without mixing branches or terminate non-zero so the
supervisor rotates the partial session and starts only a fresh generation.

## Context

Signed-app manual testing reproduced a Core Audio process-tap failure after
`SIGSTOP`/`SIGCONT`: telemetry freshness and STT recovered, but the `them`
branch remained silent and emitted `zero-run-warning` without
`zero-run-clear`. The current all-zero detector is warning-only because a
rendering application can legitimately output digital silence. The test must
separate an actual rendered-audio stall from that benign case before escalating.

## Requirements

- Preserve never-mix: system tap audio can reach only the system socket and
  `them` STT session.
- Preserve legitimate idle playback and digital silence; no watchdog may infer
  a fatal tap failure merely from the absence of callbacks or all-zero samples.
- When independent evidence establishes that audio is actively rendering and
  the system tap remains stuck, recovery is bounded and either restores
  non-zero `them` samples or exits the capturer non-zero.
- Preserve the supervisor as the sole owner of whole-generation restart; the
  native capturer must not reconnect sockets or silently create a second tap
  generation behind the recorder.
- Retain teardown ordering: stop device, destroy IOProc, destroy aggregate,
  destroy tap, then close sockets through the existing single exit path.
- Add a deterministic regression that fails against the pre-fix implementation
  and cover benign silence, restored audio, failed recovery, and teardown races.
- Verify the signed-app scenario with the supplied YouTube video as real
  system output after automated gates pass.

## Review Focus

- False-positive safety: paused playback and intentional digital silence must
  not cause a restart loop.
- Core Audio lifecycle: no duplicate IOProc, aggregate, tap, worker, or socket
  ownership during recovery.
- Process boundary: a failed recovery must be non-zero and bounded so the
  supervisor reports truncation and owns the next generation.
- Realtime safety: the IOProc remains copy-only; no logging, Core Audio API
  call, allocation-heavy recovery, or contended recovery lock runs there.

## Implementation Checklist

_Phases will be written after the required architecture and call-flow
confirmation gate._

## Technical Specifications

### Files to Modify

_To be grounded by the create-time Explore step._

### New Files to Create

- `docs/dev_plans/20261107-bug-capturer-system-tap-resume.md` — reviewed bug-fix contract and evidence workspace.

### Architecture Decisions

_Pending Explore facts and architecture confirmation._

### Dependencies

- No new runtime dependency is expected; the native capturer uses Apple
  CoreAudio/AVFoundation APIs and the existing Python supervisor contract.

### Integration Seams

_Pending Explore facts and architecture confirmation._

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

## Progress

- [ ] Phase 1: pending architecture confirmation

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
