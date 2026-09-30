"""
voice_id.py - "was that Ayush?" Local speaker verification, so the TV or a guest saying
"shut down the PC" does nothing.

Anyone can still ask her the time, the weather, a question. Only the RISKY actions need your voice:
shutting down / restarting / sleeping / locking the PC, closing apps, WhatsApp messages, emails, phone
SMS / ring, calendar events, deleting notes and lists, overwriting files (VOICE_ID_GUARDED). A "yes"
to one of her yes/no questions must be yours too. Searching your files (file_index.py) is refused only
to a voice that is clearly someone else's.

HOW
---
  * Your voice is learned from the recordings you made for the wake-word training (my_voice\\): about
    130 clips, all you. Each becomes a 256-number "voiceprint" (a WeSpeaker ResNet34 speaker model, run
    by onnxruntime, which Raziel already has); their average is your profile, saved in voice_profile.npz
    (gitignored - it never leaves this PC).
  * Every time you finish speaking, the same is done for that recording (~0.1 s, while Whisper is
    transcribing) and compared with your profile (cosine similarity, -1..1).
      >= VOICE_ID_THRESHOLD         it's you
      <  VOICE_ID_REJECT_BELOW      it's someone else
      in between, or under VOICE_ID_MIN_SPEECH seconds of speech: not sure -> she asks you to say it again
  * Commands from your phone (token-protected phone bridge) count as you.

Tested on your own clips: held-out recordings of you scored 0.51-0.8, 125 other voices 0.36 at most.
Clear checks of you (>= 0.6) slowly adapt the profile to how you sound through the mic day to day.
Every check is written to assistant.log ("Voice ID: 0.63 -> you") so the thresholds can be tuned.

The model file is downloaded once by install_new_features.bat (models\\voice_id_resnet34.onnx). Without
it Voice ID is simply off and everything works as before. To learn your voice again (new mic), delete
voice_profile.npz and restart her.
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass
from typing import Callable, List, Optional

import numpy as np

import config
import lang

logger = logging.getLogger("voice_assistant")

SAMPLE_RATE = 16000
OWNER, OTHER, UNSURE, OFF = "owner", "other", "unsure", "off"

DEFAULT_GUARDED = (
    "shutdown_pc", "restart_pc", "sleep_pc", "lock_pc", "close_app",
    "send_whatsapp_message", "compose_email", "send_phone_sms", "control_phone",
    "add_calendar_event", "clear_notes", "clear_list", "delete_note", "overwrite_file",
    "read_files",          # file_index.py: your file names and what is in them (only a clearly other voice is refused)
)

_T = {
    "not_you": {"en": "Sorry, I only do that for {owner}.",
                "hi": "माफ़ कीजिए, ये काम मैं सिर्फ़ {owner} के कहने पर करती हूँ।"},
    "unsure": {"en": "I couldn't tell that was you, {owner}. Say it again, a little closer to the mic?",
               "hi": "{owner}, मैं पहचान नहीं पाई कि ये आप हैं। माइक के थोड़ा पास से फिर से बोलिए?"},
    "answer_not_you": {"en": "That wasn't {owner}'s voice, so I cancelled it: {summary}.",
                       "hi": "ये {owner} की आवाज़ नहीं थी, इसलिए मैंने रद्द कर दिया: {summary}।"},
}


def _cfg(name: str, default):
    return getattr(config, name, default)


def _owner() -> str:
    name = str(_cfg("OWNER_NAME", "") or "").strip()
    return name or ("you" if not lang.is_hindi() else "आप")


def enabled() -> bool:
    return bool(_cfg("VOICE_ID_ENABLED", True))


def model_path() -> str:
    return str(_cfg("VOICE_ID_MODEL", os.path.join(getattr(config, "_SCRIPT_DIR", "."), "models",
                                                   "voice_id_resnet34.onnx")))


def profile_path() -> str:
    return str(_cfg("VOICE_ID_PROFILE", os.path.join(getattr(config, "_SCRIPT_DIR", "."), "voice_profile.npz")))


def enrol_dir() -> str:
    return str(_cfg("VOICE_ID_ENROL_DIR", os.path.join(getattr(config, "_SCRIPT_DIR", "."), "my_voice")))


# ------------------------------------------------------------------ audio -> features (Kaldi fbank, numpy)

def _mel_banks(num_bins: int = 80, n_fft: int = 512, low: float = 20.0, high: float = SAMPLE_RATE / 2):
    mel = lambda f: 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)
    nfb = n_fft // 2
    m = mel(np.arange(nfb) * (SAMPLE_RATE / n_fft))
    mlow, mhigh = mel(low), mel(high)
    delta = (mhigh - mlow) / (num_bins + 1)
    W = np.zeros((num_bins, nfb))
    for b in range(num_bins):
        left, center, right = mlow + b * delta, mlow + (b + 1) * delta, mlow + (b + 2) * delta
        w = np.minimum((m - left) / (center - left), (right - m) / (right - center))
        w[(m <= left) | (m >= right)] = 0
        W[b] = np.maximum(w, 0)
    return W


_MEL = None
_WIN = 0.54 - 0.46 * np.cos(2 * np.pi * np.arange(400) / 399)       # hamming, as the model was trained


def fbank(x: np.ndarray) -> np.ndarray:
    """80-dim log-mel filterbank (Kaldi-compatible: 25 ms / 10 ms, pre-emphasis 0.97, DC removal), then
    mean-normalised. `x` is float audio in -1..1 at 16 kHz; the model expects int16-scaled samples."""
    global _MEL
    if _MEL is None:
        _MEL = _mel_banks()
    x = np.asarray(x, dtype=np.float64) * 32768.0
    L, S, N = 400, 160, 512
    if len(x) < L:
        return np.zeros((0, 80), np.float32)
    n = 1 + (len(x) - L) // S
    idx = np.arange(L)[None, :] + S * np.arange(n)[:, None]
    fr = x[idx]
    fr = fr - fr.mean(axis=1, keepdims=True)
    pre = np.empty_like(fr)
    pre[:, 1:] = fr[:, 1:] - 0.97 * fr[:, :-1]
    pre[:, 0] = fr[:, 0] * 0.03
    pre *= _WIN
    spec = np.abs(np.fft.rfft(pre, n=N, axis=1)) ** 2
    feats = np.log(np.maximum(spec[:, :N // 2] @ _MEL.T, np.finfo(np.float32).eps))
    feats -= feats.mean(axis=0, keepdims=True)
    return feats.astype(np.float32)


def voiced(x: np.ndarray) -> np.ndarray:
    """Only the parts with speech in them (a simple energy gate: pauses and silence dropped)."""
    x = np.asarray(x, dtype=np.float32)
    f = 160
    n = len(x) // f
    if n < 5:
        return x[:0]
    rms = np.sqrt((x[:n * f].reshape(n, f).astype(np.float64) ** 2).mean(axis=1))
    floor = float(np.percentile(rms, 20))
    thr = max(floor * 2.5, float(rms.max()) * 0.03, 3e-4)
    keep = rms > thr
    # hang-over: keep a few frames around every voiced frame so syllable edges aren't cut
    k = np.convolve(keep.astype(np.int32), np.ones(7, dtype=np.int32), mode="same") > 0
    if not k.any():
        return x[:0]
    return x[:n * f].reshape(n, f)[k].reshape(-1)


def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"{path}: only 16-bit wav is supported")
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != SAMPLE_RATE:
        n = int(len(x) * SAMPLE_RATE / sr)
        x = np.interp(np.arange(n) * sr / SAMPLE_RATE, np.arange(len(x)), x).astype(np.float32)
    return x


# ------------------------------------------------------------------ the model

class Model:
    def __init__(self, path: str):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(_cfg("VOICE_ID_THREADS", 2))
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(path, sess_options=opts, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.lock = threading.Lock()

    def embed(self, x: np.ndarray) -> Optional[np.ndarray]:
        feats = fbank(x)
        if len(feats) < 20:
            return None
        with self.lock:
            e = self.session.run(None, {self.input: feats[None]})[0][0]
        e = np.asarray(e, dtype=np.float64)
        norm = np.linalg.norm(e)
        return e / norm if norm > 0 else None


_model: Optional[Model] = None
_model_failed = False
_profile: Optional[np.ndarray] = None
_model_lock = threading.Lock()


def _file_tag(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size}"


def get_model() -> Optional[Model]:
    global _model, _model_failed
    with _model_lock:
        if _model is not None or _model_failed:
            return _model
        path = model_path()
        if not os.path.isfile(path):
            logger.info("Voice ID is off: the model isn't downloaded yet (%s) - run install_new_features.bat", path)
            _model_failed = True
            return None
        try:
            _model = Model(path)
        except Exception:  # noqa: BLE001
            logger.exception("Voice ID is off: couldn't load %s", path)
            _model_failed = True
        return _model


# ------------------------------------------------------------------ your profile (enrolment)

def _enrol_clips(folder: str) -> List[np.ndarray]:
    """Your voice from my_voice\\: every clip except the room-noise ones; long talk clips in 3 s pieces."""
    clips = []
    for path in sorted(glob.glob(os.path.join(folder, "**", "*.wav"), recursive=True)):
        name = os.path.basename(path).lower()
        if name.startswith("room") or "noise" in name:
            continue
        try:
            x = voiced(read_wav(path))
        except Exception as e:  # noqa: BLE001
            logger.debug("Voice ID: skipping %s (%s)", path, e)
            continue
        if len(x) < 0.4 * SAMPLE_RATE:
            continue
        if len(x) > 6 * SAMPLE_RATE:
            step = 3 * SAMPLE_RATE
            clips.extend(x[k:k + step] for k in range(0, len(x) - step // 2, step))
        else:
            clips.append(x)
    return clips


def build_profile(clips: List[np.ndarray], model: Model) -> Optional[np.ndarray]:
    embs = [e for e in (model.embed(c) for c in clips) if e is not None]
    if len(embs) < 5:
        return None
    E = np.array(embs)
    centre = E.mean(axis=0)
    centre /= np.linalg.norm(centre)
    # drop the odd clip that doesn't sound like the rest (a cough, someone else in the room)
    keep = E[(E @ centre) >= 0.35]
    if len(keep) >= 5:
        centre = keep.mean(axis=0)
        centre /= np.linalg.norm(centre)
    return centre


def load_profile() -> Optional[np.ndarray]:
    global _profile
    if _profile is not None:
        return _profile
    path = profile_path()
    if not os.path.isfile(path):
        return None
    try:
        data = np.load(path, allow_pickle=False)
        if model_path() and os.path.isfile(model_path()) and str(data.get("model_tag", "")) not in ("", _file_tag(model_path())):
            logger.info("Voice ID: the saved profile was made with another model - learning your voice again")
            return None
        _profile = np.asarray(data["vec"], dtype=np.float64)
        return _profile
    except Exception:  # noqa: BLE001
        logger.exception("Voice ID: couldn't read %s", path)
        return None


def enrol(folder: Optional[str] = None) -> bool:
    """Learns your voice from `folder` (my_voice\\) and saves the profile. True when it worked."""
    global _profile
    model = get_model()
    if model is None:
        return False
    folder = folder or enrol_dir()
    t0 = time.monotonic()
    clips = _enrol_clips(folder)
    prof = build_profile(clips, model)
    if prof is None:
        logger.warning("Voice ID: not enough recordings of you in %s (%d usable clips) - Voice ID stays off",
                       folder, len(clips))
        return False
    try:
        np.savez(profile_path(), vec=prof, model_tag=_file_tag(model_path()), n=len(clips), created=time.time())
    except Exception:  # noqa: BLE001
        logger.exception("Voice ID: couldn't save %s (the profile is used for this run only)", profile_path())
    _profile = prof
    logger.info("Voice ID: learned your voice from %d clips in %.1f s", len(clips), time.monotonic() - t0)
    return True


def ensure_ready_async(delay: float = 8.0):
    """At start-up: load the model and your profile (learning it first if needed) in the background."""
    if not enabled():
        return

    def work():
        time.sleep(delay)
        if get_model() is None:
            return
        if load_profile() is None:
            enrol()

    threading.Thread(target=work, name="voice-id-setup", daemon=True).start()


def ready() -> bool:
    return enabled() and _model is not None and _profile is not None


# ------------------------------------------------------------------ checking one utterance

@dataclass
class Result:
    verdict: str            # owner | other | unsure | off
    score: float = 0.0
    speech: float = 0.0     # seconds of speech it was judged on
    raw: str = ""           # the verdict by score alone (before "too short to say it's someone else")


def judge(x: np.ndarray) -> Result:
    if not enabled():
        return Result(OFF)
    model, prof = get_model(), load_profile()
    if model is None or prof is None:
        return Result(OFF)
    v = voiced(x)
    speech = len(v) / SAMPLE_RATE
    if speech < float(_cfg("VOICE_ID_MIN_SPEECH", 0.5)):
        return Result(UNSURE, 0.0, speech)
    e = model.embed(v)
    if e is None:
        return Result(UNSURE, 0.0, speech)
    score = float(e @ prof)
    accept = float(_cfg("VOICE_ID_THRESHOLD", 0.45))
    reject = float(_cfg("VOICE_ID_REJECT_BELOW", 0.33))
    raw = OWNER if score >= accept else OTHER if score < reject else UNSURE
    verdict = raw
    if verdict == OTHER and speech < float(_cfg("VOICE_ID_OTHER_MIN_SPEECH", 1.0)):
        verdict = UNSURE            # a short or mumbled command: ask again rather than "you're not Ayush"
    if verdict == OWNER:
        _maybe_adapt(e, score)
    return Result(verdict, score, speech, raw)


def check_file(path: str) -> Result:
    try:
        return judge(read_wav(path))
    except Exception:  # noqa: BLE001
        logger.exception("Voice ID: checking %s failed", path)
        return Result(OFF)


_turn = {"result": None, "event": None}
_local = threading.local()


def begin_turn(wav_path: str):
    """main.py, right after you finished speaking: judges the recording in the background (while Whisper
    transcribes), for guard() to consult."""
    if not ready():
        _turn["result"], _turn["event"] = None, None
        return
    ev = threading.Event()
    _turn["result"], _turn["event"] = None, ev

    def work():
        r = check_file(wav_path)
        _turn["result"] = r
        ev.set()
        if r.verdict != OFF:
            logger.info("Voice ID: %.2f (%.1f s of speech) -> %s", r.score, r.speech,
                        {"owner": "you", "other": "someone else", "unsure": "not sure"}[r.verdict])

    threading.Thread(target=work, name="voice-id-check", daemon=True).start()


_adapt = {"n": 0}


def _maybe_adapt(e: np.ndarray, score: float):
    """Nudges the profile towards how you sound through the mic day to day (only from checks that are
    very clearly you), so a new room or a cold doesn't slowly lock you out."""
    global _profile
    if not _cfg("VOICE_ID_ADAPT", True) or _profile is None or score < float(_cfg("VOICE_ID_ADAPT_ABOVE", 0.6)):
        return
    w = float(_cfg("VOICE_ID_ADAPT_WEIGHT", 0.03))
    p = (1 - w) * _profile + w * e
    _profile = p / np.linalg.norm(p)
    _adapt["n"] += 1
    if _adapt["n"] % 10 == 0:
        try:
            np.savez(profile_path(), vec=_profile, model_tag=_file_tag(model_path()), n=-1, created=time.time())
        except Exception:  # noqa: BLE001
            logger.debug("Voice ID: couldn't save the adapted profile", exc_info=True)


