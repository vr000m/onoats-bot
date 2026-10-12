"""Drive the live production nemotron.sock STT server with real commits,
as background GPU load, to see whether it contends with diar+ASR running
in-process at the same time. Run from pipecat-local-stt-server's own venv
(it owns the `stt_server` package).

Usage:
    <pipecat-local-stt-server>/.venv/bin/python \
        experiments/diarization_scratch/stt_load_client.py --duration 70
"""

import argparse
import asyncio
import json
import time
import wave
from pathlib import Path

from stt_server import TranscriptionClient

AUDIO = Path(__file__).resolve().parent / "audio/clip_16k.wav"
SOCKET = "~/Library/Caches/pipecat-stt/nemotron.sock"
SEGMENT_SECONDS = 5


def read_pcm_segment(start_sample, n_samples):
    with wave.open(str(AUDIO), "rb") as f:
        f.setpos(start_sample)
        return f.readframes(n_samples)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=70.0)
    args = parser.parse_args()

    sample_rate = 16000
    seg_samples = SEGMENT_SECONDS * sample_rate
    with wave.open(str(AUDIO), "rb") as f:
        total_samples = f.getnframes()
    start = 300 * sample_rate  # past the window already used for review
    pos = start
    t0 = time.perf_counter()
    commits = []
    while time.perf_counter() - t0 < args.duration:
        if pos + seg_samples > total_samples:
            pos = start
        pcm = read_pcm_segment(pos, seg_samples)
        pos += seg_samples
        async with TranscriptionClient(socket_path=SOCKET) as c:
            await c.update_session(turn_detection=None)
            t_send = time.perf_counter()
            await c.send_audio(pcm)
            await c.commit()
            async for ev in c.events():
                if (
                    ev["type"]
                    == "conversation.item.input_audio_transcription.completed"
                ):
                    commits.append(time.perf_counter() - t_send)
                    await c.close_session()
                    break
                if ev["type"] == "error":
                    commits.append(None)
                    break

    out = Path(__file__).resolve().parent / "out/stt_load_client.json"
    ok = [c for c in commits if c is not None]
    out.write_text(
        json.dumps(
            {"n_commits": len(commits), "n_ok": len(ok), "commit_latencies_s": ok},
            indent=2,
        )
    )
    print(f"{len(ok)}/{len(commits)} commits ok -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
