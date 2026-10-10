# Task: Live speaker diarization on the "them" channel

**Status**: In Progress
**Component**: recorder, stt
**Assigned to**: Claude
**Priority**: Medium
**Branch**: feature/them-channel-diarization
**Created**: 2026-10-07
**Review Gates**: full

## Objective

Tag each finalized `them`-branch `TranscriptionFrame` with a session-scoped
speaker label (`S1`, `S2`, …; `S1 / S2, overlapping` when ambiguous), using
Nemotron-3-Diarization run live alongside the existing STT path — gated
behind a config flag, default off. The `me` branch is never diarized (always
one known speaker).

## Revision Note (2026-10-07, revision 2; revisions 3 and 4 applied 2026-10-08)

Revision 1 (the first rewrite) put a shared `DiarizationTimeline` between an
upstream feeder and a downstream labeler, replacing a frame-attribute
hand-off that could never work (the `TranscriptionFrame` is created inside
`run_stt` and only flows downstream). It dropped the in-process streaming
ASR. A second `/skein:review-plan` run (five lenses + one contradiction
pass; 1 Critical, 35 Important, 18 Minor) accepted that direction and
found the remaining gaps below. Seven decisions were resolved by interview
(`Decision (grilled)`); every other finding is applied mechanically.
Findings JSON: `.review-plan/latest-claude.json` (run `20261007215940`;
overwritten by each review run).

**Decisions made in this revision (all `Decision (grilled)`):**
1. **Thread model.** The diarizer runs on **one dedicated worker thread** that
   performs the model load, every `feed()`, and the final flush. Audio
   chunks reach it through a bounded drop-oldest queue; the feeder remains a
   pass-through observer. The earlier "never a separate OS thread" rule is
   replaced by "never share one model's streaming state across threads"
   (`experiments/diarization_scratch/latency_experiment.py` already ran
   load+feed on a single thread, RTF 0.038).
2. **One correlation clock**, owned by `diarization_timeline.py` (Req 1).
3. **Pinned streaming preset `low`** (~1.04 s output lag) with a bounded
   labeler hold, because `feed()` emits nothing until a full chunk +
   right-context window accumulates and the shipped default preset has
   ~30 s of latency.
4. **Hoisted `_preflight_diarization`** so "fail loud before any capture side
   effect" is actually true (Req 9).
5. **Per-instance `stamp_segment_samples` kwarg** so `me` frames never carry
   `onoats_segment_*`.
6. **Speaker cap read from the model config** (`num_speakers`, 8 in the
   shipped HF config; slots are total-distinct per session), not hard-coded 4.
7. **Persist canonical data** (`speakers: [...]`, `overlap: bool`), compose the
   display string at render time via one shared helper.

**Revision 3 (2026-10-08)** applies a third `/skein:review-plan` run (run
`20261007224625`; 0 Critical, 17 Important, 23 Minor) in full. The author
delegated every decision to the review's recommendation, so the choices below
are *not* interview-grilled:
- **One position authority**: the feeder's per-chunk `start_sample` tag is the
  sole pipeline-position source; the worker re-anchors on any discontinuity
  (Req 1). The "output-frame index x 160 = pipeline position" shortcut is
  dropped.
- **Worker is the sole timeline writer** (GAP records included); the
  timeline's full cross-thread API is enumerated (Req 1).
- **Labeler hold = ordered pending deque**; only `TranscriptionFrame`s wait,
  `EndFrame`/`CancelFrame` drain it, degrade skips the hold (Req 4).
- **Segment spans = per-instance FIFO** (*revision 4 replaced it with a single
  slot for pipecat 1.3.0-1.8.x, which run `run_stt` inline; revision 5 restores
  the FIFO so pipecat 1.9+, which queues segments onto one in-order task, works
  too - see Req 3*).
- **Preflight is import-free and non-downloading** (`find_spec` + local-cache
  probe), memoized (Req 9).
- **In-pipeline measurements move out of Phase 0** into a new Phase 8b gate;
  Phase 0 is a scratch-harness feasibility check only.
- **`scripts/release_check.sh` is added to Phase 8** and made to run the
  contract test (hard-fail, not skip).

**Revision 4 (2026-10-08)** applies a fourth `/skein:review-plan` run (run
`20261008072818`; 1 Critical, 14 Important, 17 Minor). Its Critical finding
was verified against the installed library: pipecat 1.3.0
`SegmentedSTTService._handle_user_stopped_speaking` awaits
`process_generator(self.run_stt(...))` **inline** inside
`process_frame(VADUserStoppedSpeakingFrame)` (`stt_service.py:765-780`) — there
is no segment queue, so revision 3's "out-of-band queue / FIFO" premise was
wrong. Decisions:
- **`Decision (grilled)` — single pending span slot, fail loud** (*superseded
  in revision 5, owner-directed: pipecat 1.9+ queues segments, so the slot
  became an ordered span queue; see Req 3*). The span is captured *before*
  delegating the VAD-stop frame to `super()` and consumed at the head of
  `run_stt`.
- **Shutdown (author question, confirmed against pipecat docs and
  `dual._shutdown_watcher`)**: `EndFrame` is an *in-band* graceful-shutdown
  frame that travels the pipeline **before** it closes (the terminal transcript
  flush runs after the drain), so the feeder sees it first and the labeler
  last. The worker's final flush is therefore **kept for `EndFrame`** (the
  feeder posts a finish sentinel; the labeler waits up to `HOLD_SEC` for the
  flush to complete, then releases) and skipped for `CancelFrame`
  (Req 2 / Req 4). *Author to veto if a no-flush shutdown is preferred.*
- Applied mechanically: worker lifecycle (stop/join/daemon), labeler wake
  task, loop-side stall guard, `coverage_high_water` semantics, the real-pipeline
  gate renamed **Phase 8b** and ordered after Phase 8, preflight call sites moved
  to Phase 1, `bot-single` handling, latency acceptance criterion, extra tests.

## Context

Validated empirically in `experiments/diarization_scratch/` (a two-speaker
podcast clip, not committed to this branch) before scoping this plan.
Figures below that could not be reproduced from the retained artifacts are
marked *(recollection)*.

- **API names** (`speaker_probs`, `frames_processed`, `num_speakers`, the
  `offline`/`low` presets) were read from the vendored mlx-audio 0.5.8 source
  under `experiments/diarization_scratch/.vendor/`, **not** by importing it in
  the repo venv (`mlx_audio` is neither installed nor locked). Phase 0
  re-confirms each against the installed package.
- **Decode approach**: the diarizer alone (`diar.feed()`) produces per-frame
  per-speaker activity probabilities at 10 ms resolution (`speaker_probs`,
  hop 160 samples @ 16 kHz). Overlap is read off this output — multiple
  speakers over threshold in the same frames — with no separate ASR decode.
  (An earlier masked-vs-timestamps ASR comparison, 64 cross-speaker
  overlapping segment pairs *(recollection)*, motivated dropping the
  in-process ASR; it is moot now.)
- **Streaming latency / coverage**: `feed()` returns an empty output until a
  full `chunk_len*8 + right_context*8` window of input has accumulated. The
  HF model config ships `chunk_len=340 / right_context=40`, equal to the
  `set_streaming_config("offline")` preset (~30.4 s input latency). The
  presets are `low` ≈ 1.04 s, `very_low` ≈ 0.64 s, `ultra_low` ≈ 0.32 s.
  This plan pins `low` (see Req 2); diarizer output for a given audio
  position therefore becomes available ~1 s after that audio was fed.
- **`feed()` input contract**: 1-D `float32`; `feed(audio, state,
  sample_rate=16000, final=False)` raises on `ndim != 1` or when the
  caller-supplied `sample_rate` kwarg != 16000 — it **cannot** detect a
  mis-rated array. The 16 kHz guarantee rests on `runtime.PIPELINE_SAMPLE_RATE
  == 16000`; the worker passes `sample_rate=PIPELINE_SAMPLE_RATE` and asserts
  it equals the model rate at load (Phase 3 test). The pipeline delivers PCM16 bytes — the feeder converts
  (`np.frombuffer(int16) → float32 / 32768`). Pinned by the Phase 8 contract
  test.
- **Real-time feasibility (diarizer-only)**: standalone measurement
  (`latency_experiment.py`) of the diarizer alone processing 100 ms chunks:
  RTF 0.026 (alone), RTF 0.038 (concurrent with a second load), on a worker
  thread. The combined diar+ASR loop (`realtime_sim.py`) measured mean
  3.7–4.8 ms, p95 6.9–7.4 ms, max 104–109 ms per 20 ms chunk but included the
  dropped ASR call and ran in a synchronous `time.sleep` harness with no
  asyncio/VAD/SmartTurn/socket pump. **Diarizer-only latency with the real
  pipeline running has not been re-validated** — Phase 8b item (Phase 0 gives
  scratch-harness estimates only).
- **Threading**: MLX Metal streams are thread-bound (a model loaded on one
  thread crashes on use from another: `There is no Stream(gpu, N) in current
  thread`). The constraint is *per-stream thread affinity*, not "must be the
  event-loop thread". A single worker thread that owns load+feed satisfies
  it and keeps a ~25–100 ms synchronous MLX call off the loop that drives
  both arms, both WS STT clients and the socket staging pump (whose
  consumer-stall is fatal per AGENTS.md). `SmartTurnShadowObserver`
  (`processors/smart_turn_shadow.py`) is the in-repo precedent for
  offloading inference behind a bounded buffer.
- **GPU contention**: the (diar+ASR) loop run concurrently with 589 real
  commits against the live `pipecat-local-stt-server` (nemotron.sock) was
  statistically indistinguishable from baseline. Not re-measured for the
  diarizer-only worker; Phase 8b item.
- **Memory footprint (diarizer only)**: +272 MB RSS measured loading
  `mlx-community/Nemotron-3-Diarization` alone in a fresh process; it loads
  inside the recorder (`onoats bot`) process, on top of its baseline RSS.
- **mlx-audio dependency**: PyPI ships `mlx-audio==0.5.8` (2026-10-05) with
  the `Nemotron-3-Diarization` class. It requires `transformers>=5.14` (not
  in `uv.lock`) and `mlx>=0.31.1`; a fresh venv resolved `mlx` 0.32.3, but
  `uv.lock`'s 0.31.2 satisfies the requirement, so whether `uv lock` bumps
  `mlx` is **decided by Phase 8's committed `uv.lock` diff** (Phase 0's scratch
  `uv lock` is advisory), not assumed. It must be
  in an optional extra, never the baseline set (CI installs baseline deps on
  `ubuntu-latest`, MLX-free).
- **Speaker slots**: the shipped model config has `num_speakers: 8`;
  `spkcache_preds` persists across the streaming session, so slots are
  **total-distinct-per-session**, not max-simultaneous. Slot stability was
  only checked on a 60 s clip whose raw per-frame outputs were discarded
  *(recollection)*; the retained `experiments/diarization_scratch/` artifacts
  also include a labelled 0-300 s ground truth and a 300 s latency run, which
  Phase 0 uses first. The real target is a 20-60 min multi-speaker recording
  (Phase 8b / manual smoke), because slot-overflow behaviour only shows up
  there.
