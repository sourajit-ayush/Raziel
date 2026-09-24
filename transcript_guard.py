"""
transcript_guard.py - stops Whisper from turning silence and noise into sentences.

THE PROBLEM (seen in assistant.log)
-----------------------------------
Whisper is a language model as much as a recogniser. Fed audio with no speech in
it, it does not return nothing - it returns the most plausible text, and
`initial_prompt` (CUSTOM_VOCABULARY + CONTACTS, e.g. "Arijit Singh") makes that
worse, because the prompt is the strongest thing it has to anchor on. Real
examples from this project's own log:

    "Hi, I'm Arijit Singh."                                (x4)
    "Hi, I'm Arijit Singh, and I'm here to talk to you about my dream. I'm
     here to talk to you about my dream. I'm here to ..."  (decoder loop)
    "msr s s s s s s s s s s s s s s s"                    (decoder loop)
    "Thank you." (x8)  "Thanks for watching!"  "Bye."  "You"

TWO INDEPENDENT GATES
---------------------
1. is_likely_silence(audio)   - runs BEFORE Whisper. Cheap energy check on the
                                 recorded wav. Saves the ~1-3 s of inference too.
2. check_segments(segments)   - runs AFTER Whisper, using Whisper's own
                                 confidence numbers (no_speech_prob, avg_logprob,
                                 compression_ratio) plus text-level rules.

Every rejection is logged with the numbers that caused it, so thresholds can be
tuned from real data instead of guessed. All thresholds live in config.py
(GUARD_*); the defaults below are used if they are absent.

Sleep phrases ("goodbye", "go to sleep") are never soft-blocked.
"""

from __future__ import annotations

import logging
import re
import wave
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

try:
    import numpy as np
except ImportError:  # pragma: no cover - numpy is a hard dependency of the app
    np = None

logger = logging.getLogger("voice_assistant")

# ---------------------------------------------------------------- thresholds

_DEFAULTS = {
    "GUARD_ENABLED": True,
    # --- audio gate (before Whisper) ---
    "GUARD_MIN_PEAK_RMS": 150,        # loudest 30 ms frame must reach this
    "GUARD_MIN_VOICED_MS": 200,       # at least this much audibly loud audio
    # --- segment gate (after Whisper) ---
    "GUARD_SEGMENT_NO_SPEECH": 0.6,   # Whisper's own rule: no_speech > this ...
    "GUARD_SEGMENT_LOGPROB": -1.0,    # ... AND avg_logprob < this  -> drop segment
    "GUARD_HARD_LOGPROB": -1.6,       # avg_logprob below this -> drop regardless
    "GUARD_MAX_COMPRESSION": 2.4,     # repetition loops compress absurdly well
    # --- transcript gate ---
    "GUARD_REJECT_LOGPROB": -1.25,    # whole transcript this unsure -> reject
    "GUARD_CONF_MAX_NO_SPEECH": 0.4,  # "confident" = no_speech <= this ...
    "GUARD_CONF_MIN_LOGPROB": -0.8,   # ... and avg_logprob >= this ...
    "GUARD_CONF_MIN_VOICED_MS": 250,  # ... and this much real audio
}


def _cfg(name: str):
    try:
        import config  # local import: keeps this module usable standalone
        return getattr(config, name, _DEFAULTS[name])
    except Exception:
        return _DEFAULTS[name]


# ---------------------------------------------------------------- text helpers

# Devanagari vowel signs and the virama are "marks", not word characters, so a plain \w
# would cut every Hindi word into pieces (and make the repetition rules see hundreds of
# one-letter tokens). The Devanagari block is therefore kept whole - except the danda
# (U+0964/0965), which is punctuation.
_PUNCT_RE = re.compile(r"[^\w\s'\u0900-\u0963\u0966-\u097F]+", re.UNICODE)


def _fold(text: str) -> str:
    """Hindi spelling variants folded together (हाँ/हां, नहीं/नही); no-op for other text."""
    try:
        import lang
        return lang.fold_hindi(text)
    except Exception:                       # lang.py missing - degrade to plain lower-casing
        return (text or "").lower()


