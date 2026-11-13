"""
show_me.py - "show me the Eiffel Tower", "what does an axolotl look like?"

Finds the matching Wikipedia article (one search call, no API key), says its first sentence, and
puts the article's main picture on Companion Deck's notch as an `image` card for 60 s (README
"Companion Deck's notch"). The picture is shrunk to at most 320 px and about 24 KB before it is
sent, as a JPEG data URL: the notch itself loads nothing from the internet.

Only clear "show me / what does X look like / picture of X" requests are matched; "show me the
weather", "show me my files" and the like are left to the matchers that own them. When Wikipedia
has no article, she says she couldn't find a picture; only when Wikipedia can't be reached does
try_handle() return None, so the LLM answers as usual.

Public API
----------
  match(transcript)        pure: the subject ("Eiffel Tower") or None; no I/O
  try_handle(transcript)   matcher for main.py: the sentence to speak, or None
  find(subject)            (title, first sentence, JPEG bytes or None), or None if nothing found
  to_jpeg(raw)             any image bytes -> JPEG of at most 320 px and MAX_IMAGE_BYTES, or None
"""

from __future__ import annotations

import base64
import io
import json
import logging
import re
import threading
import urllib.parse
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "Raziel/1.0 (personal voice assistant; Companion Deck notch)"
TIMEOUT = 6.0
MAX_SIDE = 320
MAX_IMAGE_BYTES = 24 * 1024
CARD_SECONDS = 60
MAX_SENTENCE = 220

_POLITE = r"(?:(?:please|raziel|hey|ok|okay|can you|could you|will you)\s+)*"
_PATTERNS = (
    re.compile(rf"^{_POLITE}what (?:does|do|did) (?P<s>.+?) look like\??$"),
    re.compile(rf"^{_POLITE}(?:show|give) me (?:a |an |the |some )?(?:picture|photo|image|pic|photograph)s? of (?P<s>.+?)\??$"),
    re.compile(rf"^{_POLITE}(?:a |an )?(?:picture|photo|image|pic) of (?P<s>.+?)\??$"),
    re.compile(rf"^{_POLITE}show me (?P<s>.+?)\??$"),
)
# "show me ..." alone is broad: these belong to other matchers (weather, files, the universe...).
_NOT_SUBJECTS = re.compile(
    r"\b(?:my|me|your|yourself|weather|forecast|news|time|date|calendar|mail|email|inbox|screen|"
    r"files?|folders?|universe|gestures?|timers?|reminders?|notes?|list|playlists?|songs?|music|"
    r"queue|how|what|where|when|which|why|who|around|again|more|it|this|that)\b")
_ARTICLES = re.compile(r"^(?:a|an|the|some)\s+")
# "what does it look like?" is about something said before, not a picture request.
_PRONOUNS = frozenset({"it", "this", "that", "they", "them", "these", "those", "he", "she", "you", "i", "we"})


def match(transcript: str) -> Optional[str]:
    text = " ".join(str(transcript or "").lower().replace(",", " ").split()).rstrip(".!")
    for i, pattern in enumerate(_PATTERNS):
        m = pattern.match(text)
        if not m:
            continue
        subject = _ARTICLES.sub("", m.group("s").strip())
        if not subject or subject in _PRONOUNS or len(subject) > 60 or len(subject.split()) > 6:
            return None
        if i == len(_PATTERNS) - 1 and _NOT_SUBJECTS.search(subject):
            return None  # plain "show me X" only for things, not "show me my calendar"
        return subject
    return None


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - Wikipedia only
        return resp.read(4 * 1024 * 1024)


def to_jpeg(raw: bytes) -> Optional[bytes]:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:  # noqa: BLE001 - an image Pillow can't read: no picture
        return None
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    for quality in (80, 70, 60, 50, 40):
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        if out.tell() <= MAX_IMAGE_BYTES:
            return out.getvalue()
    return None


def _first_sentence(extract: str) -> str:
    text = " ".join(str(extract or "").split())
    while True:  # pronunciations and dates in brackets, which nest: "(... [ta:dZ 'meh(e)l])"
        stripped = re.sub(r"\s*\([^()]*\)", "", text)
        if stripped == text:
            break
        text = stripped
    m = re.match(r"(.+?[.!?])(?:\s|$)", text)
    sentence = (m.group(1) if m else text)[:MAX_SENTENCE]
    return sentence


def find(subject: str) -> Optional[Tuple[str, str, Optional[bytes]]]:
    query = urllib.parse.urlencode({
        "action": "query", "format": "json", "generator": "search", "gsrsearch": subject,
        "gsrlimit": 1, "prop": "pageimages|extracts", "piprop": "thumbnail", "pithumbsize": 480,
        "exintro": 1, "explaintext": 1, "exsentences": 2,
    })
    data = json.loads(_get(f"{API}?{query}"))
    pages = list(((data.get("query") or {}).get("pages") or {}).values())
    if not pages:
        return None
    page = pages[0]
    title = str(page.get("title") or subject)[:80]
    sentence = _first_sentence(page.get("extract") or "")
    thumb = (page.get("thumbnail") or {}).get("source")
    jpeg = to_jpeg(_get(thumb)) if thumb else None
    return title, sentence, jpeg


def _show_card(title: str, jpeg: bytes) -> None:
    try:
        import avatar_server
    except Exception:  # noqa: BLE001 - no socket: the spoken answer is enough
        return
    card = {"id": "image", "kind": "image", "title": title,
            "image": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii"),
            "source": "Wikipedia", "ttl": CARD_SECONDS}
    avatar_server.show_card(card)
    timer = threading.Timer(CARD_SECONDS, avatar_server.hide_card, args=("image",))
    timer.daemon = True
    timer.start()


def try_handle(transcript: str) -> Optional[str]:
    subject = match(transcript)
    if subject is None:
        return None
    try:
        found = find(subject)
    except Exception as e:  # noqa: BLE001 - offline, Wikipedia down: let the LLM answer
        logger.info("Show me: Wikipedia didn't answer (%s)", type(e).__name__)
        return None
    if found is None:
        return f"I couldn't find a picture of {subject}."
    title, sentence, jpeg = found
    if jpeg is not None:
        _show_card(title, jpeg)
        logger.info("Show me: picture of %s on the notch (%d KB)", title, len(jpeg) // 1024)
        lead = f"Here's {title}."
    else:
        lead = f"I couldn't find a picture of {title}."
    return f"{lead} {sentence}".strip() if sentence else lead