- **Pipecat segment shape** (pipecat 1.3.0, `SegmentedSTTService`): the audio
  handed to `run_stt` is a WAV container (44-byte header) wrapping the
  buffered turn, including up to 1 s of pre-speech roll and the Silero stop
  tail; `run_stt` gets no position, and the buffer is cleared before it runs.
  The parent runs `run_stt` **inline**: `_handle_user_stopped_speaking`
  awaits `process_generator(self.run_stt(...))` inside
  `process_frame(VADUserStoppedSpeakingFrame)` (`pipecat/services/stt_service.py:765-780`),
  and `FrameProcessor` drains its input queue sequentially, so at most **one**
  span is outstanding per STT instance (there is no segment queue).
  *From pipecat 1.9.0* the handler instead puts the segment on
  `_segment_queue` and ONE `_segment_task_handler` task awaits `run_stt` for
  each in turn, so several spans can be outstanding - but still in VAD-stop
  order (revision 5; Req 3).
  `WebSocketSTTService` (`src/onoats/stt/websocket_stt_service.py`) also
  serializes its own `run_stt` calls behind an onoats-owned `_run_stt_lock`,
  which VAD segments therefore never contend.

## Architecture & Call Flow

Components across two layers plus a worker thread. The shared
`DiarizationTimeline` (leaf module `diarization_timeline.py`) is the only
thing that crosses the upstream/downstream boundary — never a frame
attribute. Two preflight/config touch points sit outside the pipeline.

```
 startup (both entrypoints, right after _acquire_instance_lock,
          BEFORE capturer spawn / PortAudio open):
   runtime._preflight_diarization(cfg)   -- find_spec + local-cache probe only
                                            (no mlx import, no download;
                                            memoized); fails loud (rc=1, ERROR,
                                            install / download hint)

[raw audio chunks, them-arm, PCM16 16 kHz]
        |
        v
  DiarizationFeeder  (new, upstream of system_stt, them-arm only; pure
                      pass-through observer, loop thread)
    - counts samples seen (the correlation clock), tags each chunk with
      its start_sample, puts (start_sample, chunk) on a bounded
      drop-oldest queue; a drop is just a missing start_sample range
      (the worker detects it -- the feeder never writes the timeline)
        |                                        \
        | (frame passes through unchanged)        v
        |                              DiarizerWorker (one OS thread, daemon;
        |                               owns model load + every feed() +
        |                               final flush on EndFrame sentinel)
        |                               - converts PCM16->float32, feed()
        |                               - re-anchors on each chunk's start_sample
        |                                 (discontinuity -> timeline GAP);
        |                                 pipeline position of an output frame
        |                                 = (piecewise) anchor + frame index * 160
        |                               - sole timeline WRITER; runtime
        |                                 failure -> degrade (Req 9)
        v                                        |
  system_stt (WebSocketSTTService,               |  shared DiarizationTimeline
    stamp_segment_samples=True)                  |  (session-scoped, ring-
    - process_frame override counts samples      |   bounded, thread-safe;
      from StartFrame; records the position at   |   no frame carries this)
      VADUserStartedSpeakingFrame / Stopped;     |
      appends (start, end) to an ordered span    |
      queue at VAD-stop, BEFORE super()          |
    - stamps onoats_segment_start_sample /       |
      onoats_segment_end_sample (pre-roll        |
      EXCLUDED), popped from the queue at the    |
      head of run_stt, on each TranscriptionFrame|
        |                                        |
        v                                        |
  SourceTagger (unchanged: stamps onoats_source / user_id)
        |                                        |
        v                                        |
  DiarizationLabeler  (new, after SourceTagger, them-arm only)
    - on TranscriptionFrame: parks it in an ordered pending deque for up to
      HOLD_SEC (preset lag + margin) until timeline coverage reaches
      segment_end_sample (or the diarizer is degraded -> no hold), then
      timeline.label_for(start, end); stamps onoats_speakers (list) +
      onoats_overlap (bool), or nothing (None) on no/partial-failed coverage.
      Every other frame passes straight through; a labeler-owned asyncio
      task (woken by the worker via call_soon_threadsafe + a per-head
      deadline) does the releases; EndFrame waits <= HOLD_SEC for the
      worker's final flush then drains the deque; CancelFrame drains at once
      (Req 4)
        |
        v
  TranscriptBuffer._handle_transcription +
  transcript_buffer._utterance_entry (module-level function)  (WRITER)
    - persists optional `speakers: ["S1"]` and `overlap: true` (omitted
      when no label)
        |
        v
  jsonl.py  (READER: Utterance gains optional speakers/overlap; old files
             without the fields read unchanged)
        |
        v
  one shared label-composition helper, called by BOTH render.py and
  live_terminal.py:
    **Them (S1):** text   |   **Them (S1 / S2, overlapping):** text
    (the "Them" part is the configured [speakers] display label)
```

`mic_arm` (`me`) is entirely untouched — no feeder, no labeler, no
`stamp_segment_samples`, no new frame attributes.

**Ownership of the shared `DiarizationTimeline`**: constructed once per
them-arm build inside `_build_dual_pipeline` (Phase 7) and passed by
reference into `DiarizationFeeder`, the `DiarizerWorker` and
`DiarizationLabeler`; not a module-level singleton; one instance per
recording session, scoped to the lifetime of `system_arm`. The model itself is created by a
`diarizer_factory` that is **invoked on the worker thread** (it returns the
loaded model); the production factory is never called on the event loop.

