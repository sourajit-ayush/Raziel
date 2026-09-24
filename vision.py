"""
vision.py - "what's on my screen?"  Raziel looks at the screen with a LOCAL vision model.

The screen is captured in memory (pyautogui, with PIL.ImageGrab as fallback), downscaled,
sent as PNG bytes to a vision model running in Ollama on this computer, and the model's
short answer is spoken. Nothing is written to disk and nothing leaves the machine.

Speed: on the user's CPU a 4B vision model needs roughly 15-60 s, so the caller may want to
speak looking_phrase() first (match() tells it whether the utterance is a screen request
without doing any work). keep_alive=0 unloads the vision model right after the answer, so the
main language model (qwen3:8b) keeps the RAM.

Public API
----------
  try_handle(transcript)        matcher for the utterances listed in match(); returns the
                                sentence to speak or None (nothing done)
  match(transcript)             pure: (kind, question) or None; no I/O
  describe_screen(question="")  the LLM tool function
  capture_screen()              PNG bytes of the (downscaled) screen
  available_model()             first installed vision model or None (for a start-up log line)
  looking_phrase()              a short "one moment" line in the current language

Settings (all optional, read with getattr(config, ...)):
  VISION_ENABLED=True, VISION_MODEL="qwen3.5:4b",
  VISION_FALLBACK_MODELS=["qwen3-vl:4b", "qwen2.5vl:3b", "gemma3:4b"],
  VISION_KEEP_ALIVE=0, VISION_MAX_WIDTH=1280, VISION_TIMEOUT=120

Everything with a side effect (_client, _grab_image, capture_screen) is a small module-level
function that tests replace.
"""

from __future__ import annotations

import io
import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import config
import lang

logger = logging.getLogger("voice_assistant")

DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_FALLBACK_MODELS = ("qwen3-vl:4b", "qwen2.5vl:3b", "gemma3:4b")
DEFAULT_QUESTION = ("Describe what is on this screen in two or three short sentences, "
                    "and read out any important text.")
# Lowered from 300 (2026-09-22): a real run showed this model writing at ~5 tokens/second on
# slower hardware, so the old 300-token ceiling alone could mean 60s of generation on top of
# whatever the model took to load. The prompt already asks for "at most 4 short spoken
# sentences" (see _INSTRUCTIONS below), which realistically needs well under 150 tokens - this
# just makes the worst case match what we actually want spoken, instead of what the model
# would ramble on to if left uncapped. If answers start getting cut off before finishing a
# sentence, raise this back up a bit.
NUM_PREDICT = 150
MAX_REPLY_SENTENCES = 5
MAX_REPLY_CHARS = 700
MAX_QUESTION_CHARS = 500

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "describe_screen",
        "description": ("Look at the user's current screen with a local vision model and answer a question "
                        "about it: describe it, read the text on it, explain an error message, summarize the "
                        "page or window. Use for 'what's on my screen', 'what am I looking at', "
                        "'explain this error'. Takes 15-60 seconds."),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "What the user wants to know about the screen. Empty = describe it.",
                }
            },
            "required": [],
        },
    },
}

_T: Dict[str, Dict[str, str]] = {
    "off": {"en": "Screen reading is turned off.",
            "hi": "स्क्रीन देखने की सुविधा बंद है।"},
    "need_model": {"en": "I need a vision model for that. Run: ollama pull {model}",
                   "hi": "इसके लिए एक विज़न मॉडल चाहिए। यह कमांड चलाइए: ollama pull {model}"},
    "down": {"en": "I can't reach Ollama right now, so I can't look at the screen. Is it running?",
             "hi": "अभी ओलामा से संपर्क नहीं हो पा रहा है, इसलिए स्क्रीन नहीं देखी जा सकती। क्या वह चालू है?"},
    "capture": {"en": "I couldn't capture the screen just now.",
                "hi": "अभी स्क्रीन कैप्चर नहीं हो पाई।"},
    "timeout": {"en": "Looking at the screen is taking too long. The vision model may be too heavy for this computer.",
                "hi": "स्क्रीन देखने में बहुत समय लग रहा है। हो सकता है यह विज़न मॉडल इस कंप्यूटर के लिए भारी हो।"},
    "memory": {"en": "The vision model needs more memory than is free right now. Try closing some programs.",
               "hi": "विज़न मॉडल को जितनी मेमोरी चाहिए उतनी अभी खाली नहीं है। कुछ प्रोग्राम बंद करके देखिए।"},
    "failed": {"en": "I couldn't read the screen just now. The vision model didn't answer.",
               "hi": "अभी स्क्रीन नहीं पढ़ी जा सकी। विज़न मॉडल ने जवाब नहीं दिया।"},
    "empty": {"en": "I looked at the screen but couldn't tell what is on it.",
              "hi": "स्क्रीन देखी, लेकिन समझ नहीं आया कि उस पर क्या है।"},
    "looking": {"en": "Let me take a look.",
                "hi": "एक पल, स्क्रीन देखते हैं।"},
}


