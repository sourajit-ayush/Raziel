"""
webinfo.py - live news, weather and "ask Google" answers for Raziel, English and Hindi, no API key needed.

Three features, all decided in code (no LLM) and all going through `netutil` for HTTP:

News     Always the LATEST from Google News RSS (keyless), fetched fresh on every request; the only thing
         remembered is the list behind "more news" (valid 10 minutes).
         "what's the news", "latest news on Tesla", "cricket news", "business news", "more news";
         Hindi: "ताज़ा ख़बरें सुनाओ", "क्रिकेट की ख़बरें", "दुनिया में क्या हो रहा है", "और ख़बरें".
         Falls back to a ddgs news search, then to one honest sentence.
Weather  Open-Meteo (keyless), place by geocoding, default place = config.DEFAULT_LOCATION or a guess from
         the IP address. "what's the weather", "weather in Mumbai tomorrow", "will it rain today",
         "do I need an umbrella", "how hot is it"; Hindi: "मौसम कैसा है", "मुंबई का मौसम", "आज बारिश होगी क्या".
Search   web_search() replaces the old DuckDuckGo-only tool. With a Gemini key (config.GEMINI_API_KEY or the
         GEMINI_API_KEY / GOOGLE_API_KEY environment variable) the question goes to Gemini with Google Search
         grounding and the answer is returned as `Answer` (a str with .final = True: speak it verbatim).
         Without a key, or when Gemini fails, DuckDuckGo (ddgs) results come back as a plain str for the LLM.

Public API
----------
  get_news(topic="", count=0)            more_news()             try_handle_news(transcript)
  get_weather(place="", when="today")                            try_handle_weather(transcript)
  web_search(query)      gemini_available()     class Answer     try_handle_search(transcript)
  try_handle_live_question(transcript)          is_time_sensitive(text)
  weather_briefing_line()   news_briefing_line(n=3)                       (hooks for the morning briefing)

Settings read with getattr(config, NAME, default): GEMINI_API_KEY "", GEMINI_MODELS [...], NEWS_COUNT 5,
NEWS_REGION "IN", NEWS_LANG_EN "en-IN", NEWS_SPEAK_SOURCE False, DEFAULT_LOCATION "", WEBINFO_TIMEOUT 8.

Matching is deliberately STRICT and anchored ("news" or "weather" inside another sentence never matches:
"I read the news yesterday", "open the weather app", "under the weather" are all ignored). Everything that
touches the outside world is a small module-level function or goes through `netutil`, so tests replace it.
"""

from __future__ import annotations

import html
import logging
import math
import os
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple

import lang
import netutil

try:                                    # config is optional at import time (tests replace attributes)
    import config
except Exception:                       # pragma: no cover
    config = None

logger = logging.getLogger("voice_assistant")

DEFAULT_GEMINI_MODELS = ["gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.5-flash"]
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def _cfg(name: str, default):
    try:
        return getattr(config, name, default)
    except Exception:
        return default


def _timeout() -> float:
    try:
        return max(1.0, float(_cfg("WEBINFO_TIMEOUT", 8)))
    except (TypeError, ValueError):
        return 8.0


# ------------------------------------------------------------------ clock and state (tests replace these)

def _now() -> datetime:
    """Current time, timezone-aware UTC."""
    return datetime.now(timezone.utc)


def _monotonic() -> float:
    return time.monotonic()


_NEWS_TTL = 600.0                 # "more news" stays valid this long (seconds)
_BARE_MORE_TTL = 120.0            # a bare "more" / "next" only counts this soon after the news
_last_news: Dict[str, object] = {}
_ip_cache: Dict[str, object] = {}
_geo_cache: Dict[str, dict] = {}
_gemini_state: Dict[str, object] = {}


def reset_state() -> None:
    """Forget everything remembered for the session (used by tests)."""
    _last_news.clear()
    _ip_cache.clear()
    _geo_cache.clear()
    _gemini_state.clear()
    _gemini_state.update({"model": None, "blocked_until": 0.0, "disabled": False, "warned": set()})


reset_state()


# ------------------------------------------------------------------ text folding with an index map

_DROP = {0x0901, 0x0902, 0x093C, 0x200C, 0x200D}       # chandrabindu, anusvara, nukta, ZWNJ, ZWJ
_DEV_DIGITS = "०१२३४५६७८९"
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate(_DEV_DIGITS)}


def _fold_pat(p: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFC", p) if ord(ch) not in _DROP)


def _c(p: str) -> "re.Pattern":
    """Compile a pattern written in natural Hindi/English spelling against FOLDED text."""
    return re.compile(_fold_pat(p))


def _fold_words(words) -> frozenset:
    return frozenset(_fold_pat(w).lower() for w in words)


class _U:
    """One utterance: original text, a folded copy for matching (lower case, punctuation gone, Hindi
    chandrabindu / anusvara / nukta dropped) and the index map back to the original spelling."""

    def __init__(self, orig: str):
        orig = unicodedata.normalize("NFC", orig or "")
        orig = orig.replace("’", "'").replace("‘", "'").replace("`", "'")
        out: List[str] = []
        idx: List[int] = []
        prev_space = True
        for i, ch in enumerate(orig):
            o = ord(ch)
            if o in _DROP:
                continue
            ch = ch.translate(_DIGIT_MAP)
            if ch == "'" or ch.isalnum() or 0x0900 <= o <= 0x097F and ch not in "।॥":
                out.append(ch.lower())
                idx.append(i)
                prev_space = False
            elif not prev_space:
                out.append(" ")
                idx.append(i)
                prev_space = True
        if out and out[-1] == " ":
            out.pop()
            idx.pop()
        self.orig = orig
        self.f = "".join(out)
        self.m = idx
        self.off = 0

    def strip_leading(self, rx) -> None:
        mt = rx.match(self.f)
        if mt and mt.end():
            self.off += mt.end()
            self.f = self.f[mt.end():]

    def strip_trailing(self, rx) -> None:
        mt = rx.search(self.f)
        if mt:
            self.f = self.f[:mt.start()]

    def span(self, s: int, e: int) -> str:
        a, b = s + self.off, e + self.off
        ia = self.m[a] if a < len(self.m) else len(self.orig)
        ib = self.m[b] if b < len(self.m) else len(self.orig)
        return self.orig[ia:ib].strip(" \t\r\n.,!?;:।'\"")

    def group(self, mt, name: str) -> str:
        if mt.group(name) is None:
            return ""
        return self.span(mt.start(name), mt.end(name))


_LEAD = _c(
    r"(?:(?:hey|hi|hello|ok|okay|so|well|um+|uh+|alright|all right|please|kindly|raziel|razil|razeel|"
    r"actually|now|and|then|just) "
    r"|(?:can|could|would|will) you(?: please)? |(?:i|we) (?:want|need|would like) you to |i'd like you to |"
    r"go ahead and |क्या आप |क्या तुम |कृपया |प्लीज़ |जरा |ज़रा |रज़ील |रेज़ील |अच्छा |सुनो |अरे |ओके |ठीक है |"
    r"आप |तुम |मेरे लिए |मुझे |हमें )+")
_TRAIL = _c(r"(?: (?:please|thanks|thank you|for me|कृपया|प्लीज़|मेरे लिए|जरा|ज़रा))+$")


def _prep(transcript, max_words: int = 24) -> Optional[_U]:
    if not isinstance(transcript, str) or not transcript.strip():
        return None
    u = _U(transcript)
    if not u.f or len(u.f.split()) > max_words + 4:
        return None
    u.strip_leading(_LEAD)
    u.strip_trailing(_TRAIL)
    if not u.f or len(u.f.split()) > max_words:
        return None
    return u


def _clean_slot(text: str, limit: int = 100) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" \t.,!?;:।'\"")
    text = re.sub(r"^(?:the|about|of) ", "", text, flags=re.I).strip()
    return text[:limit].strip()


def _tok_spans(f: str) -> List[Tuple[str, int, int]]:
    return [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", f)]


# ================================================================== NEWS

_NT: Dict[str, Dict[str, str]] = {
    "intro_top": {"en": "Here are the top headlines.", "hi": "ये हैं आज की ताज़ा ख़बरें।"},
    "intro_topic": {"en": "Here's the latest {label} news.", "hi": "ये हैं {label} की ताज़ा ख़बरें।"},
    "intro_search": {"en": "Here's the latest news about {q}.", "hi": "{q} के बारे में ये हैं ताज़ा ख़बरें।"},
    "intro_more": {"en": "Here are more headlines.", "hi": "ये और ख़बरें हैं।"},
    "according": {"en": "According to {p}.", "hi": "{p} के अनुसार।"},
    "none": {"en": "I couldn't find any recent news about {q}.", "hi": "मुझे {q} के बारे में कोई ताज़ा ख़बर नहीं मिली।"},
    "none_top": {"en": "I couldn't find any news right now.", "hi": "अभी मुझे कोई ताज़ा ख़बर नहीं मिली।"},
    "fail": {"en": "I couldn't reach the news service right now. Please check the internet connection and try again.",
             "hi": "अभी ख़बरें नहीं मिल पा रही हैं। कृपया इंटरनेट कनेक्शन देखिए और फिर से पूछिए।"},
    "no_more": {"en": "That's all the headlines I have for now. Ask me again for the latest.",
                "hi": "अभी के लिए बस इतनी ही ख़बरें हैं। ताज़ा ख़बरों के लिए दोबारा पूछिए।"},
    "brief": {"en": "In the news today: {items}", "hi": "आज की ख़बरों में: {items}"},
    "sorry": {"en": "Sorry, something went wrong while getting the news.",
              "hi": "माफ़ कीजिए, ख़बरें लाते समय कुछ गड़बड़ हो गई।"},
}

_ORD_EN = ["First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth", "Ninth", "Tenth"]
_ORD_HI = ["पहली", "दूसरी", "तीसरी", "चौथी", "पाँचवीं", "छठी", "सातवीं", "आठवीं", "नौवीं", "दसवीं"]

_NEWS_SECTIONS = {
    "WORLD": ("world", "दुनिया"), "NATION": ("national", "देश"), "BUSINESS": ("business", "कारोबार"),
    "TECHNOLOGY": ("technology", "टेक्नोलॉजी"), "SPORTS": ("sports", "खेल"),
    "ENTERTAINMENT": ("entertainment", "मनोरंजन"), "SCIENCE": ("science", "विज्ञान"),
    "HEALTH": ("health", "स्वास्थ्य"),
}
_NEWS_TOPIC_WORDS = {
    "WORLD": ["world", "international", "global", "worldwide", "foreign", "overseas", "world affairs",
              "दुनिया", "विश्व", "अंतरराष्ट्रीय", "अंतर्राष्ट्रीय", "इंटरनेशनल", "वर्ल्ड", "विदेश", "दुनिया भर",
              "दुनियाभर"],
    "NATION": ["india", "indian", "national", "nation", "country", "domestic", "desh", "देश", "भारत",
               "राष्ट्रीय", "नेशनल", "इंडिया", "राष्ट्र", "our country", "the country"],
    "BUSINESS": ["business", "market", "markets", "economy", "economic", "finance", "financial", "stock",
                 "stocks", "stock market", "share market", "sensex", "nifty", "trade", "corporate", "industry",
                 "बिज़नेस", "बिजनेस", "व्यापार", "कारोबार", "बाज़ार", "बाजार", "शेयर बाजार", "शेयर बाज़ार",
                 "अर्थव्यवस्था", "आर्थिक", "वित्त", "शेयर मार्केट", "स्टॉक मार्केट", "सेंसेक्स", "निफ्टी",
                 "व्यवसाय"],
    "TECHNOLOGY": ["tech", "technology", "gadgets", "gadget", "ai", "artificial intelligence", "टेक",
                   "टेक्नोलॉजी", "तकनीक", "तकनीकी", "गैजेट", "गैजेट्स", "प्रौद्योगिकी"],
    "SPORTS": ["sports", "sport", "खेल", "स्पोर्ट्स", "स्पोर्ट"],
    "ENTERTAINMENT": ["entertainment", "bollywood", "hollywood", "movies", "movie", "film", "films", "cinema",
                      "celebrity", "celebrities", "मनोरंजन", "बॉलीवुड", "हॉलीवुड", "फिल्म", "फिल्मी", "फ़िल्म",
                      "सिनेमा", "एंटरटेनमेंट"],
    "SCIENCE": ["science", "space", "scientific", "nasa", "isro", "विज्ञान", "अंतरिक्ष", "साइंस"],
    "HEALTH": ["health", "medical", "medicine", "wellness", "fitness", "स्वास्थ्य", "स्वास्थ", "सेहत", "हेल्थ",
               "चिकित्सा"],
}
_TOPIC_LOOKUP: Dict[str, str] = {}
for _sec, _words in _NEWS_TOPIC_WORDS.items():
    for _w in _words:
        _TOPIC_LOOKUP[_fold_pat(_w).lower()] = _sec
_TOPIC_MOD_WORDS = _fold_words([
    "the", "a", "an", "latest", "top", "news", "headlines", "headline", "today's", "todays", "recent", "current",
    "breaking", "new", "fresh", "general", "daily", "all", "some", "any", "big", "main", "major", "important",
    "morning", "evening", "last", "hourly", "of", "for", "about", "on", "in", "from",
    "आज", "आज की", "ताजा", "ताज़ा", "की", "के", "का", "नई", "नयी", "मुख्य", "बड़ी", "ख़बर", "खबर", "खबरें",
    "समाचार", "न्यूज़", "न्यूज", "सुर्खियां", "सुर्खियाँ", "सभी", "सारी", "और"])
_FRESH_WORDS = re.compile(r"\b(?:breaking|last hour|past hour|just now|this hour)\b")


def _topic_spec(text: str) -> dict:
    """Topic phrase -> {"kind": top|section|search, "key", "query", "label": (en, hi), "window"}."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    u = _U(raw)
    window = "1h" if _FRESH_WORDS.search(u.f) else "1d"
    toks = _tok_spans(u.f)
    while toks and toks[0][0] in _TOPIC_MOD_WORDS:
        toks.pop(0)
    while toks and toks[-1][0] in _TOPIC_MOD_WORDS:
        toks.pop()
    if not toks:
        return {"kind": "top", "key": "", "query": "", "label": ("", ""), "window": window}
    phrase = " ".join(t[0] for t in toks)
    sec = _TOPIC_LOOKUP.get(phrase)
    if sec is None and len(toks) == 1 and toks[0][0].rstrip("s") in _TOPIC_LOOKUP:
        sec = _TOPIC_LOOKUP[toks[0][0].rstrip("s")]
    if sec is not None:
        en, hi = _NEWS_SECTIONS[sec]
        return {"kind": "section", "key": sec, "query": "", "label": (en, hi), "window": window}
    query = _clean_slot(u.span(toks[0][1], toks[-1][2]))
    return {"kind": "search", "key": "", "query": query or phrase, "label": (query, query), "window": window}


class _Item:
    __slots__ = ("title", "publisher", "when", "link")

    def __init__(self, title: str, publisher: str = "", when: Optional[datetime] = None, link: str = ""):
        self.title, self.publisher, self.when, self.link = title, publisher, when, link

    def __repr__(self):                     # pragma: no cover
        return f"_Item({self.title!r}, {self.publisher!r}, {self.when})"


def _unescape(text: str) -> str:
    text = text or ""
    for _ in range(2):                      # Google sometimes double-escapes (&amp;amp;)
        new = html.unescape(text)
        if new == text:
            break
        text = new
    return re.sub(r"<[^>]+>", "", text)


def _split_publisher(title: str, source: str) -> Tuple[str, str]:
    """'Sensex jumps 300 points - Mint' -> ('Sensex jumps 300 points', 'Mint')."""
    title = re.sub(r"\s+", " ", title or "").strip()
    source = (source or "").strip()
    if source and title.endswith(" - " + source):
        return title[: -len(source) - 3].strip(), source
    if not source and " - " in title:
        head, tail = title.rsplit(" - ", 1)
        head, tail = head.strip(), tail.strip()
        if head and tail and len(tail) <= 45 and len(tail.split()) <= 5 and not tail[0].islower():
            return head, tail
    return title, source


def _parse_pubdate(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_rss(text: str) -> List[_Item]:
    """RSS text -> items (title with the publisher suffix stripped, publisher, date, link). ValueError if the
    text is not an RSS feed at all; an RSS feed with no items gives []."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty feed")
    t = text.lstrip("﻿ \t\r\n")
    if re.search(r"<!ENTITY", t, re.I):
        raise ValueError("feed declares entities")
    try:
        root = ET.fromstring(t)
    except ET.ParseError as e:
        raise ValueError(f"feed is not valid XML: {e}") from None
    local = root.tag.rsplit("}", 1)[-1].lower()
    if local not in ("rss", "rdf"):
        raise ValueError(f"not an RSS feed (root <{local}>)")
    items: List[_Item] = []
    for node in root.iter("item"):
        raw_title = _unescape(node.findtext("title") or "")
        if not raw_title.strip():
            continue
        src_node = node.find("source")
        source = _unescape(src_node.text or "") if src_node is not None else ""
        title, publisher = _split_publisher(raw_title, source)
        if not title:
            continue
        items.append(_Item(title, publisher, _parse_pubdate(node.findtext("pubDate")),
                           (node.findtext("link") or "").strip()))
    return items