**`DiarizationFeeder` is an observer, not a consumer**: it reads each audio
frame, enqueues a copy for the worker, and passes the frame through
unchanged — same contract as `SmartTurnShadowObserver`. It must never
swallow, mutate, or delay a frame; non-audio frames pass untouched; and it
keeps passing frames through after the diarizer has degraded. `system_stt`
sees the identical audio stream it would see with the flag off. The feeder is
inserted **immediately before `system_stt`** in `system_arm` (after any
`RawAudioDumpProcessor` / `SmartTurnShadowObserver`, `dual.py` `system_arm`
assembly), so nothing between the feeder and the STT can drop or re-chunk
audio (Req 1's shared-clock premise); a Phase 7 test asserts the adjacency.
**Lifecycle**: the feeder owns the worker thread. On `EndFrame` it posts a
finish sentinel (worker runs the final flush, sets `timeline.mark_flushed()`
and exits) *before* forwarding the frame; on `CancelFrame` it posts an
abort sentinel; it also overrides `cleanup()` as a backstop. Thread joins are
bounded (`DEFAULT_JOIN_TIMEOUT_SEC`) and the thread is `daemon=True`, so a
worker blocked in `feed()` can never hang `onoats stop` (the shutdown
contract is "never a hang"; precedent:
`SmartTurnShadowObserver._drain_pending_task`).

## Requirements

1. **`diarization_timeline.py`** (new leaf module, `src/onoats/stt/` — no
   `onoats` imports, no `mlx_audio` import; matches the leaf-module pattern of
   `stt/launchd.py`, and of `src/onoats/_redact.py` / `src/onoats/_closing.py`
   at the package root). It owns, as the **single** owner:
   - **The correlation clock contract**: the clock is the count of 16 kHz
     mono PCM samples seen since `StartFrame`. The feeder and
     `WebSocketSTTService` each count independently (no counter exists in
     the audio sources today — the socket transport carries only
     `socket_seq`/`captured_monotonic_ns` metadata, which is non-contiguous
     under drop-oldest), and both are valid only because the feeder is a
     pure pass-through (no drop, no re-chunk between them). A shared
     counter helper in this module is used by both. The clock is **not**
     reset by `onoats flush` (Req 11) and is monotonic for the session.
   - `DiarizationTimeline`: session-scoped, thread-safe, ring-bounded by
     sample count. **The worker is the sole coverage writer**; the loop only reads.
     Full cross-thread API (the single contract, pinned in
     `tests/test_diarization_timeline.py`):
     | call | thread |
     |---|---|
     | `record(start_sample, probs)`, `record_gap(start, end)`, `set_slot_count(n)` (once, after model load; `label_for` returns `None` while unset), `mark_flushed()` (after the final flush) | worker |
     | `label_for(start, end)`, `coverage_high_water()`, `is_degraded`, `is_flushed`, `add_progress_listener(cb)` | loop |
     | `mark_degraded(reason=...)` (idempotent, one WARNING total; the reason is free text, e.g. `stalled: no coverage progress over N consecutive hold windows`) | worker (its own failure paths) or loop — the labeler's stall guard and fail-open (Req 4) and the feeder's worker-start-failure / join-timeout paths |
     Coverage data (records, GAPs, slot count) is written **only** by the
     worker; `mark_degraded` is the one idempotent flag either side may set.
     `add_progress_listener(cb)` registers a callback the worker invokes
     (on the worker thread) whenever coverage advances; the labeler's callback
     only does `loop.call_soon_threadsafe(...)`. Also owns the explicit-GAP
     records. **`coverage_high_water()`** = the highest pipeline sample
     position `p` such that every position `< p` is *resolved* (recorded **or**
     covered by a GAP) — GAPs advance it, so a frame whose tail was dropped
     under load does not wait the full `HOLD_SEC`; it never regresses on ring
     eviction.
   - **Ring capacity is derived, not guessed**:
     `MAX_TURN_SEC * (1 + DECODE_TIMEOUT_RATIO) + HOLD_SEC + RING_HEADROOM_SEC`
     (`RING_HEADROOM_SEC` = queue/segment-serialisation slack), so a
     maximum-length turn cannot be evicted before its frame arrives.
     `DECODE_TIMEOUT_RATIO` is 1.5, the same factor as
     `websocket_stt_service.py` (`max(_DECODE_TIMEOUT_SECONDS,
     1.5 * audio_seconds)`) — hoisted to one shared constant or pinned by a
     parity test. `MAX_TURN_SEC` is **not** an onoats turn ceiling (the client
     enforces none): it is the STT *server's* `MAX_UNCOMMITTED_SECONDS`
     (≈ 300 s, cited in a comment in `websocket_stt_service.py`),
     sourced from `stt_server.protocol` if it exports it (a third-party
     import is allowed in a leaf module), else a documented literal. A turn
     longer than that yields an `ErrorFrame` from the server, not a label, so
     eviction of such a span is harmless.
   - **Position authority (one, not two)**: the feeder's per-chunk
     `start_sample` tag is the only pipeline-position source; the model's
     output-frame index is merely an offset *within* a continuous run of
     chunks. The worker tracks `fed_samples` (samples handed to the model)
     and, for each dequeued chunk, compares `start_sample` with
     `anchor + fed_samples`; a mismatch (queue drop) calls
     `record_gap(prev_end, start_sample)` and advances `anchor` by the gap
     length. Pipeline position of an output frame is then
     `anchor + frame_index * 160`, **where `anchor` is piecewise**: it is
     applied per output row by model-input offset (a breakpoint list), not to
     a whole batch, because the `low` preset's ~1 s lookahead emits pre-gap
     rows after a gap has re-anchored (read `frames_processed` from the
     returned state rather than keeping a second counter — Phase 0 confirms
     the field). The model's lookahead affects *availability*, not alignment.
     Coverage for a position is "available" only once the worker has
     recorded it. Pinned by a Phase 3 test: drop one queued chunk, assert a
     later window still labels against the feeder clock.
   - `label_for(start_sample, end_sample) -> Label | None` (single owner of
     the merge/label policy; no duplicate logic in feeder/labeler/
     `SourceTagger`). `Label` carries `speakers: list[str]` and
     `overlap: bool`. Policy defines, with **named constants** (values
     tuned in Phase 2/9, not guessed here): `ACTIVITY_THRESHOLD`
     (per-frame probability to count a speaker active), `DOMINANT_MARGIN`,
     `OVERLAP_MIN_FRACTION`, `MIN_COVERAGE_FRACTION`, `HOLD_SEC`,
     `MAX_TURN_SEC`, `DECODE_TIMEOUT_RATIO`, `RING_HEADROOM_SEC`. Cases: dominant speaker →
     `["S{n}"]`; overlap (two+ speakers each active for ≥
     `OVERLAP_MIN_FRACTION` of the window) → `["S{n}","S{m}"]`,
     `overlap=True`; **zero coverage** (window outside retained history, or
     only GAP) → `None`; **partial coverage** (head evicted, tail not yet
     recorded, or part GAP) → label if covered fraction ≥
     `MIN_COVERAGE_FRACTION`, else `None`. Averaging is
     **activity-weighted** (frames where no speaker is active contribute
     nothing). When the top-scored slot is not the sole
     `OVERLAP_MIN_FRACTION` candidate (no candidate, or a lower-scored one), it
     must lead the runner-up by `DOMINANT_MARGIN` or the window is `None`.
   - **Slot → label mapping**: model slot indices map to `S{n}` in the
     order each slot **first crosses `ACTIVITY_THRESHOLD`** in the session
     (a session where slot 3 speaks first labels it `S1`). The number of
     slots is `num_speakers` read from the loaded model config (8 in the
     shipped config), not a hard-coded 4. Exceeding it, or any other
     non-mappable state, degrades the affected window to `None` and logs
     **exactly one** WARNING per session (the once-flag lives on the
     `DiarizationTimeline` instance, not module/process-global, so a new
     session warns again).
   - Speaker IDs reset only when a new recording/pipeline session starts
     (Req 11).
2. **`DiarizationFeeder` + `DiarizerWorker`** (new,
   `src/onoats/processors/diarization_feeder.py`; the worker may live in the
   same module). The feeder is inserted into `system_arm` only, before
   `system_stt`. It counts samples, converts nothing itself, and enqueues
   `(start_sample, pcm_bytes)` onto a bounded drop-oldest queue (a drop is
   simply a missing `start_sample` range; the feeder never writes the
   timeline — the worker detects the discontinuity, Req 1). The **worker
   thread** invokes `diarizer_factory` (production: loads
   `mlx-community/Nemotron-3-Diarization` from its locally cached snapshot
   directory), calls
   `set_streaming_config("low")` **before the first `feed()`**, publishes
   `model.config.num_speakers` (**not** the per-call `output.num_speakers`,
   which counts distinct segment speakers) via `timeline.set_slot_count`,
   converts each `output.speaker_probs` (an `mx.array`) to numpy, converts PCM16 →
   float32/32768, and is the only thread that ever touches the model (load,
   `feed()`, final flush) and the only thread that writes the timeline.
   `mlx_audio` is imported lazily **inside the worker's loader**, never at
   module top level, so importing the module (and `onoats.dual`) succeeds
   in the native-free baseline.
3. **`WebSocketSTTService`** (`src/onoats/stt/websocket_stt_service.py`): new
   ctor kwarg `stamp_segment_samples: bool = False`, threaded through
   `runtime._create_stt_service`, set **only** for the system arm and only
   when the diarization flag is on. When true: a `process_frame` override
   counts samples from `StartFrame` using the Req 1 counter helper (in the
   `AudioRawFrame` branch of `process_frame`, which pipecat 1.3.0 enters
   exactly once per frame), records the position at
   `VADUserStartedSpeakingFrame` and `VADUserStoppedSpeakingFrame`, and
   stamps `onoats_segment_start_sample` / `onoats_segment_end_sample` on the
   `TranscriptionFrame` yielded by `run_stt`. The span **excludes**
   pipecat's pre-roll (a previous speaker's tail must not bias the label).
   **Correlation structure** (revision 5): segments reach `run_stt` in the order
   their VAD-stops were seen - pipecat 1.3.0-1.8.x await `run_stt` **inline**
   inside `process_frame(VADUserStoppedSpeakingFrame)`; 1.9+ put the segment on
   `_segment_queue` and ONE task awaits `run_stt` for each in order. The
   override therefore keeps an ordered **span queue** (`_span_queue`): it
   appends `(start_sample, end_sample)` **before delegating the VAD-stop frame
   to `super()`** (an entry appended after `super()` returns would land after an
   inline `run_stt` has already run and every frame would go unstamped), and
   `run_stt` pops the head at its start and stamps the yielded
   `TranscriptionFrame`. A VAD stop with no recorded start appends a `None`
   placeholder (that segment still reaches `run_stt`, unstamped, and later
   segments stay aligned); a VAD stop on a service that is not `is_usable`
   (pipecat 1.9+ drops that segment) appends nothing; `run_stt` finding the
   queue empty logs an ERROR and leaves that frame unstamped (the labeler then
   passes it through unlabelled) - it never guesses a span. The queue is
   cleared on `StartFrame`; a reconnect / stale-segment discard leaves it
   untouched (each entry belongs to a segment not yet at `run_stt` and is
   popped exactly once by it, so nothing stale survives). Residual risk: an
   exception raised by pipecat's stop handler between the append and the
   segment being queued would strand an entry and misalign later segments;
   not guarded.
   Guard: `runtime._preflight_diarization` calls
   `diarization_timeline.segmented_stt_orders_spans()`, which inspects the
   installed pipecat's source and accepts only the two ordered shapes (inline:
   a stop handler with exactly one `run_stt` call; queue: a plain
   `asyncio.Queue()` built once in `__init__`, one
   `self._segment_task = self.create_task(self._segment_task_handler())` in
   `start`, a stop handler whose one top-level `put` on `_segment_queue` is
   reached on every stop except the `not self.is_usable` drop the STT mirrors,
   no other `put` but the `None` finish sentinel, and `_segment_task_handler`
   as the queue's sole reader - one sentinel-ended `while` loop, with no
   `return`/`break`/`raise`, that `get`s each item and awaits its ONE
   `process_generator(self.run_stt(<item>))` under at most `if <item>:` and a
   `try` body), failing closed on anything else (unreadable source, a
   concurrent per-segment task, a one-shot or skipping reader, an unknown
   design). The
   coupling is also pinned by a test against the installed version that
   asserts the private attributes / method signatures used
   (`hasattr` / `inspect.signature`) and that
   `SegmentedSTTService.process_audio_frame` has no reconnect-replay branch.
   A bare `pipecat.__version__` assertion is avoided (fails on every harmless
   bump).
4. **`DiarizationLabeler`** (new, `src/onoats/processors/diarization_labeler.py`),
   `system_arm` only, after `SourceTagger`. On each `TranscriptionFrame` with a
   stamped range it **parks the frame in an ordered pending deque** (no
   in-line `await` in `process_frame` — that would stall the whole `them`
   arm, VAD frames included); a frame with **no** stamped range (Req 3's
   fail-loud case) is forwarded unlabelled, after draining any frames already
   held (FIFO). Releases are driven by
   a **labeler-owned asyncio task** (started at `StartFrame`, precedent
   `DualSilenceDetector.start_monitoring`), woken by (a) the worker's
   progress listener via `loop.call_soon_threadsafe` and (b) a per-head
   `HOLD_SEC` deadline — so a held frame releases even if no further frame
   ever arrives (a quiet `them` arm). The deque is released head-first when
   `timeline.coverage_high_water()` reaches that frame's
   `onoats_segment_end_sample` or `HOLD_SEC` elapses (coverage is monotonic,
   so same-arm transcription order is preserved); the released frame gets
   `label_for(...)` and `onoats_speakers` (list) / `onoats_overlap` (bool)
   via `setattr` (pattern in `source_tagger.py`), or nothing when the result
   is `None`. **Frame-class rule**: every other frame (audio, VAD, system)
   passes through immediately. **`EndFrame`** is in-band graceful shutdown (the
   terminal transcript flush runs only after the pipeline drains, so any held
   utterance must reach `TranscriptBuffer` first): the labeler waits up to
   `HOLD_SEC` for `timeline.is_flushed` (or `is_degraded`), then releases every
   pending `TranscriptionFrame` in FIFO order (label with current coverage,
   else none) and only then forwards the `EndFrame`. **`CancelFrame`**
   releases the same way immediately, without waiting. Shutdown therefore
   never truncates a held utterance and never blocks past `HOLD_SEC`.
   **Ordering contract**: `them`-arm `TranscriptionFrame`-vs-VAD ordering is
   relaxed by up to `HOLD_SEC`; nothing downstream couples them (the
   silence/gap logic reads VAD only). `TranscriptBuffer` persists in
   **arrival order** (it stores `branch_sequence` as metadata, it does not
   re-order by it), so the real effect is a cross-arm arrival-order skew of up
   to `HOLD_SEC` between a `me` line and a `them` line in the JSONL / rendered
   transcript; each entry keeps its own `frame.timestamp`. This skew is
   accepted. **Degraded**: once `timeline.is_degraded`, the labeler drains any held
   frames (FIFO) and then forwards `TranscriptionFrame`s immediately with no
   hold and no attributes.
   **Stall guard**: a `feed()` that hangs (Metal hang rather than an error)
   never trips the worker's own degrade triggers, so the labeler counts
   consecutive `HOLD_SEC` expiries during which `coverage_high_water()` did
   not advance; after `STALL_CONSECUTIVE_TIMEOUTS` it calls
   `timeline.mark_degraded(reason="stalled: ...")` (one WARNING; the reason names the no-progress condition) and stops holding.
   Two refinements keep the guard from false-tripping: expiries do not count
   while the model is still loading (`not timeline.is_ready`, within
   `MODEL_LOAD_GRACE_SEC` = 60 s of the labeler starting — a cold load
   publishes no coverage until its first feed finishes), and the streak
   resets whenever coverage has advanced since the last counted stall, so
   only genuinely back-to-back stalled windows degrade.
5. **`TranscriptBuffer`** (`src/onoats/processors/transcript_buffer.py`):
   `_handle_transcription` reads `onoats_speakers` / `onoats_overlap` (if
   present) and `_utterance_entry` (the JSONL **writer**) persists optional
   `speakers` / `overlap` keys, omitted when there is no label.
   `SourceTagger`'s canonical `me`/`them` `source` contract is unchanged.
6. **`jsonl.py`** (the **reader**): `Utterance` gains optional
   `speakers: list[str] | None` and `overlap: bool` (default false); files
   written before this feature read unchanged.