def _t(key: str, **fmt) -> str:
    return lang.tr(_T, key, **fmt)


def looking_phrase() -> str:
    """A short 'one moment' line the caller can speak before the 15-60 s wait."""
    return _t("looking")


# ------------------------------------------------------------------ settings

def _cfg(name: str, default):
    return getattr(config, name, default)


def _cfg_number(name: str, default, kind=float):
    try:
        return kind(_cfg(name, default))
    except (TypeError, ValueError):
        return kind(default)


def _candidates() -> List[str]:
    """VISION_MODEL first, then the fallbacks, without duplicates."""
    fallbacks = _cfg("VISION_FALLBACK_MODELS", DEFAULT_FALLBACK_MODELS)
    if isinstance(fallbacks, str):
        fallbacks = [fallbacks]
    out: List[str] = []
    for name in [_cfg("VISION_MODEL", DEFAULT_MODEL)] + list(fallbacks or []):
        name = str(name or "").strip()
        if name and name.lower() not in [o.lower() for o in out]:
            out.append(name)
    return out or [DEFAULT_MODEL]


# ------------------------------------------------------------------ the Ollama client

def _client():
    """The Ollama client (tests replace this). timeout = VISION_TIMEOUT if the client supports it."""
    import ollama

    host = _cfg("OLLAMA_HOST", None)
    timeout = _cfg_number("VISION_TIMEOUT", 120.0)
    kwargs = {}
    if host:
        kwargs["host"] = host
    try:
        return ollama.Client(timeout=timeout, **kwargs)
    except TypeError:
        logger.debug("Vision: this ollama client has no timeout parameter")
        return ollama.Client(**kwargs)


def _installed_names(client) -> Dict[str, str]:
    """{lower-case name: name} of the installed models. Handles the old dict shape
    {"models": [{"name": ..}]} and the new object shape .models[i].model."""
    resp = client.list()
    models = resp.get("models") if isinstance(resp, dict) else getattr(resp, "models", None)
    names: Dict[str, str] = {}
    for m in models or []:
        if isinstance(m, dict):
            name = m.get("model") or m.get("name")
        else:
            name = getattr(m, "model", None) or getattr(m, "name", None)
        if name:
            names[str(name).strip().lower()] = str(name).strip()
    return names


def _match_installed(candidate: str, names: Dict[str, str]) -> Optional[str]:
    """Exact tag, or the same name with ':latest' added / removed."""
    c = candidate.strip().lower()
    if c in names:
        return names[c]
    if ":" not in c and c + ":latest" in names:
        return names[c + ":latest"]
    if c.endswith(":latest") and c[:-7] in names:
        return names[c[:-7]]
    return None


def _find_model(client) -> Optional[str]:
    """First installed candidate, or None. Raises if Ollama can't be asked."""
    names = _installed_names(client)
    for cand in _candidates():
        found = _match_installed(cand, names)
        if found:
            return found
    return None


def available_model() -> Optional[str]:
    """The vision model that would be used (installed), or None. Never raises."""
    try:
        return _find_model(_client())
    except Exception as e:
        logger.debug("Vision: could not list Ollama models: %s", e)
        return None


# ------------------------------------------------------------------ capturing the screen

def _scaled_size(width: int, height: int, max_width: int) -> Tuple[int, int]:
    if not max_width or max_width <= 0 or width <= max_width:
        return width, height
    return max_width, max(1, int(round(height * max_width / width)))


def _grab_image():
    """A PIL image of the whole screen: pyautogui first, PIL.ImageGrab as the fallback."""
    try:
        import pyautogui
        return pyautogui.screenshot()
    except Exception as e:
        logger.debug("Vision: pyautogui screenshot failed (%s); trying PIL.ImageGrab", e)
    from PIL import ImageGrab
    return ImageGrab.grab()


