"""
briefing.py - the morning briefing: greeting, date and time, then one line from each feature.

"Give me my briefing" / "good morning" / "मेरी ब्रीफिंग सुनाओ" -> Raziel says, in one go:

    Good morning. It's 8:30 AM, Sunday, September 20. <weather> <reminders> <to-do list>
    <calendar> <email> <three headlines>

Every line comes from a hook in another module (each returns "" when it has nothing to say):
    webinfo.weather_briefing_line()     reminders.briefing_line()      notes.briefing_line()
    google_api.calendar_briefing_line() google_api.email_briefing_line() webinfo.news_briefing_line(3)
The hooks are imported lazily and called in parallel threads (they make network calls), each with an
8 second limit, so the whole briefing takes about as long as the slowest hook. A missing module, a
missing function, an exception or a timeout just means that line is left out. The text is kept under
about 90 words (the news line is trimmed first).

Once a day
----------
The table briefing_state(day, at) remembers on which day a briefing was given. The "day" changes at
03:00 local time (a briefing at 1 AM still belongs to the previous day). main.py asks
auto_briefing() on the first wake of the day; it hands back the text exactly once per day, even if two
sessions wake together (the day is claimed with one atomic INSERT). "good morning" only triggers a
briefing while none was given yet today; afterwards it is left to small talk.

Public API
----------
  try_handle(transcript)   -> Optional[str]   the matcher (explicit requests and the greeting)
  match(transcript)        -> "explicit" | "greeting" | None    pure, no I/O
  daily_briefing(**_)      -> str             the LLM tool function
  build_briefing(now=None) -> str             the text, without touching the state
  auto_briefing(now=None)  -> Optional[str]   for the first wake of the day; never raises
  should_auto_brief(now=None) -> bool, mark_briefed(now=None), last_briefed_day() -> Optional[str]

Settings (read with getattr(config, ...)):
  BRIEFING_ENABLED=True  BRIEFING_AUTO=True  BRIEFING_INCLUDE_NEWS=True
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import config
import lang
import memory

logger = logging.getLogger("voice_assistant")

HOOK_TIMEOUT = 8.0          # seconds each hook may take before its line is skipped
MAX_WORDS = 90              # the whole briefing stays under about this many words
DAY_START_HOUR = 3          # the briefing "day" changes at 03:00 local time
NEWS_HEADLINES = 3
KEEP_DAYS = 60              # old rows of briefing_state are pruned

# (key, module, function, args): the order here is the order of the spoken briefing
_HOOKS: List[Tuple[str, str, str, tuple]] = [
    ("weather", "webinfo", "weather_briefing_line", ()),
    ("reminders", "reminders", "briefing_line", ()),
    ("notes", "notes", "briefing_line", ()),
    ("calendar", "google_api", "calendar_briefing_line", ()),
    ("email", "google_api", "email_briefing_line", ()),
    ("news", "webinfo", "news_briefing_line", (NEWS_HEADLINES,)),
]
# When the text is too long lines are trimmed in this order (news first, then from the back)
_TRIM_ORDER = ["news", "email", "calendar", "notes", "reminders"]

_T: Dict[str, Dict[str, str]] = {
    "morning": {"en": "Good morning.", "hi": "सुप्रभात।"},
    "afternoon": {"en": "Good afternoon.", "hi": "नमस्कार।"},
    "evening": {"en": "Good evening.", "hi": "शुभ संध्या।"},
    "now": {"en": "It's {t}, {d}.", "hi": "अभी समय है {t}। आज {d} है।"},
    "off": {"en": "The daily briefing is turned off.", "hi": "डेली ब्रीफिंग बंद है।"},
    "failed": {"en": "I couldn't put your briefing together just now.",
               "hi": "अभी आपकी ब्रीफिंग तैयार नहीं हो पाई।"},
}


def _t(key: str, **fmt) -> str:
    return lang.tr(_T, key, **fmt)


# ------------------------------------------------------------------ settings and the clock

def _cfg(name: str, default):
    return getattr(config, name, default)


def _enabled() -> bool:
    return bool(_cfg("BRIEFING_ENABLED", True))


def _now() -> datetime:
    """The local time (tests replace this or pass `now`)."""
    return datetime.now()


# ------------------------------------------------------------------ the once-a-day state

def _day_key(now: datetime) -> str:
    """The calendar day a briefing counts for: the day changes at DAY_START_HOUR, not at midnight."""
    return (now - timedelta(hours=DAY_START_HOUR)).date().isoformat()


def _db():
    conn = memory.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS briefing_state (day TEXT PRIMARY KEY, at REAL)")
    return conn


def _claim_day(now: datetime) -> bool:
    """Atomically record a briefing for this day. True only for the caller that made the row."""
    with closing(_db()) as conn:
        cur = conn.execute("INSERT OR IGNORE INTO briefing_state (day, at) VALUES (?, ?)",
                           (_day_key(now), now.timestamp()))
        claimed = cur.rowcount == 1
        if claimed:
            oldest = _day_key(now - timedelta(days=KEEP_DAYS))
            conn.execute("DELETE FROM briefing_state WHERE day < ?", (oldest,))
        conn.commit()
    return claimed


def _release_day(now: datetime) -> None:
    with closing(_db()) as conn:
        conn.execute("DELETE FROM briefing_state WHERE day = ?", (_day_key(now),))
        conn.commit()


def release_today() -> None:
    """Gives today's briefing back (used when it took too long to build, so the next wake-up delivers it)."""
    try:
        _release_day(_now())
    except Exception:                                    # noqa: BLE001
        logger.debug("briefing: could not release today's claim", exc_info=True)