7. **Render**: one shared label-composition helper is used by `render.py` (which today
   emits `**{label}:** text`, `render.py` (`render_session`), label from `[speakers]` config)
   and `live_terminal.py`. It is **value-based**, defined in `render.py`:
   `compose_speaker_label(display_label: str, speakers: list[str] | None,
   overlap: bool) -> str`, imported by `live_terminal.py` (the two call sites
   hold different objects — `Utterance` vs frame — so the helper takes plain
   values). `live_terminal.py` keeps its hard-coded Me/Them base names (it
   has no cfg today); only the `(S1)` / `(S1 / S2, overlapping)` suffix is
   shared, and the parity test asserts exactly that. With a label the output is
   `**Them (S1):** text` / `**Them (S1 / S2, overlapping):** text` (the
   "Them" part is whatever the configured label is); without one, the
   existing output is unchanged. No display string is ever persisted.
8. **Config flag**: an `OnoatsConfig` property in
   `src/onoats/config/__init__.py` (env `ONOATS_DIARIZATION` > `config.toml
   [stt] diarization` > default `False`), resolved **once in `run_onoats_dual`** from the single `cfg`
   object and passed as a bool to both `_create_stt_service(...,
   stamp_segment_samples=flag)` (system arm only; `False` for mic) and
   `_build_dual_pipeline(diarization=flag)` — never derived from config
   inside `_create_stt_service` (which calls its own `load_config()`), and
   not a second `os.environ` read.
   When off: feeder/worker/labeler are not constructed or imported, no
   `mlx_audio` import occurs anywhere (including `onoats.dual`), and no
   diarization-related attribute is ever set on a frame.
9. **Failure semantics**:
   - **Config-time failure** — fail loud at startup before any capture side
     effect: non-zero exit, ERROR log, install hint. Implemented by
     `runtime._preflight_diarization(cfg)`: **import-free and
     non-downloading** — `importlib.util.find_spec("mlx_audio")` plus a
     local-cache probe for the model weights (owned by the leaf
     `src/onoats/stt/diarization_model.py`, shared with the feeder's loader;
     `huggingface_hub` — a baseline
     dependency — `try_to_load_from_cache(repo_id, filename)` for both
     `config.json` and `model.safetensors`, per Phase 0; **not**
     `snapshot_download(local_files_only=True)`, which raises
     `IncompleteSnapshotError` on a fully cached repo; no `mlx_audio` import), so no MLX state is ever created
     on the main thread and a cold cache / offline machine fails loud with an
     explicit "weights not downloaded; run <command>" hint rather than
     stalling startup on a network fetch. The config-independent probes (extra installed, weights cached) are
     memoized (mirror `_preflight_cache` in `runtime.py`) so the socket-mode
     double call re-probes nothing; the `stt_service` backend check is cheap
     and runs on every call. The worker does the real load. Called **right after
     `_acquire_instance_lock`** in both `_supervise_socket_session`
     (before the capturer is spawned; it names its own `load_config()` call)
     and `run_onoats_dual` (before PortAudio opens; `src/onoats/dual.py`), and
     **never called when the flag is off**. The third entrypoint,
     `onoats bot-single` / `python -m onoats` (`__main__.run_onoats`), has no
     `them` arm: with the flag on it logs one WARNING that the flag is ignored
     in single-input mode (a Non-Goal, not a fail-loud, because nothing
     capture-side is mis-configured). The flag is ignored there entirely,
     so no backend check applies (pinned by a test asserting
     `_preflight_diarization` is never called).
   - **Runtime failure** after preflight (worker model-load error, `feed()`
     raises, an accumulation window (>= `BUDGET_WINDOW_SEC` of fed audio)
     exceeds the processing budget for `BUDGET_OVERRUN_CONSECUTIVE` consecutive
     windows — a single slow
     first/warm-up call must not degrade, Metal stream error) — degrade:
     the worker stops and calls `timeline.mark_degraded()`, the feeder
     keeps passing frames and stops enqueueing, **one** WARNING is logged,
     and the labeler (seeing `is_degraded`) forwards `TranscriptionFrame`s
     immediately — no hold, no label — for all subsequent frames. A hung
     (non-erroring) `feed()` is caught by the labeler's stall guard (Req 4).
     Never a fatal `ErrorFrame`. The processing
     budget is a ratio (processing time per window < `BUDGET_RATIO` ×
     window audio duration), named constants.
10. **`me` branch is never touched** — no feeder/labeler, `stamp_segment_samples`
    stays False for `mic_arm`'s STT, no new attributes on `me` frames.
11. **`onoats flush` (SIGUSR1)**: speaker IDs, the timeline, and the sample
    clock do **not** reset at a continuation flush (the pipeline keeps
    running; only the transcript file rotates). They reset only when a new
    recording/pipeline session starts.

## Non-Goals

- **In-process streaming ASR for overlap/word-boundary detection.** Cut
  entirely, not deferred — the diarizer's per-frame probabilities suffice.
- **Final-pass / two-clock correction** (re-running a higher-accuracy
  diarizer preset over buffered audio at `flush()`). Follow-up.
- **Persistent voice-profile IDs** (recurring-speaker recognition across
  sessions, renamable names). Follow-up, likely `ONOATS_SPEAKER_PROFILES`.
- Diarizing the `me` channel.
- Any UI/menu-bar surface for speaker labels — only the transcript text
  format and the additive JSONL fields change.
- Supporting more speakers than the loaded model's `num_speakers` — the
  affected window degrades to unlabelled.
- Labels for non-`websocket` STT backends (flag fails loud with those).
- Single-input mode (`onoats bot-single` / `python -m onoats`): the flag is
  ignored there with one WARNING.

## Risks

- **Added `them` commit latency** from the labeler hold (`HOLD_SEC`, ≈ preset
  lag + margin, ~1–1.5 s). Phase 8b measures it in the real pipeline; Phase 9
  asserts a stated bound. If unacceptable, fall back to `very_low` (≈0.64 s)
  at some accuracy cost — decided by Phase 8b data (a preset change is an
  above-marker edit; see the re-mark rule below).
- **Worker-thread behaviour is unvalidated in the real pipeline.** Phase 0
  gives scratch-harness *estimates* only; the **Phase 8b gate** (after the
  pipeline is wired and the extra is locked in Phase 8, before Phase 9) must measure, with the worker running alongside the real asyncio
  pipeline (VAD, SmartTurn, both WS STT clients, socket pump): worker
  processing ratio, any event-loop stall, and GPU contention with the STT
  server. Batch N chunks (e.g. 100 ms) per `feed()` if per-call overhead
  matters; the budget is a ratio, not a per-20 ms-call assertion.
- **`mlx-audio` as a real PyPI dependency.** Must land in the existing
  `[project.optional-dependencies]` table as a new `diarization` key (the
  `macos` extra already exists), never baseline. Pin policy: tests and the
  contract pin are verified at `mlx-audio==0.5.8` and valid for
  `mlx-audio>=0.5.8,<0.6`, re-verified at each bump. The
  **single trigger** is Phase 8's committed `uv.lock` diff: if it changes the
  shared `mlx` entry (used by the `macos` extra's mlx-whisper), run the
  mlx-whisper backend re-smoke and add a CHANGELOG note; if unchanged, no
  re-smoke is needed. Phase 0's scratch `uv lock` outcome is advisory only.
- **Speaker-slot stability** across a real session is unverified beyond a
  60 s clip whose per-frame output was discarded. Validate on a longer
  multi-speaker recording; if slots churn, the label policy may need
  hysteresis (not designed). Slot cap semantics are resolved
  (total-distinct, `num_speakers`); their *behaviour at overflow* still
  needs a long-recording check.
- **Pipecat private-hook coupling** (`SegmentedSTTService` buffering/VAD
  frame ordering, segment-to-`run_stt` order) — guarded by the preflight probe
  and a hook-shape test (Req 3).
- **Above-marker changes from measurement.** Phase 0 / Phase 8b outcomes that
  change the contract (preset fallback, pin policy, `num_speakers`, a
  different cache-probe call) are above-marker edits: update the plan and
  re-run `/review-plan` (or recompute the hash per AGENTS.md) **before the
  next phase's first commit**, so `scripts/check_review_markers.py` stays
  green across the phase boundary. Pure constant tuning in code is not.
- **Label-policy thresholds** have no validated values yet; named constants,
  tuned against a real recording as part of implementation.
- **Stale-lock / queue-drop gaps**: heavy load can drop worker queue chunks
  (recorded as GAPs → `None` labels); this is the intended degradation.

## Files to Modify (phased — land in this order; each phase is independently committable without a broken intermediate state)

Phase 0 is verification only; its outputs (preset behaviour, `num_speakers`,
input contract, `frames_processed` field, non-downloading cache-probe call,
scratch estimates, `uv lock` outcome) are **recorded in `## Findings`** below
the marker, and the Phase 8 contract test pins those recorded values. The
real-pipeline measurements are recorded by the **Phase 8b gate** (after Phase
8, so the real model is installable from the committed `diarization` extra and
lock), not Phase 0.

**Phase 0 — verify, no code lands (scratch-harness feasibility; numbers are estimates only)**
- Scratch venv: install `mlx-audio==0.5.8`; import/instantiate
  `Nemotron-3-Diarization`; confirm the streaming `feed()` API (including the
  `sample_rate` kwarg behaviour), the `set_streaming_config` presets, the
  input contract (1-D float32 @ 16 kHz), that the returned state exposes
  `frames_processed` (the output-frame index source for the Req 1 position
  authority), `speaker_probs` and `num_speakers` — each re-checked against the
  installed package, not just the vendored source. Also record **which
  `huggingface_hub` call resolves the model without downloading** (feeds
  `_preflight_diarization`; no `mlx_audio` call — the preflight is import-free)
  and confirm that loading/feeding on a worker thread works. Run `uv lock`
  against the repo **in a throwaway copy or revert afterwards
  (`git checkout -- pyproject.toml uv.lock`; the Phase 1 diff must touch
  neither)** and record whether `mlx` changes (advisory — Phase 8's committed
  lock is authoritative). Go/no-go gate.
- Slot stability / overflow: start from the retained 0-300 s ground-truth
  clip in `experiments/diarization_scratch/`; state whether that is enough or
  a 20-60 min multi-speaker recording is required (that run happens at
  Phase 8b / manual smoke).
- Scratch estimates (clearly labelled as such, not acceptance bounds):
  processing ratio and loop stall with a worker thread alongside a realistic
  asyncio loop (not a `time.sleep` loop).
- If any above-marker value changes (preset, pin policy, `num_speakers`),
  update the plan and refresh the review marker before the Phase 1 commit
  (Risks → above-marker changes).

**Phase 1 — config flag + preflight (including the hoisted call sites)**
- `src/onoats/config/__init__.py` — add the `diarization` property
  (env > toml > default `False`)
- `src/onoats/runtime.py` — `_preflight_diarization(cfg)` (`find_spec` +
  non-downloading cache probe via the leaf `src/onoats/stt/diarization_model.py`;
  the probes are memoized, the `stt_service != "websocket"` rejection is not);
  no model held, no `mlx_audio` import
- `src/onoats/cli.py` (`_supervise_socket_session`, right after
  `_acquire_instance_lock`, before capturer spawn) and `src/onoats/dual.py`
  (`run_onoats_dual`, right after the lock, before PortAudio opens) — call
  `_preflight_diarization` **only when the flag is on**; `__main__.run_onoats`
  (single-input) logs one "flag ignored" WARNING instead. (These call sites
  depend only on this phase's code, so Phases 4-6 never ship a live flag
  without preflight.)
- `tests/test_onoats_imports.py` — add `mlx_audio` to `_FORBIDDEN` (one-line
  change, green immediately)
- `tests/test_config.py` — property resolution tests
- `tests/test_stt_config_wiring.py` — flag + non-websocket backend fails loud
- `tests/test_runtime_preflight.py`, `tests/test_socket_supervisor.py`,
  `tests/test_dual_socket_source.py` — flag=1-without-extra fails loud with
  the `find_spec` / cache-probe hook monkeypatched (identical in CI and on a
  dev Mac); cold-cache and offline give an explicit "weights not downloaded;
  run <command>" error and perform no download; second call re-probes nothing (extra/weights
  probes memoized; the backend check re-runs); **both entrypoints**: rc=1, capturer spawn / transport builder /
  `select_dual_input_devices` never called, and a shared mock call-order list
  asserts `_acquire_instance_lock` then `_preflight_diarization` precede any
  spawn/open call; flag **off** never calls `_preflight_diarization`;
  single-input mode logs the "ignored" WARNING once