def _downscale(img, max_width: int):
    width, height = img.size
    size = _scaled_size(width, height, max_width)
    if getattr(img, "mode", "RGB") not in ("RGB", "L"):
        img = img.convert("RGB")
    if size != (width, height):
        try:
            from PIL import Image
            resample = getattr(getattr(Image, "Resampling", Image), "LANCZOS", None)
        except ImportError:
            resample = None
        img = img.resize(size, resample) if resample is not None else img.resize(size)
    return img


def capture_screen() -> bytes:
    """PNG bytes of the screen, at most VISION_MAX_WIDTH wide. Kept in memory, never saved to disk."""
    img = _downscale(_grab_image(), _cfg_number("VISION_MAX_WIDTH", 1280, int))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ asking the model

_INSTRUCTIONS = (
    "You are the voice of a desktop assistant looking at the user's computer screen (the attached "
    "image). Answer the request below in at most 4 short spoken sentences. Plain text only: no "
    "markdown, no lists, no bullet points, no headings, no emoji. Speak as if you can see the screen; "
    "do not say 'the image' or 'the screenshot'. Read out important text such as titles, error "
    "messages and names exactly as written."
)
_HINDI_INSTRUCTION = ("Answer in natural spoken Hindi in Devanagari script. Keep file names, program names "
                      "and error codes as they appear on the screen.")


def _build_prompt(question: str) -> str:
    parts = [_INSTRUCTIONS]
    if lang.current() == "hi":
        parts.append(_HINDI_INSTRUCTION)
    parts.append("Request: " + ((question or "").strip() or DEFAULT_QUESTION))
    return "\n".join(parts)


def _chat(client, model: str, prompt: str, png: bytes):
    kwargs = dict(
        model=model,
        messages=[{"role": "user", "content": prompt, "images": [png]}],
        keep_alive=_cfg("VISION_KEEP_ALIVE", 0),
        options={"num_predict": NUM_PREDICT},
    )
    try:
        return client.chat(think=False, **kwargs)
    except Exception as e:
        # an older ollama package without `think` (TypeError), or a model/server that refuses the
        # flag: ask again without it. Any other failure is the caller's to classify.
        if "think" not in str(e).lower():
            raise
        logger.info("Vision: retrying without think=False (%s)", e)
        return client.chat(**kwargs)


def _g(obj, key, default=None):
    """Reads a field from an Ollama response, whether it is a dict or a model object."""
    if obj is None:
        return default
    try:
        value = obj.get(key, default)
    except AttributeError:
        value = getattr(obj, key, default)
    except Exception:
        value = default
    return default if value is None else value


def _log_timing(resp, model: str, started: float) -> None:
    """Ollama's own load-vs-generate breakdown, so a slow answer has an explanation in
    assistant.log instead of just a wall-clock number (mirrors llm_brain's LLM timing line)."""
    try:
        ns = 1e9
        load = _g(resp, "load_duration", 0) / ns
        e_tok = int(_g(resp, "eval_count", 0))
        e_dur = _g(resp, "eval_duration", 0) / ns
        total = _g(resp, "total_duration", 0) / ns
        if not total:
            total = time.monotonic() - started
        rate = f"{e_tok / e_dur:.0f} tok/s" if e_dur > 0 else "n/a"
        logger.info(
            "Vision: %s answered in %.1f s (model load %.1fs | wrote %d tok in %.1fs, %s)",
            model, total, load, e_tok, e_dur, rate,
        )
        if load > 2.0:
            logger.info("Vision: the model had to be loaded into memory for that request "
                        "(%.1fs) - raising VISION_KEEP_ALIVE avoids this on a quick follow-up "
                        "question, at the cost of keeping it in memory longer.", load)
    except Exception as e:                      # logging must never break a turn
        logger.debug("couldn't log vision timing: %s", e)


def _reply_text(resp) -> str:
    msg = resp.get("message") if isinstance(resp, dict) else getattr(resp, "message", None)
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
    return str(content or "")


_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_BULLET_RE = re.compile(r"^\s*(?:[-*•▪>]+|\d{1,2}[.)])\s+", re.M)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+")


