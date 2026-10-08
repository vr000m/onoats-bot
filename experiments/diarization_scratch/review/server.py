"""Local review app for diarization segments. Stdlib only.

Usage (from repo root):
    python experiments/diarization_scratch/review/server.py [--run out/masked-low.json]
Then open http://127.0.0.1:8765
"""

import argparse
import io
import json
import wave
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
SCRATCH = HERE.parent
AUDIO = SCRATCH / "audio/clip_16k.wav"
NAMES = {"speaker_0": "narration", "speaker_1": "guest", "speaker_2": "varun"}
CHOICES = ["guest", "varun", "narration", "other"]
PAD = 0.5


def load_segments(run_path):
    data = json.loads(run_path.read_text())
    segs = []
    for i, s in enumerate(data["segments"]):
        raw = s.get("speaker")
        segs.append(
            {
                "id": i,
                "start": round(s["start"], 2),
                "end": round(s["end"], 2),
                "text": s["text"],
                "model_speaker": NAMES.get(raw, "unknown")
                if raw
                else "overlap/unknown",
                "overlap": bool(s.get("overlap")),
            }
        )
    return segs


def cut_clip(start, end):
    with wave.open(str(AUDIO), "rb") as src:
        rate = src.getframerate()
        total = src.getnframes()
        a = max(0, int((start - PAD) * rate))
        b = min(total, int((end + PAD) * rate))
        src.setpos(a)
        frames = src.readframes(b - a)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as dst:
            dst.setnchannels(src.getnchannels())
            dst.setsampwidth(src.getsampwidth())
            dst.setframerate(rate)
            dst.writeframes(frames)
    return buf.getvalue()


def make_handler(segments, labels_path, index_html, port):
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def label_state():
        state = {}
        if labels_path.exists():
            for line in labels_path.read_text().splitlines():
                rec = json.loads(line)
                state[rec["id"]] = rec
        return state

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                return self._send(200, index_html.encode(), "text/html; charset=utf-8")
            if url.path == "/api/segments":
                state = label_state()
                payload = [{**s, "review": state.get(s["id"])} for s in segments]
                body = json.dumps({"segments": payload, "choices": CHOICES}).encode()
                return self._send(200, body, "application/json")
            if url.path == "/api/clip":
                q = parse_qs(url.query)
                sid = int(q["id"][0])
                s = segments[sid]
                return self._send(200, cut_clip(s["start"], s["end"]), "audio/wav")
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            if urlparse(self.path).path != "/api/verdict":
                return self._send(404, b"not found", "text/plain")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in allowed_origins:
                return self._send(403, b"forbidden origin", "text/plain")
            if (
                self.headers.get("Content-Type", "").split(";")[0].strip()
                != "application/json"
            ):
                return self._send(415, b"expected application/json", "text/plain")
            try:
                length = min(int(self.headers.get("Content-Length", 0)), 4096)
                body = json.loads(self.rfile.read(length))
                sid = body["id"]
                verdict = body["verdict"]
                correct = body.get("correct")
            except (ValueError, KeyError, TypeError):
                return self._send(400, b"bad request", "text/plain")
            if not isinstance(sid, int) or not 0 <= sid < len(segments):
                return self._send(400, b"bad id", "text/plain")
            if verdict not in {"check", "question", "cross"}:
                return self._send(400, b"bad verdict", "text/plain")
            if correct is not None and (verdict != "cross" or correct not in CHOICES):
                return self._send(400, b"bad correct", "text/plain")
            s = segments[sid]
            rec = {
                "id": sid,
                "start": s["start"],
                "end": s["end"],
                "model_speaker": s["model_speaker"],
                "text": s["text"],
                "verdict": verdict,
                "correct": correct,
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
            with labels_path.open("a") as f:
                f.write(json.dumps(rec) + "\n")
            self._send(200, b"{}", "application/json")

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, default=SCRATCH / "out/masked-low.json")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    segments = load_segments(args.run)
    labels_path = SCRATCH / "out/review_labels.jsonl"
    index_html = (HERE / "index.html").read_text()
    handler = make_handler(segments, labels_path, index_html, args.port)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"{len(segments)} segments; labels -> {labels_path}")
    print(f"http://127.0.0.1:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