def current(wait: float = 2.5) -> Optional[Result]:
    ev = _turn["event"]
    if ev is None:
        return None
    if not ev.wait(wait):
        logger.warning("Voice ID: the check took longer than %.1f s - not using it for this turn", wait)
        return None
    return _turn["result"]


@contextlib.contextmanager
def trusted():
    """Commands from your phone (token-protected) count as you."""
    prev = getattr(_local, "trusted", False)
    _local.trusted = True
    try:
        yield
    finally:
        _local.trusted = prev


def _guarded(kind: str) -> bool:
    return kind in set(_cfg("VOICE_ID_GUARDED", DEFAULT_GUARDED))


# Low-risk actions: only a voice that is clearly someone else's is refused ("Close the Google Chrome" was
# refused at 0.42 - just under "you" - which only annoys). Easily undone, nothing leaves the PC.
LENIENT_KINDS = ("lock_pc", "close_app")


def guard(kind: str, refuse_unsure: bool = True) -> Optional[str]:
    """None = go ahead. Otherwise the sentence to say instead of doing `kind`.
    refuse_unsure=False: only a voice that is clearly someone else's is refused."""
    if not ready() or getattr(_local, "trusted", False) or not _guarded(kind):
        return None
    if kind in set(_cfg("VOICE_ID_LENIENT", LENIENT_KINDS)):
        refuse_unsure = False
    r = current()
    if r is None or r.verdict == OFF:
        # Voice ID is on, but this turn has no verdict (it was still starting up when you spoke, the
        # check took too long, or it failed): not sure - never "go ahead" by default.
        logger.info("Voice ID: no verdict for this command - treated as 'not sure'")
        r = Result(UNSURE)
    if r.verdict == OWNER or (not refuse_unsure and r.verdict != OTHER):
        return None
    logger.info("Voice ID: %s refused (%s, %.2f)", kind, r.verdict, r.score)
    return lang.tr(_T, "not_you" if r.verdict == OTHER else "unsure", owner=_owner())