_SIM_STOP = frozenset({"the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "is", "are", "as", "by",
                       "with", "from", "after", "over", "its", "his", "her"})


_SIM_PUNCT = ".,;:!?\"'()[]{}“”‘’…–—-|/\\।"


def _sim_tokens(title: str) -> frozenset:
    words = (w.strip(_SIM_PUNCT) for w in lang.normalize_hindi(title).lower().split())
    return frozenset(w for w in words if w and w not in _SIM_STOP)


def _similar(a: frozenset, b: frozenset) -> bool:
    if not a or not b:
        return False
    inter = len(a & b)
    jac = inter / len(a | b)
    small = min(len(a), len(b))
    return jac >= 0.7 or (small >= 4 and inter / small >= 0.85)


def _prepare(items: List[_Item], search: bool) -> List[_Item]:
    """Newest first, exact and near-duplicate titles dropped, too-old items dropped (search feeds)."""
    now = _now()
    if search:
        cutoff = now - timedelta(days=3)
        items = [i for i in items if i.when is None or i.when >= cutoff]
    dated = sorted((i for i in items if i.when is not None), key=lambda i: i.when, reverse=True)
    undated = [i for i in items if i.when is None]
    out: List[_Item] = []
    seen: List[frozenset] = []
    for it in dated + undated:
        toks = _sim_tokens(it.title)
        if any(toks == s or _similar(toks, s) for s in seen):
            continue
        seen.append(toks)
        out.append(it)
    return out


# ---- feeds

_NEWS_BASE = "https://news.google.com/rss"


def _edition(hindi: bool) -> str:
    region = str(_cfg("NEWS_REGION", "IN") or "IN").strip().upper()
    if hindi:
        hl, cl = "hi", "hi"
    else:
        hl = str(_cfg("NEWS_LANG_EN", "en-IN") or "en-IN").strip()
        cl = hl.split("-")[0]
    return f"hl={hl}&gl={region}&ceid={region}:{cl}"


def _feed_url(spec: dict, hindi: bool, window: str = "1d") -> str:
    ed = _edition(hindi)
    if spec["kind"] == "top":
        return f"{_NEWS_BASE}?{ed}"
    if spec["kind"] == "section":
        return f"{_NEWS_BASE}/headlines/section/topic/{spec['key']}?{ed}"
    q = urllib.parse.quote(f"{spec['query']} when:{window}", safe="")
    return f"{_NEWS_BASE}/search?q={q}&{ed}"


def _fetch_items(spec: dict, hindi: bool) -> List[_Item]:
    """Fetches and prepares the feed. Raises on network / parse failure; [] means 'nothing recent'."""
    search = spec["kind"] == "search"
    windows = [None]
    if search:
        windows = ["1h", "1d", "7d"] if spec.get("window") == "1h" else ["1d", "7d"]
    result: List[_Item] = []
    for w in windows:
        url = _feed_url(spec, hindi, w or "1d")
        logger.info("News: fetching %s", url)
        text = netutil.get_text(url, timeout=_timeout())
        result = _prepare(_parse_rss(text), search)
        if result:
            break
    return result


def _ddgs_module():
    try:
        from ddgs import DDGS
        return DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
            return DDGS
        except ImportError:
            return None


def _ddgs_news(query: str, n: int) -> List[_Item]:
    DDGS = _ddgs_module()
    if DDGS is None:
        return []
    try:
        rows = list(DDGS().news(query, max_results=n) or [])
    except Exception as e:
        logger.warning("News fallback (ddgs) failed: %s", e)
        return []
    out = []
    for r in rows:
        title = _unescape(str(r.get("title") or "")).strip()
        if title:
            out.append(_Item(title, str(r.get("source") or "").strip(), _parse_pubdate(r.get("date")),
                             str(r.get("url") or "")))
    return out


def _fallback_query(spec: dict) -> str:
    if spec["kind"] == "search":
        return spec["query"]
    if spec["kind"] == "section":
        return f"{spec['label'][0]} news"
    return "latest news today"


# ---- speaking

def _speakable(text: str) -> str:
    """Headline -> something a TTS voice reads well."""
    hi = lang.current() == "hi"
    t = re.sub(r"https?://\S+", "", text or "")
    t = t.replace("&", " और " if hi else " and ")
    t = re.sub(r"₹\s*([\d][\d,.]*)(\s*(?:lakh crore|crore|lakh|thousand|million|billion|trillion))?",
               lambda m: f"{m.group(1)}{m.group(2) or ''} रुपये" if hi else f"{m.group(1)}{m.group(2) or ''} rupees", t)
    t = re.sub(r"\$\s*([\d][\d,.]*)(\s*(?:thousand|million|billion|trillion))?",
               lambda m: f"{m.group(1)}{m.group(2) or ''} डॉलर" if hi else f"{m.group(1)}{m.group(2) or ''} dollars", t)
    t = re.sub(r"(\d)\s*%", r"\1 प्रतिशत" if hi else r"\1 percent", t)
    t = re.sub(r"[|»«\[\]{}<>*_#]+", " ", t)
    t = t.replace("…", "").replace("...", "")
    return re.sub(r"\s+", " ", t).strip(" .")


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    if text[-1] in ".!?।":
        return text
    return text + ("।" if lang.current() == "hi" and lang.has_devanagari(text) else ".")


def _ordinal(i: int) -> str:
    if lang.current() == "hi":
        return _ORD_HI[i] if i < len(_ORD_HI) else f"ख़बर नंबर {i + 1}"
    return _ORD_EN[i] if i < len(_ORD_EN) else f"Number {i + 1}"


def _news_count(count) -> int:
    try:
        n = int(float(count))
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        try:
            n = int(_cfg("NEWS_COUNT", 5))
        except (TypeError, ValueError):
            n = 5
    return max(1, min(10, n))


def _speak_source() -> bool:
    return bool(_cfg("NEWS_SPEAK_SOURCE", False))


def _format_batch(items: List[_Item], intro: str) -> str:
    parts = [intro] if intro else []
    for i, it in enumerate(items):
        title = _sentence(_speakable(it.title))
        if not title:
            continue
        line = f"{_ordinal(i)}, {title}"
        if _speak_source() and it.publisher:
            line += " " + lang.tr(_NT, "according", p=_speakable(it.publisher))
        parts.append(line)
    return " ".join(parts)


def _intro(spec: dict) -> str:
    hindi = lang.current() == "hi"
    if spec["kind"] == "section":
        return lang.tr(_NT, "intro_topic", label=spec["label"][1 if hindi else 0])
    if spec["kind"] == "search":
        return lang.tr(_NT, "intro_search", q=spec["query"])
    return lang.tr(_NT, "intro_top")


def _get_news_spec(spec: dict, count) -> str:
    n = _news_count(count)
    hindi = lang.current() == "hi"
    items: Optional[List[_Item]] = None
    try:
        items = _fetch_items(spec, hindi)
    except Exception as e:
        logger.warning("News feed failed (%s); trying the ddgs fallback", e)
    if items is None:
        items = _ddgs_news(_fallback_query(spec), max(n, 5))
        if not items:
            return lang.tr(_NT, "fail")
        logger.info("News: %d headline(s) from the ddgs fallback", len(items))
    if not items:
        if spec["kind"] == "search":
            return lang.tr(_NT, "none", q=spec["query"])
        return lang.tr(_NT, "none_top")
    batch = items[:n]
    _last_news.clear()
    _last_news.update({"items": items, "pos": len(batch), "spec": spec, "at": _monotonic()})
    logger.info("News: speaking %d of %d headline(s)", len(batch), len(items))
    return _format_batch(batch, _intro(spec))


def get_news(topic: str = "", count: int = 0, **_ignored) -> str:
    """Latest headlines from Google News, fetched fresh every time. `topic` may be empty (top stories), a section
    word (world, india, business, tech, sports, entertainment, science, health) or anything else (searched)."""
    try:
        spec = _topic_spec(topic if isinstance(topic, str) else str(topic or ""))
        logger.info("get_news(topic=%r, count=%r) -> %s %s", topic, count, spec["kind"], spec["key"] or spec["query"])
        return _get_news_spec(spec, count)
    except Exception:
        logger.exception("get_news failed")
        return lang.tr(_NT, "sorry")


def _news_active(ttl: float = _NEWS_TTL) -> bool:
    return bool(_last_news) and (_monotonic() - float(_last_news.get("at", 0))) <= ttl


def more_news(count: int = 0, **_ignored) -> str:
    """The next headlines from the list read out last (valid 10 minutes); fresh top stories if there is none."""
    try:
        if not _news_active():
            logger.info("more_news: no news list open, fetching the top stories")
            return get_news("", count)
        n = _news_count(count)
        items: List[_Item] = _last_news["items"]           # type: ignore[assignment]
        pos = int(_last_news.get("pos", 0))
        batch = items[pos:pos + n]
        if not batch:
            return lang.tr(_NT, "no_more")
        _last_news["pos"] = pos + len(batch)
        _last_news["at"] = _monotonic()
        logger.info("more_news: headlines %d-%d of %d", pos + 1, pos + len(batch), len(items))
        return _format_batch(batch, lang.tr(_NT, "intro_more"))
    except Exception:
        logger.exception("more_news failed")
        return lang.tr(_NT, "sorry")


# ---- the matcher

_EN_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10}
_N_NUM = r"(?P<n>\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"
_N_LEAD = (r"(?:(?:what(?:'s| is| are)|whats|tell me|give me|read me|read out|read|show me|let me hear|let me know|"
           r"i want to hear|i wanna hear|i want to know|i wanna know|get me|play|say|do you have|have you got|got|"
           r"is there|are there|any|brief me on|catch me up on|update me on|fill me in on) )?")
_N_MODS = (r"(?:(?:the|a|an|some|any|all|today's|todays|latest|top|recent|current|breaking|new|fresh|big|main|"
           r"major|important|general|daily|morning|evening|last|hourly) )*")
_N_WORD = (r"(?:news(?: (?:update|updates|briefing|summary|roundup|digest|bulletin|headlines))?|headlines?|"
           r"top stories|news stories|news items)")
_N_TAIL = (r"(?: (?:today|now|right now|update|updates|for today|this morning|this evening|tonight|again|"
           r"please|for me|live))*")
_TOPIC_WORD = r"[a-z0-9][a-z0-9']*"

_NEWS_GENERIC = _c(rf"{_N_LEAD}{_N_MODS}(?:{_N_NUM} )?{_N_MODS}{_N_WORD}{_N_TAIL}")
_NEWS_INNEWS = _c(rf"(?:what(?:'s| is)|whats) (?:happening |going on |new )?in the news{_N_TAIL}")
_NEWS_TOPIC_EN = _c(rf"{_N_LEAD}{_N_MODS}(?:{_N_NUM} )?(?P<topic>{_TOPIC_WORD}(?: {_TOPIC_WORD}){{0,2}}) "
                    rf"(?:news|headlines?){_N_TAIL}")
_NEWS_ABOUT = _c(rf"{_N_LEAD}{_N_MODS}(?:news|headlines?)(?: update| updates| story| stories)? "
                 rf"(?:about|on|of|regarding|related to|concerning|for|from|in|around|over|involving) "
                 rf"(?P<q>{_TOPIC_WORD}(?: {_TOPIC_WORD}){{0,7}}?){_N_TAIL}")
