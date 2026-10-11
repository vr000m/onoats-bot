# Task: Run id header and never-reused speaker labels

**Status**: In Progress
**Component**: recorder, stt
**Assigned to**: Claude
**Priority**: Medium
**Branch**: feature/them-channel-diarization (folded into PR #29)
**Created**: 2026-10-10
**Review Gates**: none

## Objective

Two additive changes that let the koda consumer attribute speaker labels
correctly. Built as a follow-up to the them-channel diarization work and folded
into the same PR (#29) so the labels and the header can be tested together:

1. **Run id header.** Every session file starts with a `session_meta` line that
   carries a per-recorder-run id, so a consumer can tell "same run" from
   "unknown" across the files a run rotates through.
2. **Never-reused speaker labels.** Within one run a label never refers to two
   different speakers: a model slot that has been idle for
   `SLOT_IDLE_REMINT_SEC` is given a fresh label when it next speaks.

Names are mapped by the consumer (koda), per transcript; onoats has no UI for
that and never rewrites `speakers` with display names. S-labels stay canonical.

## Decisions (from the koda exchange, 2026-10-10)

- Header placement: extend the first-line `session_meta`, emitted always.
  `category` is written **only when a category lock exists**. koda treats any
  non-blank `category` as a hard lock that skips classification, so a default
  of `"uncategorized"` would pin every transcript; absent means no lock.
- Header content is known at file creation only: `run_id` (random opaque token,
  `uuid4().hex`, no PII) and, when diarization is configured on,
  `diarization: {"model": <repo id>, "version": <cached snapshot revision>}`.
  Never a speaker list. The header says "configured", not "actually labelled":
  a runtime degrade (fail open) leaves it unchanged, and per-utterance
  `speakers` is the truth.
- Labels: run-scoped monotonic counter, never reused (`S1..S8`, then `S9`,
  `S10` after a re-mint). Grammar stays `^S[0-9]+$`. No hashes.
- Re-mint rule: a slot idle for more than `SLOT_IDLE_REMINT_SEC` (300 s) gets a
  new label on its next activity. This errs toward splitting one person in two
  (safe) and does not catch a swap while both speakers are active. How the
  model behaves on a 20-60 minute multi-speaker recording is still unverified
  (Phase 9 smoke is open), so labels are documented as best-effort.

## Requirements

1. `session_meta` is line 1 of every session file written by `TranscriptBuffer`
   (including rotated/continuation files and `bot-single`), with
   `{"type":"session_meta", "category"?, "run_id", "diarization"?}`.
2. `category` is omitted when no lock exists; `session_meta_line(None)` must not
   default it.
3. One run id per `TranscriptBuffer` instance, so it is stable across
   continuation flushes and distinct across process starts.
4. `diarization` appears only when the flag is on, and is computed without
   importing `mlx_audio` or touching the network.
5. `DiarizationTimeline` allocates labels from a monotonic counter; a slot idle
   for more than `SLOT_IDLE_REMINT_SEC` re-mints on its next active frame; the
   label for an utterance window uses the generation in force at the slot's
   last active frame in that window.
6. The `me` branch, the flag-off path and the JSONL utterance keys are unchanged.

## Files to Modify

- `src/onoats/categories.py` - `session_meta_line(category, *, run_id, diarization)`
- `src/onoats/processors/transcript_buffer.py` - run id, always-on header
- `src/onoats/dual.py`, `src/onoats/stt/diarization_model.py` - header provenance
- `src/onoats/stt/diarization_timeline.py` - slot generations, re-mint
- tests, `AGENTS.md`, `README.md`, `CHANGELOG.md`, the diarization plan (pointer)

## Progress

- [x] Header: `session_meta_line` + `TranscriptBuffer` + provenance + tests
- [x] Labels: generations and re-mint in `DiarizationTimeline` + tests
- [x] Docs: AGENTS, README, CHANGELOG, sibling plan pointer
- [x] Pre-push checks; folded into PR #29

## Findings

- The integration test no longer bounds `S<n>` by the slot count: numbers can pass the model's 8 slots after a re-mint.
- `SLOT_IDLE_REMINT_SEC = 300` was chosen to match the default silence-timeout; it is a named constant in `diarization_timeline.py` and not configurable. A speaker who is quiet for longer is split into a new label.
- Not verified: behaviour of the model's slots over a 20-60 minute multi-speaker recording (the Phase 9 manual smoke is still open). The re-mint rule is a safeguard, not a guarantee.
