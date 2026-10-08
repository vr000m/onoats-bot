"""Measure RSS before/after loading diar+ASR models, to size the memory
footprint that would be added to onoats' process (or a sidecar) alongside
the already-running production STT server.

Usage (from repo root):
    uv run --with-editable experiments/diarization_scratch/.vendor/mlx-audio \
        python experiments/diarization_scratch/memory_footprint.py
"""

import json
import resource
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIAR_MODEL = "mlx-community/Nemotron-3-Diarization"
ASR_MODEL = "mlx-community/nemotron-3.5-asr-streaming-0.6b"


def rss_mb():
    # macOS: ru_maxrss is in bytes (Linux: KB) -- this script targets macOS (darwin).
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)


def main():
    baseline = rss_mb()
    print(f"baseline RSS: {baseline:.1f} MB", file=sys.stderr)

    from mlx_audio.stt.models.nemotron_asr.session import NemotronStreamingSession
    from mlx_audio.stt.utils import load as load_asr
    from mlx_audio.vad import load as load_diarization

    diar = load_diarization(DIAR_MODEL, strict=True)
    after_diar = rss_mb()
    print(
        f"after diarizer load: {after_diar:.1f} MB (+{after_diar - baseline:.1f})",
        file=sys.stderr,
    )

    asr = load_asr(ASR_MODEL)
    after_asr = rss_mb()
    print(
        f"after ASR load: {after_asr:.1f} MB (+{after_asr - after_diar:.1f})",
        file=sys.stderr,
    )

    diar.set_streaming_config("low")
    diar_state = diar.init_streaming_state()
    session = NemotronStreamingSession(asr)

    import numpy as np

    chunk = np.zeros(320, dtype=np.float32)  # 20ms @ 16kHz
    for _ in range(50):
        _, diar_state = diar.feed(chunk, diar_state)
        session.feed(chunk)
        session.step(max_decode_tokens=8)
    after_run = rss_mb()
    print(
        f"after 1s of streaming: {after_run:.1f} MB (+{after_run - after_asr:.1f})",
        file=sys.stderr,
    )

    results = {
        "baseline_mb": baseline,
        "after_diar_load_mb": after_diar,
        "after_asr_load_mb": after_asr,
        "after_streaming_mb": after_run,
        "diar_load_delta_mb": after_diar - baseline,
        "asr_load_delta_mb": after_asr - after_diar,
        "streaming_delta_mb": after_run - after_asr,
        "total_added_mb": after_run - baseline,
    }
    out = HERE / "out/memory_footprint.json"
    out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    print(f"\n-> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