**Phase 2 — leaf module, no model load, no `mlx_audio` import**
- `src/onoats/stt/diarization_timeline.py` (new) — clock contract/counter
  helper, `DiarizationTimeline` (full Req 1 API incl. `coverage_high_water`,
  `mark_flushed`, progress listeners), GAP records, label policy, named
  constants
- `tests/test_diarization_timeline.py` (new) — dominant, overlap
  above/below threshold, zero/partial coverage, GAP, ring eviction, slot
  first-activity mapping (slot 3 active first → `S1`), `num_speakers`
  overflow → `None` with exactly one WARNING (loguru capture asserting
  count), the once-flag is per-instance (two timelines overflowed
  sequentially → two WARNINGs), `label_for` returns `None` until
  `set_slot_count`, **`coverage_high_water()`** (advances on `record` and on
  `record_gap`, stops at an unresolved hole, never regresses on eviction),
  **ring-capacity invariant** (record
  `MAX_TURN_SEC*(1+DECODE_TIMEOUT_RATIO)+HOLD_SEC+RING_HEADROOM_SEC` seconds
  of synthetic frames, then `label_for(0, MAX_TURN_SEC*16000)` is non-`None`
  with coverage 1.0; one headroom-second past that is partial/`None`), parity
  of `DECODE_TIMEOUT_RATIO` with `websocket_stt_service` and of `MAX_TURN_SEC`
  with `stt_server.protocol`, progress listener fires on coverage advance,
  idempotent `mark_degraded` (one WARNING), and a **bounded deterministic
  thread-safety test**: N writer records + M reader `label_for` calls from a
  barrier start with a hard `Thread.join(5)` + `assert not alive`, asserting
  no exception and every fully-recorded window labels non-`None`, plus a
  single-threaded test that interleaves a `record` into an in-progress
  `label_for` via a mocked lock so the invariant does not depend on
  scheduling

**Phase 3 — upstream processor + worker**
- `src/onoats/processors/diarization_feeder.py` (new) — feeder + worker,
  lazy `mlx_audio` import inside the loader only; worker invokes
  `diarizer_factory` on its own thread; finish/abort sentinels, bounded join,
  `daemon=True`
- `tests/test_diarization_feeder.py` (new) — stub diarizer, no model load:
  observer contract (never swallow/mutate/delay; non-audio frames pass;
  keeps passing after degrade **and stops enqueueing**), sample counting,
  **queue drop → worker records a GAP and later windows still label against
  the feeder clock** (drop one queued chunk, assert alignment), worker owns
  all model calls (single thread id, including the factory call), call
  sequence is `['load','set_streaming_config(low)','set_slot_count(n)',
  'feed',…]`, `feed` receives a 1-D `float32` array scaled by 1/32768 with
  `sample_rate=PIPELINE_SAMPLE_RATE`, a loader that raises → degrade (one
  WARNING, feeder keeps passing frames, no `ErrorFrame`), runtime failure →
  degrade with exactly one WARNING and never a fatal `ErrorFrame`,
  consecutive-overrun threshold (single slow call does not degrade),
  **`EndFrame` → final flush runs, `mark_flushed` set, thread exits;
  `CancelFrame` → abort, thread exits; a stub whose `feed()` blocks forever
  still lets the pipeline finish (bounded join, daemon thread)**, `mlx_audio`
  not in `sys.modules` after import/stub run

**Phase 4 — STT segment-sample stamping**
- `src/onoats/stt/websocket_stt_service.py` — `stamp_segment_samples`,
  `process_frame` counting, ordered span queue `_span_queue` (appended before
  delegating VAD-stop to `super()`, popped at the head of `run_stt`; revision
  5), stamping
- `src/onoats/runtime.py` — `_create_stt_service(..., stamp_segment_samples:
  bool = False)` threads the kwarg through (never derives it from config)
- `src/onoats/dual.py` — `run_onoats_dual` resolves `cfg.diarization` once and
  passes `stamp_segment_samples=flag` on the **system-arm**
  `_create_stt_service` call (mic stays `False`); Phase 7 reuses the same
  resolved bool for `_build_dual_pipeline`. (The flag is inert end-to-end
  until Phase 7 wires the feeder/labeler.)
- `tests/test_websocket_stt_reconnect.py` (existing home of
  `WebSocketSTTService` tests) and/or a new
  `tests/test_websocket_stt_stamping.py` — stamped start/end == decoded
  segment's VAD span (pre-roll excluded) across a `StartFrame` reset, driven
  through the real VAD-stop → `run_stt` path (inline or via pipecat's queue
  task, whichever is installed) with a stubbed deferred decode; spans stay
  aligned when several are outstanding before the first `run_stt`; a segment
  dropped by an unusable service queues no span; `run_stt` with an empty queue
  → no stamp (no crash); queue cleared on `StartFrame`, untouched by a
  reconnect / stale discard; kwarg False → no attributes; hook-shape pipecat
  test (attributes/signatures used, segments reach `run_stt` in order, no
  replay branch in `process_audio_frame`);
  **clock parity**: the same frame stream through the feeder counter and
  `WebSocketSTTService`'s counter yields equal sample positions
- `tests/test_stt_config_wiring.py` — kwarg wiring (system arm only;
  `_create_stt_service` never derives it from config)

**Phase 5 — downstream labeler**
- `src/onoats/processors/diarization_labeler.py` (new)
- `tests/test_diarization_labeler.py` (new) — frame attributes only (no
  `TranscriptBuffer`): reads stamped range, hold until coverage or
  `HOLD_SEC` (fake clock), sets `onoats_speakers`/`onoats_overlap`;
  **unstamped `TranscriptionFrame` passes through untouched**;
  evicted-from-ring and N-chunks-behind cases; **a held frame releases at
  `HOLD_SEC` with no further frame traffic, and releases on a worker progress
  callback with no further traffic**; **VAD frame pushed while a
  `TranscriptionFrame` is held reaches downstream first**; **two held frames
  release in FIFO order**; **`EndFrame` mid-hold waits ≤ `HOLD_SEC` for
  `is_flushed` (releases earlier when flushed), releases the held frame with
  the flush-completed coverage, and it reaches downstream before the
  `EndFrame`; `CancelFrame` mid-hold releases immediately**; **degraded →
  forwarded with no hold (< `HOLD_SEC`) and no attributes**; **stall guard:
  `STALL_CONSECUTIVE_TIMEOUTS` expiries without coverage progress →
  `mark_degraded` once, one WARNING, later frames not held**; continuation
  case (two `TranscriptionFrame`s with timeline windows S1 then S2 →
  labelled S1 then S2, clock monotonic)

**Phase 6 — transcript persistence + render**
- `src/onoats/processors/transcript_buffer.py` — module-level
  `_utterance_entry` writes optional `speakers`/`overlap`
- `src/onoats/jsonl.py` — `Utterance` reader gains the optional fields
- `src/onoats/render.py` — `compose_speaker_label(display_label, speakers,
  overlap)`; `src/onoats/processors/live_terminal.py` — imports it (keeps
  its Me/Them base names; shares only the suffix)
- `tests/test_speaker_labels.py` — extend (do not create
  `tests/test_source_tagger.py`; it doesn't exist): schema test (keys
  omitted when None; for a labelled utterance `set(entry) == EXPECTED_KEYS`
  and the configured display label string appears nowhere in
  `json.dumps(entry)`), writer→reader round-trip, **a label surviving
  `TranscriptBuffer.flush(next_session_file=...)` rotation** (S1 then S2, a
  later S2-dominated window still labels S2), old-file back-compat,
  canonical `source` enum unchanged, render strings with default and
  configured labels, live-terminal suffix parity

**Phase 7 — wiring**
- `src/onoats/dual.py` — in `_build_dual_pipeline`, insert feeder/labeler
  into `system_arm` only, gated on the Phase 1 flag (the bool resolved once
  in `run_onoats_dual`, Phase 4); the feeder goes **immediately before
  `system_stt`** (after `RawAudioDumpProcessor` / `SmartTurnShadowObserver`);
  feeder/labeler imports live **inside the flag branch** (function-local, like
  the existing processor imports); accept a `diarizer_factory=` kwarg so
  tests inject a stub model (invoked on the worker thread; no `mlx_audio`
  needed in CI)
- `tests/test_dual_socket_source.py` — `mic_arm` processor types identical
  flag on/off (portaudio and socket builds); flag on uses the stub factory;
  feeder adjacent to `system_stt`; me/them × on/off matrix, every cell
  written out: **`me` frames never carry
  `onoats_speakers`/`onoats_overlap`/`onoats_segment_*` (flag on or off)**,
  **`them` frames with the flag off never carry any of them either (Req 8)**,
  `them` with the flag on carry them only via the labeler; **wired-arm
  degrade**: a raising stub factory / raising `feed()` through the built
  `system_arm` still delivers `TranscriptionFrame`s to `TranscriptBuffer`
  with no `ErrorFrame` and the session continues
- `tests/test_onoats_imports.py` — clean-interpreter subprocess that
  imports `onoats.dual` and the new processor modules and builds flag-off,
  asserting `mlx_audio` is absent from `sys.modules`

**Phase 8 — dependency + contract (one commit)**
- `pyproject.toml` — add a `diarization` key (`mlx-audio>=0.5.8,<0.6`) to
  the existing `[project.optional-dependencies]` table, never baseline
- `uv.lock` — regenerate; diff the `mlx`/`mlx-metal` entries; if changed,
  re-run the mlx-whisper backend smoke and add the CHANGELOG note (the single
  trigger)
- `tests/test_diarization_contract.py` (new) — pins the exact
  `mlx_audio` symbols/kwargs used, the preset names, the input contract
  the Phase 0-recorded `model.config.num_speakers` (8), and `speaker_probs` row rate (feeding N whole 160-sample frames the way the worker does, then its `final=True` flush, yields exactly N emitted rows - counted from `speaker_probs`, not from the model's own `frames_processed` - each feed's rows starting at the `frames_processed` read before it); skip-unless `mlx_audio`
  importable, **but hard-fails instead of skipping when
  `ONOATS_REQUIRE_DIARIZATION_CONTRACT=1`**; local-only, not a CI gate