def normalize(text: str) -> str:
    """Lowercase, straighten apostrophes, drop punctuation, collapse spaces (Hindi-safe)."""
    t = (text or "").replace("’", "'").replace("‘", "'")
    t = _fold(t).replace("\u0964", " ").replace("\u0965", " ")
    t = _PUNCT_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


# Always junk, whatever the confidence: YouTube-caption residue Whisper learned
# from its training data. Nobody says these to a voice assistant.
HARD_BLOCK_PHRASES = (
    "thanks for watching",
    "thank you for watching",
    "thank you for listening",
    "please subscribe",
    "like and subscribe",
    "don't forget to subscribe",
    "see you in the next video",
    "subtitles by",
    "subtitles made by",
    "captions by",
    "transcribed by",
    "amara org",
)
# The same kind of residue in Hindi (Whisper's Hindi training data is full of video captions).
HARD_BLOCK_PHRASES += tuple(_fold(p) for p in (
    "वीडियो देखने के लिए धन्यवाद",
    "देखने के लिए धन्यवाद",
    "सब्सक्राइब करना न भूलें",
    "चैनल को सब्सक्राइब",
    "चैनल को लाइक और सब्सक्राइब",
    "इस वीडियो को लाइक",
    "अगले वीडियो में मिलते हैं",
    "अगली वीडियो में मिलते हैं",
    "उपशीर्षक",
    "सबटाइटल",
    "आपका बहुत बहुत धन्यवाद",
))

# Short utterances Whisper produces from noise - but which a person CAN really
# say. So these are only accepted when Whisper is confident and there is real
# audio behind them. ("goodbye" is deliberately NOT here: it ends the session.)
SOFT_BLOCK_PHRASES = frozenset({
    "thank you", "thanks", "thank you very much", "thanks a lot",
    "bye", "bye bye", "you", "so", "oh", "hmm", "uh", "um", "huh",
    "okay", "ok", "hello", "hey", "hi", "the end", "i'm sorry", "sorry",
}) | frozenset(_fold(p) for p in (
    # Hindi fillers Whisper produces from noise; a real "धन्यवाद" is still accepted when it is confident
    "धन्यवाद", "शुक्रिया", "थैंक यू", "बाय", "बाय बाय", "हेलो", "हैलो", "ओके", "हम्म", "अच्छा",
))

_SELF_INTRO_LEAD = r"(?:(?:hi|hello|hey|hi there)\s+)?(?:i am|i'm|im|my name is|this is|it's|its)\s+"


# ---------------------------------------------------------------- audio gate

@dataclass
class AudioStats:
    duration_s: float = 0.0
    peak_rms: float = 0.0     # loudest 30 ms frame
    voiced_ms: int = 0        # audibly-loud audio, relative to the clip's own peak


FRAME_MS = 30