def _briefed_today(now: datetime) -> bool:
    """True if a briefing was given today. On a database problem also True: better silent than nagging."""
    try:
        with closing(_db()) as conn:
            row = conn.execute("SELECT 1 FROM briefing_state WHERE day = ?", (_day_key(now),)).fetchone()
        return row is not None
    except Exception as e:
        logger.warning("Briefing: cannot read briefing_state: %s", e)
        return True


def should_auto_brief(now: Optional[datetime] = None) -> bool:
    """True if the automatic first-wake briefing is on and none was given yet today. Never raises."""
    try:
        if not (_enabled() and _cfg("BRIEFING_AUTO", True)):
            return False
        return not _briefed_today(now or _now())
    except Exception as e:
        logger.warning("Briefing: should_auto_brief failed: %s", e)
        return False


def mark_briefed(now: Optional[datetime] = None) -> None:
    """Remember that today's briefing has been given. Never raises."""
    try:
        _claim_day(now or _now())
    except Exception as e:
        logger.warning("Briefing: could not record the briefing: %s", e)


def last_briefed_day() -> Optional[str]:
    """The last day (YYYY-MM-DD, day change at 03:00) a briefing was given, or None."""
    try:
        with closing(_db()) as conn:
            row = conn.execute("SELECT day FROM briefing_state ORDER BY day DESC LIMIT 1").fetchone()
        return row["day"] if row else None
    except Exception as e:
        logger.warning("Briefing: cannot read briefing_state: %s", e)
        return None


# ------------------------------------------------------------------ the hooks

def _hook(module_name: str, func_name: str, *args) -> str:
    """Call module_name.func_name(*args) and return its line, or "" for ANY problem (module or
    function missing, exception, non-text result). The import happens here, lazily."""
    try:
        module = importlib.import_module(module_name)
        result = getattr(module, func_name)(*args)
    except Exception as e:                      # ImportError, AttributeError, whatever the hook raised
        logger.debug("Briefing: %s.%s unavailable: %s: %s", module_name, func_name, type(e).__name__, e)
        return ""
    if result is None:
        return ""
    return re.sub(r"\s+", " ", str(result)).strip()


