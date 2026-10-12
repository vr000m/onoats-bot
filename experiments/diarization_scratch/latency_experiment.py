"""Measure realtime feasibility: per-chunk latency and RTF for diar + ASR,
run separately and concurrently, on a slice of the podcast clip.

Usage (from repo root):
    uv run --with-editable experiments/diarization_scratch/.vendor/mlx-audio \
        python experiments/diarization_scratch/latency_experiment.py
"""

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
AUDIO = HERE / "audio/clip_16k.wav"
SAMPLE_RATE = 16000
SLICE_SECONDS = 300  # first 5 minutes of the clip, already ground-truthed
CHUNK_MS = 100  # simulated live arrival granularity
DIAR_MODEL = "mlx-community/Nemotron-3-Diarization"
ASR_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"


def load_slice():
    from mlx_audio.stt.utils import load_audio

    audio = load_audio(str(AUDIO), sr=SAMPLE_RATE)
    return audio[: SLICE_SECONDS * SAMPLE_RATE]


def chunks(audio, chunk_ms):
    n = int(SAMPLE_RATE * chunk_ms / 1000)
    for i in range(0, len(audio), n):
        yield audio[i : i + n]


def run_diarizer(audio, results, key):
    from mlx_audio.vad import load as load_diarization

    model = load_diarization(DIAR_MODEL, strict=True)
    model.set_streaming_config("low")
    state = model.init_streaming_state()
    chunk_times = []
    t_total0 = time.perf_counter()
    for chunk in chunks(audio, CHUNK_MS):
        t0 = time.perf_counter()
        _, state = model.feed(chunk, state)
        chunk_times.append(time.perf_counter() - t0)
    t_final0 = time.perf_counter()
    _, state = model.feed(np.zeros(0, dtype=np.float32), state, final=True)
    final_latency = time.perf_counter() - t_final0
    total_wall = time.perf_counter() - t_total0
    results[key] = {
        "n_chunks": len(chunk_times),
        "mean_chunk_s": float(np.mean(chunk_times)),
        "p95_chunk_s": float(np.percentile(chunk_times, 95)),
        "max_chunk_s": float(np.max(chunk_times)),
        "final_flush_s": final_latency,
        "total_wall_s": total_wall,
        "audio_s": len(audio) / SAMPLE_RATE,
        "rtf": total_wall / (len(audio) / SAMPLE_RATE),
    }


def run_asr(audio, results, key):
    from mlx_audio.stt.models.nemotron_asr.session import NemotronStreamingSession
    from mlx_audio.stt.utils import load as load_asr

    model = load_asr(ASR_MODEL)
    session = NemotronStreamingSession(model)
    chunk_times = []
    t_total0 = time.perf_counter()
    for chunk in chunks(audio, CHUNK_MS):
        t0 = time.perf_counter()
        session.feed(chunk)
        session.step(max_decode_tokens=8)
        chunk_times.append(time.perf_counter() - t0)
    t_final0 = time.perf_counter()
    session.close()
    drain_steps = 0
    while not session.done:
        session.step(max_decode_tokens=8)
        drain_steps += 1
        if drain_steps > 10000:
            raise RuntimeError("ASR session failed to drain")
    final_latency = time.perf_counter() - t_final0
    total_wall = time.perf_counter() - t_total0
    results[key] = {
        "n_chunks": len(chunk_times),
        "mean_chunk_s": float(np.mean(chunk_times)),
        "p95_chunk_s": float(np.percentile(chunk_times, 95)),
        "max_chunk_s": float(np.max(chunk_times)),
        "final_flush_s": final_latency,
        "drain_steps": drain_steps,
        "total_wall_s": total_wall,
        "audio_s": len(audio) / SAMPLE_RATE,
        "rtf": total_wall / (len(audio) / SAMPLE_RATE),
    }


def main():
    print("loading audio slice...", file=sys.stderr)
    audio = load_slice()
    results = {}

    print("diarizer alone...", file=sys.stderr)
    run_diarizer(audio, results, "diar_alone")

    print("asr alone...", file=sys.stderr)
    run_asr(audio, results, "asr_alone")

    print("concurrent...", file=sys.stderr)
    t0 = time.perf_counter()
    td = threading.Thread(target=run_diarizer, args=(audio, results, "diar_concurrent"))
    ta = threading.Thread(target=run_asr, args=(audio, results, "asr_concurrent"))
    td.start()
    ta.start()
    td.join()
    ta.join()
    results["concurrent_wall_s"] = time.perf_counter() - t0

    out = HERE / "out/latency_experiment.json"
    out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    print(f"\n-> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