- `scripts/release_check.sh` — new step that exports
  `ONOATS_REQUIRE_DIARIZATION_CONTRACT=1` and runs
  `uv run --extra diarization pytest tests/test_diarization_contract.py`
  (today the script is version-check only and runs no tests; this makes the
  release check working-tree dependent for this one step, so the extra must
  be installable on the release machine; the step fails unless `HEAD` is the
  commit under check and the tree is clean, so it cannot validate a different
  checkout than checks 1-4 inspected)
- `tests/test_release_meta.py` — assert `release_check.sh` references the
  contract test file and the env var; plus a **subprocess test** that runs the
  contract test with `mlx_audio` blocked and the env var set to `1`, asserting
  it exits non-zero (fails, not skips), and with the var unset asserts it
  skips — so the hard-fail mechanism cannot silently regress

**Phase 8b — real-pipeline validation gate (no new code; blocks Phase 9; runs after Phase 8 with `uv sync --extra diarization` against the committed lock)**
- With the pipeline wired (flag on, real model, local machine), measure and
  record in `## Findings`: worker processing ratio, event-loop stall,
  GPU contention with the STT server, added `them` commit latency under the
  `low` preset (p95, flag on − off), socket staging-drop WARNINGs, and slot
  stability / overflow over a 20-60 min multi-speaker recording. These
  numbers become the bounds Phase 9 asserts; the latency bound is recorded as
  `LATENCY_BUDGET_SEC` (default 2.0 s, i.e. `HOLD_SEC` + margin). Outcomes
  that change the contract trigger the re-mark rule (Risks).

**Phase 9 — integration + manual smoke**
- `pyproject.toml` — register the `diarization_integration` marker under
  `[tool.pytest.ini_options]` `markers`
- Fixture: a short (≤ 30 s) redistributable two-speaker clip committed
  under `tests/fixtures/` (size and licence stated in the PR); the
  `ONOATS_DIARIZATION_FIXTURE` env-path variant is a fallback only when no
  clip can be committed, in which case the test skips when it is unset
- `tests/test_diarization_integration.py` (new) — flag on, skip-unless
  `mlx_audio` importable, `@pytest.mark.diarization_integration`, local-only
  (`uv run --extra diarization pytest -m diarization_integration`): asserts
  ≥ 2 distinct labels across `them` utterances on a clearly-separated-speaker
  fixture (no exact-sequence assertion — Metal inference is not bit-
  deterministic), every label matches `^S[0-9]+$` with index ≤ N (N = model
  `num_speakers`), no `me` utterance carries `speakers`; flag off: no
  `mlx_audio` import
- Manual smoke (bounded): `them` commit latency with flag on stays within
  `LATENCY_BUDGET_SEC` of flag off (the Phase 8b-measured bound, stated
  numerically in the PR as a p95 over a multi-minute real call); no
  socket-transport staging-drop WARNINGs; no event-loop stall beyond the
  Phase 8b bound; `**Them (S1):**` lines appear and look plausible over a
  longer multi-speaker call

**Phase 10 — docs**
- `AGENTS.md` — new invariants section (them-only, flag default off,
  worker thread owns the model and the coverage writes, no cross-thread model
  use, daemon worker with bounded join, hoisted import-free preflight vs
  runtime-degrade split, feeder `start_sample` is the position authority,
  ordered span queue appended before `super()`, labeler pending-deque hold
  with stall guard and EndFrame flush-wait, canonical persistence)
- `README.md`, `CHANGELOG.md` (include the `mlx` bump note if Phase 8's
  `uv.lock` showed one)

## Implementation Checklist

Conduct-compatible index of Phases 1-8 (Phase 0 is done; see `## Findings`).
**Phases 8b, 9 and 10 are intentionally NOT listed:** 8b needs an interactive Mac
session with the real model, 9 consumes 8b's measured bounds, and 10 is docs.
The authoritative spec for every phase is its block under `## Files to Modify`;
the slots below only drive `/skein:conduct` (test files listed are the new/edited
test modules; edits to other existing test files named in the spec are in scope).
The `Review Gates: full` auto-chain is deferred until 8b-10 land (the operator
runs the gates by hand after the final phase).

### Phase 1: Config flag + preflight

**Goal:** Add the `diarization` flag and a hoisted, import-free, non-downloading `_preflight_diarization`, called right after `_acquire_instance_lock` in both entrypoints and only when the flag is on.
**Impl files:** src/onoats/config/__init__.py, src/onoats/runtime.py, src/onoats/cli.py, src/onoats/dual.py, src/onoats/__main__.py
**Test files:** tests/test_onoats_imports.py, tests/test_config.py, tests/test_stt_config_wiring.py, tests/test_runtime_preflight.py, tests/test_socket_supervisor.py, tests/test_dual_socket_source.py
**Test command:** `uv run ruff format --check src tests && uv run ruff check src tests && uv run pytest tests/test_onoats_imports.py tests/test_config.py tests/test_stt_config_wiring.py tests/test_runtime_preflight.py tests/test_socket_supervisor.py tests/test_dual_socket_source.py -q`

Spec: the "Phase 1" block under `## Files to Modify` and the Requirements it cites.

### Phase 2: Leaf module (`diarization_timeline.py`)

**Goal:** A leaf module (no onoats imports, no mlx_audio) owning the single correlation clock, the worker-written/loop-read DiarizationTimeline, label policy and constants.
**Impl files:** src/onoats/stt/diarization_timeline.py
**Test files:** tests/test_diarization_timeline.py
**Test command:** `uv run ruff format --check src tests && uv run ruff check src tests && uv run pytest tests/test_diarization_timeline.py -q`

Spec: the "Phase 2" block under `## Files to Modify` and the Requirements it cites.

### Phase 3: Feeder + worker

**Goal:** Pass-through feeder tags chunks with start_sample; one daemon worker thread owns model load, every feed(), final flush and all timeline writes; degrade never raises a fatal ErrorFrame.
**Impl files:** src/onoats/processors/diarization_feeder.py
**Test files:** tests/test_diarization_feeder.py
**Test command:** `uv run ruff format --check src tests && uv run ruff check src tests && uv run pytest tests/test_diarization_feeder.py -q`

Spec: the "Phase 3" block under `## Files to Modify` and the Requirements it cites.

### Phase 4: STT segment-sample stamping

**Goal:** Stamp each them-arm TranscriptionFrame with its VAD span via an ordered span queue appended before delegating VAD-stop to super() and popped at the head of run_stt (revision 5); me arm never stamped.
**Impl files:** src/onoats/stt/websocket_stt_service.py, src/onoats/runtime.py, src/onoats/dual.py
**Test files:** tests/test_websocket_stt_reconnect.py, tests/test_websocket_stt_stamping.py, tests/test_stt_config_wiring.py
**Test command:** `uv run ruff format --check src tests && uv run ruff check src tests && uv run pytest tests/test_websocket_stt_reconnect.py tests/test_websocket_stt_stamping.py tests/test_stt_config_wiring.py -q`

Spec: the "Phase 4" block under `## Files to Modify` and the Requirements it cites.

### Phase 5: Labeler

**Goal:** Downstream labeler holds stamped TranscriptionFrames in a pending deque until timeline coverage or HOLD_SEC, with EndFrame flush-wait, immediate CancelFrame release and a stall guard.
**Impl files:** src/onoats/processors/diarization_labeler.py
**Test files:** tests/test_diarization_labeler.py
**Test command:** `uv run ruff format --check src tests && uv run ruff check src tests && uv run pytest tests/test_diarization_labeler.py -q`

Spec: the "Phase 5" block under `## Files to Modify` and the Requirements it cites.

### Phase 6: Persistence + render

**Goal:** Persist canonical speakers/overlap on utterances and compose the display suffix at render time through one shared helper.
**Impl files:** src/onoats/processors/transcript_buffer.py, src/onoats/jsonl.py, src/onoats/render.py, src/onoats/processors/live_terminal.py
**Test files:** tests/test_speaker_labels.py
**Test command:** `uv run ruff format --check src tests && uv run ruff check src tests && uv run pytest tests/test_speaker_labels.py -q`

Spec: the "Phase 6" block under `## Files to Modify` and the Requirements it cites.

### Phase 7: Wiring

**Goal:** Insert feeder (immediately before system_stt) and labeler into the system arm only, gated on the flag, with a diarizer_factory kwarg for stub injection; mic arm unchanged.
**Impl files:** src/onoats/dual.py
**Test files:** tests/test_dual_socket_source.py, tests/test_onoats_imports.py
**Test command:** `uv run ruff format --check src tests && uv run ruff check src tests && uv run pytest tests/test_dual_socket_source.py tests/test_onoats_imports.py -q`

Spec: the "Phase 7" block under `## Files to Modify` and the Requirements it cites.

### Phase 8: Dependency + contract

**Goal:** Add the optional `diarization` extra, regenerate uv.lock, pin the mlx_audio contract in a skip-unless test that hard-fails under ONOATS_REQUIRE_DIARIZATION_CONTRACT=1, and wire release_check.sh.
**Impl files:** pyproject.toml, uv.lock, scripts/release_check.sh
**Test files:** tests/test_diarization_contract.py, tests/test_release_meta.py
**Test command:** `uv run ruff format --check src tests && uv run ruff check src tests && uv run pytest tests/test_diarization_contract.py tests/test_release_meta.py -q`

Spec: the "Phase 8" block under `## Files to Modify` and the Requirements it cites.

## Testing Notes

- [x] Phase 0 go/no-go recorded in `## Findings`: API/presets/input
      contract/`num_speakers`/`frames_processed`, non-downloading cache-probe
      call, `uv lock` outcome (advisory, reverted), scratch estimates, 0-300 s
      slot stability
- [ ] Phase 8b gate recorded in `## Findings`: worker-vs-loop latency, loop
      stall, GPU contention, added commit latency (p95) vs
      `LATENCY_BUDGET_SEC`, long-recording slot stability / overflow
- [x] Unit: label policy — dominant, overlap above/below threshold,
      zero coverage, partial coverage, GAP, ring eviction, ring-capacity
      invariant, `coverage_high_water` semantics, slot first-activity
      mapping, `num_speakers` overflow → `None` + exactly one WARNING (per
      instance), bounded deterministic thread-safety test
- [x] Unit: feeder observer contract + sample counting; queue-drop → worker
      GAP + later windows still aligned; worker thread owns all model calls
      (incl. the factory) in the order load → `set_streaming_config('low')` →
      `set_slot_count` → `feed`; PCM16→float32 conversion; feeder stops
      enqueueing after degrade
- [x] Unit: worker lifecycle — `EndFrame` final flush + thread exit,
      `CancelFrame` abort, blocking `feed()` does not hang shutdown
- [x] Unit: runtime failure (incl. loader raising post-preflight) → degrade +
      exactly one WARNING, never a fatal `ErrorFrame`; consecutive-overrun
      threshold; hung `feed()` → labeler stall guard degrades once
- [x] Unit: `WebSocketSTTService` stamping — VAD span (pre-roll excluded),
      ordered span queue appended before `super()` (aligned when several spans
      are outstanding, unusable service → no span, empty queue → no stamp),
      StartFrame reset / reconnect leaves it alone, kwarg False → nothing,
      hook-shape pipecat test (ordered segment delivery), feeder↔STT clock parity