_NEWS_LATEST_ON = _c(rf"(?:what(?:'s| is)|whats) (?:the )?(?:latest|newest|new|news|update|updates) "
                     rf"(?:on|about|with|in|from|regarding) (?P<q>{_TOPIC_WORD}(?: {_TOPIC_WORD}){{0,7}}?){_N_TAIL}")
_NEWS_WORLD = _c(rf"(?:what(?:'s| is| has been| have been)|whats) (?:going on|happening|new|up|been happening|"
                 rf"been going on) (?:in|around|across|over|throughout) (?:the )?"
                 rf"(?P<where>{_TOPIC_WORD}(?: {_TOPIC_WORD}){{0,2}}?){_N_TAIL}")
_NEWS_WORLD2 = _c(rf"what (?:happened|is happening|has happened|has been going on) (?:in|around|across) "
                  rf"(?:the )?(?P<where>world|country){_N_TAIL}")
_NEWS_MORE = _c(r"(?:(?:give me|tell me|read me|read|show me|get me|i want|i want to hear|let me hear|play|"
                r"is there|are there|do you have|have you got|got|any|what) )?"
                r"(?:(?:some|any|the|a) )*(?:more|other|next|another|further|additional) "
                r"(?:news|headlines?|stories|story|news stories|news items|updates?)(?: (?:please|now|too))*")
_NEWS_ELSE = _c(r"(?:what|anything) else(?: is| in| on)?(?: in| on)?(?: the)? news(?: today)?")
_NEWS_BARE_MORE = _fold_words([
    "more", "next", "next one", "another", "another one", "go on", "keep going", "continue", "more please",
    "and more", "and next", "read more", "what else", "anything else", "what next", "next ones", "the next ones",
    "next five", "और", "और सुनाओ", "और बताओ", "आगे", "आगे बताओ", "आगे सुनाओ", "अगली", "आगे पढ़ो", "और पढ़ो",
    "aur", "aur sunao", "aur batao", "आगे और", "और ख़बरें", "और सुनाइए"])

_TOPIC_STOP_EN = frozenset({
    "that", "that's", "this", "it", "it's", "there", "here", "my", "your", "our", "his", "her", "their", "some",
    "no", "bad", "good", "great", "big", "fake", "old", "sad", "happy", "sorry", "wonderful", "terrible", "awesome",
    "amazing", "exciting", "best", "worst", "any", "what", "which", "how", "i", "we", "you", "he", "she", "they",
    "is", "are", "was", "be", "google", "the", "a", "an", "nice", "sweet", "cool", "interesting", "shocking",
    "let", "let's", "have", "has", "had", "got", "get", "do", "does", "did", "can", "will", "would", "when",
    "where", "who", "why", "and", "or", "but", "if", "so", "not", "of", "to", "in", "on", "at", "for", "with",
    "by", "from", "as", "about", "tell", "give", "read", "show", "play", "open", "close", "start", "stop", "make",
    "take", "put", "set", "turn", "call", "send", "watch", "listen", "hear", "said", "say", "says"})
_QUERY_BAD_WORDS = re.compile(r"\b(?:app|apps|website|websites|site|channel|channels|widget|tab|page|newspaper|"
                              r"feed|application|program|software|extension|icon)\b")
_QUERY_BAD_FIRST = frozenset({"you", "me", "us", "him", "her", "them", "it", "this", "that", "these", "those",
                              "my", "your", "our", "his", "their", "here", "there"})

_HN_WORD = (r"(?:खबर(?:ें|े|ों|ो)?|समाचार(?:ों)?|न्यूज़|न्यूज|न्यूस|सुर्खियाँ|सुर्खियां|सुर्ख़ियाँ|सुर्ख़ियां|सुर्खिया|"
            r"हेडलाइन्स|हेडलाइंस|हेडलाइन)")
_HN_VERB = (r"(?:सुनाओ|सुनाइए|सुनाइये|सुना दो|सुना दीजिए|सुनाएं|सुनाएँ|सुनाए|सुनाना|बताओ|बताइए|बताइये|बता दो|"
            r"बता दीजिए|बताएं|बताएँ|बताए|बताना|पढ़ो|पढ़िए|पढ़कर सुनाओ|पढ़ कर सुनाओ|पढ़ के सुनाओ|पढ़कर बताओ|दिखाओ|"
            r"दिखाइए|दो|दीजिए|दीजिये|चाहिए|सुनूं|सुनूँ|सुनना है|सुनना चाहता हूं|सुनना चाहती हूं|जानना है|बोलो|कहो|"
            r"क्या है|क्या हैं|क्या हुआ|क्या रहीं|क्या रही|है|हैं)")
_HN_MODS = (r"(?:(?:आज की|आज के|आज का|आज|अभी की|अभी|ताज़ातरीन|ताजातरीन|ताज़ा तरीन|ताजा तरीन|ताज़ा|ताजा|नई|नयी|"
            r"मुख्य|बड़ी|सभी|सारी|सब|कुछ|थोड़ी|आख़िरी|आखिरी) )*")
_HN_TAIL = (rf"(?: (?:{_HN_VERB}|आज|अभी|ताजा|ताज़ा|क्या|कौन सी|कौनसी|कौन कौन सी|मुझे|हमें|कृपया|जरा|ज़रा|मेरे लिए|"
            r"सभी|सारी|हुई|रही|रहीं|रहे|चल रही|चल रहीं|आई|आईं|मिली|मिलीं|कुछ|और|भी))*")
_HN_TOK = r"[^\s]+"
_HN_NUMBER = r"(?P<n>\d{1,2})"
_HNEWS_GENERIC = _c(rf"(?:{_HN_VERB} )*{_HN_MODS}(?:{_HN_NUMBER} )?{_HN_MODS}{_HN_WORD}{_HN_TAIL}")
_HNEWS_TOPIC = _c(rf"(?:{_HN_VERB} )*{_HN_MODS}(?P<topic>{_HN_TOK}(?: {_HN_TOK}){{0,2}}) "
                  rf"(?:की|के|का|से जुड़ी|से जुड़े|संबंधी|सम्बंधी) {_HN_MODS}(?:{_HN_NUMBER} )?{_HN_WORD}{_HN_TAIL}")
_HNEWS_TOPIC2 = _c(rf"(?:{_HN_VERB} )*{_HN_MODS}(?P<topic>{_HN_TOK}(?: {_HN_TOK}){{0,1}}) "
                   rf"{_HN_WORD}{_HN_TAIL}")
_HNEWS_ABOUT = _c(rf"(?:{_HN_VERB} )*(?P<topic>{_HN_TOK}(?: {_HN_TOK}){{0,3}}) "
                  rf"(?:के बारे में|के बारे मे|के विषय में|के बारे|को लेकर|पर) {_HN_MODS}{_HN_WORD}{_HN_TAIL}")
_HNEWS_WORLD = _c(r"(?:आज |आजकल |अभी )?(?:पूरी |सारी |समूची )?(?P<where>[^\s]+(?: [^\s]+)??)(?: भर)? (?:में|मे) "
                  r"(?:आज |अभी |आजकल )?क्या (?:हो रहा|चल रहा|हुआ|नया|खास|ख़ास) ?(?:है|हैं)?")
_HNEWS_MORE = _c(r"(?:और|अगली|अगला|आगे की|आगे के|दूसरी|दूसरे|बाकी|बाक़ी|अन्य|ओर) "
                 rf"(?:{_HN_MODS})?{_HN_WORD}{_HN_TAIL}")
_HNEWS_LAT = re.compile(
    r"(?:(?:aaj ki|aaj ka|aaj ke|taaza|taza|latest|abhi ki|tazatarin) )*(?:(?P<topic>[a-z]+) (?:ki|ke|ka) )?"
    r"(?P<w>khabar|khabarein|khabren|khabrein|khabaren|khabre|samachar|samachaar|news)"
    r"(?P<v>(?: (?:sunao|batao|suna do|bata do|padho|dikhao|sunaiye|bataiye|de do|do|dijiye|dijiye))*)")
_HNEWS_LAT_MORE = re.compile(r"(?:aur|agli|agla|aage ki) (?:khabar|khabarein|khabren|khabrein|samachar|news)"
                             r"(?: (?:sunao|batao|suna do|bata do|padho|dikhao|sunaiye|bataiye|de do))*")
_HNEWS_LAT_WORLD = re.compile(r"(?:(?:puri|poori) )?(?P<where>duniya|desh|world) (?:mein|me) "
                              r"(?:aaj )?kya (?:ho raha|chal raha) hai")
_HN_WHERE_WORLD = _fold_words(["दुनिया", "विश्व", "दुनियाभर", "वर्ल्ड", "संसार", "जगत", "duniya", "world"])
_HN_WHERE_NATION = _fold_words(["देश", "भारत", "इंडिया", "मुल्क", "desh", "india"])
_HN_WHERE_STOP = _fold_words([
    "मेरे", "मेरी", "मेरा", "आपके", "तुम्हारे", "इस", "उस", "यहाँ", "यहां", "यहा", "वहाँ", "वहां", "वहा", "घर",
    "कमरे", "कमरा", "ऑफिस", "स्कूल", "क्लास", "मीटिंग", "फोन", "फ़ोन", "दिमाग", "जिंदगी", "ज़िंदगी", "दिल",
    "आज", "अभी", "कल", "यह", "ये", "वो", "वह", "हमारे", "अपने", "रसोई", "बाहर", "अंदर", "पास", "आसपास"])
_HN_TOPIC_STOP = _fold_words([
    "मेरी", "मेरे", "मेरा", "तुम्हारी", "आपकी", "यह", "ये", "वो", "वह", "फेक", "झूठी", "अच्छी", "बुरी", "अच्छा",
    "बुरा", "कोई", "क्या", "कौन", "कब", "कहाँ", "कहां", "कैसे", "क्यों", "और", "या", "पर", "में", "से", "को", "का",
    "की", "के", "है", "हैं", "सुनाओ", "बताओ", "दिखाओ", "पढ़ो", "खोलो", "चलाओ", "बजाओ"])
_HN_TOPIC_BAD = _c(r"(?:ऐप|एप|ऐप्लिकेशन|एप्लिकेशन|चैनल|वेबसाइट|साइट|अखबार|अख़बार|टीवी)")

_WHERE_STOP_EN = frozenset({"my", "your", "our", "this", "that", "here", "there", "his", "her", "their", "life",
                            "between", "with", "the meeting", "the office", "the house", "the game",
                            "the match", "the movie", "the room", "the kitchen", "the class", "the call"})


def _count_from(g: Optional[str]) -> int:
    if not g:
        return 0
    if g.isdigit():
        return int(g)
    return _EN_NUM.get(g, 0)


def _news_query_ok(q: str) -> bool:
    if not q:
        return False
    low = q.lower()
    words = low.split()
    if not words or len(words) > 8:
        return False
    if words[0] in _QUERY_BAD_FIRST or _QUERY_BAD_WORDS.search(low):
        return False
    return True


def _match_news(u: _U) -> Optional[Tuple[dict, int]]:
    """(topic spec, count) if `u` is a request for the news, else None."""
    f = u.f
    if not f or len(f.split()) > 14:
        return None
    if re.match(r"(?:open|launch|start|close|go to|visit|switch to|download|install|uninstall|delete|turn|mute) ", f):
        return None
    if f in ("what is news", "what are news", "what is a news", "what is the news channel"):
        return None
    m = _NEWS_GENERIC.fullmatch(f)
    if m:
        return _topic_spec(""), _count_from(m.groupdict().get("n"))
    if _NEWS_INNEWS.fullmatch(f):
        return _topic_spec(""), 0
    m = _NEWS_WORLD2.fullmatch(f)
    if m:
        return _topic_spec("world"), 0
    m = _NEWS_ABOUT.fullmatch(f)
    if m:
        q = _clean_slot(u.group(m, "q"))
        if _news_query_ok(q):
            return _topic_spec(q), 0
        return None
    m = _NEWS_LATEST_ON.fullmatch(f)
    if m:
        q = _clean_slot(u.group(m, "q"))
        if _news_query_ok(q):
            return _topic_spec(q), 0
        return None
    m = _NEWS_WORLD.fullmatch(f)
    if m:
        where = _clean_slot(u.group(m, "where"))
        wl = where.lower()
        if not where or wl in _WHERE_STOP_EN or wl.split()[0] in _WHERE_STOP_EN:
            return None
        if wl in ("news",):
            return _topic_spec(""), 0
        if wl in ("world", "whole world", "entire world"):
            return _topic_spec("world"), 0
        if wl in ("country", "nation", "india"):
            return _topic_spec("india"), 0
        if not _news_query_ok(where):
            return None
        return _topic_spec(where), 0
    m = _NEWS_TOPIC_EN.fullmatch(f)
    if m:
        toks = _tok_spans(m.group("topic"))
        while toks and toks[0][0] in _TOPIC_MOD_WORDS and toks[0][0] not in _TOPIC_LOOKUP:
            toks.pop(0)
        if not toks:
            return _topic_spec(""), _count_from(m.groupdict().get("n"))
        if toks[0][0] in _TOPIC_STOP_EN or len(toks) > 3:
            return None
        ptxt = _clean_slot(u.span(m.start("topic") + toks[0][1], m.end("topic")))
        if _QUERY_BAD_WORDS.search(ptxt.lower()):
            return None
        return _topic_spec(ptxt), _count_from(m.groupdict().get("n"))
    return _match_news_hindi(u)


def _match_news_hindi(u: _U) -> Optional[Tuple[dict, int]]:
    f = u.f
    m = _HNEWS_GENERIC.fullmatch(f)
    if m:
        return _topic_spec(""), _count_from(m.groupdict().get("n"))
    m = _HNEWS_WORLD.fullmatch(f)
    if m:
        where = _clean_slot(u.group(m, "where"))
        wf = _fold_pat(where).lower()
        if not where or any(t in _HN_WHERE_STOP for t in wf.split()):
            return None
        if wf in _HN_WHERE_WORLD:
            return _topic_spec("world"), 0
        if wf in _HN_WHERE_NATION:
            return _topic_spec("india"), 0
        if len(wf.split()) <= 2:
            return _topic_spec(where), 0
        return None
    for rx in (_HNEWS_TOPIC, _HNEWS_ABOUT, _HNEWS_TOPIC2):
        m = rx.fullmatch(f)
        if not m:
            continue
        topic = _clean_slot(u.group(m, "topic"))
        tf = _fold_pat(topic).lower().split()
        while tf and tf[0] in _TOPIC_MOD_WORDS:
            tf.pop(0)
            topic = " ".join(topic.split()[1:])
        if not tf:
            return _topic_spec(""), _count_from(m.groupdict().get("n"))
        if tf[0] in _HN_TOPIC_STOP or _HN_TOPIC_BAD.search(_fold_pat(topic).lower()):
            continue
        return _topic_spec(topic), _count_from(m.groupdict().get("n"))
    m = _HNEWS_LAT.fullmatch(f)
    if m and (m.group("w") != "news" or m.group("v")):
        topic = m.group("topic") or ""
        if topic and topic in ("aaj", "taaza", "taza", "latest", "abhi"):
            topic = ""
        return _topic_spec(topic), 0
    m = _HNEWS_LAT_WORLD.fullmatch(f)
    if m:
        return _topic_spec("india" if m.group("where") == "desh" else "world"), 0
    return None