def answer_refusal(summary: str) -> Optional[str]:
    """For a "yes" to a yes/no question: the sentence to say if the voice clearly wasn't yours.
    A short "yes" that can't be judged is accepted (the question itself was already checked)."""
    if not ready() or getattr(_local, "trusted", False):
        return None
    r = current()
    if r is None or (r.raw or r.verdict) != OTHER:     # judged by score: a "yes" is usually under a second
        return None
    logger.info("Voice ID: a 'yes' in someone else's voice (%.2f) - cancelled", r.score)
    return lang.tr(_T, "answer_not_you", owner=_owner(), summary=summary)


if __name__ == "__main__":
    # venv\Scripts\python.exe voice_id.py            learn your voice now (from my_voice\)
    # venv\Scripts\python.exe voice_id.py clip.wav   how much a recording sounds like you
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if get_model() is None:
        print("Voice ID: the model isn't there yet:", model_path())
        sys.exit(1)
    if len(sys.argv) > 1:
        if load_profile() is None and not enrol():
            sys.exit(1)
        for p in sys.argv[1:]:
            r = check_file(p)
            print(f"{p}: {r.score:.2f} -> {r.verdict}")
        sys.exit(0)
    ok = enrol()
    print("Voice ID: your voice profile is ready." if ok else "Voice ID: couldn't learn your voice (see above).")
    sys.exit(0 if ok else 1)