def _collect(keys: List[str]) -> Dict[str, str]:
    """Run the hooks for `keys` in parallel and return {key: line}. A hook that is still running
    after HOOK_TIMEOUT seconds is abandoned (its thread is a daemon)."""
    specs = {k: (m, f, a) for k, m, f, a in _HOOKS}
    results: Dict[str, str] = {}
    threads: Dict[str, threading.Thread] = {}
    lock = threading.Lock()

    def run(key: str):
        module_name, func_name, args = specs[key]
        line = _hook(module_name, func_name, *args)
        with lock:
            results[key] = line

    started = time.monotonic()
    for key in keys:
        th = threading.Thread(target=run, args=(key,), name=f"briefing-{key}", daemon=True)
        threads[key] = th
        th.start()
    deadline = started + float(HOOK_TIMEOUT)
    for th in threads.values():
        th.join(max(0.0, deadline - time.monotonic()))
    lines: Dict[str, str] = {}
    report = []
    with lock:
        for key in keys:
            if key in results:
                lines[key] = results[key]
                report.append(f"{key}={'ok' if results[key] else 'empty'}")
            else:
                lines[key] = ""
                report.append(f"{key}=timeout")
    logger.info("Briefing: hooks finished in %.1f s (%s)", time.monotonic() - started, ", ".join(report))
    return lines


# ------------------------------------------------------------------ building the text

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?।])\s+")


def _end(line: str) -> str:
    """The line with a closing full stop."""
    line = line.strip()
    if line and line[-1] not in ".!?।":
        line += "।" if lang.current() == "hi" else "."
    return line


def _words(text: str) -> int:
    return len(text.split())


def _drop_last_sentence(line: str, keep_first: bool) -> str:
    parts = _SENTENCE_SPLIT_RE.split(line.strip())
    if len(parts) > 1:
        return " ".join(parts[:-1])
    return line if keep_first else ""


def _daypart(now: datetime) -> str:
    if 4 <= now.hour < 12:
        return "morning"
    if 12 <= now.hour < 17:
        return "afternoon"
    return "evening"


def _fit(head: str, lines: Dict[str, str]) -> str:
    """Join head + lines, trimming the news line first, then email, calendar, notes and reminders
    (never below one sentence, the weather is never trimmed) until it is about MAX_WORDS long."""
    lines = dict(lines)

    def total() -> int:
        return _words(head) + sum(_words(v) for v in lines.values())

    for key in _TRIM_ORDER:
        while lines.get(key) and total() > MAX_WORDS:
            shorter = _drop_last_sentence(lines[key], keep_first=key != "news")
            if shorter == lines[key]:
                break
            lines[key] = shorter
    if total() > MAX_WORDS:
        logger.info("Briefing: %d words, over the %d word target", total(), MAX_WORDS)
    order = [k for k, _m, _f, _a in _HOOKS]
    return " ".join([head] + [lines[k] for k in order if lines.get(k)])


def build_briefing(now: Optional[datetime] = None) -> str:
    """The briefing text in the current language. Does not touch the once-a-day state. Never raises."""
    try:
        now = now or _now()
        head = f"{_t(_daypart(now))} {_t('now', t=lang.fmt_time(now), d=lang.fmt_date(now))}"
        keys = [k for k, _m, _f, _a in _HOOKS if k != "news" or _cfg("BRIEFING_INCLUDE_NEWS", True)]
        lines = {k: _end(v) for k, v in _collect(keys).items() if v}
        text = _fit(head, lines)
        logger.info("Briefing: built %d words (%s)", _words(text), ", ".join(lines) or "greeting only")
        return text
    except Exception as e:
        logger.warning("Briefing: build failed: %s", e, exc_info=True)
        return _t("failed")


def _give(now: Optional[datetime] = None) -> str:
    """Build the briefing and record it as given today."""
    now = now or _now()
    text = build_briefing(now)
    mark_briefed(now)
    return text