def try_handle_news(transcript: str) -> Optional[str]:
    """The finished sentence if `transcript` asks for the news (or "more news"), else None."""
    try:
        u = _prep(transcript, max_words=14)
        if u is None:
            return None
        f = u.f
        if _NEWS_MORE.fullmatch(f) or _NEWS_ELSE.fullmatch(f) or _HNEWS_MORE.fullmatch(f) \
                or _HNEWS_LAT_MORE.fullmatch(f):
            logger.info("News matcher: 'more news' (%r)", transcript)
            return more_news()
        if f in _NEWS_BARE_MORE:
            if _news_active(_BARE_MORE_TTL):
                logger.info("News matcher: bare %r right after the news -> more news", f)
                return more_news()
            return None
        hit = _match_news(u)
    except Exception:
        logger.exception("News matcher failed on %r", transcript)
        return None
    if hit is None:
        return None
    spec, count = hit
    logger.info("News matcher: %r -> %s %s (count %s)", transcript, spec["kind"],
                spec["key"] or spec["query"] or "top", count or "default")
    try:
        return _get_news_spec(spec, count)
    except Exception:
        logger.exception("News request failed")
        return lang.tr(_NT, "sorry")


def news_briefing_line(n: int = 3) -> str:
    """Top headlines as one or two spoken sentences for the morning briefing; "" on any failure."""
    try:
        n = max(1, min(6, int(n)))
        items = _fetch_items(_topic_spec(""), lang.current() == "hi")
        titles = [_sentence(_speakable(i.title)) for i in items[:n]]
        titles = [t for t in titles if t]
        if not titles:
            return ""
        return lang.tr(_NT, "brief", items=" ".join(titles))
    except Exception as e:
        logger.warning("news_briefing_line failed: %s", e)
        return ""


# ================================================================== WEATHER

# WMO weather code -> (English phrase, Hindi clause now, Hindi clause for a forecast)
WMO: Dict[int, Tuple[str, str, str]] = {
    0: ("clear", "आसमान साफ़ है", "आसमान साफ़ रहेगा"),
    1: ("mainly clear", "आसमान ज़्यादातर साफ़ है", "आसमान ज़्यादातर साफ़ रहेगा"),
    2: ("partly cloudy", "आसमान आंशिक रूप से बादलों से घिरा है", "आसमान आंशिक रूप से बादलों से घिरा रहेगा"),
    3: ("overcast", "आसमान पूरी तरह बादलों से ढका है", "आसमान पूरी तरह बादलों से ढका रहेगा"),
    45: ("foggy", "कोहरा छाया हुआ है", "कोहरा छाया रहेगा"),
    48: ("foggy with frost", "पाले के साथ कोहरा छाया हुआ है", "पाले के साथ कोहरा छाया रहेगा"),
    51: ("light drizzle", "हल्की बूंदाबांदी हो रही है", "हल्की बूंदाबांदी होगी"),
    53: ("drizzle", "बूंदाबांदी हो रही है", "बूंदाबांदी होगी"),
    55: ("heavy drizzle", "तेज़ बूंदाबांदी हो रही है", "तेज़ बूंदाबांदी होगी"),
    56: ("light freezing drizzle", "हल्की ठंडी बूंदाबांदी हो रही है", "हल्की ठंडी बूंदाबांदी होगी"),
    57: ("freezing drizzle", "ठंडी बूंदाबांदी हो रही है", "ठंडी बूंदाबांदी होगी"),
    61: ("light rain", "हल्की बारिश हो रही है", "हल्की बारिश होगी"),
    63: ("rain", "बारिश हो रही है", "बारिश होगी"),
    65: ("heavy rain", "तेज़ बारिश हो रही है", "तेज़ बारिश होगी"),
    66: ("light freezing rain", "हल्की बर्फ़ीली बारिश हो रही है", "हल्की बर्फ़ीली बारिश होगी"),
    67: ("freezing rain", "बर्फ़ीली बारिश हो रही है", "बर्फ़ीली बारिश होगी"),
    71: ("light snow", "हल्की बर्फ़बारी हो रही है", "हल्की बर्फ़बारी होगी"),
    73: ("snow", "बर्फ़बारी हो रही है", "बर्फ़बारी होगी"),
    75: ("heavy snow", "भारी बर्फ़बारी हो रही है", "भारी बर्फ़बारी होगी"),
    77: ("snow grains", "बर्फ़ के दाने गिर रहे हैं", "बर्फ़ के दाने गिरेंगे"),
    80: ("light rain showers", "हल्की बौछारें पड़ रही हैं", "हल्की बौछारें पड़ेंगी"),
    81: ("rain showers", "बारिश की बौछारें पड़ रही हैं", "बारिश की बौछारें पड़ेंगी"),
    82: ("heavy rain showers", "तेज़ बौछारें पड़ रही हैं", "तेज़ बौछारें पड़ेंगी"),
    85: ("light snow showers", "हल्की बर्फ़ की बौछारें पड़ रही हैं", "हल्की बर्फ़ की बौछारें पड़ेंगी"),
    86: ("heavy snow showers", "भारी बर्फ़ की बौछारें पड़ रही हैं", "भारी बर्फ़ की बौछारें पड़ेंगी"),
    95: ("thunderstorms", "आंधी-तूफ़ान के साथ बारिश हो रही है", "आंधी-तूफ़ान के साथ बारिश होगी"),
    96: ("thunderstorms with hail", "गरज के साथ ओले पड़ रहे हैं", "गरज के साथ ओले पड़ेंगे"),
    99: ("heavy thunderstorms with hail", "भारी ओलों के साथ आंधी-तूफ़ान है", "भारी ओलों के साथ आंधी-तूफ़ान रहेगा"),
}
_WMO_UNKNOWN = ("changeable weather", "मौसम मिला-जुला है", "मौसम मिला-जुला रहेगा")
_WET_CODES = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99})
_SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})


def describe_weather(code, future: bool = False, language: Optional[str] = None) -> str:
    """WMO weather code -> English phrase ('partly cloudy') or Hindi clause ('आसमान ... है' / '... रहेगा')."""
    language = language or lang.current()
    try:
        entry = WMO.get(int(code), _WMO_UNKNOWN)
    except (TypeError, ValueError):
        entry = _WMO_UNKNOWN
    if language == "hi":
        return entry[2] if future else entry[1]
    return entry[0]


_WT: Dict[str, Dict[str, str]] = {
    "now": {"en": "It's {t} degrees and {cond} in {place}{feels}.{today}",
            "hi": "{place} में अभी {t} डिग्री है और {cond}{feels}।{today}"},
    "feels": {"en": ", feels like {f}", "hi": ", {f} डिग्री जैसा महसूस हो रहा है"},
    "today": {"en": " Today's high is {hi}, low {lo}{rain}.",
              "hi": " आज का अधिकतम तापमान {hi} और न्यूनतम {lo} डिग्री है{rain}।"},
    "today_hi_only": {"en": " Today's high is {hi}.", "hi": " आज का अधिकतम तापमान {hi} डिग्री है।"},
    "rain_c": {"en": ", {p} percent chance of {what}", "hi": ", और {what} की संभावना {p} प्रतिशत है"},
    "rain_c_zero": {"en": ", no {what} expected", "hi": ", और {what} की कोई संभावना नहीं है"},
    "fc": {"en": "{Day} in {place} it'll be {cond}, with a high of {hi} and a low of {lo}{rain}.",
           "hi": "{Day} {place} में {cond}, अधिकतम तापमान {hi} और न्यूनतम {lo} डिग्री रहेगा{rain}।"},
    "fc_rain": {"en": ", and the chance of {what} is {p} percent", "hi": ", और {what} की संभावना {p} प्रतिशत है"},
    "fc_rain_zero": {"en": ", and no {what} expected", "hi": ", और {what} की कोई संभावना नहीं है"},
    "fc_nohilo": {"en": "{Day} in {place} it'll be {cond}.", "hi": "{Day} {place} में {cond}।"},
    "temp_now": {"en": "It's {t} degrees in {place} right now{feels}.{today}",
                 "hi": "{place} में अभी {t} डिग्री है{feels}।{today}"},
    "temp_fc": {"en": "The high in {place} {day} is {hi} degrees and the low is {lo}.",
                "hi": "{day} {place} में अधिकतम तापमान {hi} और न्यूनतम {lo} डिग्री रहेगा।"},
    "rain_now": {"en": "Yes, it's {what_ing} in {place} right now.", "hi": "हाँ, {place} में अभी {what_ing} हो रही है।"},
    "rain_yes": {"en": "Yes, the chance of {what} in {place} {day} is {p} percent.",
                 "hi": "हाँ, {day} {place} में {what} की संभावना {p} प्रतिशत है।"},
    "rain_yes_np": {"en": "Yes, {what} is expected in {place} {day}.", "hi": "हाँ, {day} {place} में {what} हो सकती है।"},
    "rain_maybe": {"en": "It might. The chance of {what} in {place} {day} is {p} percent.",
                   "hi": "हो सकती है। {day} {place} में {what} की संभावना {p} प्रतिशत है।"},
    "rain_unlikely": {"en": "Probably not. The chance of {what} in {place} {day} is only {p} percent.",
                      "hi": "शायद नहीं। {day} {place} में {what} की संभावना सिर्फ़ {p} प्रतिशत है।"},
    "rain_no": {"en": "No, no {what} is expected in {place} {day}.",
                "hi": "नहीं, {day} {place} में {what} की कोई संभावना नहीं है।"},
    "umb_yes": {"en": "Yes, better take an umbrella. The chance of {what} in {place} {day} is {p} percent.",
                "hi": "हाँ, छाता साथ रख लीजिए। {day} {place} में {what} की संभावना {p} प्रतिशत है।"},
    "umb_yes_np": {"en": "Yes, better take an umbrella. {What} is expected in {place} {day}.",
                   "hi": "हाँ, छाता साथ रख लीजिए। {day} {place} में {what} हो सकती है।"},
    "umb_no": {"en": "No, you probably won't need an umbrella. The chance of {what} in {place} {day} is only {p} percent.",
               "hi": "नहीं, शायद छाते की ज़रूरत नहीं पड़ेगी। {day} {place} में {what} की संभावना सिर्फ़ {p} प्रतिशत है।"},
    "umb_no_zero": {"en": "No, no {what} is expected in {place} {day}, so you won't need an umbrella.",
                    "hi": "नहीं, {day} {place} में {what} की कोई संभावना नहीं है, इसलिए छाते की ज़रूरत नहीं है।"},
    "brief": {"en": "Today in {place} it's {t} degrees, high {hi}{rain}.",
              "hi": "आज {place} में अभी {t} डिग्री है, अधिकतम तापमान {hi} डिग्री{rain}।"},
    "brief_nohi": {"en": "Today in {place} it's {t} degrees{rain}.", "hi": "आज {place} में अभी {t} डिग्री है{rain}।"},
    "brief_rain": {"en": ", with {p} percent chance of rain", "hi": ", और बारिश की संभावना {p} प्रतिशत है"},
    "brief_rain_zero": {"en": ", with no rain expected", "hi": ", और बारिश की कोई संभावना नहीं है"},
    "notfound": {"en": "I couldn't find a place called {p}.", "hi": "मुझे {p} नाम की कोई जगह नहीं मिली।"},
    "fail": {"en": "I couldn't get the weather right now. Please check the internet connection and try again.",
             "hi": "अभी मौसम की जानकारी नहीं मिल पा रही है। कृपया इंटरनेट कनेक्शन देखिए और फिर से पूछिए।"},
    "noloc": {"en": "I'm not sure where you are. Say a city, like weather in Mumbai, or set a default location "
                    "in the settings.",
              "hi": "मुझे नहीं पता कि आप कहाँ हैं। किसी शहर का नाम बोलिए, जैसे मुंबई का मौसम, या सेटिंग्स में डिफ़ॉल्ट "
                    "जगह चुन लीजिए।"},
    "sorry": {"en": "Sorry, something went wrong while checking the weather.",
              "hi": "माफ़ कीजिए, मौसम देखते समय कुछ गड़बड़ हो गई।"},
}
_WHAT = {"rain": {"en": "rain", "hi": "बारिश"}, "snow": {"en": "snow", "hi": "बर्फ़बारी"}}
_WHAT_ING = {"rain": {"en": "raining", "hi": "बारिश"}, "snow": {"en": "snowing", "hi": "बर्फ़बारी"}}
_DAY_EN = ["today", "tomorrow", "the day after tomorrow"]
_DAY_HI = ["आज", "कल", "परसों"]


def _day_word(i: int) -> str:
    return (_DAY_HI if lang.current() == "hi" else _DAY_EN)[max(0, min(2, i))]


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _deg(x) -> Optional[int]:
    try:
        return int(math.floor(float(x) + 0.5))
    except (TypeError, ValueError):
        return None


def _dw(v: Optional[int]) -> str:
    """A temperature as spoken text ('29', 'minus 3')."""
    if v is None:
        return "?"
    if v < 0:
        return f"{'माइनस' if lang.current() == 'hi' else 'minus'} {abs(v)}"
    return str(v)