- [x] Unit: labeler — hold/coverage/`HOLD_SEC` timeout, release with no
      further traffic, evicted and N-chunks-behind cases, VAD passthrough
      during hold, FIFO release order, unstamped passthrough, `EndFrame`
      flush-wait / `CancelFrame` immediate release, degraded → no hold, flush-
      continuation (Req 11)
- [x] Unit: persistence + render — schema (omit-when-None, exact key set, no
      display string persisted), writer→reader round-trip, rotation via
      `TranscriptBuffer.flush`, old-file back-compat, canonical `source` enum
      unchanged, render strings with default/configured labels, live-terminal
      suffix parity
- [x] Unit: config-time failure (`mlx_audio` missing / cold cache / offline /
      non-websocket STT / preflight) → fail loud, rc=1, transport builder and
      capturer spawn never called, on **both** entrypoints; lock-then-preflight
      call order; flag off never calls preflight; extra/weights probes memoized (backend
      check re-runs every call);
      single-input mode logs the "ignored" WARNING
- [x] Unit: `me` branch structurally and behaviourally unaffected (me/them ×
      flag on/off × portaudio/socket, every cell); wired-arm degrade keeps
      `them` transcription flowing
- [x] Import hygiene: `mlx_audio` in `_FORBIDDEN` (Phase 1); clean-interpreter
      subprocess imports `onoats.dual` and the new modules flag-off;
      `mlx_audio` absent from `sys.modules`
- [x] Contract (local-only; hard-fails under
      `ONOATS_REQUIRE_DIARIZATION_CONTRACT=1`, which `release_check.sh`
      exports): `mlx-audio` API surface pinned at 0.5.8, valid for
      `>=0.5.8,<0.6`; `test_release_meta.py` asserts `release_check.sh` references the contract
      test file and the env var, and
      a subprocess test proves hard-fail-not-skip
- [ ] Integration (local-only, `-m diarization_integration`): flag on,
      committed/env fixture → label assertions (Phase 9); flag off → no
      `mlx_audio` import
- [ ] Manual smoke: numeric bounded latency criterion and no loop-stall /
      staging-drop regressions (Phase 9); speaker-ID stability over a longer
      real multi-speaker call

## Acceptance Criteria

- With the flag off, behaviour and imports are identical to today (no
  `mlx_audio` import, no new attributes, no JSONL/render change).
- With the flag on and the extra installed, `them` utterances carry
  `speakers`/`overlap` where coverage exists and render as
  `**Them (S1):** …`; `me` utterances are never labelled.
- Flag on without the extra (or with a non-websocket STT backend) exits
  non-zero before any capture side effect.
- A runtime diarizer failure degrades to unlabelled `them` lines with one
  WARNING and never ends the session.
- Added `them` commit latency (p95, flag on − off, measured in Phase 8b over
  a multi-minute real call) is within `LATENCY_BUDGET_SEC`, and the unit
  suite proves the labeler never holds a frame longer than `HOLD_SEC`.
- Full test suite, `ruff format`, `ruff check` clean; contract and
  integration tests **pass, not skip** locally with the extra installed and
  the committed fixture present (`pytest -rs` shows 0 skipped in
  `tests/test_diarization_*.py`; the env-path fixture fallback skips by
  design when unset).

<!-- reviewed: 2026-10-09 @ 8be9afa9728cff58ce53ba671fb5b4e3565d2958 -->

<!-- /review-plan writes the marker line above. Everything below is the workspace: edits here do NOT invalidate the marker. -->

## Progress

- [x] Phase 0: Verify (no code lands) — 2026-10-08, see Findings
- [x] Phase 1: Config flag + preflight
- [x] Phase 2: Leaf module (`diarization_timeline.py`)
- [x] Phase 3: Feeder + worker
- [x] Phase 4: STT segment-sample stamping
- [x] Phase 5: Labeler
- [x] Phase 6: Persistence + render
- [x] Phase 7: Wiring
- [x] Phase 8: Dependency + contract
- [ ] Phase 8b: Real-pipeline validation gate
- [ ] Phase 9: Integration + smoke
- [ ] Phase 10: Docs

## Findings

Phase 0 outputs are recorded here (preset behaviour, `num_speakers`, input
contract, `frames_processed`, non-downloading cache-probe call, scratch
estimates, advisory `uv lock` outcome, 0-300 s slot stability). Phase 8b
records the real-pipeline numbers (latency/stall/contention, long-recording
slot stability).

### Phase 0 results (2026-10-08, scratch venv, `mlx-audio==0.5.8`, mlx 0.32.3, transformers 5.19.0, Apple Silicon)

All checked against the **installed** package, load+feed on a non-main daemon thread. **Go. No above-marker value changes; marker untouched.**

- **Presets** (`set_streaming_config`): `offline`=(340,40), `low`=(9,4), `very_low`=(6,2), `ultra_low`=(3,1) as (chunk_len, right_context). Shipped default config is (340,40) = offline. `low` accepted; unknown preset raises `ValueError`. With `low`, the first non-empty output arrived after **1.1 s** of audio fed (72 frames = `chunk_len*8`).
- **Contract**: `model.feed(audio, state, sample_rate=16000, *, final=False, threshold, min_duration, merge_gap)` returns `(DiarizationOutput, state)`. `sample_rate != 16000` raises `ValueError("feed requires 16000 Hz audio...")`; 2-D input raises `ValueError("feed expects one-dimensional mono audio")`. It cannot detect a mis-rated array (as planned). `model.sample_rate == 16000`, `hop_length == 160`, `output_subsampling_factor == 1`.
- **Position source**: `StreamingState.frames_processed` (native 10 ms frames). Over 300 chunks x 100 ms (30 s) the total emitted `speaker_probs` rows == `frames_processed` == 2952, and after `final=True` 3000 (== `samples_received / 160`). So output rows are 1:1 with 10 ms frames; row index of a call = `frames_processed` before the call.
- **Output types**: `output.speaker_probs` is an `mx.array` shape `(n, 8)` (not numpy; worker must `np.array()` it). **Trap:** `output.num_speakers` is the per-call count of distinct speakers in that call's *segments* (an `int`), NOT the slot cap. The cap is `model.config.num_speakers` (**8**), which is what `set_slot_count` must read.
- **Worker thread**: load, `set_streaming_config`, `init_streaming_state`, 300 `feed()` calls and the final flush all ran on one `diar-worker` thread with no MLX stream error.
- **Non-downloading cache probe**: `huggingface_hub.try_to_load_from_cache(repo_id, filename)` returns the snapshot path when cached and `None` when not (no network). `load()` fetches exactly two files: `config.json` and `model.safetensors`; probe both. **Do not** use `snapshot_download(local_files_only=True)`: on a fully cached repo it raised `IncompleteSnapshotError` (hub 1.33) and on a missing repo `LocalEntryNotFoundError`. `try_to_load_from_cache` and `huggingface_hub.errors.LocalEntryNotFoundError` both import under the repo-locked `huggingface-hub 1.18.0`. Probe `.cache/huggingface` honours `HF_HOME`/`HF_HUB_CACHE` automatically.
- **Advisory `uv lock`** (throwaway copy, extra `diarization = ["mlx-audio==0.5.8"]`): adds `mlx-audio 0.5.8`, `transformers 5.17.0`, `safetensors 0.8.0`, `miniaudio 1.71`, `sounddevice 0.5.6`. **`mlx` stays 0.31.2** (no bump). Not yet verified: that mlx-audio 0.5.8 runs against mlx 0.31.2 (Phase 0 ran on mlx 0.32.3); Phase 8's committed lock + Phase 8b must confirm. Repo `pyproject.toml`/`uv.lock` untouched (`git status` clean).
- **Scratch speed** (300 s clip, 100 ms chunks, bare thread, no asyncio loop): 8.7 s wall = RTF 0.029 (consistent with retained RTF 0.026-0.038). Not an acceptance number; Phase 8b measures with the real pipeline.
- **Slot behaviour on the retained 0-300 s clip** (3 real speakers, `low` preset, threshold 0.5): slots activate in first-speech order (slot 0 at 3.5 s = narration, slot 1 at 17.3 s, slot 2 at 26.2 s), only 3 of 8 ever active, so first-activity -> `S1..S3` mapping works. **But slot stability is imperfect:** `guest` frames split between slot 1 (5911) and slot 2 (7292), and slot 2 is also `varun`'s (16948). So ~55% of one speaker's active frames land in another speaker's slot (overlap or a real identity swap; not disambiguated here). A 0-300 s, 3-speaker clip is **not sufficient** to bound this; the 20-60 min multi-speaker run at Phase 8b is required, and the dominant-slot-wins label policy should be expected to mislabel such stretches. Surface in the final docs as a known accuracy limit, not a blocker.

### Conduct notes (workspace; not contract)

