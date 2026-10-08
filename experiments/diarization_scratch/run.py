"""Scratch: run Nemotron diarization + ASR on a clip and print speaker turns.

Wraps mlx-audio's examples/nemotron_diarization_asr.py (vendored, gitignored).
Usage (from repo root):
    uv run --with-editable experiments/diarization_scratch/.vendor/mlx-audio \
        python experiments/diarization_scratch/run.py [--mode masked|timestamps]
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE / ".vendor/mlx-audio/examples/nemotron_diarization_asr.py"
AUDIO = HERE / "audio/clip_16k.wav"
OUT = HERE / "out"


def load_example():
    spec = importlib.util.spec_from_file_location("nd_example", EXAMPLE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("masked", "timestamps"), default="masked")
    parser.add_argument("--diar-preset", default="low")
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args()

    nd = load_example()
    from mlx_audio.stt import load as load_asr
    from mlx_audio.stt.utils import load_audio
    from mlx_audio.vad import load as load_diarization

    audio = load_audio(str(AUDIO), sr=nd.SAMPLE_RATE)
    diar = load_diarization(nd.DIAR_MODEL, strict=True)
    diar.set_streaming_config(args.diar_preset)
    asr = load_asr(nd.ASR_MODEL)

    result = nd.transcribe_with_speakers(
        audio, diar, asr, threshold=args.threshold, mode=args.mode
    )

    OUT.mkdir(exist_ok=True)
    out_path = OUT / f"{args.mode}-{args.diar_preset}.json"
    out_path.write_text(json.dumps(result, indent=2, default=str))

    for seg in result["segments"]:
        tag = seg.get("speaker") or "?"
        if seg.get("overlap"):
            tag = f"overlap{seg.get('candidates')}"
        print(f"{seg['start']:8.2f} {seg['end']:8.2f} {tag:>12}  {seg['text'][:100]}")
    print(f"\n{len(result['segments'])} segments -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