_HI_PLACES_RAW = {
    "मुंबई": "Mumbai", "बंबई": "Mumbai", "दिल्ली": "Delhi", "नई दिल्ली": "New Delhi", "कोलकाता": "Kolkata",
    "कलकत्ता": "Kolkata", "चेन्नई": "Chennai", "मद्रास": "Chennai", "बेंगलुरु": "Bengaluru", "बेंगलूरु": "Bengaluru",
    "बैंगलोर": "Bengaluru", "बंगलौर": "Bengaluru", "हैदराबाद": "Hyderabad", "पुणे": "Pune", "पूना": "Pune",
    "अहमदाबाद": "Ahmedabad", "जयपुर": "Jaipur", "लखनऊ": "Lucknow", "कानपुर": "Kanpur", "पटना": "Patna",
    "भोपाल": "Bhopal", "इंदौर": "Indore", "नागपुर": "Nagpur", "सूरत": "Surat", "चंडीगढ़": "Chandigarh",
    "गुवाहाटी": "Guwahati", "रांची": "Ranchi", "वाराणसी": "Varanasi", "बनारस": "Varanasi", "आगरा": "Agra",
    "अमृतसर": "Amritsar", "देहरादून": "Dehradun", "शिमला": "Shimla", "गोवा": "Goa", "कोच्चि": "Kochi",
    "कोचीन": "Kochi", "तिरुवनंतपुरम": "Thiruvananthapuram", "भुवनेश्वर": "Bhubaneswar", "रायपुर": "Raipur",
    "जोधपुर": "Jodhpur", "उदयपुर": "Udaipur", "मेरठ": "Meerut", "गाज़ियाबाद": "Ghaziabad", "नोएडा": "Noida",
    "गुड़गांव": "Gurgaon", "गुरुग्राम": "Gurugram", "लंदन": "London", "न्यूयॉर्क": "New York", "न्यूयार्क": "New York",
    "पेरिस": "Paris", "दुबई": "Dubai", "टोक्यो": "Tokyo", "सिंगापुर": "Singapore", "वाशिंगटन": "Washington",
    "ढाका": "Dhaka", "काठमांडू": "Kathmandu", "कराची": "Karachi", "लाहौर": "Lahore", "सिडनी": "Sydney",
    "बीजिंग": "Beijing", "मॉस्को": "Moscow", "मास्को": "Moscow", "बर्लिन": "Berlin", "रोम": "Rome",
    "टोरंटो": "Toronto", "शिकागो": "Chicago", "सैन फ्रांसिस्को": "San Francisco", "लॉस एंजेलेस": "Los Angeles",
}
_HI_PLACES = {_fold_pat(k).lower(): v for k, v in _HI_PLACES_RAW.items()}

_DEFAULT_PLACE_WORDS = frozenset({
    "home", "my home", "my city", "my area", "my location", "my town", "my place", "the city", "this city",
    "the area", "this area", "this town", "our city", "my neighborhood", "my neighbourhood", "town", "city",
    "my current location", "current location", "my current city"})


def _ip_location() -> Optional[dict]:
    """A guess of where the user is from the IP address (cached for the session; failures for 5 minutes)."""
    cached = _ip_cache.get("loc")
    if cached:
        return cached                                       # type: ignore[return-value]
    failed = _ip_cache.get("failed_at")
    if failed is not None and _monotonic() - float(failed) < 300:
        return None
    try:
        data = netutil.get_json("https://ipwho.is/", timeout=_timeout())
        if not isinstance(data, dict) or not data.get("success", True):
            raise ValueError("ipwho.is refused the request")
        lat, lon = float(data["latitude"]), float(data["longitude"])
        name = str(data.get("city") or data.get("region") or data.get("country") or "").strip()
        loc = {"name": name or "your area", "spoken": name or "", "lat": lat, "lon": lon}
        _ip_cache["loc"] = loc
        logger.info("Weather: location guessed from the IP address: %s", name)
        return loc
    except Exception as e:
        logger.warning("Weather: could not guess the location from the IP address (%s)", e)
        _ip_cache["failed_at"] = _monotonic()
        return None


def _geocode_query(name: str) -> Optional[dict]:
    url = ("https://geocoding-api.open-meteo.com/v1/search?name=" + urllib.parse.quote(name)
           + "&count=1&language=en&format=json")
    data = netutil.get_json(url, timeout=_timeout())
    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        return None
    r = results[0]
    return {"name": str(r.get("name") or name), "lat": float(r["latitude"]), "lon": float(r["longitude"]),
            "timezone": r.get("timezone"), "country": r.get("country"), "admin1": r.get("admin1")}


def _geocode(name: str) -> Optional[dict]:
    """Place name -> {"name", "lat", "lon", ...} or None when there is no such place. Raises on network errors."""
    key = _fold_pat(name).lower().strip()
    if key in _geo_cache:
        return _geo_cache[key]
    tries = [_HI_PLACES.get(key), name]
    if "," in name:
        tries.append(name.split(",", 1)[0].strip())
    if lang.has_devanagari(name):
        tries.append(lang.to_latin(name))
    seen = set()
    for cand in tries:
        if not cand or cand.lower() in seen:
            continue
        seen.add(cand.lower())
        loc = _geocode_query(cand)
        if loc:
            _geo_cache[key] = loc
            return loc
    return None


def _resolve_place(place: str) -> Tuple[Optional[dict], str]:
    """(location, "") or (None, spoken error). Empty place = DEFAULT_LOCATION, else a guess from the IP."""
    place = re.sub(r"\s+", " ", str(place or "")).strip(" .,!?")
    if place and place.lower() in _DEFAULT_PLACE_WORDS:
        place = ""
    if place:
        try:
            loc = _geocode(place)
        except Exception as e:
            logger.warning("Weather: geocoding %r failed: %s", place, e)
            return None, lang.tr(_WT, "fail")
        if not loc:
            logger.info("Weather: no place called %r", place)
            return None, lang.tr(_WT, "notfound", p=place)
        loc = dict(loc)
        loc["spoken"] = place if lang.has_devanagari(place) else loc["name"]
        return loc, ""
    default = str(_cfg("DEFAULT_LOCATION", "") or "").strip()
    if default:
        try:
            loc = _geocode(default)
        except Exception as e:
            logger.warning("Weather: geocoding DEFAULT_LOCATION %r failed: %s", default, e)
            return None, lang.tr(_WT, "fail")
        if loc:
            loc = dict(loc)
            loc["spoken"] = default if lang.has_devanagari(default) else loc["name"]
            return loc, ""
        logger.warning("Weather: DEFAULT_LOCATION %r was not found; guessing from the IP address", default)
    loc = _ip_location()
    if loc:
        return dict(loc), ""
    return None, lang.tr(_WT, "noloc")


