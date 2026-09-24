"""
instant_replies.py - things Raziel can answer or do WITHOUT asking the LLM.

Why this exists
---------------
Measured from assistant.log: every question that goes to qwen3:8b costs 4-5 s
warm (12-17 s for the first one after start-up), even a bare "Thank you". A
question like "what is today's date?" needs no language model at all - the
answer is `datetime.now()`. So, following the rule this project already lives
by (handoff section 5: match it in code before the LLM), the common, unambiguous
requests are matched here and answered in microseconds.

Everything in this module is a PURE function: text in, decision out. No I/O,
no Spotify, no side effects - tools.py does the acting. That keeps it trivially
testable and means a bad match can never do anything worse than answer wrongly.

Matching is deliberately STRICT (whole-utterance, anchored). A request that
carries extra detail ("what time is it in Tokyo", "take a screenshot and save it
as x", "play Kesariya on YouTube") does not match and falls through to the LLM,
exactly as before. Being conservative here costs nothing; being greedy would
make her do the wrong thing confidently.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional, Tuple

# ----------------------------------------------------------------- normalising

_LEAD_FILLER_RE = re.compile(
    r"^(?:okay|ok|so|alright|all right|well|umm?|uhh?|hey|hi|please|now|actually|"
    r"and|oh|raziel)\s+",
    re.IGNORECASE,
)
_POLITE_RE = re.compile(r"^(?:can|could|would|will) you (?:please )?", re.IGNORECASE)
_ASK_LEAD_RE = re.compile(
    r"^(?:tell me|give me|say|do you know|i want to know|i wanna know|let me know)\s+",
    re.IGNORECASE,
)


def _basic(text: str) -> str:
    """Lowercase; keep letters, digits, apostrophes and spaces; collapse whitespace."""
    text = (text or "").replace("’", "'").replace("`", "'").lower()
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize(text: str) -> str:
    """_basic() plus leading filler / politeness stripped ("so, can you tell me ...")."""
    text = _basic(text)
    for _ in range(4):
        before = text
        text = _LEAD_FILLER_RE.sub("", text, count=1)
        text = _POLITE_RE.sub("", text, count=1)
        text = _ASK_LEAD_RE.sub("", text, count=1)
        text = text.strip()
        if text == before:
            break
    return text


# ------------------------------------------------------------------ date / time

_ASK = r"(?:what(?:'s| is)|whats)"
_TAIL = r"(?: (?:right now|now|currently|today|please))*"

_TIME_RES = (
    rf"{_ASK} (?:the )?(?:current |exact )?time(?: is it)?{_TAIL}",
    rf"what time is it{_TAIL}",
    rf"(?:the )?(?:current )?time{_TAIL}",
    r"(?:do you have|have you got|got) the time",
)
_DATE_RES = (
    rf"{_ASK} (?:the |today's )(?:current )?date{_TAIL}",
    rf"{_ASK} date{_TAIL}",
    rf"(?:today's|the current|the) date{_TAIL}",
    rf"what date is (?:it|today){_TAIL}",
)
_DAY_RES = (
    rf"what day (?:of the week )?is (?:it|today){_TAIL}",
    rf"which day is (?:it|today){_TAIL}",
    rf"{_ASK} the day(?: of the week)?{_TAIL}",
    rf"what day it is{_TAIL}",
)
_DAY_AND_DATE_RES = (rf"{_ASK} today{_TAIL}",)
_BOTH_RES = (
    rf"{_ASK} (?:the )?(?:current )?date and (?:the )?time{_TAIL}",
    rf"{_ASK} (?:the )?(?:current )?time and (?:the )?date{_TAIL}",
    rf"(?:the )?(?:current )?(?:date and time|time and date){_TAIL}",
    rf"{_ASK} (?:the )?day and (?:the )?date{_TAIL}",
)


def _any_full(patterns, text: str) -> bool:
    return any(re.fullmatch(p, text) for p in patterns)


def _fmt_date(now: datetime) -> str:
    # No %-d / %#d: those differ per platform.
    return f"{now.strftime('%A, %B')} {now.day}, {now.year}"


def _fmt_time(now: datetime) -> str:
    hour12 = now.hour % 12 or 12
    return f"{hour12}:{now.minute:02d} {'AM' if now.hour < 12 else 'PM'}"


def answer_datetime(transcript: str, now: Optional[datetime] = None) -> Optional[str]:
    """
    "what's the date / time / day", in all the usual phrasings -> a short spoken
    answer, or None if the utterance is anything else (including anything that
    adds detail: "what time is it in London", "what's the date on Friday").
    """
    text = normalize(transcript)
    if not text or len(text.split()) > 9:
        return None
    now = now or datetime.now()

    if _any_full(_BOTH_RES, text):
        return f"{_fmt_date(now)}. It's {_fmt_time(now)}."
    if _any_full(_TIME_RES, text):
        return f"It's {_fmt_time(now)}."
    if _any_full(_DATE_RES, text):
        return f"{_fmt_date(now)}."
    if _any_full(_DAY_RES, text):
        return f"Today is {now.strftime('%A')}."
    if _any_full(_DAY_AND_DATE_RES, text):
        return f"Today is {_fmt_date(now)}."
    return None


# -------------------------------------------------------------------- small talk

_SMALLTALK_PREFIX = r"(?:(?:okay|ok|alright|oh|so|hey|and)\s+)*"
_SMALLTALK_SUFFIX = r"(?:\s+(?:raziel|sir|mate|buddy|man))?"

_THANKS_RE = re.compile(
    _SMALLTALK_PREFIX
    + r"(?:thanks|thank you|thank u|cheers|thanks a lot|thanks so much|thanks very much|"
      r"thank you so much|thank you very much|thank you a lot)"
    + _SMALLTALK_SUFFIX
)
_GREETING_RE = re.compile(
    _SMALLTALK_PREFIX
    + r"(?P<g>hello|hi|hey|yo|hello there|hi there|hey there|good morning|"
      r"good afternoon|good evening)"
    + _SMALLTALK_SUFFIX
)


def answer_smalltalk(transcript: str) -> Optional[str]:
    """A bare "thanks" or "hello" (and nothing else) -> a two-word reply."""
    text = _basic(transcript)
    if not text or len(text.split()) > 6:
        return None
    if _THANKS_RE.fullmatch(text):
        return "Anytime."
    m = _GREETING_RE.fullmatch(text)
    if m:
        greeting = m.group("g")
        if greeting.startswith("good "):
            return f"{greeting.capitalize()}. Standing by."
        return "Hello. Standing by."
    return None


# --------------------------------------------------------------------- screenshot

_SCREENSHOT_TAKE_RE = re.compile(
    r"(?:(?:take|capture|grab|make|get)(?: me)? (?:a |the |my )?"
    r"(?:screenshot|screen shot|screen capture|screengrab|snapshot)"
    r"(?: of (?:my |the )?(?:screen|desktop|display))?"
    r"|(?:a )?screenshot(?: of (?:my |the )?(?:screen|desktop|display))?)"
    r"(?: right now| now| please)*"
)
_SCREENSHOT_SHOW_RE = re.compile(
    r"(?:show|open|display|view|pull up|bring up)(?: me)?(?: the| that| my)?"
    r"(?: last| latest| previous| recent)? (?:screenshot|screen shot)"
    r"(?: again| please)*"
)


def parse_screenshot_request(transcript: str) -> Optional[str]:
    """Returns "take", "show", or None."""
    text = normalize(transcript)
    if not text or len(text.split()) > 9:
        return None
    if _SCREENSHOT_TAKE_RE.fullmatch(text):
        return "take"
    if _SCREENSHOT_SHOW_RE.fullmatch(text):
        return "show"
    return None


# ------------------------------------------------------------------------- music

_PLAY_RE = re.compile(r"^(?:play|put on|start playing)\s+(?P<q>.+)$")
_TAIL_RE = re.compile(
    r"(?:\s+(?:on|in|using|via|with)\s+spotify|\s+please|\s+for me|\s+now|\s+right now)+$"
)
# Anything that says it is NOT a Spotify track request.
_NOT_SPOTIFY_RE = re.compile(
    r"\b(?:youtube|you tube|yt|netflix|prime video|hotstar|twitch|soundcloud|"
    r"apple music|amazon|gaana|wynk|jiosaavn|saavn|deezer|tidal|vlc|itunes|browser|"
    r"chrome|video|videos|movie|movies|film|films|game|games|podcast|podcasts|radio|"
    r"episode|episodes|series|trailer|file|folder|screenshot|alarm|ringtone)\b"
)
_MULTI_STEP_RE = re.compile(
    r"\band (?:then )?(?:open|pause|search|launch|send|take|show|close|stop|queue|add|"
    r"set|turn|tell|remind)\b|\bthen\b"
)
_VAGUE = {
    "it", "this", "that", "them", "those", "these", "one", "again", "it again",
    "that again", "another", "another one", "another song", "another music",
    "something else", "something different", "different song", "anything else",
    "a different song", "the same", "the same song", "it back", "along", "back",
    "the music", "music again", "next again",
}
_GENERIC_MUSIC = {
    "music", "some music", "any music", "something", "a song", "any song", "some songs",
    "songs", "song", "random song", "a random song", "random music", "some random music",
    "some random songs", "anything", "some tunes", "tunes", "any songs", "a random music",
    "some good music", "some good songs", "a good song", "something good",
}
_NEXT = {
    "next", "next song", "next track", "next one", "the next one", "the next song",
    "the next track", "next music", "the next music",
}
_PREVIOUS = {
    "previous", "previous song", "previous track", "previous one", "the previous song",
    "the previous one", "the previous track", "the last song", "last song", "the last one",
    "the song before", "the previous music", "previous music",
}
# "play with me", "play what I was listening to", "play Kesariya twice", "play the
# piano", "play football": not requests for a Spotify track. They go to the LLM.
_NOT_A_TRACK_RE = re.compile(
    r"^(?:with|for|to|at|on|in|around|along|outside|inside|together|out|back|up|down|off|"
    r"what|whatever|whichever|when|how|why|who|where)\b"
    r"|\b(?:twice|thrice|again|once more|on repeat|on loop|in a loop|louder|quieter|softer|"
    r"faster|slower|later|tomorrow|tonight|today|yesterday)$"
)
_NOT_MUSIC_WORDS = {
    "piano", "the piano", "guitar", "the guitar", "violin", "the violin", "drums", "the drums",
    "chess", "football", "cricket", "tennis", "badminton", "poker", "cards", "basketball",
    "volleyball", "golf", "hockey", "news", "the news", "dead", "fair", "dumb", "along",
    "games", "sports", "cricket match", "a match", "the match", "with me", "with us",
}
_GAME_WORDS = {
    "chess", "cricket", "football", "tennis", "poker", "cards", "ludo", "carrom", "badminton",
    "basketball", "monopoly", "hockey", "golf", "volleyball", "games", "game",
}
_ARTIST_ONLY_RE = re.compile(
    r"^(?:some\s+)?(?:something|anything|songs?|music|tracks?|tunes?)\s+by\s+(?P<a>.+)$"
)
_ME_A_RE = re.compile(r"^me\s+(?=(?:a|some|any|something|anything|the|one)\b)")
_LEADING_DET_RE = re.compile(r"^(?:some|any|a|an|the)\s+(?=\S)")
_LEADING_NOISE_RE = re.compile(r"^(?:song|track|tune|music)\s+(?=\S)")
_MY_PLAYLIST_RE = re.compile(r"^my\s+(?P<n>.+?)\s+playlist$")


def parse_play_request(transcript: str) -> Optional[Tuple[str, str]]:
    """
    "play X" -> one of
        ("play", query)        query may be "" for "play some music"
        ("playlist", name)     "play my <name> playlist"
        ("next", "") / ("previous", "")
    or None when it isn't clearly a Spotify request (YouTube, video games,
    multi-step, "play it again"...), leaving those to the LLM.
    """
    text = normalize(transcript)
    m = _PLAY_RE.match(text)
    if not m:
        return None
    q = _TAIL_RE.sub("", m.group("q")).strip()
    if not q or len(q.split()) > 9:
        return None
    if _NOT_SPOTIFY_RE.search(q) or _MULTI_STEP_RE.search(q):
        return None
    q = _ME_A_RE.sub("", q)               # "play me a song" -> "a song" (generic below)
    if q.startswith("me ") or q.split()[0] in _GAME_WORDS:
        return None                       # "play me Kesariya" is ambiguous; "play chess with me" is a game
    if q in _VAGUE or q in _NOT_MUSIC_WORDS or _NOT_A_TRACK_RE.search(q):
        return None
    am = _ARTIST_ONLY_RE.match(q)         # "something by Coldplay" / "songs by X" -> just the artist
    if am:
        q = am.group("a").strip()
    if q in _NEXT:
        return ("next", "")
    if q in _PREVIOUS:
        return ("previous", "")
    if q in _GENERIC_MUSIC:
        return ("play", "")

    pm = _MY_PLAYLIST_RE.match(q)
    if pm:
        return ("playlist", pm.group("n").strip())
    if q.endswith(" playlist") or q == "playlist":
        return None                       # not "my ..." -> let the LLM decide

    # Free text: strip determiners, "the song ...", and turn "X by Y" into "X Y".
    q = _LEADING_DET_RE.sub("", q)
    q = _LEADING_NOISE_RE.sub("", q)
    q = re.sub(r"\s+by\s+", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    if not q or q in _VAGUE or q in _GENERIC_MUSIC:
        return None
    return ("play", q)
