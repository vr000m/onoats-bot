"""Simulate live mic-callback pacing against diar + ASR, single-threaded.

MLX's Metal streams are not safely shared across arbitrary OS threads for
this model's streaming state (confirmed: "There is no Stream(gpu, N) in
current thread" from two different fixes). That also matches how onoats'
own pipeline would run this: one asyncio event loop, not separate OS
threads for CPU-bound work. So this feeds one chunk at a time, in order,
to diar.feed() then asr.feed()+step(), and measures whether that combined
per-chunk work fits inside the chunk's real-time arrival budget.

Usage (from repo root):
    uv run --with-editable experiments/diarization_scratch/.vendor/mlx-audio \
        python experiments/diarization_scratch/realtime_sim.py
"""

import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
AUDIO = HERE / "audio/clip_16k.wav"
SAMPLE_RATE = 16000
SLICE_SECONDS = 60
CHUNK_MS = 20  # matches the ~10-20 ms capture callback granularity noted in AGENTS.md
DIAR_MODEL = "mlx-community/Nemotron-3-Diarization"
ASR_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"


def load_slice():
    from mlx_audio.stt.utils import load_audio

    audio = load_audio(str(AUDIO), sr=SAMPLE_RATE)
    return np.array(audio[: SLICE_SECONDS * SAMPLE_RATE])


def main():
    print("loading audio slice...", file=sys.stderr)
    audio = load_slice()

    from mlx_audio.stt.models.nemotron_asr.session import NemotronStreamingSession
    from mlx_audio.stt.utils import load as load_asr
    from mlx_audio.vad import load as load_diarization

    print("loading models...", file=sys.stderr)
    diar = load_diarization(DIAR_MODEL, strict=True)
    diar.set_streaming_config("low")
    diar_state = diar.init_streaming_state()
    asr = load_asr(ASR_MODEL)
    session = NemotronStreamingSession(asr)

    n = int(SAMPLE_RATE * CHUNK_MS / 1000)
    print("warming up (Metal kernel compile)...", file=sys.stderr)
    warm = np.zeros(n, dtype=np.float32)
    t_warm = time.perf_counter()
    _, diar_state = diar.feed(warm, diar_state)
    session.feed(warm)
    session.step(max_decode_tokens=8)
    print(f"warmup took {time.perf_counter() - t_warm:.3f}s", file=sys.stderr)
    diar_state = diar.init_streaming_state()
    session = NemotronStreamingSession(asr)

    budget = CHUNK_MS / 1000
    chunk_times, lags = [], []
    backlog = 0.0  # cumulative time this loop is behind the live clock
    t0 = time.perf_counter()
    next_due = t0
    print("streaming...", file=sys.stderr)
    for i in range(0, len(audio), n):
        chunk = audio[i : i + n]
        now = time.perf_counter()
        if next_due > now:
            time.sleep(next_due - now)
        t_start = time.perf_counter()
        _, diar_state = diar.feed(chunk, diar_state)
        session.feed(chunk)
        session.step(max_decode_tokens=8)
        t_end = time.perf_counter()
        processing = t_end - t_start
        chunk_times.append(processing)
        lags.append(max(0.0, processing - budget))
        backlog = max(0.0, backlog + processing - budget)
        next_due += budget

    t_final0 = time.perf_counter()
    diar.feed(np.zeros(0, dtype=np.float32), diar_state, final=True)
    session.close()
    drain_steps = 0
    while not session.done:
        session.step(max_decode_tokens=8)
        drain_steps += 1
        if drain_steps > 10000:
            raise RuntimeError("ASR session failed to drain")
    final_flush_s = time.perf_counter() - t_final0

    results = {
        "n_chunks": len(chunk_times),
        "chunk_budget_s": budget,
        "mean_processing_s": float(np.mean(chunk_times)),
        "p95_processing_s": float(np.percentile(chunk_times, 95)),
        "max_processing_s": float(np.max(chunk_times)),
        "over_budget_chunks": int(sum(t > budget for t in chunk_times)),
        "final_backlog_s": backlog,
        "final_flush_s": final_flush_s,
        "drain_steps": drain_steps,
        "wall_s": time.perf_counter() - t0,
        "audio_s": len(audio) / SAMPLE_RATE,
    }
    out = HERE / "out/realtime_sim.json"
    out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    print(f"\n-> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