_FORECAST_URL = ("https://api.open-meteo.com/v1/forecast?latitude={lat:.4f}&longitude={lon:.4f}"
                 "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day"
                 "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                 "&forecast_days=3&timezone=auto")


def _forecast(loc: dict) -> dict:
    data = netutil.get_json(_FORECAST_URL.format(lat=loc["lat"], lon=loc["lon"]), timeout=_timeout())
    if not isinstance(data, dict) or not isinstance(data.get("current"), dict) \
            or not isinstance(data.get("daily"), dict):
        raise ValueError("unexpected forecast format")
    return data


def _daily(data: dict, key: str, i: int):
    arr = data["daily"].get(key)
    if isinstance(arr, list) and 0 <= i < len(arr):
        return arr[i]
    return None


def _what(code) -> str:
    try:
        return "snow" if int(code) in _SNOW_CODES else "rain"
    except (TypeError, ValueError):
        return "rain"


def _rain_clause(key_pos: str, key_zero: str, p: Optional[int], what: str) -> str:
    if p is None:
        return ""
    w = lang.tr(_WHAT, what)
    if p <= 0:
        return lang.tr(_WT, key_zero, what=w)
    return lang.tr(_WT, key_pos, p=p, what=w)


def _feels_clause(cur: dict) -> str:
    t, f = _deg(cur.get("temperature_2m")), _deg(cur.get("apparent_temperature"))
    if t is None or f is None or abs(f - t) < 2:
        return ""
    return lang.tr(_WT, "feels", f=_dw(f))


def _weather_reply(loc: dict, data: dict, when: int, focus: str) -> str:
    """The spoken answer. when: 0 today/now, 1 tomorrow, 2 the day after. focus: general | temp | rain | umbrella."""
    place = loc.get("spoken") or loc.get("name") or ""
    cur, i = data["current"], max(0, min(2, when))
    t = _deg(cur.get("temperature_2m"))
    code_now = cur.get("weather_code")
    hi, lo = _deg(_daily(data, "temperature_2m_max", i)), _deg(_daily(data, "temperature_2m_min", i))
    p_raw = _daily(data, "precipitation_probability_max", i)
    p = _deg(p_raw) if p_raw is not None else None
    code_day = _daily(data, "weather_code", i)
    if code_day is None:
        code_day = code_now
    day = _day_word(i)

    if focus in ("rain", "umbrella"):
        what_key = _what(code_day)
        what = lang.tr(_WHAT, what_key)
        raining_now = i == 0 and code_now is not None and _safe_int(code_now) in (_WET_CODES | _SNOW_CODES)
        if focus == "rain" and raining_now:
            return lang.tr(_WT, "rain_now", place=place, what_ing=lang.tr(_WHAT_ING, _what(code_now)))
        wet_day = _safe_int(code_day) in (_WET_CODES | _SNOW_CODES)
        if focus == "umbrella":
            if raining_now or (p is not None and p >= 30) or (p is None and wet_day):
                if p is not None and not raining_now:
                    return lang.tr(_WT, "umb_yes", p=p, what=what, place=place, day=day)
                return lang.tr(_WT, "umb_yes_np", What=_cap(what), what=what, place=place, day=day)
            if p:
                return lang.tr(_WT, "umb_no", p=p, what=what, place=place, day=day)
            return lang.tr(_WT, "umb_no_zero", what=what, place=place, day=day)
        if p is None:
            key = "rain_yes_np" if wet_day else "rain_no"
            return lang.tr(_WT, key, p=0, what=what, place=place, day=day)
        if p >= 60:
            return lang.tr(_WT, "rain_yes", p=p, what=what, place=place, day=day)
        if p >= 30:
            return lang.tr(_WT, "rain_maybe", p=p, what=what, place=place, day=day)
        if p > 0:
            return lang.tr(_WT, "rain_unlikely", p=p, what=what, place=place, day=day)
        return lang.tr(_WT, "rain_no", what=what, place=place, day=day)

    if focus == "temp":
        if i == 0:
            today = lang.tr(_WT, "today_hi_only", hi=_dw(hi)) if hi is not None else ""
            return lang.tr(_WT, "temp_now", t=_dw(t), place=place, feels=_feels_clause(cur), today=today)
        return lang.tr(_WT, "temp_fc", place=place, day=day, hi=_dw(hi), lo=_dw(lo))

    if i == 0:
        today = ""
        if hi is not None and lo is not None:
            today = lang.tr(_WT, "today", hi=_dw(hi), lo=_dw(lo),
                            rain=_rain_clause("rain_c", "rain_c_zero", p, _what(code_day)))
        return lang.tr(_WT, "now", t=_dw(t), cond=describe_weather(code_now), place=place,
                       feels=_feels_clause(cur), today=today)
    cond = describe_weather(code_day, future=True)
    if hi is None or lo is None:
        return lang.tr(_WT, "fc_nohilo", Day=_cap(day), place=place, cond=cond)
    return lang.tr(_WT, "fc", Day=_cap(day), place=place, cond=cond, hi=_dw(hi), lo=_dw(lo),
                   rain=_rain_clause("fc_rain", "fc_rain_zero", p, _what(code_day)))


def _safe_int(x) -> int:
    try:
        return int(x)
    except (TypeError, ValueError):
        return -1


def _parse_when(when) -> Tuple[int, str]:
    """Sloppy `when` argument -> (day index 0-2, focus)."""
    w = str(when or "").lower()
    day = 0
    if re.search(r"day after|parso|परसों|परसो", w):
        day = 2
    elif re.search(r"tomorrow|kal\b|कल", w):
        day = 1
    focus = "general"
    if re.search(r"umbrella|छाता|छतरी", w):
        focus = "umbrella"
    elif re.search(r"rain|snow|बारिश|बरसात", w):
        focus = "rain"
    elif re.search(r"temp|hot|cold|warm|गर्मी|ठंड|तापमान", w):
        focus = "temp"
    return day, focus


def _weather_answer(place: str, day: int, focus: str) -> str:
    loc, err = _resolve_place(place)
    if loc is None:
        return err
    try:
        data = _forecast(loc)
    except Exception as e:
        logger.warning("Weather: forecast failed: %s", e)
        return lang.tr(_WT, "fail")
    reply = _weather_reply(loc, data, day, focus)
    logger.info("Weather: %s (day %d, %s) -> %s", loc.get("name"), day, focus, reply)
    return reply


def get_weather(place: str = "", when: str = "today", **_ignored) -> str:
    """Weather for `place` (default: the configured / guessed location) for today, tomorrow or the day after."""
    try:
        day, focus = _parse_when(when)
        return _weather_answer(str(place or "").strip() if place is not None else "", day, focus)
    except Exception:
        logger.exception("get_weather failed")
        return lang.tr(_WT, "sorry")


def weather_briefing_line() -> str:
    """One sentence about today's weather for the morning briefing; "" on any failure."""
    try:
        loc, _err = _resolve_place("")
        if loc is None:
            return ""
        data = _forecast(loc)
        t = _deg(data["current"].get("temperature_2m"))
        hi = _deg(_daily(data, "temperature_2m_max", 0))
        if t is None:
            return ""
        p_raw = _daily(data, "precipitation_probability_max", 0)
        p = _deg(p_raw) if p_raw is not None else None
        rain = _rain_clause("brief_rain", "brief_rain_zero", p, "rain")
        key = "brief" if hi is not None else "brief_nohi"
        return lang.tr(_WT, key, place=loc.get("spoken") or loc.get("name"), t=_dw(t), hi=_dw(hi), rain=rain)
    except Exception as e:
        logger.warning("weather_briefing_line failed: %s", e)
        return ""


# ---- the matcher

_W_LEAD = (r"(?:(?:what(?:'s| is| are)|whats|how(?:'s| is)|hows|tell me|give me|show me|get me|read me|"
           r"let me know|i want to know|i wanna know|check|do you know|what about|can i get|i need|i want|"
           r"is there) )?")
_W_REST = r"(?P<rest>(?: .+)?)"
_W_GEN = _c(rf"{_W_LEAD}(?P<pre>(?:(?:the|today's|tomorrow's|current|latest|local|outside|outdoor) )*)"
            rf"(?:weather(?: (?:report|update|forecast|conditions?|situation|status|outlook))?|forecast)"
            rf"(?: like)?{_W_REST}")
_W_TEMP = _c(rf"{_W_LEAD}(?P<pre>(?:(?:the|today's|tomorrow's|current|outside|outdoor|outdoors|air) )*)"
             rf"temperature{_W_REST}")
_W_HOW = _c(rf"how (?P<adj>hot|cold|warm|chilly|freezing|humid|windy){_W_REST}")
_W_ISIT = _c(r"(?:is|will) it (?:be |get |going to be |gonna be |going to get |gonna get |getting )?"
             r"(?P<adj>hot|cold|warm|chilly|freezing|boiling|humid|windy|sunny|cloudy|foggy|snowing|raining|"
             rf"rainy|stormy){_W_REST}")
_W_OUT = _c(rf"(?:what(?:'s| is)|how(?:'s| is)|whats|hows) it (?:like )?(?:outside|out there|outdoors|out){_W_REST}")
_W_RAIN = _c(r"(?:(?:will|is|does|would|might|could) (?:it|there)|do you think (?:it will|it's going to|it is going to|"
             r"it'll|there will be))(?: (?:be|is|going to|gonna|likely to|be likely to|possibly|really|actually|to))*"
             r"(?: (?:a|any|much))?(?: (?:chance|chances|possibility|probability) of)? "
             rf"(?P<what>rain|raining|rainy|snow|snowing|snowy|storm|stormy|thunder|thunderstorm|thunderstorms|"
             rf"drizzle|showers){_W_REST}")
_W_RAIN2 = _c(r"(?:what(?:'s| is| are) the |whats the |any )?(?:chances? of|probability of|possibility of|odds of) "
              rf"(?P<what>rain|snow|storms?|showers|thunderstorms?){_W_REST}")
_W_UMB = _c(r"(?:do i|should i|shall i|must i|will i|would i|need i|do we|should we)(?: (?:really|actually))? "
            r"(?:need|take|carry|bring|want|have to|require|pack|grab)(?: to (?:take|carry|bring))?(?: (?:an|my|a))? "
            rf"(?:umbrella|brolly){_W_REST}")
_W_UMB2 = _c(rf"(?:is|will) (?:an )?umbrella (?:needed|necessary|required|useful){_W_REST}")

_W_FILLER = frozenset({
    "is", "it", "it's", "be", "will", "going", "to", "gonna", "like", "outside", "out", "there", "outdoors", "the",
    "weather", "a", "this", "now", "currently", "today", "tomorrow", "tonight", "later", "moment", "at", "right",
    "then", "again", "please", "morning", "afternoon", "evening", "day", "after", "for", "on", "get", "gets",
    "getting", "feel", "feels", "does", "do", "today's", "tomorrow's", "forecast", "up", "around", "so", "far",
    "hot", "cold", "warm", "chilly", "rain", "raining", "outside", "here"})
_W_TRAIL = frozenset({
    "outside", "out", "there", "outdoors", "right", "now", "currently", "please", "like", "today", "tomorrow",
    "tonight", "morning", "afternoon", "evening", "later", "moment", "the", "at", "day", "after", "for", "here",
    "again", "then", "this", "weather"})
_W_PREPS = frozenset({"in", "around", "near", "over", "for"})
_W_BLOCK_PLACE = frozenset({
    "app", "apps", "channel", "website", "site", "widget", "icon", "screen", "page", "window", "tab", "oven", "fridge",
    "freezer", "room", "house", "kitchen", "car", "office", "cpu", "gpu", "processor", "laptop", "computer",
    "server", "body", "pool", "water", "engine", "sun", "surface", "core", "here", "there", "me", "us", "you", "it",
    "this", "that", "them", "him", "her", "gym", "bathroom", "bedroom", "garage", "freezer", "microwave", "phone",
    "battery", "machine", "system", "lab", "class", "classroom", "movie", "song", "game", "story", "book",
    "morning", "afternoon", "evening", "night", "week", "weekend", "month", "year", "future", "past", "general",
    "particular", "detail", "details", "short", "brief", "case", "fact", "order", "time", "mind", "heart", "soul"})


def _parse_wrest(rest: str, base: int, u: _U, focus: str = "general") -> Optional[Tuple[int, str]]:
    """The text after a weather phrase ("in mumbai tomorrow", "outside right now") -> (day, place) or None when
    it holds anything that is not a time word, a filler word or a place."""
    toks = _tok_spans(rest)
    words = [t[0] for t in toks]
    preps = _W_PREPS if focus not in ("umbrella", "rain") else _W_PREPS - {"for"}
    prep_i = next((i for i, w in enumerate(words) if w in preps and i + 1 < len(words)
                   and not (w == "for" and all(x in _W_TRAIL for x in words[i + 1:]))), None)
    a = toks if prep_i is None else toks[:prep_i]
    b = [] if prep_i is None else toks[prep_i + 1:]
    if any(w not in _W_FILLER for w, _, _ in a):
        return None
    while b and b[-1][0] in _W_TRAIL:
        b.pop()
    place = ""
    if b:
        if len(b) > 4:
            return None
        bw = [w for w, _, _ in b]
        if bw[0] == "the":
            b, bw = b[1:], bw[1:]
        if not b:
            return None
        phrase = " ".join(bw)
        if phrase in _DEFAULT_PLACE_WORDS:
            place = ""
        else:
            if any(w in _W_BLOCK_PLACE for w in bw) or bw[0] in ("my", "our", "your", "his", "her", "their"):
                return None
            place = _clean_slot(u.span(base + b[0][1], base + b[-1][2]))
            if not place:
                return None
    blob = " " + " ".join(words) + " "
    if re.search(r" (?:day after|day after tomorrow|parso) ", blob):
        day = 2
    elif re.search(r" tomorrow(?:'s)? ", blob):
        day = 1
    else:
        day = 0
    return day, place


_HW_WEATHER = _fold_words(["मौसम", "वेदर", "weather", "mausam", "mosam", "पूर्वानुमान", "फोरकास्ट", "forecast"])
_HW_TEMP = _fold_words(["तापमान", "टेम्परेचर", "टेंपरेचर", "temperature", "tapman", "तापमान"])
_HW_HEAT = _fold_words(["गर्मी", "गरमी", "garmi", "ठंड", "ठण्ड", "ठंडी", "सर्दी", "thand", "sardi", "ठंडा", "गर्म"])
_HW_RAIN = _fold_words(["बारिश", "बारीश", "बरसात", "baarish", "barish", "barsaat", "barsat", "बरसात"])
_HW_UMB = _fold_words(["छाता", "छतरी", "छाते", "chhata", "chata", "अंब्रेला", "umbrella"])
_HW_SKY = _fold_words(["धूप", "dhoop", "कोहरा", "बादल", "आंधी", "तूफान", "तूफ़ान", "बर्फ़बारी", "बर्फबारी"])
_HW_TIME_TODAY = _fold_words(["आज", "अभी", "अब", "फिलहाल", "आजकल", "aaj", "abhi", "abhi", "इस", "वक्त", "वक़्त", "समय"])
_HW_TOMORROW = _fold_words(["कल", "kal"])
_HW_DAY_AFTER = _fold_words(["परसों", "परसो", "parso"])
_HW_FILLER = _fold_words([
    "का", "की", "के", "में", "मे", "कैसा", "कैसी", "कैसे", "है", "हैं", "हो", "होगा", "होगी", "होंगे", "रहेगा",
    "रहेगी", "रहेंगे", "रहा", "रही", "रहे", "क्या", "कितना", "कितनी", "कितने", "बाहर", "यहां", "यहाँ", "यहा",
    "मुझे", "हमें", "बताओ", "बताइए", "बताइये", "बता", "दो", "दीजिए", "दीजिये", "जरा", "कृपया", "सुनाओ",
    "दिखाओ", "चाहिए", "जानना", "पता", "करो", "बताएं", "बताए", "जानकारी", "हाल", "स्थिति", "रिपोर्ट", "रात",
    "सुबह", "शाम", "दोपहर", "ले", "लू", "जाऊं", "जाऊँ", "जाना", "लेना", "जरूरत", "पड़ेगी", "पड़ेगा", "लगेगी",
    "लगेगा", "से", "को", "आने", "वाला", "वाली", "वाले", "आज", "कुछ", "भी", "और", "तो", "ही", "आएगी", "आएगा",
    "गिरेगी", "गिरेगा", "पड़ेगी", "पड़ रही", "पड़", "रहा", "कैसी", "होता", "होती", "हुई", "ka", "ki", "ke", "mein", "me",
    "kaisa", "kaisi", "hai", "hain", "kitna", "kitni", "hoga", "hogi", "kya", "bahar", "bata", "batao", "do",
    "dijiye", "sunao", "chahiye", "ho", "raha", "rahi", "rahega", "rahegi", "kaise", "abhi"])
_HW_MARKERS = _fold_words(["का", "की", "के", "में", "मे", "ka", "ki", "ke", "mein", "me"])
_HW_PLACE_STOP = (_HW_WEATHER | _HW_TEMP | _HW_HEAT | _HW_RAIN | _HW_UMB | _HW_SKY | _HW_TIME_TODAY
                  | _HW_TOMORROW | _HW_DAY_AFTER | _HW_FILLER)
_HW_PLACE_MAXTOK = 3
_HW_PLACE_BLOCK = _fold_words(["मेरे", "मेरी", "मेरा", "हमारे", "हमारा", "अपने", "अपना", "कमरे", "कमरा", "घर", "फ्रिज",
                               "फ़्रिज", "ओवन", "सीपीयू", "लैपटॉप", "कंप्यूटर", "फोन", "फ़ोन", "पानी", "शरीर", "बैटरी",
                               "ऐप", "एप", "वेबसाइट", "चैनल", "विजेट", "इस", "उस", "यहां", "यहाँ", "यहा", "वहां", "वहाँ"])


def _match_weather_hindi(u: _U) -> Optional[Tuple[int, str, str]]:
    """Hindi / Hinglish weather request -> (day 0-2, place, focus) or None. Shapes: "मौसम कैसा है",
    "मुंबई का मौसम", "कल दिल्ली में बारिश होगी क्या", "आज बेंगलुरु का मौसम कैसा है"."""
    toks = _tok_spans(u.f)
    if not toks or len(toks) > 12:
        return None
    words = [t[0] for t in toks]
    keyword_sets = (_HW_WEATHER, _HW_TEMP, _HW_HEAT, _HW_RAIN, _HW_UMB, _HW_SKY)
    if not any(w in ks for w in words for ks in keyword_sets):
        return None
    day_words = _HW_TIME_TODAY | _HW_TOMORROW | _HW_DAY_AFTER
    start = 0
    while start < len(words) - 1 and words[start] in day_words:
        start += 1
    body = words[start:]
    place_end = None                                        # index in `body` of the का/की/के/में marker
    for i, w_ in enumerate(body):
        if w_ in _HW_MARKERS and 1 <= i <= _HW_PLACE_MAXTOK:
            if all(x not in _HW_PLACE_STOP for x in body[:i]):
                place_end = i
            break
    attempts = (place_end, None) if place_end else (None,)
    for attempt in attempts:
        rest = words[:start] + (body[attempt + 1:] if attempt else body)
        if not (all(w_ in _HW_PLACE_STOP for w_ in rest) and any(w_ in ks for w_ in rest for ks in keyword_sets)):
            continue
        place = ""
        if attempt:
            if any(w_ in _HW_PLACE_BLOCK for w_ in body[:attempt]):
                return None
            place = _clean_slot(u.span(toks[start][1], toks[start + attempt - 1][2]))
            if place.lower() in _DEFAULT_PLACE_WORDS:
                place = ""
        day = 0
        if any(w_ in _HW_DAY_AFTER for w_ in rest):
            day = 2
        elif any(w_ in _HW_TOMORROW for w_ in rest):
            day = 1
        if any(w_ in _HW_UMB for w_ in rest):
            focus = "umbrella"
        elif any(w_ in _HW_RAIN for w_ in rest):
            focus = "rain"
        elif any(w_ in _HW_TEMP or w_ in _HW_HEAT for w_ in rest):
            focus = "temp"
        else:
            focus = "general"
        return day, place, focus
    return None


def _match_weather(transcript) -> Optional[Tuple[int, str, str]]:
    """(day 0-2, place, focus) if `transcript` asks about the weather, else None."""
    u = _prep(transcript, max_words=14)
    if u is None:
        return None
    f = u.f
    if re.match(r"(?:open|launch|start|close|go to|visit|switch to|download|install|uninstall|delete|turn|mute|"
                r"play|search|google) ", f):
        return None
    focus = None
    m = None
    for rx, foc in ((_W_UMB, "umbrella"), (_W_UMB2, "umbrella"), (_W_RAIN, "rain"), (_W_RAIN2, "rain"),
                    (_W_HOW, "temp"), (_W_TEMP, "temp"), (_W_ISIT, None), (_W_OUT, "general"), (_W_GEN, "general")):
        m = rx.fullmatch(f)
        if m:
            focus = foc
            if rx is _W_ISIT:
                adj = m.group("adj")
                if adj in ("raining", "rainy", "stormy", "snowing"):
                    focus = "rain"
                elif adj in ("sunny", "cloudy", "foggy"):
                    focus = "general"
                else:
                    focus = "temp"
            break
    if m is None:
        r = _match_weather_hindi(u)
        return r
    rest = m.group("rest") or ""
    parsed = _parse_wrest(rest, m.start("rest"), u, focus or "general")
    if parsed is None:
        return None
    day, place = parsed
    pre = m.groupdict().get("pre") or ""
    if "tomorrow's" in pre and day == 0:
        day = 1
    if re.search(r"\bin (?:here|there)$", f) and not place:
        return None
    return day, place, focus or "general"


def try_handle_weather(transcript: str) -> Optional[str]:
    """The spoken weather answer if `transcript` asks about the weather, else None."""
    try:
        hit = _match_weather(transcript)
    except Exception:
        logger.exception("Weather matcher failed on %r", transcript)
        return None
    if hit is None:
        return None
    day, place, focus = hit
    logger.info("Weather matcher: %r -> place=%r day=%d focus=%s", transcript, place or "(default)", day, focus)
    try:
        return _weather_answer(place, day, focus)
    except Exception:
        logger.exception("Weather request failed")
        return lang.tr(_WT, "sorry")


# ================================================================== WEB SEARCH (Gemini + Google Search, or ddgs)

class Answer(str):
    """A finished spoken answer: `.final` tells the caller to speak it verbatim, without an LLM rewrite."""
    final = True


_PLACEHOLDER_KEY = re.compile(r"^(?:your|paste|put|enter|insert|add|type|my)[_ -]|<.*>|\s|^x{4,}$|^\*+$|^\.+$|"
                              r"^(?:none|null|nil|false|no|changeme|change[_ -]me|placeholder|todo|tbd|key|api[_ -]?key|"
                              r"gemini[_ -]?api[_ -]?key|your[_ -]?key|xxx+)$", re.I)


def _gemini_key() -> str:
    for cand in (_cfg("GEMINI_API_KEY", ""), os.environ.get("GEMINI_API_KEY", ""), os.environ.get("GOOGLE_API_KEY", "")):
        key = str(cand or "").strip().strip("\"'")
        if key and not _PLACEHOLDER_KEY.search(key):
            return key
    return ""


def _gemini_models() -> List[str]:
    raw = _cfg("GEMINI_MODELS", None)
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.split(",")]
    models = [str(m).strip() for m in (raw or []) if str(m).strip()]
    return models or list(DEFAULT_GEMINI_MODELS)


