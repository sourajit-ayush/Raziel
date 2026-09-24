"""
dictation.py - "type what I say": Raziel types the user's words into whatever app has the keyboard focus.

Two ways in:
  * Dictation mode.  "start typing" / "start dictation" / "dictation mode" / "type what I say" /
    "टाइपिंग शुरू करो" / "जो मैं बोलूँ वो टाइप करो" turns it on. From then on EVERY transcript is typed
    (winutil.type_text, real Unicode key events, so Hindi works) until the user says "stop typing".
  * One-shot.  "type hello world" / "type this: see you at 5" / "टाइप करो: नमस्ते" types just that text.

How it plugs into main.py
-------------------------
  1. main.py calls `handle(transcript)` FIRST for every transcript.
       - dictation OFF -> returns None immediately (nothing else happens).
       - dictation ON  -> the transcript is consumed: it is typed (or is a command, see below) and the return
         value is "" = "handled, say nothing". A stop phrase returns "Dictation off." (spoken); an idle timeout
         returns "Dictation timed out." and that transcript is NOT typed and NOT routed.
  2. `try_handle(transcript)` is the normal matcher for the start phrases and the one-shot "type ...".
  3. `is_active()` is True while dictation is on. main.py must NOT run its sleep-phrase check ("goodbye",
     "go to sleep") on a transcript while `is_active()` - a dictated sentence may contain the word - or at least
     only for a short utterance. (handle() already consumes those transcripts before the check is reached when
     it is called first, as documented above; is_active() is for any other place that looks at the raw text.)

Inside dictation (whole utterances only, so ordinary sentences are never mistaken for a command):
  stop        "stop typing", "stop dictation", "end dictation", "done typing", "that's all", "finish typing",
              "टाइपिंग बंद करो", "डिक्टेशन बंद करो", "बस"  (polite fillers - "please", "ok", "Raziel" - are ignored)
  new line    "new line" / "नई लाइन" (one Enter), "new paragraph" / "नया पैराग्राफ" (two Enters), "press enter"
  delete      "delete that" / "scratch that" / "undo that" / "यह हटा दो": Backspace exactly the last typed chunk
              (a stack of the last 10 chunks, so saying it twice removes the two chunks before; nothing typed = ignored)
  punctuation "comma", "full stop", "period", "question mark", "exclamation mark", "colon" (Hindi "अल्पविराम",
              "पूर्ण विराम" -> "।", "प्रश्न चिह्न") ONLY as the entire utterance or at the very end
              ("see you tomorrow full stop"). It is typed without a leading space (the trailing space of the
              previous chunk is taken back) and followed by a space.
Idle timeout: `config.DICTATION_IDLE_TIMEOUT` seconds (default 300) without any transcript switches dictation off at
the NEXT transcript.

Safety: dictation is never switched on (and a one-shot never types) while Raziel's own window has the focus
(title contains "Raziel" or the program is python / pythonw) - she would be typing into herself; she asks the user
to click the target app first. When Windows refuses the keystrokes (an administrator window and UIPI, or not
Windows at all) dictation is switched off and a friendly sentence explains it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata
from typing import Dict, List, Optional, Tuple

import config
import lang
import winutil

logger = logging.getLogger("voice_assistant")

MAX_UNDO_CHUNKS = 10

# ------------------------------------------------------------------ strings (English / Hindi)

_T = {
    "on": {"en": "Dictation on. Click where you want the text, then talk. Say stop typing when you're done.",
           "hi": "डिक्टेशन चालू है। जहाँ टेक्स्ट चाहिए वहाँ क्लिक कीजिए, फिर बोलिए। पूरा होने पर टाइपिंग बंद करो कहिए।"},
    "off": {"en": "Dictation off.", "hi": "डिक्टेशन बंद।"},
    "timeout": {"en": "Dictation timed out.", "hi": "डिक्टेशन का समय खत्म हो गया।"},
    "already_on": {"en": "Dictation is already on.", "hi": "डिक्टेशन पहले से चालू है।"},
    "already_off": {"en": "Dictation is already off.", "hi": "डिक्टेशन पहले से बंद है।"},
    "self_front": {"en": "I'd be typing into myself. Click the app where you want the text first, then say start typing.",
                   "hi": "मैं खुद में ही टाइप कर दूँगी। पहले उस ऐप पर क्लिक कीजिए जहाँ टेक्स्ट चाहिए, फिर टाइपिंग शुरू करो कहिए।"},
    "typed": {"en": "Typed.", "hi": "टाइप कर दिया।"},
    "need_text": {"en": "What should I type?", "hi": "क्या टाइप करूँ?"},
    "blocked": {"en": "I couldn't type there. Windows blocked the keystrokes, probably because that app is running as "
                      "administrator. Dictation is off.",
                "hi": "मैं वहाँ टाइप नहीं कर पाई। Windows ने कीस्ट्रोक रोक दिए, शायद वह ऐप एडमिनिस्ट्रेटर के तौर पर चल रहा है। "
                      "डिक्टेशन बंद है।"},
    "blocked_once": {"en": "I couldn't type there. Windows blocked the keystrokes, probably because that app is running "
                           "as administrator.",
                     "hi": "मैं वहाँ टाइप नहीं कर पाई। Windows ने कीस्ट्रोक रोक दिए, शायद वह ऐप एडमिनिस्ट्रेटर के तौर पर चल रहा है।"},
    "no_windows": {"en": "Typing into other apps works on Windows only.",
                   "hi": "दूसरे ऐप में टाइप करना सिर्फ़ Windows पर काम करता है।"},
    "unknown_action": {"en": "I can start or stop dictation, or type a sentence for you.",
                       "hi": "मैं डिक्टेशन शुरू या बंद कर सकती हूँ, या आपके लिए कोई वाक्य टाइप कर सकती हूँ।"},
    "sorry": {"en": "Sorry, something went wrong with typing.", "hi": "माफ़ कीजिए, टाइपिंग में कुछ गड़बड़ हो गई।"},
}


def _tr(key: str, **kw) -> str:
    return lang.tr(_T, key, **kw)


# ------------------------------------------------------------------ state

_lock = threading.RLock()
_active = False
_last_activity = 0.0
# The last typed chunks, newest last: (characters typed, ends with a space, a space of the PREVIOUS chunk was taken back).
_chunks: List[Tuple[int, bool, bool]] = []
_after_space = False               # the character before the cursor is a trailing space that WE typed


def _now() -> float:
    """Monotonic clock for the idle timeout (tests replace this or pass now= to handle())."""
    return time.monotonic()


def _idle_timeout() -> float:
    try:
        return float(getattr(config, "DICTATION_IDLE_TIMEOUT", 300))
    except (TypeError, ValueError):
        return 300.0


def is_active() -> bool:
    with _lock:
        return _active


def start() -> None:
    global _active, _last_activity, _after_space
    with _lock:
        _active = True
        _last_activity = _now()
        _chunks.clear()
        _after_space = False
    logger.info("dictation: ON")


def stop() -> bool:
    """Switches dictation off. Returns True if it was on."""
    global _active
    with _lock:
        was = _active
        _active = False
        _chunks.clear()
    if was:
        logger.info("dictation: OFF")
    return was


# ------------------------------------------------------------------ side effects (tests replace these)

def _type(text: str) -> int:
    """Types text into the focused window. Returns the number of characters typed. Raises OSError."""
    n = winutil.type_text(text)
    return n if isinstance(n, int) else len(text)


def _backspace(n: int) -> None:
    if n > 0:
        winutil.tap_key(winutil.VK_BACK, n)


def _enter(times: int = 1) -> None:
    winutil.tap_key(winutil.VK_RETURN, times)


def _foreground() -> Dict[str, object]:
    """{'title', 'exe', ...} of the window with the focus. Raises OSError when not on Windows."""
    return winutil.foreground_window()


def _looks_like_self(fg: Dict[str, object]) -> bool:
    title = str(fg.get("title") or "").lower()
    exe = str(fg.get("exe") or "").lower()
    return "raziel" in title or exe.startswith(("python", "pythonw"))


def _err_sentence(exc: BaseException, dictation: bool) -> str:
    if "windows only" in str(exc).lower():
        return _tr("no_windows")
    return _tr("blocked" if dictation else "blocked_once")


# ------------------------------------------------------------------ normalisation

_LEAD = {"ok", "okay", "please", "raziel", "razil", "razeel", "hey", "hi", "hello", "now", "so", "alright",
         "and", "well", "um", "uh", "umm", "uhh", "kindly", "just", "कृपया", "प्लीज़", "प्लीज", "रज़ील", "रेज़ील",
         "अब", "अच्छा", "ठीक", "है", "सुनो"}
_TRAIL = {"please", "now", "thanks", "thank", "you", "ok", "okay", "raziel", "कृपया", "प्लीज़", "प्लीज", "अब",
          "धन्यवाद", "शुक्रिया"}


_POLITE = re.compile(r"^(?:(?:can|could|would|will) you (?:please )?|(?:i want|i'd like|i need) you to |go ahead and )")
_POLITE_TAIL = re.compile(r"\s(?:for me|मेरे लिए)$")


def _tokens(text: str) -> List[str]:
    return lang.normalize_hindi(text or "").split()


_CMD_TRAIL = {"please", "now", "कृपया", "प्लीज़", "प्लीज"}
# the token lists above are compared with normalize_hindi() output (nukta / anusvara dropped): fold them the same way
_LEAD = {lang.normalize_hindi(w) for w in _LEAD}
_TRAIL = {lang.normalize_hindi(w) for w in _TRAIL}
_CMD_TRAIL = {lang.normalize_hindi(w) for w in _CMD_TRAIL}


def _plain(text: str) -> str:
    """Whole-utterance form of an EDIT command ("new line", "comma", "delete that"): lower case, punctuation gone,
    a trailing "please" tolerated. No leading filler is stripped: "Hello, comma" is a dictated "Hello" plus a comma."""
    toks = _tokens(text)
    while toks and toks[-1] in _CMD_TRAIL:
        toks.pop()
    return " ".join(toks)


def _core(text: str) -> str:
    """Whole-utterance form of a START / STOP phrase: lower case, punctuation gone, polite fillers stripped."""
    s = " ".join(_tokens(text))
    for _ in range(4):                                   # "ok, could you please ..." - a few layers are enough
        before = s
        s = _POLITE.sub("", s, count=1)
        toks = s.split()
        while toks and toks[0] in _LEAD:
            toks.pop(0)
        while toks and toks[-1] in _TRAIL:
            toks.pop()
        s = " ".join(toks)
        s = _POLITE_TAIL.sub("", s, count=1)
        if s == before:
            break
    return s


def _f(*phrases: str) -> frozenset:
    return frozenset(lang.normalize_hindi(p) for p in phrases)


_STOP = _f(
    "stop typing", "stop dictation", "end dictation", "done typing", "that's all", "thats all", "that is all",
    "finish typing", "stop dictating", "end typing", "stop the dictation", "dictation off", "turn off dictation",
    "typing off", "finish dictation", "i'm done typing", "im done typing", "stop typing now", "stop type",
    "टाइपिंग बंद करो", "डिक्टेशन बंद करो", "टाइपिंग बंद", "डिक्टेशन बंद", "टाइपिंग बंद कर दो", "डिक्टेशन बंद कर दो",
    "टाइपिंग बंद करें", "डिक्टेशन बंद करें", "बस", "बस करो", "टाइपिंग रोक दो", "टाइपिंग खत्म करो",
    "डिक्टेशन खत्म करो", "टाइप करना बंद करो")
# only these may switch a dictation off that is not on (a bare "that's all" or "बस" must not match then)
_STOP_EXPLICIT = _f(
    "stop typing", "stop dictation", "end dictation", "stop dictating", "finish typing", "end typing",
    "stop the dictation", "dictation off", "turn off dictation", "typing off", "finish dictation",
    "टाइपिंग बंद करो", "डिक्टेशन बंद करो", "टाइपिंग बंद", "डिक्टेशन बंद", "टाइपिंग बंद कर दो", "डिक्टेशन बंद कर दो")
_NEWLINE = _f("new line", "newline", "next line", "नई लाइन", "नयी लाइन", "अगली लाइन", "नई पंक्ति", "न्यू लाइन")
_PARAGRAPH = _f("new paragraph", "next paragraph", "नया पैराग्राफ", "नया पैरा", "नया पैराग्राफ", "न्यू पैराग्राफ",
                "अगला पैराग्राफ")
_ENTER = _f("press enter", "hit enter", "press return", "एंटर दबाओ", "एंटर दबाइए", "एंटर प्रेस करो")
_DELETE = _f("delete that", "scratch that", "undo that", "erase that", "remove that", "delete this", "scratch this",
             "undo", "यह हटा दो", "ये हटा दो", "इसे हटा दो", "वो हटा दो", "वह हटा दो", "यह मिटा दो", "इसे मिटा दो",
             "वो मिटा दो", "यह डिलीट करो", "इसे डिलीट करो")

# spoken punctuation -> (character typed)
_PUNCT: Dict[str, str] = {}
for _words, _ch in (
        (("comma", "कॉमा", "अल्पविराम", "अल्प विराम"), ","),
        (("full stop", "period", "fullstop", "पूर्ण विराम", "पूर्णविराम", "फुल स्टॉप", "फुलस्टॉप"), None),   # None = language default
        (("question mark", "प्रश्न चिह्न", "प्रश्नचिह्न", "प्रश्न चिन्ह", "प्रश्नचिन्ह", "क्वेश्चन मार्क"), "?"),
        (("exclamation mark", "exclamation point", "विस्मयादिबोधक चिह्न", "विस्मयादिबोधक चिन्ह", "एक्सक्लेमेशन मार्क"), "!"),
        (("colon", "कोलन"), ":"),
        (("semicolon", "semi colon", "सेमीकोलन", "सेमी कोलन"), ";")):
    for _w in _words:
        _PUNCT[lang.normalize_hindi(_w)] = _ch if _ch is not None else ("।" if lang.has_devanagari(_w) else ".")
_PUNCT_MAX_WORDS = max(len(k.split()) for k in _PUNCT)

_DROP = {0x0901, 0x0902, 0x093C, 0x200C, 0x200D}          # chandrabindu, anusvara, nukta, ZWNJ, ZWJ


def _dropc(text: str) -> str:
    """Text without the Hindi combining marks that Whisper spells inconsistently (keeps case and length of words)."""
    return "".join(ch for ch in unicodedata.normalize("NFC", text) if ord(ch) not in _DROP)


# ------------------------------------------------------------------ start / one-shot phrases

_START = _f(
    "start typing", "start dictation", "dictation mode", "type what i say", "start dictating", "begin dictation",
    "begin typing", "turn on dictation", "enable dictation", "start typing what i say", "type as i speak",
    "type what i'm saying", "type what im saying", "type what i speak", "take dictation", "dictation on",
    "typing mode", "start the dictation", "let's dictate", "lets dictate", "type what i say please",
    "टाइपिंग शुरू करो", "डिक्टेशन शुरू करो", "टाइपिंग शुरु करो", "डिक्टेशन शुरु करो", "जो मैं बोलूँ वो टाइप करो",
    "जो मैं बोलूं वो टाइप करो", "जो मैं बोलूँ वह टाइप करो", "जो मैं बोलूँ उसे टाइप करो", "जो मैं बोलूं उसे टाइप करो",
    "जो मैं बोलूं वह टाइप करो", "जो मैं बोलता हूँ वो टाइप करो", "जो मैं बोलता हूं वो टाइप करो",
    "जो मैं बोलूँ टाइप करो", "जो मैं बोलूं टाइप करो", "जो बोलूँ वो टाइप करो", "जो बोलूं वो टाइप करो",
    "टाइपिंग मोड", "डिक्टेशन मोड", "टाइपिंग मोड चालू करो", "डिक्टेशन मोड चालू करो", "टाइप करना शुरू करो",
    "टाइप करना शुरु करो", "डिक्टेशन चालू करो", "टाइपिंग चालू करो", "टाइपिंग शुरू करें", "डिक्टेशन शुरू करें")

_ONESHOT_EN = re.compile(
    r"^type(?![a-z])(?: out)?(?: for me)?(?: (?:this|the following|the words|the text))?\s*[:,;\-]?\s*(?P<c>.+)$",
    re.S | re.I)
_EN_FILLER = re.compile(
    r"^(?:(?:hey|hi|ok|okay|so|please|kindly|raziel|and)[, ]+|(?:can|could|would|will) you (?:please )?|"
    r"(?:i want|i'd like|i need) you to |go ahead and )+", re.I)
_HI_FILLER = re.compile(r"^(?:(?:कृपया|प्लीज़|प्लीज|ज़रा|जरा|रज़ील|अब)[, ]+|क्या आप )+")
_ONESHOT_HI = re.compile(_dropc(
    r"^(?:(?:यह|ये|इसे|यह टेक्स्ट|ये टेक्स्ट) )?टाइप (?:करो|कर दो|करें|कीजिए|कीजिये|कर दीजिए)\s*[:,;]?\s*(?P<c>.+)$"), re.S)
_ONESHOT_HI_SUFFIX = re.compile(_dropc(
    r"^(?P<c>.+?)\s+टाइप (?:करो|कर दो|कर दीजिए|करें|कीजिए|कीजिये)$"), re.S)
_PRONOUN_CONTENT = {"it", "that", "this", "them", "these", "those", "something", "इसे", "उसे", "यह", "वो", "वह", "ये",
                    "इसको", "उसको"}
_PRONOUN_CONTENT_F = {_dropc(p) for p in _PRONOUN_CONTENT}
# "type it out", "write that down": a pronoun plus a particle is still not text to type
_PRONOUN_PARTICLE = re.compile(r"^(?:it|that|this|them|these|those)\s+(?:out|down|up|in|now|again|here|for me)$|^(?:out|down|up)$", re.I)
# "type of car", "type in the search box": an explanation, not text to type
_NOT_TEXT_START = re.compile(
    r"^(?:\d+ diabetes\b|(?:of|in|on|at|into|onto|with|using|about|for|to|as)\b|(?:an? |the |my |some )?(?:message|e-?mail|note|letter|"
    r"reply|response|comment|post|summary|report|essay|paragraph|story|poem|caption|password)\b)", re.I)


# ------------------------------------------------------------------ the typing helpers

def _push_chunk(n: int, space_after: bool = True, took_space: bool = False) -> None:
    if n <= 0:
        return
    _chunks.append((n, space_after, took_space))
    del _chunks[:-MAX_UNDO_CHUNKS]


def _type_chunk(text: str) -> None:
    """Types one dictated chunk plus its trailing space and records it for 'delete that'."""
    global _after_space
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return
    n = _type(text + " ")
    _push_chunk(n)
    _after_space = True


def _type_punct(ch: str) -> None:
    """Spoken punctuation: no space before it (the previous chunk's trailing space is taken back), one after."""
    global _after_space
    took = False
    if _after_space and _chunks:
        _backspace(1)
        n0, sp, tk = _chunks[-1]
        _chunks[-1] = (n0 - 1, sp, tk)
        if _chunks[-1][0] <= 0:
            _chunks.pop()
        took = True
    elif _after_space:
        _backspace(1)
        took = True
    _after_space = False
    n = _type(ch + " ")
    _push_chunk(n, True, took)
    _after_space = True


def _undo_last() -> bool:
    """'delete that': Backspace the whole last chunk. False if nothing was typed."""
    global _after_space
    if not _chunks:
        return False
    n, _space, took = _chunks.pop()
    _backspace(n)
    _after_space = _chunks[-1][1] if _chunks else False
    if took:                                                # the punctuation had eaten a space: give it back
        _type(" ")
        if _chunks:
            n0, sp, tk = _chunks[-1]
            _chunks[-1] = (n0 + 1, sp, tk)
        _after_space = True
    return True


def _spoken_mark(text: str) -> Optional[str]:
    """The mark for an utterance that is nothing but a spoken mark ("comma", "Full stop."), else None."""
    return _PUNCT.get(" ".join(_tokens(text)))


# "for a period", "cancer of the colon": the word is a noun there, not a spoken mark
_NOT_BEFORE_MARK = frozenset({"the", "a", "an", "this", "that", "my", "your", "his", "her", "its", "our", "their",
                              "of", "for", "to", "in", "on", "at", "by", "with", "every", "each", "one", "same"})


def _split_trailing_punct(text: str):
    """('see you tomorrow', '.') for 'see you tomorrow full stop'; None if the text does not end in a spoken mark."""
    words = text.split()
    for k in range(min(_PUNCT_MAX_WORDS, len(words) - 1), 0, -1):
        tail = lang.normalize_hindi(" ".join(words[-k:]))
        if tail in _PUNCT:
            body = " ".join(words[:-k]).rstrip()
            body = re.sub(r"[,.;:!?।]+$", "", body).rstrip()
            if body and body.split()[-1].lower() not in _NOT_BEFORE_MARK:
                return body, _PUNCT[tail]
    return None


# ------------------------------------------------------------------ handle() - called first for every transcript

def handle(transcript: str, now: Optional[float] = None) -> Optional[str]:
    """
    None while dictation is off. While it is on the transcript is consumed: "" when it was typed / executed as a
    command (nothing to speak), otherwise the sentence to speak ("Dictation off.", "Dictation timed out.", an error).
    `now` (seconds, monotonic) is for tests; it defaults to the real clock.
    """
    if not _active:                               # cheap unlocked read: the common case costs nothing
        return None
    try:
        return _handle_active(transcript, _now() if now is None else now)
    except Exception:
        logger.exception("dictation: handle failed")
        return _tr("sorry")


def _handle_active(transcript: str, now: float) -> Optional[str]:
    global _last_activity, _after_space
    with _lock:
        if not _active:
            return None
        timeout = _idle_timeout()
        if timeout > 0 and now - _last_activity > timeout:
            _stop_locked()
            logger.info("dictation: idle for %.0f s - switched off, transcript not typed", now - _last_activity)
            return _tr("timeout")
        _last_activity = now
        raw = (transcript or "").strip()
        core = _plain(raw)                                  # edit commands are matched on the plain utterance
        if not core:
            return ""                                       # no words at all (silence, "...")
        if _core(raw) in _STOP:
            _stop_locked()
            logger.info("dictation: stop phrase %r", raw)
            return _tr("off")
        if _core(raw) in _START:                            # "start typing" while it is already on: never typed
            logger.info("dictation: start phrase while already on")
            return _tr("already_on")
        try:
            if core in _NEWLINE:
                _enter(1)
                _push_chunk(1, False)
                _after_space = False
                logger.info("dictation: new line")
                return ""
            if core in _PARAGRAPH:
                _enter(2)
                _push_chunk(2, False)
                _after_space = False
                logger.info("dictation: new paragraph")
                return ""
            if core in _ENTER:
                _enter(1)
                _push_chunk(1, False)
                _after_space = False
                logger.info("dictation: enter")
                return ""
            if core in _DELETE:
                logger.info("dictation: deleted the last chunk" if _undo_last() else "dictation: nothing to delete")
                return ""
            if _spoken_mark(raw) is not None:              # the whole utterance is a mark
                ch = _spoken_mark(raw)
                _type_punct(ch)
                logger.info("dictation: punctuation %r", ch)
                return ""
            split = _split_trailing_punct(raw)
            if split:
                body, ch = split
                _type_chunk(body)
                _type_punct(ch)
                logger.info("dictation: typed %d chars + punctuation %r", len(body), ch)
                return ""
            _type_chunk(raw)
            logger.info("dictation: typed %d chars", len(raw))
            return ""
        except OSError as e:
            logger.warning("dictation: typing failed (%s) - switching dictation off", e)
            _stop_locked()
            return _err_sentence(e, dictation=True)


def _stop_locked() -> None:
    global _active
    _active = False
    _chunks.clear()
    logger.info("dictation: OFF")


# ------------------------------------------------------------------ one-shot typing

def _self_check() -> Optional[str]:
    """None if it is fine to type; else the sentence to speak (Raziel has the focus / not Windows)."""
    try:
        fg = _foreground()
    except OSError as e:
        return _err_sentence(e, dictation=False)
    if _looks_like_self(fg or {}):
        return _tr("self_front")
    return None


def type_now(text: str) -> str:
    """Types `text` once into the focused window (used by 'type hello world'). Returns the sentence to speak."""
    text = (str(text) if text is not None else "").strip()
    if not text:
        return _tr("need_text")
    problem = _self_check()
    if problem:
        return problem
    try:
        with _lock:
            _type(text)
    except OSError as e:
        logger.warning("dictation: one-shot typing failed: %s", e)
        return _err_sentence(e, dictation=False)
    logger.info("dictation: one-shot typed %d chars", len(text))
    return _tr("typed")


def _start_dictation() -> str:
    if is_active():
        return _tr("already_on")
    problem = _self_check()
    if problem:
        logger.info("dictation: not started (%s)", problem)
        return problem
    start()
    return _tr("on")


# ------------------------------------------------------------------ try_handle()

def _clean_transcript(transcript) -> Optional[str]:
    if not isinstance(transcript, str):
        return None
    t = transcript.replace("’", "'").strip()
    return t if t and len(t) <= 1500 else None


def try_handle(transcript: str) -> Optional[str]:
    """Start phrases and the one-shot 'type ...'. (While dictation is on, main.py's handle() consumes everything
    before this is reached.)"""
    try:
        t = _clean_transcript(transcript)
        if t is None:
            return None
        core = _core(t)
        if not core:
            return None
        if core in _START:
            logger.info("dictation: start phrase %r", t)
            return _start_dictation()
        if core in _STOP_EXPLICIT:
            logger.info("dictation: stop phrase %r while off", t)
            return _tr("off") if stop() else _tr("already_off")
        content = _oneshot_content(t)
        if content is None:
            return None
        logger.info("dictation: one-shot type %r", content[:40])
    except Exception:
        logger.exception("dictation: matching failed")
        return None
    try:
        return type_now(content)
    except Exception:
        logger.exception("dictation: one-shot failed")
        return _tr("sorry")


def _oneshot_content(t: str) -> Optional[str]:
    """The text to type for 'type hello world', 'type this: see you at 5', 'टाइप करो: नमस्ते'; None if it isn't one."""
    s = _HI_FILLER.sub("", _EN_FILLER.sub("", t)).strip()
    mt = _ONESHOT_EN.match(s)
    if mt:
        c = re.sub(r"(?i)\s+(?:please|for me)$", "", mt.group("c").strip()).strip()
        if (not c or c.lower().strip(".,!?") in _PRONOUN_CONTENT or _PRONOUN_PARTICLE.match(c.strip(".,!? "))
                or _NOT_TEXT_START.match(c)):
            return None
        return c
    if not lang.has_devanagari(s):
        return None
    fs = _dropc(s)
    mt = _ONESHOT_HI.match(fs)
    if mt:
        c = _last_words(s, len(mt.group("c").split()))
        return c if c and _dropc(c).strip(".,!?।") not in _PRONOUN_CONTENT_F else None
    mt = _ONESHOT_HI_SUFFIX.match(fs)
    if mt:
        c = _first_words(s, len(mt.group("c").split()))
        if c and _dropc(c).strip(".,!?।") not in _PRONOUN_CONTENT_F and len(c.split()) <= 60:
            return c
    return None


def _last_words(orig: str, k: int) -> str:
    """The last k words of `orig` (the folded copy has the same words, so counts line up)."""
    words = orig.split()
    return " ".join(words[-k:]).strip() if 0 < k <= len(words) else ""


def _first_words(orig: str, k: int) -> str:
    words = orig.split()
    return " ".join(words[:k]).strip() if 0 < k <= len(words) else ""


# ------------------------------------------------------------------ tool function

def dictation_control(action: str = "", text: str = "", **_ignored) -> str:
    """
    LLM tool: action = start | stop | type. `text` is what to type for action 'type'.
    Returns the sentence to speak.
    """
    try:
        act = re.sub(r"[\s\-]+", "_", str(action or "").strip().lower())
        act = {"on": "start", "begin": "start", "start_dictation": "start", "start_typing": "start", "enable": "start",
               "off": "stop", "end": "stop", "stop_dictation": "stop", "stop_typing": "stop", "disable": "stop",
               "write": "type", "type_text": "type", "type_now": "type"}.get(act, act)
        logger.info("dictation tool: %s", act)
        if act == "start":
            return _start_dictation()
        if act == "stop":
            return _tr("off") if stop() else _tr("already_off")
        if act == "type":
            return type_now(text)
        return _tr("unknown_action")
    except Exception:
        logger.exception("dictation tool failed")
        return _tr("sorry")
