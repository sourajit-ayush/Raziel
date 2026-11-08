"""
kokoro_worker.py - Kokoro text-to-speech in its own process, for speaker.py (TTS_ENGINE = "kokoro").

Kokoro (kokoro-onnx) needs numpy 2 and onnxruntime 1.20+, while Raziel's own venv is pinned below both
(openwakeword, ctranslate2, Piper, mediapipe). So it runs from venv-kokoro, started by speaker.py:

    venv-kokoro\\Scripts\\python.exe kokoro_worker.py <model.onnx> <voices.bin>

and talks over its stdin / stdout:
    in   one JSON line per sentence: {"text": "...", "voice": "bf_emma", "lang": "en-gb", "speed": 1.0}
    out  one JSON line {"ok": true, "rate": 24000, "bytes": N} followed by N bytes of 16-bit mono PCM,
         or {"ok": false, "error": "..."} with nothing after it.
It prints {"ok": true, "ready": true} once the model is loaded. Nothing else ever goes to stdout.
"""

import json
import sys


def main() -> int:
    model, voices = sys.argv[1], sys.argv[2]
    out = sys.stdout.buffer
    try:
        import numpy as np
        from kokoro_onnx import Kokoro
        engine = Kokoro(model, voices)
    except Exception as e:  # noqa: BLE001
        out.write((json.dumps({"ok": False, "error": f"load: {e}"}) + "\n").encode())
        out.flush()
        return 1
    out.write(b'{"ok": true, "ready": true}\n')
    out.flush()
    for line in sys.stdin.buffer:
        try:
            req = json.loads(line)
            samples, rate = engine.create(str(req["text"]), voice=str(req.get("voice", "bf_emma")),
                                          speed=float(req.get("speed", 1.0)), lang=str(req.get("lang", "en-gb")))
            pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2").tobytes()
            out.write((json.dumps({"ok": True, "rate": int(rate), "bytes": len(pcm)}) + "\n").encode())
            out.write(pcm)
        except Exception as e:  # noqa: BLE001 - one bad sentence never ends the worker
            out.write((json.dumps({"ok": False, "error": str(e)[:300]}) + "\n").encode())
        out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