def gemini_available() -> bool:
    """True when a Gemini key is configured and Gemini has not just failed (rejected key, quota, network)."""
    if not _gemini_key() or _gemini_state.get("disabled"):
        return False
    return _monotonic() >= float(_gemini_state.get("blocked_until", 0.0))


def _warn_once(kind: str, message: str) -> None:
    warned = _gemini_state.setdefault("warned", set())
    if kind in warned:                                      # type: ignore[operator]
        logger.info("Gemini: %s", message)
        return
    warned.add(kind)                                        # type: ignore[union-attr]
    logger.warning("Gemini: %s", message)


def _gemini_block(seconds: float, kind: str, message: str, permanent: bool = False) -> None:
    if permanent:
        _gemini_state["disabled"] = True
    else:
        _gemini_state["blocked_until"] = _monotonic() + seconds
    _warn_once(kind, message)


def _system_instruction() -> str:
    now = _now().astimezone()
    text = ("You are a voice assistant. Answer the question using Google Search for up-to-date facts. "
            "Reply in at most three short spoken sentences, plain text only: no markdown, no lists, no URLs, "
            "no citations, no emoji. Today is " + lang.fmt_date(now, "en", with_year=True) + ".")
    if lang.current() == "hi":
        text += " Reply in natural spoken Hindi in Devanagari script."
    return text


_EMOJI_RE = re.compile("[\U00010000-\U0010ffff☀-➿⬀-⯿️‍←-⇿]")