def load_wav(path: str):
    """Read a 16-bit wav (what audio_recorder writes). Returns (int16 array, sample_rate)."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if width != 2:
        raise ValueError(f"expected 16-bit audio, got {width * 8}-bit")
    samples = np.frombuffer(raw, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return samples, sr


def analyze_audio(audio, sr: int = 16000) -> AudioStats:
    """Frame-level loudness summary of a clip (int16 or float [-1, 1] samples)."""
    x = np.asarray(audio)
    if x.size == 0:
        return AudioStats()
    if np.issubdtype(x.dtype, np.floating):
        x = x * 32768.0
    x = x.astype(np.float32)

    n = max(1, int(sr * FRAME_MS / 1000))
    if len(x) < n:
        frames = x[None, :]
    else:
        frames = x[: len(x) // n * n].reshape(-1, n)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    peak = float(rms.max())

    # "Voiced" is judged against the clip's own peak, so a quiet talker in a
    # quiet room and a loud talker in a noisy one are both measured fairly.
    floor = max(_cfg("GUARD_MIN_PEAK_RMS") * 0.8, 0.3 * peak)
    voiced = int((rms >= floor).sum())
    return AudioStats(duration_s=len(x) / float(sr), peak_rms=peak, voiced_ms=voiced * FRAME_MS)


def check_audio(audio, sr: int = 16000) -> Tuple[bool, AudioStats, str]:
    """Returns (has_speech_energy, stats, reason_if_not)."""
    stats = analyze_audio(audio, sr)
    if stats.peak_rms < _cfg("GUARD_MIN_PEAK_RMS"):
        return False, stats, f"silence (peak rms {stats.peak_rms:.0f} < {_cfg('GUARD_MIN_PEAK_RMS')})"
    if stats.voiced_ms < _cfg("GUARD_MIN_VOICED_MS"):
        return False, stats, f"too little audio ({stats.voiced_ms} ms voiced < {_cfg('GUARD_MIN_VOICED_MS')})"
    return True, stats, ""


def is_likely_silence(audio, sr: int = 16000) -> bool:
    """True if the clip is silence / a click, i.e. not worth sending to Whisper."""
    if not _cfg("GUARD_ENABLED"):
        return False
    ok, _stats, _reason = check_audio(audio, sr)
    return not ok


# ---------------------------------------------------------------- segment gate

@dataclass
class GuardResult:
    ok: bool
    text: str = ""
    reason: str = ""
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0
    dropped: List[str] = field(default_factory=list)

    def __bool__(self):
        return self.ok


def _g(seg, name: str, default: float = 0.0) -> float:
    val = seg.get(name) if isinstance(seg, dict) else getattr(seg, name, None)
    try:
        return default if val is None else float(val)
    except (TypeError, ValueError):
        return default


def _seg_text(seg) -> str:
    val = seg.get("text") if isinstance(seg, dict) else getattr(seg, "text", "")
    return (val or "").strip()


def _repetition_reason(tokens: Sequence[str]) -> Optional[str]:
    """Detects decoder loops: 'msr s s s s s', or one sentence repeated 3 times."""
    n = len(tokens)
    if n < 4:
        return None

    counts = Counter(tokens)
    tok, cnt = counts.most_common(1)[0]
    if n >= 6 and cnt / n >= 0.6:
        return f"token {tok!r} makes up {cnt}/{n} of the text"

    tiny = sum(1 for t in tokens if len(t) == 1 and t not in ("a", "i"))
    if n >= 8 and tiny / n >= 0.5:
        return "mostly single-letter tokens"

    def occurrences(gram: Tuple[str, ...]) -> int:
        size, i, found = len(gram), 0, 0
        while i <= n - size:
            if tuple(tokens[i:i + size]) == gram:
                found += 1
                i += size
            else:
                i += 1
        return found

    for size in range(2, min(12, n // 2) + 1):
        grams = Counter(tuple(tokens[i:i + size]) for i in range(n - size + 1))
        for gram, seen in grams.most_common(2):
            if seen < 3:
                break
            reps = occurrences(gram)
            if reps >= 3 and reps * size >= 0.5 * n:
                return f"{size}-word phrase repeated {reps} times"
    return None


def _confident(nsp: float, alp: float, stats: Optional[AudioStats]) -> bool:
    if nsp > _cfg("GUARD_CONF_MAX_NO_SPEECH"):
        return False
    if alp < _cfg("GUARD_CONF_MIN_LOGPROB"):
        return False
    if stats is not None and stats.voiced_ms < _cfg("GUARD_CONF_MIN_VOICED_MS"):
        return False
    return True


def _vocab_forms(custom_vocabulary: Iterable[str]) -> List[str]:
    forms = [normalize(v) for v in (custom_vocabulary or []) if v and normalize(v)]
    return sorted(set(forms), key=len, reverse=True)


def filter_transcript(text: str, custom_vocabulary: Iterable[str] = (), nsp: float = 0.0,
                      alp: float = 0.0, stats: Optional[AudioStats] = None,
                      protected_phrases: Optional[Iterable[str]] = None) -> Tuple[bool, str]:
    """Text-level rules. Returns (ok, reason). See module docstring for why each exists."""
    norm = normalize(text)
    if not norm:
        return False, "empty"

    tokens = norm.split()

    reason = _repetition_reason(tokens)
    if reason:
        return False, f"repetition loop ({reason})"

    padded = f"{norm} "
    for phrase in HARD_BLOCK_PHRASES:
        if phrase in padded:
            return False, f"caption residue ({phrase.strip()!r})"

    vocab = _vocab_forms(custom_vocabulary)

    # Prompt echo, form 1: "Hi, I'm Arijit Singh" - the decoder introduces the
    # name it was primed with. People do not introduce themselves as the singer
    # they want to hear, so this is rejected outright.
    if vocab:
        alt = "|".join(re.escape(v) for v in vocab)
        if re.match(rf"^{_SELF_INTRO_LEAD}(?:{alt})(?:\s|$)", norm):
            return False, "self-introduction anchored to CUSTOM_VOCABULARY (prompt echo)"

    protected = [normalize(p) for p in (protected_phrases or []) if p]
    if any(p and p in norm for p in protected):
        return True, ""          # "goodbye" / "go to sleep" always get through

    # Prompt echo, form 2: the bare name and nothing else. Real (someone testing
    # "Arijit Singh") or echo - so trust it only when Whisper is confident.
    if vocab:
        joined = " ".join(vocab)
        if norm in vocab or norm == joined:
            if not _confident(nsp, alp, stats):
                return False, "bare vocabulary name with low confidence (prompt echo)"

    if norm in SOFT_BLOCK_PHRASES and not _confident(nsp, alp, stats):
        return False, (f"filler {norm!r} without confidence "
                       f"(no_speech={nsp:.2f}, logprob={alp:.2f})")

    if alp < _cfg("GUARD_REJECT_LOGPROB"):
        return False, f"low confidence (avg_logprob {alp:.2f} < {_cfg('GUARD_REJECT_LOGPROB')})"

    return True, ""


def check_segments(segments, custom_vocabulary: Iterable[str] = (), stats: Optional[AudioStats] = None,
                   protected_phrases: Optional[Iterable[str]] = None) -> GuardResult:
    """
    Vet faster-whisper segments. Accepts the generator transcribe() returns (it
    is consumed once) or any list of objects/dicts with .text / .no_speech_prob /
    .avg_logprob / .compression_ratio.

    Bad segments are dropped individually; whatever survives is then judged as a
    whole transcript. Returns GuardResult(ok, text, reason, ...).
    """
    if not _cfg("GUARD_ENABLED"):
        text = " ".join(t for t in (_seg_text(s) for s in segments) if t).strip()
        return GuardResult(ok=bool(text), text=text, reason="" if text else "empty")

    kept, dropped = [], []
    for seg in segments:
        text = _seg_text(seg)
        if not text:
            continue
        nsp = _g(seg, "no_speech_prob")
        alp = _g(seg, "avg_logprob")
        cr = _g(seg, "compression_ratio")

        why = None
        if nsp > _cfg("GUARD_SEGMENT_NO_SPEECH") and alp < _cfg("GUARD_SEGMENT_LOGPROB"):
            why = f"Whisper says no speech (no_speech={nsp:.2f}, logprob={alp:.2f})"
        elif alp < _cfg("GUARD_HARD_LOGPROB"):
            why = f"very low confidence (logprob={alp:.2f})"
        elif cr > _cfg("GUARD_MAX_COMPRESSION"):
            why = f"repetitive (compression_ratio={cr:.2f})"

        if why:
            dropped.append(f"{text!r}: {why}")
        else:
            kept.append((text, nsp, alp))

    if not kept:
        reason = "all segments rejected" if dropped else "empty"
        result = GuardResult(ok=False, reason=reason, dropped=dropped)
        _log(result, "")
        return result

    text = " ".join(t for t, _n, _a in kept).strip()
    nsp = sum(n for _t, n, _a in kept) / len(kept)
    alp = sum(a for _t, _n, a in kept) / len(kept)

    ok, reason = filter_transcript(text, custom_vocabulary, nsp, alp, stats, protected_phrases)
    result = GuardResult(ok=ok, text=text if ok else "", reason=reason,
                         no_speech_prob=nsp, avg_logprob=alp, dropped=dropped)
    _log(result, text)
    return result


def _log(result: GuardResult, text: str):
    if result.ok:
        if result.dropped:
            logger.info("Guard kept transcript but dropped: %s", "; ".join(result.dropped))
        return
    logger.info(
        "Guard rejected %r - %s (no_speech=%.2f, logprob=%.2f)%s",
        text, result.reason, result.no_speech_prob, result.avg_logprob,
        f" | dropped: {'; '.join(result.dropped)}" if result.dropped else "",
    )
