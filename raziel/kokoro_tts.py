"""
kokoro_tts.py - Kokoro, a more natural local voice than Piper, for speaker.py (TTS_ENGINE = "kokoro").

Kokoro runs in kokoro_worker.py under venv-kokoro (it needs numpy 2 / onnxruntime 1.20+, which Raziel's own
venv can't have); this module starts that worker once, in the background, and sends it one sentence at a
time. Anything wrong - venv-kokoro or the model missing, the worker crashing, a sentence taking too long -
and synth() returns None: speaker.py then speaks that sentence with Piper, as before. After a few failures
in a row Kokoro is left alone for the rest of the run.

Settings: TTS_ENGINE ("piper" | "kokoro"), KOKORO_VOICE (bf_emma), KOKORO_SPEED, KOKORO_HINDI (False: Hindi
stays with Piper's Hindi voice), KOKORO_HINDI_VOICE, KOKORO_MODEL / KOKORO_VOICES (models/kokoro/...).
"""

import json
import logging
import os
import subprocess
import threading
import time
from typing import Optional, Tuple

import config

logger = logging.getLogger("voice_assistant")

READY_TIMEOUT = 90.0        # loading the model on a busy PC
SENTENCE_TIMEOUT = 30.0
MAX_FAILURES = 3


def _cfg(name: str, default):
    return getattr(config, name, default)


def _here() -> str:
    return getattr(config, "_SCRIPT_DIR", os.path.dirname(os.path.abspath(__file__)))


def paths() -> Tuple[str, str, str, str]:
    """(python in venv-kokoro, worker script, model, voices)."""
    here = _here()
    return (os.path.join(here, "venv-kokoro", "Scripts", "python.exe"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "kokoro_worker.py"),
            _cfg("KOKORO_MODEL", os.path.join(here, "models", "kokoro", "kokoro-v1.0.int8.onnx")),
            _cfg("KOKORO_VOICES", os.path.join(here, "models", "kokoro", "voices-v1.0.bin")))


def wanted() -> bool:
    return str(_cfg("TTS_ENGINE", "piper")).lower() == "kokoro"


class KokoroClient:
    def __init__(self, popen=subprocess.Popen):
        self._popen = popen
        self._proc = None
        self._lock = threading.Lock()
        self._failures = 0
        self.disabled_reason = ""

    def _start(self) -> bool:
        python, worker, model, voices = paths()
        missing = [p for p in (python, worker, model, voices) if not os.path.exists(p)]
        if missing:
            self.disabled_reason = "missing " + ", ".join(os.path.basename(p) for p in missing)
            return False
        flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
        self._proc = self._popen([python, worker, model, voices], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, creationflags=flags)
        started = time.monotonic()
        line = self._read_line(READY_TIMEOUT)
        if not line or not line.get("ready"):
            self.disabled_reason = (line or {}).get("error") or "the worker didn't start"
            self._stop()
            return False
        logger.info("Kokoro voice ready (%.1f s).", time.monotonic() - started)
        return True

    def _read_line(self, timeout: float) -> Optional[dict]:
        box = {}

        def read():
            try:
                box["line"] = self._proc.stdout.readline()
            except Exception:  # noqa: BLE001
                box["line"] = b""
        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive() or not box.get("line"):
            return None
        try:
            return json.loads(box["line"])
        except ValueError:
            return None

    def _stop(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None

    def synth(self, text: str, lang: str = "en") -> Optional[Tuple[bytes, int]]:
        """(16-bit mono PCM, sample rate) for `text`, or None (speak it with Piper instead)."""
        if self._failures >= MAX_FAILURES or not text.strip():
            return None
        hindi = lang == "hi"
        voice = _cfg("KOKORO_HINDI_VOICE", "hf_alpha") if hindi else _cfg("KOKORO_VOICE", "bf_emma")
        code = "hi" if hindi else ("en-gb" if str(voice).startswith("b") else "en-us")
        with self._lock:
            try:
                if self._proc is None or self._proc.poll() is not None:
                    if not self._start():
                        return self._fail()
                req = json.dumps({"text": text, "voice": voice, "lang": code,
                                  "speed": float(_cfg("KOKORO_SPEED", 1.0))}) + "\n"
                self._proc.stdin.write(req.encode("utf-8"))
                self._proc.stdin.flush()
                head = self._read_line(SENTENCE_TIMEOUT)
                if not head or not head.get("ok"):
                    self.disabled_reason = (head or {}).get("error") or "no answer in time"
                    if head is None:
                        self._stop()                      # stuck: the next sentence starts a fresh worker
                    return self._fail()
                pcm = self._proc.stdout.read(int(head["bytes"]))
                if len(pcm) != int(head["bytes"]):
                    self._stop()
                    return self._fail()
                self._failures = 0
                return pcm, int(head["rate"])
            except Exception as e:  # noqa: BLE001
                self.disabled_reason = str(e)
                self._stop()
                return self._fail()

    def _fail(self):
        self._failures += 1
        if self._failures == MAX_FAILURES:
            logger.warning("Kokoro voice off for this run (%s) - Piper speaks instead.", self.disabled_reason)
        else:
            logger.info("Kokoro voice: %s - Piper speaks this one.", self.disabled_reason)
        return None

    def close(self):
        with self._lock:
            self._stop()


_client: Optional[KokoroClient] = None
_client_lock = threading.Lock()


def client() -> Optional[KokoroClient]:
    """The worker's client when TTS_ENGINE is "kokoro", else None."""
    global _client
    if not wanted():
        return None
    with _client_lock:
        if _client is None:
            _client = KokoroClient()
            try:
                import shutdown
                shutdown.on_shutdown(_client.close)
            except Exception:  # noqa: BLE001
                pass
    return _client