def daily_briefing(**_ignored) -> str:
    """The LLM tool: give the morning briefing now (weather, reminders, to-do list, calendar, email,
    news). Returns the text to speak; never raises."""
    try:
        if not _enabled():
            return _t("off")
        return _give()
    except Exception as e:
        logger.warning("Briefing: daily_briefing failed: %s", e, exc_info=True)
        return _t("failed")


def auto_briefing(now: Optional[datetime] = None) -> Optional[str]:
    """For the first wake of the day: the briefing text (and today is marked as briefed), or None if
    it is not due (already given today, disabled, BRIEFING_AUTO off, or nothing to say). Never raises."""
    try:
        now = now or _now()
        if not (_enabled() and _cfg("BRIEFING_AUTO", True)):
            logger.info("Briefing: automatic briefing is off")
            return None
        if not _claim_day(now):                 # atomic: only one session gets the day
            logger.info("Briefing: already given today (%s)", _day_key(now))
            return None
        text = ""
        try:
            text = build_briefing(now)
        finally:
            if not text:
                _release_day(now)               # nothing was said: leave the day open
        logger.info("Briefing: automatic briefing given (%s)", _day_key(now))
        return text or None
    except Exception as e:
        logger.warning("Briefing: auto_briefing failed: %s", e, exc_info=True)
        return None


# ------------------------------------------------------------------ the matcher

def _c(pattern: str):
    """Compile with the Devanagari parts folded like transcripts (chandrabindu, anusvara, nukta dropped)."""
    return re.compile(re.sub(r"[^\x00-\x7f]+", lambda m: lang.fold_hindi(m.group()), pattern))


_LEAD_RE = _c(
    r"^(?:raziel|razil|rajil|hey|hi|hello|yo|ok|okay|so|well|please|kindly|now|and|"
    r"can you|could you|would you|will you|can u|i want you to|i need you to|i would like you to|"
    r"i'd like you to|"
    r"कृपया|ज़रा|जरा|प्लीज़|प्लीज|रज़ील|रजील|राजील|रेजील|अरे|सुनो|ओके|अच्छा|तो|मुझे)\s+")
_TAIL_RE = _c(
    r"(?:\s+(?:please|for me|right now|now|thanks|thank you|raziel|razil|sir|boss|"
    r"कृपया|प्लीज़|प्लीज|ज़रा|जरा|जी|अभी|दो|दीजिए|दीजिये|दीजिये|रज़ील|रजील))+$")


def _normalize(transcript: str) -> str:
    text = lang.normalize_hindi(str(transcript or ""))
    for _ in range(5):
        before = text
        text = _LEAD_RE.sub("", text, count=1).strip()
        text = _TAIL_RE.sub("", text, count=1).strip()
        if text == before:
            break
    return text


_B_MOD = r"(?:morning|daily|day|today's|todays)"
# "briefing" may stand alone; "brief" / "rundown" / "digest" only with morning / daily / day in front
_BRIEFING = rf"(?:(?:my|the|a|our|today's|todays) )?(?:(?:{_B_MOD} )?briefing|{_B_MOD} (?:brief|rundown|digest))"
_UPDATE = r"(?:(?:my|the|a) )?(?:morning|daily) (?:update|report|summary)"
_B_WHEN = r"(?: (?:for )?(?:today|this morning|now))?"
_B_VERB = (r"(?:(?:give me|get me|read|read out|read me|tell me|run|do|start|begin|let me hear|let's hear|"
           r"i want to hear|i want|i need|i'd like|i would like|hear) )?")

_H_BRIEF = r"(?:ब्रीफिंग|ब्रिफिंग|ब्रीफ़िंग|ब्रीफ)"
_H_BMOD = (r"(?:(?:मेरी|मेरा|आज की|आज का|अपनी|हमारी|रोज़ की|रोज की|आज के दिन की|सुबह की|डेली|"
           r"मॉर्निंग|मार्निंग|दैनिक) )*")