def _clean_reply(text: str) -> str:
    """Model output -> plain speakable text: no think blocks, markdown, list markers or URLs,
    at most MAX_REPLY_SENTENCES sentences."""
    text = _THINK_RE.sub(" ", text or "")
    text = re.sub(r"</?think>", " ", text, flags=re.I)
    text = text.replace("```", " ")
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    text = _EMOJI_RE.sub("", text)
    text = text.replace("|", " ")
    text = _BULLET_RE.sub("", text)
    text = re.sub(r"[*#`]+", "", text)
    text = re.sub(r"__+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    sentences = _SENTENCE_SPLIT_RE.split(text)
    if len(sentences) > 1 and sentences[-1][-1:] not in ".!?।":
        sentences = sentences[:-1]              # cut off by num_predict: drop the dangling half
    out: List[str] = []
    total = 0
    for s in sentences:
        if out and (len(out) >= MAX_REPLY_SENTENCES or total + len(s) > MAX_REPLY_CHARS):
            break
        out.append(s)
        total += len(s) + 1
    return " ".join(out)


def _classify_error(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "connect" in name or "refused" in msg or "10061" in msg or "failed to connect" in msg:
        return "down"
    if "timeout" in name or "timed out" in msg:
        return "timeout"
    if getattr(exc, "status_code", None) == 404 or ("model" in msg and "not found" in msg):
        return "missing"
    if "memory" in msg:
        return "memory"
    return "failed"


def describe_screen(question: str = "", **_ignored) -> str:
    """Capture the screen and answer `question` about it with the local vision model (default:
    describe it). Returns the sentence(s) to speak; never raises."""
    try:
        return _describe(str(question or "").strip()[:MAX_QUESTION_CHARS])
    except Exception as e:                      # last line of defence: never a traceback
        logger.warning("Vision: unexpected failure: %s", e, exc_info=True)
        return _t("failed")


def _describe(question: str) -> str:
    if not _cfg("VISION_ENABLED", True):
        logger.info("Vision: disabled in settings")
        return _t("off")

    try:
        client = _client()
        model = _find_model(client)
    except Exception as e:
        logger.warning("Vision: cannot reach Ollama to list models: %s", e)
        return _t("down")
    if not model:
        logger.warning("Vision: no vision model installed (looked for %s)", ", ".join(_candidates()))
        return _t("need_model", model=_candidates()[0])

    try:
        png = capture_screen()
    except Exception as e:
        logger.warning("Vision: could not capture the screen: %s", e)
        return _t("capture")

    prompt = _build_prompt(question)
    logger.info("Vision: asking %s about the screen (%d KB image, lang=%s): %s",
                model, len(png) // 1024, lang.current(), question or "(describe)")
    started = time.monotonic()
    try:
        resp = _chat(client, model, prompt, png)
    except Exception as e:
        kind = _classify_error(e)
        logger.warning("Vision: %s failed after %.1f s (%s): %s", model, time.monotonic() - started,
                       kind, e)
        if kind == "missing":
            return _t("need_model", model=model)
        return _t(kind)
    _log_timing(resp, model, started)

    answer = _clean_reply(_reply_text(resp))
    if not answer:
        logger.warning("Vision: %s returned an empty answer", model)
        return _t("empty")
    return answer


# ------------------------------------------------------------------ the matcher

def _c(pattern: str):
    """Compile a pattern with its Devanagari parts folded the way transcripts are folded
    (chandrabindu, anusvara and nukta dropped), so 'मैं' / 'मै' and 'ज़' / 'ज' spellings meet.
    ASCII (regex syntax and English words) is left alone."""
    return re.compile(re.sub(r"[^\x00-\x7f]+", lambda m: lang.fold_hindi(m.group()), pattern))


_LEAD_RE = _c(
    r"^(?:raziel|razil|rajil|hey|hi|hello|yo|ok|okay|so|well|please|kindly|now|actually|and|also|"
    r"can you|could you|would you|will you|can u|i want you to|i need you to|i would like you to|"
    r"i'd like you to|tell me|let me know|i want to know|i wanna know|"
    r"कृपया|ज़रा|जरा|प्लीज़|प्लीज|रज़ील|रजील|राजील|रेजील|अरे|सुनो|ओके|अच्छा|तो|मुझे बताओ|बताओ|बताइए|बताएं)\s+")
_TAIL_RE = _c(
    r"(?:\s+(?:please|for me|right now|now|thanks|thank you|raziel|razil|at the moment|currently|sir|"
    r"कृपया|प्लीज़|प्लीज|ज़रा|जरा|जी|अभी|मुझे बताओ|बताओ|बताइए|बताएं))+$")


def _normalize(transcript: str, tail: bool = True) -> str:
    text = lang.normalize_hindi(str(transcript or ""))
    for _ in range(5):
        before = text
        text = _LEAD_RE.sub("", text, count=1).strip()
        if tail:
            text = _TAIL_RE.sub("", text, count=1).strip()
        if text == before:
            break
    return text


# --- English building blocks
_WHATS = r"what(?:'s| is|s)"
_SCR = r"(?:(?:my|the|this) )?(?:(?:computer|laptop|pc) )?(?:screen|display|monitor)"
_ERR = (r"(?:error|error message|error popup|error pop up|error dialog|error box|error code|"
        r"warning|warning message|exception)")
_PAGE = (r"(?:page|web page|webpage|window|article|screen|tab|document|website|site|post|blog post|"
         r"story|text|paragraph|thread|pdf)")

# what may follow "look at my screen": a request, not more nouns ("look at my screen time report")
_Q_START = (r"tell|what|read|explain|describe|summari[sz]e|is|are|does|do|why|how|where|which|who|help|find|"
            r"see|check|let|say|for|to|please|kindly|and|then")

# --- Hindi building blocks (written in plain Devanagari; _c() folds them)
_H_SCR = (r"(?:(?:मेरी|मेरे|इस|इसी|उस|पूरी|अपनी|कंप्यूटर की|लैपटॉप की) )*"
          r"(?:स्क्रीन|स्क्रिन|डिस्प्ले|मॉनिटर|screen)")
_H_ON = r"(?:पर|पे|में|मे)"
_H_TELL = r"(?: (?:मुझे )?(?:बताओ|बताइए|बताएं|बताना|बता दो|बता दीजिए|सुनाओ|सुनाइए|समझाओ|समझाइए))?"
_H_ERR = (r"(?:एरर मैसेज|एरर संदेश|एरर|इरर|त्रुटि संदेश|त्रुटि|वॉर्निंग|वार्निंग|चेतावनी|error message|error)")
_H_PAGE = (r"(?:पेज|वेबपेज|वेब पेज|पन्ने|पन्ना|विंडो|आर्टिकल|लेख|टैब|साइट|वेबसाइट|डॉक्यूमेंट|दस्तावेज़|"
           r"पोस्ट|खबर|न्यूज़ आर्टिकल|page|article|window)")
_H_READ = (r"(?:पढ़कर सुनाओ|पढ़कर सुनाइए|पढ़कर बताओ|पढ़कर बताइए|पढ़ कर सुनाओ|पढ़ो|पढ़िए|पढ़ दो|"
           r"सुनाओ|सुनाइए|बताओ|बताइए)")

# (kind, compiled pattern). Order matters: the first full match wins. All patterns are anchored
# with fullmatch, so a sentence that merely CONTAINS these words never matches.
_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [(k, _c(p)) for k, p in [
    # ---- describe
    ("describe", rf"{_WHATS} (?:(?:currently|now|showing|displayed|happening|going on|open|visible) )*"
                 rf"(?:on|in) {_SCR}"),
    ("describe", rf"describe (?:(?:what(?:'s| is|s)|what you see|what is showing) (?:on|in) )?{_SCR}(?: to me)?"),
    ("describe", rf"what (?:(?:do|can|did) )?you see (?:on|in) {_SCR}"),
    ("describe", rf"(?:do you )?see {_SCR}"),
    ("describe", r"what (?:am i|are we) looking at(?: here| right now)?"),
    ("describe", r"describe (?:this|the|that) (?:page|window|tab|app|website)"),
    # ---- read
    ("read", rf"read (?:out )?(?:to me )?(?:{_WHATS} (?:on|in) )?{_SCR}(?: to me| out loud| aloud| out)*"),
    ("read", rf"read (?:out )?(?:the )?(?:text|words|content) (?:on|in|from) {_SCR}(?: to me| out loud| aloud)*"),
    ("read", r"read this(?: out)?(?: to me)?(?: out loud| aloud)?"),
    ("read", r"read (?:this|that) (?:page|text|window|article|paragraph|section|screen)"
             r"(?: out)?(?: to me)?(?: out loud| aloud)?"),
    ("read", r"what does (?:this|that) (?:say|read)(?: here| there)?(?: on (?:my |the )?screen)?"),
    ("read", rf"{_WHATS} written (?:here|there|on this|on {_SCR})"),
    # ---- error
    ("error", rf"{_WHATS} (?:this|that) {_ERR}(?: on {_SCR})?(?: here)?"),
    ("error", rf"{_WHATS} the {_ERR} on {_SCR}"),
    ("error", rf"about (?:this|that) {_ERR}"),
    ("error", rf"(?:explain|describe|read|decode|help me understand|help me with|help me fix|"
              rf"what does) (?:this|that|the) {_ERR}(?: (?:mean|means|say|says))?(?: on {_SCR})?(?: to me)?"),
    ("error", rf"(?:explain|read|describe) (?:the )?{_ERR} (?:on|in) {_SCR}(?: to me)?"),
    ("error", rf"why (?:am i getting|do i get|did i get|is there) (?:this|that|an?) {_ERR}"),
    # ---- summary
    ("summary", rf"(?:summari[sz]e|sum up|recap|give me a (?:quick )?summary of|give me the (?:gist|summary) of|"
                rf"summary of|gist of|tldr) (?:this|the|that|my current|the current|current) {_PAGE}"
                rf"(?: to me| for me)?"),
    ("summary", r"(?:summari[sz]e|sum up) this"),
    ("summary", rf"{_WHATS} (?:this|that) {_PAGE} (?:about|saying|say)"),
    ("summary", rf"{_WHATS} (?:on|in) (?:this|the|that) {_PAGE}"),
    # ---- free-form: "look at my screen and tell me ..."
    ("free", rf"(?:(?:have|take) a )?(?:look|glance) (?:at|on) {_SCR}(?: and| then)?(?: (?P<q>(?:{_Q_START})\b.*))?"),

    # ---- Hindi: describe
    ("describe", rf"{_H_SCR} {_H_ON} (?:अभी |इस वक्त |इस समय )?क्या(?: क्या)? (?:है|हैं|दिख रहा है|दिख रही है|"
                 rf"दिख रहे हैं|दिखाई दे रहा है|दिखाई दे रही है|चल रहा है|हो रहा है|खुला है|खुले हैं|"
                 rf"खुला हुआ है|दिखता है|आ रहा है){_H_TELL}"),
    ("describe", rf"{_H_SCR} (?:के बारे में|का वर्णन करो|का वर्णन कीजिए|का विवरण दो|का विवरण दीजिए){_H_TELL}"),
    ("describe", r"(?:मैं|हम) क्या देख (?:रहा|रही|रहे) (?:हूँ|हूं|हैं)"),
    ("free", rf"{_H_SCR} (?:को )?(?:देखो|देखकर|देख कर|देखिए|देखिये)(?: और)?(?: (?:बताओ|बताइए))?(?: (?P<q>.+))?"),
    # ---- Hindi: read
    ("read", r"(?:यह|ये|इसमें|इसमे|इस पर|इसपर|इस पे|यहाँ|यहां|वहाँ|वहां|उस पर) क्या "
             r"(?:लिखा है|लिखा हुआ है|लिखा गया है|कहा गया है|लिखा)"),
    ("read", rf"{_H_SCR} {_H_ON} क्या (?:लिखा है|लिखा हुआ है)"),
    ("read", r"(?:यह|ये|इसे|इस) (?:सब )?(?:पढ़कर|पढ़ कर|पढ़ो|पढ़िए|पढ़ के)"
             r"(?: (?:मुझे )?(?:सुनाओ|सुनाइए|सुना दो|बताओ))?"),
    ("read", rf"{_H_SCR} (?:को )?(?:पढ़कर|पढ़ कर|पढ़ो|पढ़िए)(?: (?:मुझे )?(?:सुनाओ|सुनाइए|सुना दो|बताओ))?"),
    ("read", rf"{_H_SCR} {_H_ON} (?:जो )?(?:लिखा है|लिखा हुआ है|लिखा|दिख रहा है)"
            rf"(?: (?:वो|वह|उसे|उसको|यह|ये))? {_H_READ}"),
    # ---- Hindi: error
    ("error", rf"(?:इस|उस|इस वाले|स्क्रीन के|स्क्रीन वाले) {_H_ERR} "
              rf"(?:के बारे में|को|का मतलब|का अर्थ|के मतलब)(?: क्या है)?{_H_TELL}"),
    ("error", rf"(?:यह|ये|वह|यह वाला|ये वाला) {_H_ERR} क्या (?:है|कह रहा है|बोल रहा है|मतलब है|कहता है)"),
    ("error", rf"(?:इस |यह |ये )?{_H_ERR} (?:को )?(?:समझाओ|समझाइए|समझा दो|पढ़ो|पढ़कर सुनाओ)"),
    ("error", rf"{_H_SCR} {_H_ON} (?:जो |ये |यह )?{_H_ERR} (?:आया है|आ रहा है|दिख रहा है|है|आई है)"
              rf"(?: (?:वो|वह|उसे|उसको))?(?: (?:समझाओ|समझाइए|बताओ|बताइए))?"),
    ("error", rf"(?:मुझे )?(?:यह|ये) {_H_ERR} क्यों (?:आ रहा है|आया)"),
    # ---- Hindi: summary
    ("summary", rf"(?:इस|उस|इस पूरे|इस पूरी|इस वाले) {_H_PAGE}(?: (?:का|की|के|को))? "
                rf"(?:सारांश|सार|समरी|संक्षेप)(?: में)?{_H_TELL}"),
    ("summary", rf"(?:इस|उस) {_H_PAGE} (?:के बारे में|में|पर) "
                rf"(?:क्या (?:है|लिखा है|लिखा हुआ है|चल रहा है)|बताओ|समझाओ|बताइए)"),
    ("summary", rf"(?:इसका|इसकी|इसे|इसको) (?:सारांश|सार|समरी)(?: में)?{_H_TELL}"),

    # ---- Hinglish (romanised Hindi)
    ("describe", r"(?:meri |mere |is )?(?:screen|display) (?:par|pe|pr|mein|me) (?:abhi )?kya "
                 r"(?:hai|dikh raha hai|dikh rahi hai|chal raha hai|khula hai|dikhai de raha hai)"
                 r"(?: (?:batao|bataiye|bataao))?"),
    ("read", r"(?:yeh|ye|yah|is par|isme|is pe) kya likha (?:hai|hua hai)"),
    ("summary", r"(?:is|iss) (?:page|article|window|website|tab) (?:ka|ki) (?:summary|saransh)"
                r"(?: (?:batao|bataiye|sunao|do|de do))?"),
    ("error", r"(?:is|iss) error (?:ke baare mein|ke bare mein|ke baare me|ko|ka matlab)"
              r"(?: (?:batao|samjhao|bataiye|samjhaiye|kya hai))?"),
    ("error", r"(?:yeh|ye|yah) error kya hai"),
]]

_HINTS = {
    "read": "Read out the main text on this screen, starting with the most important part.",
    "error": ("There is an error or warning message on this screen. Read it out and explain in plain "
              "words what it means and what to try."),
    "summary": "Summarize the page, window or article on this screen in two or three short sentences.",
    "free": "Look at this screen and answer what the user asks about it.",
}
_MAX_WORDS = 30


def match(transcript: str) -> Optional[Tuple[str, str]]:
    """Pure: (kind, question) if the utterance is a request to look at the screen, else None.
    kind is describe / read / error / summary / free; question "" means 'use the default'."""
    for text in dict.fromkeys((_normalize(transcript), _normalize(transcript, tail=False))):
        hit = _match_text(transcript, text)
        if hit is not None:
            return hit
    return None


def _match_text(transcript: str, text: str) -> Optional[Tuple[str, str]]:
    if not text or len(text.split()) > _MAX_WORDS:
        return None
    for kind, rx in _PATTERNS:
        m = rx.fullmatch(text)
        if not m:
            continue
        q = ""
        if "q" in rx.groupindex:
            q = (m.group("q") or "").strip()
            q = re.sub(r"^(?:and|then|और|फिर)\s+", "", q).strip()
        if kind == "describe" or (kind == "free" and not q):
            question = ""                       # the default question
        else:
            # the user's own words (not the folded text) travel with the image
            words = re.sub(r"\s+", " ", str(transcript or "")).strip()
            question = f'{_HINTS[kind]} The user said: "{words}".'
        return kind, question
    return None


def try_handle(transcript: str) -> Optional[str]:
    """'what's on my screen', 'read this to me', 'explain this error', ... -> the model's spoken
    answer, or None if the utterance is anything else (nothing is captured or asked)."""
    try:
        hit = match(transcript)
        if hit is None:
            return None
        kind, question = hit
        logger.info("Vision: matched %r as %s -> describe_screen(%r)", transcript, kind, question)
        return describe_screen(question)        # says so itself if VISION_ENABLED is off
    except Exception as e:
        logger.warning("Vision: try_handle failed: %s", e, exc_info=True)
        return _t("failed")