- **Phase 3 advisory review (opus)**: (1) fixed — GAP re-anchor must be applied per output row by model-input offset (breakpoints), not to the whole batch, because the `low` preset's ~1 s lookahead emits pre-gap rows after the gap; the plan's literal "anchor + frame_index*160" is only correct with piecewise anchors. Exact for chunks that are multiples of 160 samples (off-multiple chunks round up to the next row boundary: sub-frame skew, untested). (2) fixed — the overrun budget is evaluated per 1 s accumulation window (`BUDGET_WINDOW_SEC`), not per `feed()` call. (3) resolved (2026-10-08) — Req 1's API table now lists the feeder's loop-side `mark_degraded` calls (join timeout / start failure) alongside the labeler's stall guard and fail-open.
- **Phase 2**: `HOLD_SEC` set to 1.5 (`low` preset lag 1.04 s + margin) so the 2.0 s `LATENCY_BUDGET_SEC` default stays HOLD_SEC + margin.
- **Phase 4 advisory review (opus)**: fixed — a second `VADUserStartedSpeakingFrame` while a span is open now logs ERROR and keeps the first start (pipecat decodes from the first). **Open, for Phase 8b/9 to measure:** VAD start/stop events fire `start_secs`/`stop_secs` (~0.2 s default) after the speech edges, so the stamp (counter value at the event) misses ~0.2 s of leading speech and includes ~0.2 s of trailing audio; subtracting `frame.start_secs*16000` / `frame.stop_secs*16000` (clamped) would tighten it but changes the Req 3 "stamp == VAD-event position" contract, so it is not applied without a measured accuracy case.
- **Phase 5 advisory review (opus)**: all fixed with regression tests — the stall streak now resets on the in-line covered release too; it counts distinct `hold_sec` windows (not expired frames), so one hiccup with two parked finals cannot degrade; the release task fails open (one ERROR, degrade, deliver in-flight + pending unlabelled FIFO). The stall WARNING is the timeline's single `mark_degraded` line (the labeler adds none).
- **Phase 7 (found by the wired-pipeline matrix tests, not by Phase 5's unit tests)**: `DiarizationLabeler` stored its injectable clock as `self._clock`, which pipecat's `FrameProcessor.setup()` overwrites with a `SystemClock`, so every stamped `them` transcript raised `'SystemClock' object is not callable` in a real pipeline. Renamed to `self._now`; swept the other new processors for FrameProcessor-attribute collisions (none). Lesson for Phase 8b/9: unit tests that bypass `setup()` cannot catch framework-attribute shadowing.
- **Phase 8**: `uv.lock` adds exactly `mlx-audio 0.5.8`, `transformers 5.17.0`, `safetensors 0.8.0`, `miniaudio 1.71`, `sounddevice 0.5.6` (+ `provides-extras`); **`mlx`/`mlx-metal` unchanged at 0.31.2, so no mlx-whisper re-smoke and no CHANGELOG mlx note is triggered.** The local uv (0.12.23) also tried to rewrite the lock `revision` 3->5 and drop `sys_platform` markers on 10 nvidia edges; that churn was reverted by hand (diff is additions only; `uv lock --check` passes). The contract test ran green against the real installed package with `ONOATS_REQUIRE_DIARIZATION_CONTRACT=1` (also confirms mlx-audio 0.5.8 runs on mlx 0.31.2 for load/feed with cached weights). The contract test purges `mlx_audio*` from `sys.modules` on module teardown, because other tests assert it is absent. Full suite with the extra installed: 6737 passed.

- **Post-/code-review fixes (2026-10-08)**: (1) labeler stall guard now ignores expiries while the model loads (`DiarizationTimeline.is_ready`, `MODEL_LOAD_GRACE_SEC`=60): a cold load otherwise tripped `mark_degraded("stalled")` for the whole session. This amended the Req 4 stall-guard text above the marker (done in the later administrative marker refresh recorded below). (2) Labeler pass-through (unstamped/degraded) now takes `_release_lock`, so it cannot overtake the head frame the release task is mid-push. (3) Feeder: `EndFrame`/`CancelFrame` post the finish/abort sentinel and are forwarded **immediately**; the bounded join moved to `cleanup()` (which aborts first if no terminal frame arrived). Matches the plan's "sentinel before forwarding" + "cleanup() backstop"; the labeler's `HOLD_SEC` wait on `is_flushed` is unchanged. (4) `OnoatsConfig.diarization` warns once per process per bad value. (5) Preflight install hint covers `uv tool install`; the preflight and the loader share one repo id (initially pinned by a parity test between two literals; since round 2 both read `diarization_model.REPO_ID`, a single owner, and the alias is gone).
- **Gauntlet round 5 (2026-10-08)**: `label_for` margin guard now keys on "top slot is not a candidate" (was "no candidates"), per owner decision; the above-marker label policy gained one sentence (administrative marker refresh). Codex's adversarial gate was unavailable (usage limit) in the final confirming pass; owner chose to close on the other gates.
- **Accepted, recorded in AGENTS.md Review Checklist**: continuation-flush spill of labeler-held frames; contract-test weights skip under `REQUIRE`.
- **Still open (Phase 8b/9)**: VAD-span edge offset (see Phase 4 note). Facts for the decision: the STT decodes pipecat's `_audio_buffer` (<= 1 s pre-roll + everything to the VAD-stop event), the server returns no timestamps onoats consumes, and `VADUserStartedSpeakingFrame.start_secs` / `VADUserStoppedSpeakingFrame.stop_secs` carry the confirmation delays.

- **Marker refreshed administratively (2026-10-08, owner-approved during the first review-gauntlet round)**: the above-marker text was amended to match shipped, advisory-reviewed behaviour — `Status` now In Progress; the worker's anchor mapping is stated as piecewise (per-row breakpoints, Phase 3 advisory review item 1); the Req 4 stall guard carries the model-load grace and the coverage-advance streak reset. No requirement was added or relaxed; the hash was recomputed per the AGENTS.md recipe rather than via `/review-plan`.
- **Loader (gauntlet round 1, owner-approved)**: the production loader passes the *cached snapshot directory* (resolved via `try_to_load_from_cache`, no network) to `mlx_audio.vad.load(..., strict=True)` instead of the bare repo id, which `mlx_audio` first tests as a CWD-relative path and otherwise `snapshot_download`s on every start. A missing cache fails loud with the `hf download` hint. Revision pinning was deliberately not added. pipecat's lower bound is now `>=1.3.0` (the inline-`run_stt` stamping hook is verified only there).

- **release_check.sh check 5 guard (gauntlet round 1)**: beyond "working-tree dependent" (Files to Modify, above the marker), the shipped check now *fails* unless `HEAD` equals the resolved commit and the tree is clean, so it can never validate a different checkout than checks 1-4 inspected; checks 1-4 are unchanged.

- **code-review xhigh (2026-10-09)**: (1) fixed — the plan's pipecat premise ("`run_stt` runs inline on VAD-stop") holds for 1.3.0-1.8.x only; 1.9.0 introduced `SegmentedSTTService._segment_queue`/`_segment_task_handler` (verified from the PyPI wheels of 1.4/1.6/1.8 vs 1.9/1.10/1.12) and the declared range `>=1.3.0,<2.0.0` admits them, where the single-slot `_pending_span` can stamp a segment with another segment's span (a wrong label, not a missing one). `tests/test_websocket_stt_stamping.py` pins the shape only on the locked version, so the startup preflight now also calls `diarization_timeline.segmented_stt_runs_inline()` and exits non-zero on a non-inline pipecat. Tightening the `pyproject.toml` cap to `<1.9` was deliberately not done (it would constrain non-diarization users). (2) fixed — `DiarizationLabeler._stop_task` swallowed a cancellation of its own caller; now `gather(..., return_exceptions=True)`. (3) fixed — the worker refuses a model with `output_subsampling_factor != 1` (the weights are not revision-pinned and the contract test skips on a cold cache). **Superseded by revision 5 (owner-directed, 2026-10-09)**: item (1)'s refusal was replaced by support - the STT's single slot became an ordered span queue (Req 3), so pipecat 1.9+ works; the preflight probe is now `segmented_stt_orders_spans()` (accepts inline `run_stt` or one in-order queue task, fails closed otherwise). Verified by running the stamping, timeline, feeder, labeler, preflight, reconnect and dual-source tests under pipecat 1.9.0, 1.11.0 and 1.12.0 (1.3.0 is the locked version). The `_detach_client` span clear (and its Review Checklist entry) was dropped: each queued span is popped once by its own `run_stt`, so a reconnect leaves nothing stale.

- **review-gauntlet round (2026-10-09)**: (1) fixed — a model row whose input straddles an *unaligned* queue drop (pre-gap tail + post-gap head) was recorded at the pre-gap anchor, overlapping the GAP so dropped audio counted toward `MIN_COVERAGE_FRACTION`; the worker now skips that row and starts the GAP where it would have begun (no probability row ever overlaps a GAP; still no coverage hole). (2) fixed — the `output_subsampling_factor` guard read a missing field as 1 and `int()`-truncated non-integral values; it now fails closed unless the value is exactly the `int` 1 (mlx-audio 0.5.x's `ModelConfig` always carries it, default 1; the cached config has 1). (3) fixed — Phase 4 / Files-to-Modify text and the data-flow diagram still described the pre-revision-5 single span slot; reworded to the ordered span queue (marker refreshed). (4) fixed — CHANGELOG now says the 1.9.0/1.11.0/1.12.0 runs were by hand in isolated environments; CI runs the locked 1.3.0 with queue-shape fixtures.
- **Shutdown close ordering (2026-10-09, e1e34df)**: fixed — on pipecat 1.9+ `SegmentedSTTService.stop()` drains its queued segments through `run_stt`, but `WebSocketSTTService.stop()` closed the websocket first, so a segment still decoding at shutdown lost its transcript and the shutdown drain timed out. `stop()` now awaits `super().stop()` and closes the session in a `finally` (graceful even when the parent raises); a cancelled drain takes the hard-close path and re-raises; `cancel()` still hard-closes first. No change on 1.3.0-1.8.x. Pinned by `tests/test_websocket_stt_reconnect.py::test_stop_drains_the_parent_before_closing_the_websocket`, `::test_stop_still_closes_gracefully_when_the_parent_stop_raises`, `::test_stop_hard_closes_and_propagates_when_the_parent_drain_is_cancelled`; recorded in AGENTS.md and the CHANGELOG `Fixed` entry.
- **review-gauntlet round (2026-10-09, row-hop + span-order probe)**: (1) fixed — `diarization_model.one_row_per_frame()` checked only `output_subsampling_factor`, but a row spans `hop_length * output_subsampling_factor` input samples and the weights are not revision-pinned, so a `hop_length=320` revision passed the worker guard and every label landed at half its real time; the predicate now also requires `config.processor_config.hop_length` (the public `ModelConfig` field; the model's `_processor_config` is the same object, pinned by the contract test) to be exactly the `int` 160, and the worker degrades with one WARNING otherwise. (2) fixed — `segmented_stt_orders_spans()` read only the put/get/`run_stt` method bodies, so a LIFO/priority `_segment_queue` or several handler tasks would have passed; the queue branch now also parses every `SegmentedSTTService` method and requires one `asyncio.Queue()` assignment in `__init__`, one top-level `self._segment_task = self.create_task(self._segment_task_handler())` in `start`, and the handler as the queue's only reader (fails closed otherwise). Re-verified True on pipecat 1.3.0 (locked), 1.9.0, 1.11.0 and 1.12.0, with the timeline, stamping, preflight, feeder, model, reconnect and dual-source tests passing under 1.9.0 and 1.12.0.
- **review-gauntlet round (2026-10-09, reader loop + row rate)**: (1) fixed — `segmented_stt_orders_spans()` required one `get` and one awaited `run_stt` but no loop and no one-put-per-stop, so a reader that transcribes one segment and returns (its own accepted fixture had that shape), a `run_stt` outside the loop, a second or skippable `run_stt`, or a conditional/duplicate `put` passed and could strand a span or pair it with another segment. The queue branch now requires the pipecat 1.9-1.12 reader verbatim in structure (one top-level sentinel-ended `while`, no `return`/`break`/`raise` in it, one `get` assignment, the handler's single `run_stt` awaited via `process_generator` under only `if <item>:`/`try`), one unconditional top-level segment `put` in the stop handler (only the `not self.is_usable` guard may exit early - the STT mirrors it), and only `None` sentinels elsewhere; the inline branch requires exactly one `run_stt` call. The `if <item>:` skip of an empty segment is unreachable for `WebSocketSTTService` (pipecat's default `wants_wav_segments` = True: every segment is a non-empty WAV). `SegmentedSTTService`'s queue methods are byte-identical across 1.9.0-1.12.0; the probe is True on 1.3.0 (locked), 1.9.0, 1.10.0, 1.11.0 and 1.12.0, with the timeline, stamping and preflight tests passing under 1.9.0 and 1.12.0. (2) fixed (contract test) — the row-rate contract compared emitted rows only with the model's own `frames_processed`; it now feeds N whole 160-sample frames the worker's way (10 ms and 100 ms chunks, then `final=True`) and requires exactly N emitted rows (verified on the cached weights, mlx-audio 0.5.8: 400 frames -> 400 rows, 360 before the flush), with a weights-free stub test pinning that a doubled-rate model fails. Runtime half quarantined: an exact rows-vs-samples equality only exists after the final flush, after every label is already released, and a mid-stream bound would rely on undocumented lookahead/padding behaviour that could reject a valid revision; the worker keeps the `one_row_per_frame()` config guard and the release-time contract test is the guard for rate drift.

### Review Waivers

(none)