_H_VERB = (r"(?: (?:मुझे )?(?:सुनाओ|सुनाइए|सुना दो|बताओ|बताइए|बता दो|पढ़ो|पढ़कर सुनाओ|दिखाओ|शुरू करो|"
           r"शुरू कीजिए|चलाओ|दो|दीजिए))?")

_EXPLICIT = [_c(p) for p in (
    # ---- English
    rf"{_B_VERB}{_BRIEFING}{_B_WHEN}",
    rf"{_B_VERB}{_UPDATE}{_B_WHEN}",
    r"brief me(?: (?:for|on) (?:today|the day|my day|this morning))?",
    r"(?:start|begin) (?:my|the) (?:day|morning)",
    r"what(?:'s| is) (?:my day|today|the day|my morning) (?:look(?:ing)?|going to be|gonna be|shaping up)"
    r"(?: like)?(?: today)?",
    r"what does (?:my day|today|the day) look like(?: today)?",
    r"how(?:'s| is| does) (?:my day|today|the day) (?:look(?:ing)?|shaping up|going to be)(?: like)?(?: today)?",
    r"what(?:'s| is) up (?:for )?today",
    # ---- Hindi
    rf"{_H_BMOD}{_H_BRIEF}{_H_VERB}",
    r"(?:मेरा आज का|मेरा|आज का|आज) दिन (?:कैसा|कैसे) (?:रहेगा|होगा|है|जाएगा|लग रहा है)(?: (?:बताओ|बताइए))?",
    r"(?:मेरा |मेरे )?आज का शेड्यूल (?:बताओ|बताइए|सुनाओ|सुनाइए|दिखाओ)",
    r"(?:मेरा |मेरे )?दिन (?:शुरू करो|शुरू कीजिए|शुरू करें)",
    # ---- Hinglish
    r"(?:meri |mera |aaj ki |aaj ka )?(?:morning |daily )?briefing (?:sunao|batao|do|bataiye|sunaiye|de do|shuru karo)",
    r"aaj ka din kaisa (?:rahega|hoga|hai)(?: (?:batao|bataiye))?",
)]
_GREETING = [_c(p) for p in (
    r"good morning(?: (?:to you|everyone|there|buddy))?",
    r"(?:सुप्रभात|शुभ प्रभात|गुड मॉर्निंग|गुड मार्निंग|गुड मॉर्निंग जी)",
    r"(?:suprabhat|shubh prabhat)",
)]
_MAX_WORDS_IN = 12


def match(transcript: str) -> Optional[str]:
    """Pure: 'explicit' (an ask for the briefing), 'greeting' ('good morning', 'सुप्रभात') or None."""
    text = _normalize(transcript)
    if not text or len(text.split()) > _MAX_WORDS_IN:
        return None
    if any(rx.fullmatch(text) for rx in _EXPLICIT):
        return "explicit"
    if any(rx.fullmatch(text) for rx in _GREETING):
        return "greeting"
    return None


def try_handle(transcript: str) -> Optional[str]:
    """'give me my briefing', 'brief me', 'what's my day look like', 'मेरी ब्रीफिंग सुनाओ' -> the briefing.
    'good morning' / 'सुप्रभात' -> the briefing only if none was given yet today, else None (small talk).
    Returns None (and does nothing) for everything else. Never raises."""
    try:
        kind = match(transcript)
        if kind is None:
            return None
        now = _now()
        if kind == "greeting":
            if not _enabled():
                return None
            if _briefed_today(now):
                logger.info("Briefing: %r is a greeting and today's briefing was already given", transcript)
                return None
        logger.info("Briefing: matched %r as %s -> briefing", transcript, kind)
        if not _enabled():
            return _t("off")
        return _give(now)
    except Exception as e:
        logger.warning("Briefing: try_handle failed: %s", e, exc_info=True)
        return _t("failed")