def _voice_text(text: str) -> str:
    """Model text -> plain speech: no markdown, links, citation marks, emoji or line breaks."""
    t = text or ""
    t = re.sub(r"\[([^\]]+)\]\((?:[^)]*)\)", r"\1", t)
    t = re.sub(r"https?://\S+|www\.\S+", "", t)
    t = re.sub(r"\[\d+(?:\s*[,\-–]\s*\d+)*\]", "", t)
    t = re.sub(r"^\s*(?:[-*•·▪◦>]+|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"[`*#~|]+", "", t)
    t = _EMOJI_RE.sub("", t)
    t = re.sub(r"\s*\n+\s*", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if len(t) > 700:
        cut = max(t.rfind(". ", 0, 700), t.rfind("। ", 0, 700), t.rfind("? ", 0, 700))
        t = t[:cut + 1] if cut > 200 else t[:700].rsplit(" ", 1)[0] + "."
    return t


def _gemini_text(data: dict) -> Tuple[str, str]:
    """(answer text, reason-if-empty) from a generateContent response: the parts of candidate 0, concatenated."""
    cands = data.get("candidates") if isinstance(data, dict) else None
    if not cands:
        block = ((data or {}).get("promptFeedback") or {}).get("blockReason") if isinstance(data, dict) else None
        return "", f"no candidates{' (blocked: ' + str(block) + ')' if block else ''}"
    cand = cands[0] or {}
    parts = ((cand.get("content") or {}).get("parts")) or []
    text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)).strip()
    if not text:
        return "", f"empty answer (finishReason {cand.get('finishReason')})"
    return text, ""


def _gemini_answer(query: str) -> Optional[Answer]:
    """Asks Gemini with Google Search grounding. None (after logging why) on any failure; the caller falls back."""
    key = _gemini_key()
    if not key or not gemini_available():
        return None
    logger.info("Gemini (Google Search) answering: %s", query)
    payload = {
        "systemInstruction": {"parts": [{"text": _system_instruction()}]},
        "contents": [{"role": "user", "parts": [{"text": query}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"maxOutputTokens": 512, "temperature": 0.3},
    }
    headers = {"x-goog-api-key": key}
    models = _gemini_models()
    working = _gemini_state.get("model")
    if working in models:
        models = [working] + [m for m in models if m != working]
    timeout = max(_timeout() * 2, 12.0)
    tried_models = []
    for model in models:
        try:
            data = netutil.post_json(_GEMINI_URL.format(model=model), payload, headers=headers, timeout=timeout)
        except netutil.NetError as e:
            status = int(getattr(e, "status", 0) or 0)
            body = str(getattr(e, "body", "") or "").lower()
            if status == 429 or "resource_exhausted" in body or "quota" in body:
                _gemini_block(120, "quota", "the free quota or rate limit is used up (HTTP 429); using DuckDuckGo "
                                            "for a couple of minutes.")
                return None
            if status in (401, 403) or (status == 400 and ("api key" in body or "api_key" in body)):
                _gemini_block(0, "key", f"Google rejected the API key or the request (HTTP {status}); using "
                                        "DuckDuckGo instead until Raziel restarts. Check GEMINI_API_KEY.",
                              permanent=True)
                return None
            if status == 404 or (status == 400 and "model" in body):
                logger.info("Gemini: model %s is not available (HTTP %s), trying the next one", model, status)
                tried_models.append(model)
                continue
            _gemini_block(60, "network", f"request failed ({e}); using DuckDuckGo for a minute.")
            return None
        except Exception as e:
            _gemini_block(60, "network", f"request failed ({e}); using DuckDuckGo for a minute.")
            return None
        text, why = _gemini_text(data)
        if not text:
            _warn_once("empty", f"model {model} gave no answer ({why}); using DuckDuckGo for this question.")
            return None
        _gemini_state["model"] = model
        spoken = _voice_text(text)
        if not spoken:
            _warn_once("empty", f"model {model} gave no speakable answer; using DuckDuckGo for this question.")
            return None
        logger.info("Gemini answered via %s: %s", model, spoken)
        return Answer(spoken)
    _gemini_block(600, "models", "none of the configured models (" + ", ".join(tried_models or models)
                  + ") could be used; using DuckDuckGo. Check GEMINI_MODELS.")
    return None


# ---- time-sensitive questions

_TS_STRONG = re.compile(
    r"\b(?:latest|newest|breaking|trending|live scores?|live updates?|scoreboard|standings|points table|"
    r"who(?:'s| is| was)? (?:won|winning|leading|trailing|batting|bowling|playing)|who won|who wins|who lost|"
    r"who scored|who is leading|who's leading|"
    r"(?:match|election|exam|board|poll|lottery|game|test|final|semi final) results?|scores?|"
    r"(?:when|what time) (?:is|does|will|do) (?:the |our )?next (?:(?:[a-z]+ ){0,3})?(?:match|game|fixture|race|launch|"
    r"election|episode|season|release|event|ipl|flight|train|bus|festival|holiday|eclipse)|"
    r"current(?:ly)? (?:price|rate|cm|pm|president|prime minister|chief minister|governor|ceo|captain|coach|champion|"
    r"leader|status|situation|score|standings|trend|value|holder|minister|mayor|speaker|head|weather|rank)|"
    r"who(?:'s| is) (?:the )?(?:current |new |present |incumbent )?(?:cm|pm|prime minister|president|chief minister|"
    r"governor|ceo|mayor|captain|coach|home minister|finance minister|defence minister|foreign minister|"
    r"speaker|chief justice|rbi governor|richest (?:man|person))|"
    r"sensex|nifty|nasdaq|dow jones|bitcoin|ethereum|crypto(?:currency)?|stock market|share market|"
    r"exchange rate|petrol price|diesel price|gold rate|silver rate|gold price|silver price|fuel price|"
    r"(?:share|stock) prices?)\b")
_TS_COMMOD = re.compile(r"\b(?:gold|silver|petrol|diesel|cng|lpg|crude(?: oil)?|dollar|rupee|euro|pound|"
                        r"iphone|samsung|tesla|oil|gas|fuel|stock|stocks|shares?|market|markets|currency|ticket|flight)\b")
_TS_PRICE = re.compile(r"\b(?:price|prices|rate|rates|cost|costs|worth|value|up|down|open|opened|closed|"
                       r"crash(?:ed|ing)?|rally|rallying|green|red|higher|lower|trading|falling|rising|record|"
                       r"today|now|current|latest)\b")
_TS_WEAK = re.compile(r"\b(?:today|tonight|yesterday|last night|right now|as of now|at the moment|this week|"
                      r"this weekend|this month|this year|currently|these days|nowadays|recently|so far|"
                      r"this morning|this evening|last week)\b")
_TS_CUE = re.compile(r"\b(?:happen(?:ed|ing|s)?|won|win|wins|winner|lost|lose|score|match|game|result|results|price|"
                     r"rate|market|stock|open|closed|launch(?:ed)?|releas(?:e|ed)|announc(?:e|ed)|died|dead|"
                     r"arrest(?:ed)?|kill(?:ed)?|resign(?:ed)?|elect(?:ed|ion)|champion|leading|trailing|traffic|"
                     r"delay(?:ed)?|cancel(?:led|ed)?|strike|protest(?:s)?|record|cost|worth|value|trending|viral|"
                     r"status|update|outage|new|news|going on|in the news|fixture|final|ipl|playing)\b")
_TS_HI_STRONG = _c(
    r"(?:सेंसेक्स|निफ्टी|नैस्डैक|बिटकॉइन|स्कोर|कौन जीता|कौन जीत रहा|कौन जीतेगा|कौन हारा|"
    r"अगला (?:मैच|आईपीएल|चुनाव|मुकाबला|फ़िल्म|फिल्म) कब|"
    r"(?:वर्तमान|मौजूदा|अभी के|आज के|नए|नये) (?:मुख्यमंत्री|प्रधानमंत्री|राष्ट्रपति|कप्तान|कोच|सीईओ)|"
    r"(?:मुख्यमंत्री|प्रधानमंत्री|राष्ट्रपति) कौन है|शेयर बाजार|शेयर बाज़ार|स्टॉक मार्केट|"
    r"(?:चुनाव|मैच|परीक्षा|बोर्ड) (?:के )?(?:नतीजे|परिणाम|रिजल्ट)|लाइव स्कोर)")
_TS_HI_COMMOD = _c(r"(?:सोने|सोना|चांदी|चाँदी|पेट्रोल|डीजल|डीज़ल|एलपीजी|सीएनजी|कच्चे तेल|डॉलर|रुपया|रुपये|शेयर|आईफोन|टेस्ला)")
_TS_HI_PRICE = _c(r"(?:भाव|कीमत|दाम|रेट|मूल्य|कितना|कितनी|ऊपर|नीचे|गिरा|चढ़ा|बढ़ा|खुला|बंद|तेजी|मंदी|गिरावट|आज|अभी)")
_TS_HI_WEAK = _c(r"(?:आज|अभी|आजकल|कल रात|इस समय|इस वक्त|इस हफ्ते|इस साल|हाल ही में)")
_TS_HI_CUE = _c(r"(?:क्या हुआ|क्या हो रहा|क्या चल रहा|हुआ|जीता|हारा|खुला|बंद|कीमत|भाव|दाम|रेट|स्कोर|मैच|नतीजे|परिणाम|"
                r"लॉन्च|रिलीज़|रिलीज|मौत|गिरफ्तार|इस्तीफा|इस्तीफ़ा|चुनाव|ट्रैफिक|ट्रैफ़िक|हड़ताल|प्रदर्शन|रिकॉर्ड|"
                r"आईपीएल|फाइनल)")


_TS_TIMELESS = re.compile(
    r"^(?:explain|define|describe|teach me|how (?:does|do|did|can|could|to)|why (?:does|do|did|is|are|was)|what does .* mean|"
    r"what(?:'s| is) (?:a|an) |meaning of|history of|who invented|who discovered|who founded|who wrote|who painted|"
    r"who composed|who directed|who was the first)\b")
_TS_HISTORY = re.compile(r"\b(?:1\d{3}|20[01]\d|202[0-4])\b|\b(?:world war|civil war|war of|battle of|independence|"
                         r"of all time|in history|ever won|ever scored|first ever|dynasty|empire|ancient|medieval|"
                         r"centur(?:y|ies)|history|historic|decade)\b")


_TS_NOT_A_QUESTION = re.compile(r"^(?:i|i'm|i've|i'd|i'll|my|we|our|play|open|set|call|send|remind|turn|start|stop|"
                                r"add|create|delete|make|save|launch|close|mute|pause|skip)\b")


def is_time_sensitive(text: str) -> bool:
    """True for a question about fast-changing facts (scores, prices, who currently holds an office, what happened
    today). Timeless questions ("what's the capital of France") are False."""
    try:
        u = _U(str(text or ""))
        f = u.f
        if not f:
            return False
        if _TS_NOT_A_QUESTION.match(f):
            return False
        recent = bool(_TS_WEAK.search(f))
        if not recent and (_TS_TIMELESS.match(f) or _TS_HISTORY.search(f)):
            return False
        if _TS_STRONG.search(f):
            return True
        if _TS_COMMOD.search(f) and _TS_PRICE.search(f):
            return True
        if _TS_WEAK.search(f) and _TS_CUE.search(f):
            return True
        if lang.has_devanagari(f):
            if _TS_HI_STRONG.search(f):
                return True
            if _TS_HI_COMMOD.search(f) and _TS_HI_PRICE.search(f):
                return True
            if _TS_HI_WEAK.search(f) and _TS_HI_CUE.search(f):
                return True
        return False
    except Exception:
        return False


# ---- DuckDuckGo path

_WS_T = {
    "ask": {"en": "What should I search for?", "hi": "क्या खोजूँ?"},
    "missing": {"en": "Web search isn't available - run pip install ddgs", "hi": "वेब सर्च उपलब्ध नहीं है - pip install ddgs चलाइए।"},
    "fail": {"en": "I couldn't complete that search right now.", "hi": "अभी मैं वह खोज पूरी नहीं कर पाया।"},
    "none": {"en": "I didn't find anything useful for that search.", "hi": "इस खोज में मुझे कुछ काम का नहीं मिला।"},
}


def _ddg_search(query: str) -> str:
    DDGS = _ddgs_module()
    if DDGS is None:
        return lang.tr(_WS_T, "missing", lang="en")
    results: list = []
    try:
        if is_time_sensitive(query) or re.search(r"\bnews\b", query.lower()):
            try:
                results = list(DDGS().news(query, max_results=4) or [])
            except Exception as e:
                logger.info("Web search: ddgs news failed (%s), trying text search", e)
                results = []
        if not results:
            results = list(DDGS().text(query, max_results=4) or [])
    except Exception as e:
        logger.error("Web search failed: %s", e)
        return lang.tr(_WS_T, "fail")
    if not results:
        return lang.tr(_WS_T, "none")
    parts = []
    for r in results[:4]:
        title = str(r.get("title") or "").strip()
        body = str(r.get("body") or "").strip()
        src = str(r.get("source") or "").strip()
        if not title and not body:
            continue
        head = f"{title} ({src})" if src else title
        parts.append(f"{head}: {body[:300]}" if head and body else (head or body[:300]))
    return " | ".join(parts) if parts else lang.tr(_WS_T, "none")


def web_search(query: str = "", **_ignored) -> str:
    """Answers a question with Google (Gemini + Search grounding) when a key is set - an `Answer` to speak verbatim -
    otherwise DuckDuckGo results as plain text for the LLM to summarise."""
    q = str(query or _ignored.get("q") or _ignored.get("search") or _ignored.get("text") or "").strip()
    if not q:
        return lang.tr(_WS_T, "ask")
    try:
        if gemini_available():
            ans = _gemini_answer(q)
            if ans is not None:
                return ans
            logger.warning("Web search: Gemini did not answer, using DuckDuckGo for: %s", q)
        logger.info("Web search (DuckDuckGo): %s", q)
        return _ddg_search(q)
    except Exception:
        logger.exception("web_search failed")
        return lang.tr(_WS_T, "fail")


# ---- explicit "google X" / "look up X" wording

_S_SITES = (r"youtube|amazon|spotify|netflix|flipkart|instagram|facebook|twitter|whatsapp|wikipedia|maps|google maps|"
            r"gmail|linkedin|reddit|github|ebay|myntra|zomato|swiggy|prime video|hotstar|jiosaavn|gaana|snapchat|"
            r"telegram|pinterest|tiktok|discord|steam|play store|app store|my files|files|folder|computer|pc|laptop|"
            r"this computer|my computer|my pc|desktop|drive|documents|downloads")
_S_SITE_TAIL = re.compile(rf"\b(?:on|in|at|from|within) (?:the )?(?:{_S_SITES})$")
_S_ENGINE_TAIL = re.compile(r"\b(?:on|using|with|via|in) (?:google|the web|the internet|the net|internet|web|bing|duckduckgo)$"
                            r"|\bonline$")
_S_APP_FIRST = frozenset({
    "maps", "map", "chrome", "drive", "docs", "sheets", "slides", "photos", "translate", "meet", "calendar", "gmail",
    "play", "home", "assistant", "earth", "keep", "classroom", "forms", "news", "pay", "lens", "fit", "it", "that",
    "this", "them", "him", "her", "again", "now", "images", "scholar", "flights", "search", "app", "account",
    "settings", "and", "or", "please", "back", "up", "for"})
_S_BAD_FIRST = frozenset({"to", "at", "from", "into", "in", "my", "up", "down", "over", "through", "and", "or",
                          "out", "onto", "upon", "the sky", "your", "you", "me", "it", "them", "him", "her", "us",
                          "toward", "towards", "for"})
_S_AUX = frozenset({"is", "are", "was", "were", "has", "have", "had", "does", "did", "can", "will", "would",
                    "should", "could", "isn't", "doesn't", "won't", "was", "be", "being", "been", "seems", "looks"})
_S_SEARCH_NOUNS = frozenset({"bar", "engine", "results", "result", "history", "settings", "page", "console", "box", "tab",
                             "bar", "app", "operators", "index", "ranking", "rankings", "algorithm", "trends", "tips"})
_S_PERSONAL = re.compile(r"\b(?:my|mine|myself|me|our)\b|\b(?:file|files|folder|folders|computer|pc|laptop|hard drive|"
                         r"desktop|documents|downloads)\b")

_S_EN = [
    _c(r"(?:do |run |perform |make |try )?(?:a )?(?:google|web|internet|online) search (?:for|on|about|of) (?P<q>.+)"),
    _c(r"search (?:on |in |with |using )?(?:google|the web|the internet|the net|web|internet|online|bing|duckduckgo)"
       r"(?: for| about| on| up| to find| to see)? (?P<q>.+)"),
    _c(r"(?:google|googling)(?: for| up| about)? (?P<q>.+)"),
    _c(r"search (?:for )?(?P<q>.+?) (?:on|in|using|with|via) (?:google|the web|the internet|the net|internet|web|"
       r"bing|duckduckgo)"),
    _c(r"search (?:for )?(?P<q>.+?) online"),
    _c(r"(?:look up|lookup|search up|look into) (?P<q>.+)"),
    _c(r"find out (?:more |a little |a bit )?(?:about|on|regarding) (?P<q>.+)"),
    _c(r"find out (?P<q>(?:who|what|when|where|why|how|which|whether|if) .+)"),
    _c(r"what does google (?:say|know|think|have)(?: (?:about|on|of|regarding))? (?P<q>.+)"),
    _c(r"ask google (?:about |for )?(?P<q>.+)"),
    _c(r"(?:google|web|internet|online) search (?P<q>.+)"),
]
_H_SITE = r"(?:गूगल|गुगल|इंटरनेट|नेट|वेब|ऑनलाइन|ऑनलाईन|ऑन लाइन|वेब सर्च|गूगल सर्च)"
_H_SVERB = (r"(?:सर्च करो|सर्च कर दो|सर्च कीजिए|सर्च कीजिये|सर्च करें|सर्च कर|सर्च|खोजो|खोजिए|खोजिये|खोज करो|"
            r"खोज कर दो|खोजें|खोजें|खोज|ढूंढो|ढूँढो|ढूंढिए|ढूँढिये|ढूंढ दो|ढूंढें|ढूंढ|पता करो|पता कीजिए|पता कर दो|"
            r"पता लगाओ|पता लगाइए|पता लगा दो|देखो|देखिए|गूगल करो|गूगल कर दो|गूगल कीजिए|पूछो)")
_S_HI = [
    _c(rf"{_H_SITE} (?:पर|में|मे|से) (?:(?:सर्च करो|सर्च कीजिए|खोजो|ढूंढो|देखो) )?(?P<q>.+?)"
       rf"(?: (?:को|के बारे में|के बारे मे|की जानकारी|के बारे में जानकारी))? {_H_SVERB}"),
    _c(rf"{_H_SITE} (?:पर|में|मे|से) {_H_SVERB} (?P<q>.+)"),
    _c(rf"(?P<q>.+?) के बारे (?:में|मे) (?:(?:गूगल|इंटरनेट|नेट) (?:पर|से) )?(?:पता करो|पता कीजिए|पता कर दो|"
       rf"पता लगाओ|पता लगाइए|पता लगा दो|जानकारी खोजो|जानकारी ढूंढो|जानकारी निकालो|सर्च करो|खोजो|ढूंढो|गूगल करो)"),
    _c(r"(?P<q>.+?) (?:को )?गूगल (?:करो|कर दो|कीजिए)"),
    _c(r"गूगल (?:करो|कर दो|कीजिए) (?P<q>.+)"),
    _c(rf"(?P<q>.+?) (?:को |के बारे में )?{_H_SITE} (?:पर )?{_H_SVERB}"),
    re.compile(r"google (?:par|pe|mein) (?P<q>.+?) (?:search karo|search kar do|dhundo|khojo|dekho)"),
    re.compile(r"(?P<q>.+?) (?:ko )?google karo"),
]
_S_HI_BAD = _c(r"(?:मेरे|मेरा|मेरी|मुझे|हमारे|अपने|फाइल|फ़ाइल|फोल्डर|कंप्यूटर|कम्प्यूटर|लैपटॉप|मैप्स|क्रोम)")


def _clean_search_query(q: str) -> str:
    q = _clean_slot(q, 200)
    for _ in range(2):
        q2 = re.sub(r"\s+(?:on google|online|on the web|on the internet|please|for me|now)$", "", q, flags=re.I).strip()
        q2 = re.sub(r"^(?:for|about|up|that|the fact that) ", "", q2, flags=re.I).strip()
        if q2 == q:
            break
        q = q2
    return q


def _extract_search_query(u: _U) -> Optional[str]:
    f = u.f
    if not f or len(f.split()) > 26:
        return None
    for rx in _S_EN:
        m = rx.fullmatch(f)
        if not m:
            continue
        q = _clean_search_query(u.group(m, "q"))
        ql = _fold_pat(q).lower()
        words = ql.split()
        if not words or len(words) > 14:
            continue
        if words[0] in _S_BAD_FIRST or (rx is _S_EN[2] and (words[0] in _S_APP_FIRST or words[0] in _S_AUX)):
            continue
        if ql.startswith("the sky") or _S_SITE_TAIL.search(ql) or _S_PERSONAL.search(ql):
            continue
        if len(words) == 1 and words[0] in _S_APP_FIRST:
            continue
        if rx is _S_EN[-1] and words[0] in _S_SEARCH_NOUNS:
            continue
        return q
    for rx in _S_HI:
        m = rx.fullmatch(f)
        if not m:
            continue
        q = _clean_search_query(u.group(m, "q"))
        ql = _fold_pat(q).lower()
        if not ql or len(ql.split()) > 14 or _S_HI_BAD.search(ql):
            continue
        return q
    return None


def try_handle_search(transcript: str) -> Optional[str]:
    """Explicit "google X" / "look up X" / "search the web for X" wording. Returns Gemini's Google-grounded `Answer`
    when a key is set and it answers, else None (the LLM handles it with the web_search tool)."""
    try:
        u = _prep(transcript, max_words=24)
        if u is None:
            return None
        q = _extract_search_query(u)
        if not q:
            return None
        if not gemini_available():
            logger.info("Search matcher: %r is an explicit search but Gemini is not available -> LLM tool", q)
            return None
        logger.info("Search matcher: explicit search for %r", q)
        return _gemini_answer(q)
    except Exception:
        logger.exception("Search matcher failed on %r", transcript)
        return None


# ---- live questions (only with Gemini)

_LQ_START_EN = re.compile(
    r"^(?:what|what's|whats|who|who's|whos|whom|when|where|which|how|is|are|was|did|does|do|has|have|will|can|could|"
    r"tell me|give me|show me|find out|let me know|do you know|any idea|latest|current|today's|todays|any|"
    r"i want to know|i wanna know)\b")
_LQ_DENY_START = re.compile(
    r"^(?:play|open|set|turn|call|send|remind|add|create|delete|remove|start|stop|take|read|write|make|search|look|"
    r"save|launch|close|switch|increase|decrease|lower|raise|mute|pause|resume|skip|go|check|note|note down|shut|"
    r"lock|unlock|copy|paste|type|click|press|schedule|cancel|snooze|wake|sleep|volume|brightness)\b")
_LQ_EXCLUDE = re.compile(
    r"\b(?:what time|the time|what's the time|what is the time|what day|the date|what date|weather|temperature|"
    r"forecast|rain|raining|umbrella|humidity|snow|news|headlines|remind|reminder|alarm|timer|calendar|meeting|"
    r"schedule|battery|volume|brightness|wifi|bluetooth|my|mine|i|i'm|i've|i'd|i'll|we|our|us|you|your|yourself|"
    r"joke|story|poem|song|music)\b")
_LQ_EXCLUDE_HI = _c(
    r"(?:मौसम|तापमान|बारिश|छाता|खबर|ख़बर|समाचार|न्यूज़|न्यूज|हेडलाइन|समय क्या|कितने बजे|तारीख|तिथि|अलार्म|टाइमर|"
    r"रिमाइंडर|याद दिला|मेरा|मेरी|मेरे|मुझे|हमारा|हमारी|हमारे|तुम्हारा|तुम्हारी|आपका|आपकी|चलाओ|बजाओ|खोलो|बंद करो|"
    r"लगाओ|भेजो|गाना|संगीत|चुटकुला|कहानी)")
_LQ_QWORD_HI = _c(r"(?:क्या|कौन|कब|कितना|कितनी|कितने|कहां|कहाँ|कैसे|कैसा|कैसी|किसने|किसका|किसकी|कौनसी|कौन सी|बताओ|"
                  r"बताइए)")
_LQ_STRIP = re.compile(r"^(?:tell me|let me know|i want to know|i wanna know|do you know|any idea|give me|show me|"
                       r"find out)\s+")


def try_handle_live_question(transcript: str) -> Optional[str]:
    """A question about fast-changing facts (scores, prices, who holds an office, what happened today), answered by
    Gemini with Google Search - only when a Gemini key is set (else None, and the LLM handles it)."""
    try:
        u = _prep(transcript, max_words=22)
        if u is None:
            return None
        f = u.f
        words = f.split()
        if len(words) < 2 or len(words) > 22:
            return None
        hindi = lang.has_devanagari(f)
        if hindi:
            if _LQ_EXCLUDE_HI.search(f) or not _LQ_QWORD_HI.search(f):
                return None
        else:
            body = _LQ_STRIP.sub("", f)
            if _LQ_EXCLUDE.search(body):
                return None
            if _LQ_DENY_START.match(f):
                return None
            starts_ok = bool(_LQ_START_EN.match(f))
            if not starts_ok and len(words) > 7:
                return None
        if not is_time_sensitive(f):
            return None
        if not gemini_available():
            logger.info("Live question %r: no Gemini key, leaving it to the LLM", transcript)
            return None
        query = _clean_slot(u.span(0, len(f)), 220)
        logger.info("Live-question matcher: %r", query)
        return _gemini_answer(query)
    except Exception:
        logger.exception("Live-question matcher failed on %r", transcript)
        return None
