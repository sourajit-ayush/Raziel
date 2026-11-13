"""
hindi_commands.py - the deterministic HINDI matchers for the features Raziel already has.

Why this exists
---------------
instant_replies.py and tools.py match English only ("open notepad", "play Kesariya",
"take a screenshot"). Whisper writes Hindi in Devanagari - and writes the English
loanwords in Devanagari too ("नोटपैड खोलो", "यूट्यूब", "क्रोम", "व्हाट्सऐप") - so every
Hindi command missed those matchers and fell through to qwen3:8b (4-5 s per reply).
This module is the same idea as instant_replies.py, written for Devanagari: strict,
anchored, whole-utterance patterns that DO the action through tools.py and hand back
the finished sentence in Hindi.

Public API
----------
  try_handle(transcript, now=None) -> Optional[str]
        The matcher main.py calls before the LLM. Hindi utterances only (Devanagari,
        lang.detect()=='hi', or a clear Hinglish command word); pure English -> None.
  localize(text, code=None) -> str
        Turns the fixed ENGLISH sentences tools.py/main.py/llm_brain.py return into
        Hindi while the turn is Hindi. Unknown text comes back unchanged, never raises.
  is_sleep_hindi(transcript) -> bool
        "अलविदा", "सो जाओ", "बाय बाय" ... - used next to config.SLEEP_PHRASES.

How the matching works
----------------------
Everything is matched on lang.normalize_hindi()-folded text (lower case, no nukta /
anusvara / chandrabindu, no punctuation, no danda), and the PATTERNS are folded the same
way at import time by _c(), so a pattern can be written in natural Devanagari ("संदेश",
"फ़ज़ल") and still match the folded transcript ("सदेश", "फजल"). Because fold_hindi()
lower-cases, patterns must avoid upper-case regex escapes (\\S, \\W, \\b): use [^ ] and
explicit spaces instead.

The original spelling is kept alongside the folded one token by token (_Text), so a
captured group can be handed back with its matras intact - that matters for
lang.hint_latin(): "अरिजीत सिंह" -> "arijit singh" for Spotify, "फ़ज़ल" -> "fajal" for
the contact lookup, while the WhatsApp message stays Devanagari, exactly as spoken.

Settings (read with getattr, never required):
  HINDI_COMMANDS_ENABLED = True     set False to switch this whole module off
"""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from typing import Callable, List, Optional, Tuple

import lang

logger = logging.getLogger("voice_assistant")

_MAX_WORDS = 30              # nothing longer than this is one of our commands
_SHORT_WORDS = 9             # screenshot / date / small talk are short utterances
_SLEEP_WORDS = 6


def _enabled() -> bool:
    try:
        import config
        return bool(getattr(config, "HINDI_COMMANDS_ENABLED", True))
    except Exception:
        return True


# --------------------------------------------------------------- text preparation

_PUNCT_RE = re.compile(r"[^\w\s'ऀ-ॿ]+", re.UNICODE)


def _soft(text: str) -> str:
    """Punctuation-free, whitespace-collapsed text with the Devanagari spelling intact."""
    t = unicodedata.normalize("NFC", text or "")
    t = t.replace("’", "'").replace("`", "'")
    t = t.replace("।", " ").replace("॥", " ")        # । ॥
    t = _PUNCT_RE.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip()


class _Text:
    """Folded text for matching + the original tokens, kept index-aligned."""

    __slots__ = ("words", "folded", "text")

    def __init__(self, words: List[str], folded: List[str]):
        self.words = words
        self.folded = folded
        self.text = " ".join(folded)

    def orig(self, m, group) -> str:
        """The captured group in its ORIGINAL spelling (matras, nukta, case kept)."""
        try:
            start = m.start(group)
            captured = m.group(group) or ""
        except Exception:
            return ""
        if start < 0 or not captured:
            return ""
        i = len(self.text[:start].split())
        n = len(captured.split())
        return " ".join(self.words[i:i + n])


def _make(text: str) -> _Text:
    words, folded = [], []
    for tok in _soft(text).split(" "):
        if not tok:
            continue
        f = lang.fold_hindi(tok).strip()
        if not f:
            continue
        words.append(tok)
        folded.append(f)
    return _Text(words, folded)


# Leading filler ("रज़ील, ज़रा ...") and trailing politeness ("... प्लीज़ जी").
_LEAD_ONE = {lang.fold_hindi(w) for w in (
    "रज़ील", "रेज़ील", "रजील", "राज़ील", "हे", "अरे", "ओके", "ओकै", "प्लीज़", "प्लीज",
    "कृपया", "ज़रा", "सुनो", "सुनिए", "अब", "तो", "raziel", "hey", "ok", "okay",
    "please", "zara", "suno", "ab",
)}
_LEAD_PAIRS = {tuple(lang.fold_hindi(p).split()) for p in (
    "क्या तुम", "क्या आप", "ठीक है", "ओके ठीक", "मुझे ज़रा", "क्या तू",
)}
_TRAIL_ONE = {lang.fold_hindi(w) for w in (
    "प्लीज़", "प्लीज", "कृपया", "जी", "ना", "यार", "भाई", "अभी", "तो", "please", "now",
)}


def _strip_fillers(t: _Text) -> _Text:
    i, j = 0, len(t.folded)
    for _ in range(6):
        before = (i, j)
        if j - i >= 3 and (t.folded[i], t.folded[i + 1]) in _LEAD_PAIRS:
            i += 2
        elif j - i >= 2 and t.folded[i] in _LEAD_ONE:
            i += 1
        elif j - i >= 2 and t.folded[j - 1] in _TRAIL_ONE:
            j -= 1
        if (i, j) == before:
            break
    if (i, j) == (0, len(t.folded)):
        return t
    return _Text(t.words[i:j], t.folded[i:j])


# lang.fold_hindi() also lower-cases, which would wreck "(?P<name>...)", so patterns get
# their own folding: the same combining marks dropped, nothing else touched. (This is the
# one helper lang.py does not expose - everything else comes from there.)
_MARKS_DROP = dict.fromkeys(map(ord, "ँं़‌‍"), None)


def _fold_pattern(pattern: str) -> str:
    t = unicodedata.normalize("NFC", pattern)
    t = unicodedata.normalize("NFD", t).translate(_MARKS_DROP)
    return unicodedata.normalize("NFC", t)


def _c(pattern: str) -> "re.Pattern":
    """Compile a pattern written in natural Devanagari against folded transcripts."""
    return re.compile(_fold_pattern(pattern))


# --------------------------------------------------------------- Devanagari -> Latin

_FOLDED_APP_WORDS = {lang.fold_hindi(k): v for k, v in lang.HINDI_APP_WORDS.items()}

# The letter-by-letter romanizer in lang.py is ITRANS-ish ("अरिजीत" -> "arijeet"); the
# spellings Spotify indexes are the plain ones. These two rules plus a handful of word
# fixes turn "अरिजीत सिंह" into "arijit singh".
_VOWEL_FIXES = (("aa", "a"), ("ee", "i"), ("oo", "u"))
_ROMAN_WORD_FIXES = {
    "sinh": "singh", "sinha": "singh", "kaur": "kaur", "khaan": "khan",
    "yaadein": "yaadein", "nh": "ngh",
}


def _latin_name(text: str) -> str:
    """App / folder / site name as Latin letters: 'नोटपैड' -> 'notepad'."""
    out = lang.hint_latin((text or "").strip()).strip().lower()
    return re.sub(r"\s+", " ", out)


def _latin_query(text: str) -> str:
    """Song / artist / playlist text for Spotify: 'अरिजीत सिंह' -> 'arijit singh'."""
    out = []
    for tok in (text or "").split():
        if lang.has_devanagari(tok):
            folded = lang.fold_hindi(tok)
            word = lang.hint_latin(tok).strip().lower()
            if folded not in _FOLDED_APP_WORDS:
                for _ in range(3):             # "varkaaaut" -> "varkaaut" -> "varkaut"
                    before = word
                    for a, b in _VOWEL_FIXES:
                        word = word.replace(a, b)
                    if word == before:
                        break
                word = _ROMAN_WORD_FIXES.get(word, word)
            out.append(word)
        else:
            out.append(tok.lower())
    return re.sub(r"\s+", " ", " ".join(out)).strip()


# lang.hint_latin("मम्मी") is "mammee", which matches nothing in config.CONTACTS. The
# kinship words people actually use are mapped to the names a contact list uses.
_CONTACT_ALIASES = {lang.fold_hindi(k): v for k, v in {
    "मम्मी": "mom", "मम्मा": "mom", "माँ": "mom", "मां": "mom", "अम्मा": "mom",
    "माता जी": "mom", "मम्मी जी": "mom", "पापा": "dad", "पिताजी": "dad",
    "पिता जी": "dad", "डैडी": "dad", "डैड": "dad", "भाई": "bhai", "भैया": "bhaiya",
    "दीदी": "didi", "बहन": "behan", "पत्नी": "wife", "बीवी": "wife", "पति": "husband",
}.items()}


def _latin_contact(text: str) -> str:
    key = lang.fold_hindi((text or "").strip())
    if key in _CONTACT_ALIASES:
        return _CONTACT_ALIASES[key]
    return _latin_name(text)


# --------------------------------------------------------------- apps and folders

_FOLDER_CANON = {"downloads", "documents", "desktop", "pictures", "music", "videos", "home"}
_FOLDER_ALIASES = {lang.fold_hindi(k): v for k, v in {
    "डाउनलोड": "downloads", "डाउनलोड्स": "downloads", "डाउनलोडस": "downloads",
    "डॉक्यूमेंट्स": "documents", "डॉक्यूमेंट": "documents", "डाक्यूमेंट्स": "documents",
    "दस्तावेज़": "documents", "डेस्कटॉप": "desktop", "डेस्कटाप": "desktop",
    "पिक्चर्स": "pictures", "तस्वीरें": "pictures", "तस्वीर": "pictures",
    "फोटोज़": "pictures", "फ़ोटो": "pictures", "फोटो": "pictures",
    "म्यूज़िक": "music", "संगीत": "music", "वीडियो": "videos", "वीडियोज़": "videos",
    "वीडियोस": "videos", "होम": "home", "घर": "home",
    "downloads": "downloads", "download": "downloads", "documents": "documents",
    "desktop": "desktop", "pictures": "pictures", "photos": "pictures",
    "music": "music", "videos": "videos", "video": "videos", "home": "home",
}.items()}
# Said WITHOUT the word "फ़ोल्डर" these still mean the folder ("डेस्कटॉप खोलो").
_BARE_FOLDERS = {"desktop", "downloads", "documents", "pictures", "videos"}

# HINDI_APP_WORDS also holds feature words ("टाइमर", "मौसम"); those are not apps.
_NOT_APPS = {
    "screenshot", "clipboard", "timer", "alarm", "reminder", "note", "notes",
    "weather", "news", "volume", "brightness", "battery", "wifi", "bluetooth",
    "desktop", "downloads", "documents", "pictures", "music", "video", "videos",
    "email",
}
_EXTRA_APPS = {
    "whatsapp", "spotify", "youtube", "notepad", "calculator", "word", "excel",
    "powerpoint", "paint", "settings", "camera", "calendar", "mail", "gmail",
    "instagram", "facebook", "twitter", "telegram", "discord", "zoom", "teams",
    "skype", "netflix", "amazon", "flipkart", "wikipedia", "maps", "google maps",
    "explorer", "file explorer", "task manager", "command prompt", "terminal",
    "vs code", "visual studio", "photoshop", "vlc", "steam", "edge", "firefox",
    "store", "microsoft store", "outlook", "onenote",
}


def _known_apps() -> set:
    try:
        import tools
        known = set(getattr(tools, "APP_COMMANDS", {}))
        known |= set(getattr(tools, "WEBSITE_URLS", {}))
        known |= set(getattr(tools, "PROTOCOL_APPS", {}))
    except Exception:
        known = set()
    known |= _EXTRA_APPS
    known |= {v for v in lang.HINDI_APP_WORDS.values() if v not in _NOT_APPS}
    return known


_SITE_NAMES = {lang.fold_hindi(k): v for k, v in {
    "गूगल": "google", "गुगल": "google", "यूट्यूब": "youtube", "यू ट्यूब": "youtube",
    "विकिपीडिया": "wikipedia", "बिंग": "bing", "अमेज़न": "amazon", "अमेजन": "amazon",
    "google": "google", "youtube": "youtube", "wikipedia": "wikipedia",
    "bing": "bing", "amazon": "amazon",
}.items()}


# --------------------------------------------------------------- shared fragments

_NAME = r"[^ ]+(?: [^ ]+){0,2}?"
# Lazy on purpose: "अरिजीत सिंह के गाने चलाओ" must give the query "अरिजीत सिंह", not
# "अरिजीत सिंह के" - the shortest query that still lets the rest of the sentence match wins.
_QTEXT = r"[^ ]+(?: [^ ]+){0,6}?"

_V_OPEN = (r"(?:खोलो|खोल दो|खोलिए|खोलिये|खोल दीजिए|खोल दीजिये|खोलना|खोलें|खोल सकते हैं|"
           r"खोल सकती हैं|खोल सकते हो|खोल सकती हो|ओपन करो|ओपन कर दो|ओपन करिए|ओपन कीजिए|ओपन|"
           r"kholo|khol do|kholiye|open karo|open kar do|open)")
_V_LAUNCH = (r"(?:चालू करो|चालू कर दो|चालू करिए|चालू कीजिए|शुरू करो|शुरू कर दो|स्टार्ट करो|"
             r"स्टार्ट कर दो|लॉन्च करो|चलाओ|चला दो|चलाइए|chalu karo|chalu kar do|start karo|"
             r"shuru karo|launch karo)")
_MUSIC_WORD = (r"(?:गाना|गाने|गीत|सॉन्ग|सांग|संगीत|म्यूज़िक|म्यूजिक|म्युज़िक|music|song|"
               r"songs|gaana|gana|gaane)")
_V_PLAY = (r"(?:चलाओ|चला दो|चलाइए|चलाईये|लगाओ|लगा दो|लगाइए|बजाओ|बजा दो|बजाइए|प्ले करो|"
           r"प्ले कर दो|chalao|chala do|bajao|baja do|lagao|laga do|play karo)")
_V_BAJAO = r"(?:बजाओ|बजा दो|बजाइए|बजाईये|bajao|baja do)"


# --------------------------------------------------------------- open app / folder

# "खोलो" can also mean the folder ("डेस्कटॉप खोलो"); "चलाओ" never does ("वीडियो चलाओ" is
# not the Videos folder), so the launch verbs get their own, app-only pattern.
_APP_OPEN_RE = _c(rf"^(?:मेरा |मेरी |my )?(?P<name>{_NAME}) (?:को |ko )?{_V_OPEN}$")
_APP_LAUNCH_RE = _c(rf"^(?:मेरा |मेरी |my )?(?P<name>{_NAME}) (?:को |ko )?{_V_LAUNCH}$")
_FOLDER_RE = _c(rf"^(?:मेरा |मेरी |मेरे |my )?(?P<name>[^ ]+(?: [^ ]+)?) (?:का |की |वाला |वाली )?"
                rf"(?:फ़ोल्डर|फोल्डर|फोलडर|folder) (?:को )?(?:{_V_OPEN}|{_V_LAUNCH})$")


def _h_open(t: _Text, now) -> Optional[str]:
    m = _FOLDER_RE.fullmatch(t.text)
    if m:
        spoken = t.orig(m, "name")
        key = _FOLDER_ALIASES.get(lang.fold_hindi(spoken)) or _latin_name(spoken)
        logger.info("Hindi: open folder %r -> %r", spoken, key)
        return _act(lambda tools: tools.open_folder(key))

    m = _APP_OPEN_RE.fullmatch(t.text)
    opens_folders = bool(m)
    if not m:
        m = _APP_LAUNCH_RE.fullmatch(t.text)
    if not m:
        return None
    spoken = t.orig(m, "name")
    folded = lang.fold_hindi(spoken)
    latin = _latin_name(spoken)
    folder = _FOLDER_ALIASES.get(folded) or (latin if latin in _FOLDER_ALIASES else None)
    if opens_folders and folder in _BARE_FOLDERS:
        logger.info("Hindi: open folder (bare name) %r -> %r", spoken, folder)
        return _act(lambda tools: tools.open_folder(folder))
    if latin in _known_apps():
        logger.info("Hindi: open app %r -> %r", spoken, latin)
        return _act(lambda tools: tools.open_app(latin))
    logger.info("Hindi: %r looks like an open request but %r is not a known app", t.text, latin)
    return None


# --------------------------------------------------------------- website search

_V_SEARCH = (r"(?:सर्च करो|सर्च कर दो|सर्च करिए|सर्च कीजिए|सर्च|खोजो|खोज दो|खोजिए|ढूंढो|"
             r"ढूँढो|ढूंढ दो|ढूँढ दो|ढूंढिए|search karo|search kar do|khojo|dhundo|dhoondo)")
_ON = r"(?:पर|पे|में|par|pe|me|mein)"

_SEARCH_RE = _c(rf"^(?P<site>[^ ]+(?: ट्यूब)?) {_ON} (?P<q>{_QTEXT}) (?:के बारे में )?{_V_SEARCH}$")
_YT_PLAY_RE = _c(rf"^(?P<site>[^ ]+(?: ट्यूब)?) {_ON} (?P<q>{_QTEXT}) "
                 rf"(?:का (?:गाना|वीडियो) |वाला वीडियो )?{_V_PLAY}$")


def _h_search(t: _Text, now) -> Optional[str]:
    for rx, kind in ((_SEARCH_RE, "search"), (_YT_PLAY_RE, "play")):
        m = rx.fullmatch(t.text)
        if not m:
            continue
        site = _SITE_NAMES.get(lang.fold_hindi(t.orig(m, "site")))
        if not site:
            continue
        if kind == "play" and site != "youtube":
            continue                      # "स्पॉटिफाई पर X चलाओ" is Spotify, not a web search
        query = t.orig(m, "q").strip()
        if not query:
            continue
        logger.info("Hindi: %s on %s for %r", kind, site, query)
        return _act(lambda tools: tools.open_website_search(site, query))
    return None


# --------------------------------------------------------------- music

_GENERIC_Q = {lang.fold_hindi(w) for w in (
    "कुछ", "कोई", "कोई भी", "कुछ भी", "कोई सा भी", "कोई अच्छा", "कुछ अच्छा",
    "कोई अच्छा सा", "रैंडम", "कोई रैंडम", "कुछ भी अच्छा", "random", "kuch", "koi",
)}
_NOT_MUSIC_Q = {lang.fold_hindi(w) for w in (
    "वीडियो", "फिल्म", "मूवी", "न्यूज़", "खबर", "ख़बरें", "टीवी", "रेडियो", "पॉडकास्ट",
    "गेम", "शतरंज", "क्रिकेट", "कहानी", "पंखा", "एसी", "कंप्यूटर", "लाइट", "अलार्म",
    "youtube", "video", "news",
)}
_SPOTIFY_ON = r"(?:स्पॉटिफाई|स्पॉटिफ़ाई|स्पोटिफाई|spotify)"

_PLAY_ANY_RE = _c(rf"^(?:{_SPOTIFY_ON} {_ON} )?(?:कुछ |कोई |कोई भी |कुछ भी |थोड़ा |अच्छा )?"
                  rf"{_MUSIC_WORD} (?:को )?{_V_PLAY}$")
_PLAY_SONG_RE = _c(rf"^(?:{_SPOTIFY_ON} {_ON} )?(?P<q>{_QTEXT}) (?:का |के |की )?"
                   rf"(?:{_MUSIC_WORD} )(?:को )?{_V_PLAY}$")
_PLAY_SPOTIFY_RE = _c(rf"^{_SPOTIFY_ON} {_ON} (?P<q>{_QTEXT}) (?:को )?{_V_PLAY}$")
_PLAY_BAJAO_RE = _c(rf"^(?P<q>{_QTEXT}) (?:को )?{_V_BAJAO}$")
_PLAYLIST_RE = _c(rf"^(?:मेरी |मेरा |मेरे |my )?(?P<n>{_NAME}) (?:वाली |वाला )?"
                  rf"(?:प्लेलिस्ट|प्ले लिस्ट|playlist) (?:को )?{_V_PLAY}$")
# "मेरी प्लेलिस्ट चलाओ" names no playlist - that one is left to the LLM.
_NOT_PLAYLIST = {lang.fold_hindi(w) for w in ("मेरी", "मेरा", "मेरे", "कोई", "कुछ", "वो", "यह", "my")}

# "रुको" / "रुक जाओ" are left out on purpose: mid-reply they mean "wait", not "pause".
_PAUSE_BARE = {lang.fold_hindi(w) for w in (
    "रोको", "रोक दो", "रोकिए", "पॉज़", "पॉज", "पॉज़ करो", "पॉज करो",
    "पॉज़ कर दो", "पॉज कर दो", "pause", "pause karo", "roko", "rok do",
)}
_PAUSE_RE = _c(rf"^(?:{_MUSIC_WORD}) (?:को )?(?:रोको|रोक दो|रोकिए|रोक दीजिए|बंद करो|"
               rf"बंद कर दो|बंद कीजिए|पॉज़ करो|पॉज़ कर दो|पॉज करो|band karo|band kar do|"
               rf"roko|rok do|pause karo)$")
_RESUME_BARE = {lang.fold_hindi(w) for w in (
    "जारी रखो", "जारी रखिए", "रिज़्यूम", "रिज़्यूम करो", "रिज्यूम करो", "रिज़्यूम कर दो",
    "फिर से चलाओ", "फिर से चला दो", "वापस चलाओ", "resume", "resume karo",
)}
_RESUME_RE = _c(rf"^(?:{_MUSIC_WORD}) (?:को )?(?:चालू करो|चालू कर दो|चालू कीजिए|जारी रखो|"
                rf"फिर से चलाओ|फिर से चला दो|वापस चालू करो|रिज़्यूम करो|रिज्यूम करो|"
                rf"chalu karo|resume karo|jari rakho)$")
_NEXT_RE = _c(rf"^(?:(?:यह|ये|इस|वो) )?(?:{_MUSIC_WORD} |ट्रैक |track )?"
              rf"(?:स्किप करो|स्किप कर दो|स्किप कीजिए|स्किप|skip|skip karo)$"
              rf"|^(?:{_MUSIC_WORD} |ट्रैक |track )(?:छोड़ो|छोड़ दो|बदलो|बदल दो|आगे बढ़ाओ)$"
              rf"|^(?:अगला|अगली|अगले|नेक्स्ट|next|agla|agla gana)"
              rf"(?: (?:गाना|गाने|ट्रैक|सॉन्ग|सांग|वाला|song|track|gaana|gana))?"
              rf"(?: (?:चलाओ|चला दो|बजाओ|लगाओ|करो|चलाइए|chalao|karo|bajao))?$"
              rf"|^आगे (?:बढ़ाओ|बढ़ा दो|बढ़ाइए)$")
_PREV_RE = _c(rf"^(?:पिछला|पिछली|पिछले|पहले वाला|प्रीवियस|previous|pichla|pichhla)"
              rf"(?: (?:गाना|गाने|ट्रैक|सॉन्ग|सांग|वाला|song|track|gaana|gana))?"
              rf"(?: (?:चलाओ|चला दो|बजाओ|लगाओ|करो|चलाइए|chalao|karo|bajao))?$"
              rf"|^(?:पीछे जाओ|वापस जाओ|एक गाना पीछे|पीछे चलो)$")
_QUEUE_RES = (
    _c(rf"^(?P<q>{_QTEXT}) (?:को )?(?:क्यू|कतार|लिस्ट|queue) (?:में|मे|me) "
       rf"(?:जोड़ो|जोड़ दो|जोड़िए|डालो|डाल दो|डालिए|लगाओ|लगा दो|ऐड करो|add karo|dalo|jodo)$"),
    _c(rf"^(?:क्यू|कतार|queue) (?:में|मे|me) (?P<q>{_QTEXT}) (?:को )?"
       rf"(?:जोड़ो|जोड़ दो|डालो|डाल दो|ऐड करो|add karo|dalo|jodo)$"),
    _c(rf"^अगले (?:गाने|गाना|ट्रैक) (?:में|पर|मे) (?P<q>{_QTEXT}) (?:को )?"
       rf"(?:डालो|डाल दो|जोड़ो|जोड़ दो|लगाओ|लगा दो)$"),
)


def _mentions_youtube(t: _Text) -> bool:
    return any(f in ("यूट्यूब", "youtube", "यू") for f in t.folded)


def _h_music_control(t: _Text, now) -> Optional[str]:
    if t.text in _PAUSE_BARE or _PAUSE_RE.fullmatch(t.text):
        logger.info("Hindi: pause music (%r)", t.text)
        return _act(lambda tools: tools.pause_music())
    if t.text in _RESUME_BARE or _RESUME_RE.fullmatch(t.text):
        logger.info("Hindi: resume music (%r)", t.text)
        return _act(lambda tools: tools.resume_music())
    if _NEXT_RE.fullmatch(t.text):
        logger.info("Hindi: next track (%r)", t.text)
        return _act(lambda tools: tools.next_track())
    if _PREV_RE.fullmatch(t.text):
        logger.info("Hindi: previous track (%r)", t.text)
        return _act(lambda tools: tools.previous_track())
    return None


def _h_queue(t: _Text, now) -> Optional[str]:
    for rx in _QUEUE_RES:
        m = rx.fullmatch(t.text)
        if not m:
            continue
        spoken = t.orig(m, "q").strip()
        if lang.fold_hindi(spoken) in _GENERIC_Q or lang.fold_hindi(spoken) in _NOT_MUSIC_Q:
            return None
        query = _latin_query(spoken)
        if not query:
            return None
        logger.info("Hindi: queue %r -> %r", spoken, query)
        return _act(lambda tools: tools.queue_music(query))
    return None


def _h_play(t: _Text, now) -> Optional[str]:
    if _mentions_youtube(t):
        return None

    m = _PLAYLIST_RE.fullmatch(t.text)
    if m:
        spoken = t.orig(m, "n").strip()
        name = _latin_query(spoken)
        folded = lang.fold_hindi(spoken)
        if name and folded not in _GENERIC_Q and folded not in _NOT_PLAYLIST:
            logger.info("Hindi: play playlist %r -> %r", spoken, name)
            return _act(lambda tools: tools.play_playlist(name))
        return None

    if _PLAY_ANY_RE.fullmatch(t.text):
        logger.info("Hindi: play music (no query) (%r)", t.text)
        return _act(lambda tools: tools.play_music(""))

    for rx in (_PLAY_SONG_RE, _PLAY_SPOTIFY_RE, _PLAY_BAJAO_RE):
        m = rx.fullmatch(t.text)
        if not m:
            continue
        spoken = t.orig(m, "q").strip()
        folded = lang.fold_hindi(spoken)
        if folded in _NOT_MUSIC_Q or any(w in _NOT_MUSIC_Q for w in folded.split()):
            return None
        if folded in _GENERIC_Q:
            logger.info("Hindi: play music (generic %r)", spoken)
            return _act(lambda tools: tools.play_music(""))
        query = _latin_query(spoken)
        if not query:
            return None
        logger.info("Hindi: play music %r -> %r", spoken, query)
        return _act(lambda tools: tools.play_music(query))
    return None


# --------------------------------------------------------------- WhatsApp

_WA = (r"(?:व्हाट्सएप|व्हाट्सऐप|व्हाट्सअप|व्हाट्सआप|व्हात्सएप|वाट्सएप|वाट्सऐप|वॉट्सएप|"
       r"वॉट्सऐप|वॉट्सअप|वट्सऐप|वट्सएप|whatsapp|watsapp|whatsap)")
_MSG_WORD = r"(?:मैसेज|मेसेज|मेैसेज|संदेश|सन्देश|सँदेश|message|msg)"
_V_SEND = (r"(?:भेजो|भेज दो|भेजिए|भेजिये|भेज दीजिए|करो|कर दो|कीजिए|लिखो|लिख दो|लिखिए|"
           r"bhejo|bhej do|likho|send karo|karo)")
_KI = r"(?: (?:कि|की|ki))?"
_MSG_TAIL = r"(?: (?P<msg>.+))?"

_WA_RES = (
    _c(rf"^(?P<who>{_NAME}) (?:को|ko) {_WA} {_ON} (?:{_MSG_WORD} )?{_V_SEND}{_KI}{_MSG_TAIL}$"),
    _c(rf"^(?P<who>{_NAME}) (?:को|ko) {_WA} (?:{_MSG_WORD} )?{_V_SEND}{_KI}{_MSG_TAIL}$"),
    _c(rf"^{_WA} {_ON} (?P<who>{_NAME}) (?:को|ko) (?:{_MSG_WORD} )?{_V_SEND}{_KI}{_MSG_TAIL}$"),
    _c(rf"^(?P<who>{_NAME}) (?:को|ko) {_MSG_WORD} {_V_SEND}{_KI}{_MSG_TAIL}$"),
)
# Filler around the message itself: "... भेजो कि <message> बोल दो". Longest phrase first.
_MSG_TRAIL = sorted(
    (tuple(lang.fold_hindi(p).split()) for p in (
        "बोल दो", "बोल देना", "लिख दो", "लिख देना", "कह दो", "कह देना", "भेज दो",
        "बोलो", "लिखो", "कहो", "प्लीज़", "प्लीज", "कृपया",
    )), key=len, reverse=True)
_MSG_LEAD = sorted(
    (tuple(lang.fold_hindi(p).split()) for p in (
        "कहो कि", "बोलो कि", "लिखो कि", "कि", "की", "ki", "that",
    )), key=len, reverse=True)


def _clean_message(t: _Text, m) -> str:
    spoken = t.orig(m, "msg").strip()
    if not spoken:
        return ""
    words = spoken.split()
    folded = [lang.fold_hindi(w) for w in words]
    for _ in range(3):
        before = len(words)
        for phrase in _MSG_LEAD:
            n = len(phrase)
            if len(words) > n and tuple(folded[:n]) == phrase:
                words, folded = words[n:], folded[n:]
                break
        for phrase in _MSG_TRAIL:
            n = len(phrase)
            if len(words) > n and tuple(folded[-n:]) == phrase:
                words, folded = words[:-n], folded[:-n]
                break
        if len(words) == before:
            break
    if len(words) == 1 and any(tuple(folded) == p for p in _MSG_LEAD + _MSG_TRAIL):
        return ""
    return " ".join(words)


def _h_whatsapp(t: _Text, now) -> Optional[str]:
    for rx in _WA_RES:
        m = rx.fullmatch(t.text)
        if not m:
            continue
        spoken_who = t.orig(m, "who").strip()
        if not spoken_who:
            continue
        contact = _latin_contact(spoken_who)
        if not contact:
            continue
        message = _clean_message(t, m)
        logger.info("Hindi: WhatsApp to %r (%r), message %r", spoken_who, contact, message)
        return _act(lambda tools: tools.send_whatsapp_message(contact, message))
    return None


# --------------------------------------------------------------- screenshot

_SS = r"(?:स्क्रीनशॉट|स्क्रीन शॉट|स्क्रीनशाट|स्क्रीन शाट|स्क्रीनशोट|screenshot|screen shot)"
_SS_TAKE_RE = _c(rf"^(?:एक |मेरा |इस स्क्रीन का |स्क्रीन का )?{_SS} (?:को )?"
                 rf"(?:लो|ले लो|लेलो|लीजिए|लीजिये|ले लीजिए|खींचो|खींच लो|खींचिए|निकालो|"
                 rf"निकाल दो|कैप्चर करो|lo|le lo|lelo|lijiye)$")
_SS_SHOW_RE = _c(rf"^(?:पिछला |आखिरी |आख़िरी |अंतिम |पिछले वाला |वो |last )?{_SS} (?:को )?"
                 rf"(?:दिखाओ|दिखा दो|दिखाइए|दिखाईये|दिखाओ ना|खोलो|खोल दो|dikhao|dikha do)$")


def _h_screenshot(t: _Text, now) -> Optional[str]:
    if len(t.words) > _SHORT_WORDS:
        return None
    if _SS_TAKE_RE.fullmatch(t.text):
        logger.info("Hindi: take screenshot (%r)", t.text)
        return _act(lambda tools: tools.take_screenshot())
    if _SS_SHOW_RE.fullmatch(t.text):
        logger.info("Hindi: show last screenshot (%r)", t.text)
        return _act(lambda tools: tools.show_last_screenshot())
    return None


# --------------------------------------------------------------- date / time

_T_WORD = r"(?:समय|टाइम|वक्त|वक़्त|time|samay|taim)"
_D_WORD = r"(?:तारीख|तारीख़|तिथि|डेट|date|tareekh|tarikh)"
_NOW = r"(?:अभी |इस वक्त |अभी का |अभी कितना |abhi )?"

_TIME_RE = _c(rf"^{_NOW}(?:{_T_WORD} (?:क्या (?:हुआ है|हुआ|है|हो गया है|हो गया)|"
              rf"कितना (?:हुआ है|हुआ)|बताओ|बताइए|बता दो|बताईये|batao)"
              rf"|क्या {_T_WORD} (?:हुआ है|हुआ|है|हो गया है)"
              rf"|कितना {_T_WORD} (?:हुआ है|हुआ|है))$"
              rf"|^{_NOW}(?:कितने|kitne) (?:बजे|बज|baje)(?: (?:हैं|है|रहे हैं|गए हैं|"
              rf"चुके हैं|hain|hai))?$")
_IS = r"(?:है|hai)"
_DATE_RE = _c(rf"^(?:आज |आज की |आज का |aaj |aaj ki )?{_D_WORD} "
              rf"(?:क्या (?:है|हुई है|हुई|hai)|बताओ|बताइए|बता दो|batao)$"
              rf"|^(?:आज|aaj) (?:कौन सी|कौनसी|कितनी|क्या|kaun si|kitni) {_D_WORD} {_IS}$")
_DAY_RE = _c(rf"^(?:आज|aaj) (?:कौन सा|कौनसा|कौन से|क्या|kaun sa|kaunsa) (?:दिन|वार|din) {_IS}$"
             rf"|^(?:आज|aaj) (?:दिन|din) (?:कौन सा|कौनसा|क्या|kaun sa) {_IS}$"
             rf"|^(?:आज|aaj) (?:कौन सा|कौनसा) (?:दिन|वार) (?:है|चल रहा है|hai)$")
_BOTH_RE = _c(rf"^(?:आज की |आज का |aaj ki )?{_D_WORD} और {_T_WORD} "
              rf"(?:बताओ|बताइए|बता दो|क्या है|batao)$"
              rf"|^{_T_WORD} और {_D_WORD} (?:बताओ|बताइए|बता दो|क्या है|batao)$")


def _fmt_time_hi(now: datetime) -> str:
    spoken = lang.fmt_time(now, "hi")
    return f"अभी {spoken} हैं।" if now.minute == 0 else f"अभी {spoken} हुए हैं।"


def _fmt_date_hi(now: datetime) -> str:
    return f"आज {lang.fmt_date(now, 'hi', with_weekday=True)} है।"


def _h_datetime(t: _Text, now) -> Optional[str]:
    if len(t.words) > _SHORT_WORDS:
        return None
    if not (_TIME_RE.fullmatch(t.text) or _DATE_RE.fullmatch(t.text)
            or _DAY_RE.fullmatch(t.text) or _BOTH_RE.fullmatch(t.text)):
        return None
    now = now or datetime.now()
    if _BOTH_RE.fullmatch(t.text):
        logger.info("Hindi: date and time (%r)", t.text)
        return f"{_fmt_date_hi(now)} {_fmt_time_hi(now)}"
    if _TIME_RE.fullmatch(t.text):
        logger.info("Hindi: time (%r)", t.text)
        return _fmt_time_hi(now)
    if _DATE_RE.fullmatch(t.text):
        logger.info("Hindi: date (%r)", t.text)
        return _fmt_date_hi(now)
    logger.info("Hindi: weekday (%r)", t.text)
    return f"आज {lang.WEEKDAYS_HI[now.weekday()]} है।"


# --------------------------------------------------------------- small talk

_GREET_RE = _c(r"^(?:नमस्ते|नमस्कार|नमस्ते जी|हैलो|हेलो|हलो|हाय|हाई|हेल्लो|"
               r"hello|hi|hey|namaste|namaskar)(?: (?:रज़ील|रेज़ील|raziel|जी))?$")
_THANKS_RE = _c(r"^(?:बहुत |बहुत बहुत )?(?:धन्यवाद|शुक्रिया|थैंक यू|थैंक्स|थैंक यु|"
                r"thank you|thanks|shukriya|dhanyavad|dhanyawad)(?: (?:रज़ील|जी|यार|"
                r"सो मच|बहुत))?$")
_PRAISE_RE = _c(r"^(?:बहुत बढ़िया|बढ़िया|बहुत अच्छा|अच्छा किया|शाबाश|कमाल|कमाल है|"
                r"बहुत खूब|बहुत बढ़िया रज़ील|मस्त|वाह|वाह वाह|perfect|great job)$")
_HOWRU_RE = _c(r"^(?:तुम |आप |तू )?(?:कैसी हो|कैसे हो|कैसे हैं|कैसी हैं|कैसा है|"
               r"क्या हाल है|क्या हाल चाल है|कैसी चल रही हो|kaisi ho|kaise ho|"
               r"kaise hain)(?: (?:आप|तुम|रज़ील))?$")


_MORNING_RE = _c(r"^(?:गुड मॉर्निंग|गुड मॉर्निग|गुड मोर्निंग|सुप्रभात|शुभ प्रभात|good morning|suprabhat)"
                 r"(?: (?:रज़ील|रेज़ील|raziel|जी))?$")
_AFTERNOON_RE = _c(r"^(?:गुड आफ्टरनून|गुड आफ्टरनुन|शुभ दोपहर|good afternoon)(?: (?:रज़ील|रेज़ील|raziel|जी))?$")
_EVENING_RE = _c(r"^(?:गुड इवनिंग|गुड ईवनिंग|शुभ संध्या|good evening)(?: (?:रज़ील|रेज़ील|raziel|जी))?$")


def _h_smalltalk(t: _Text, now) -> Optional[str]:
    if len(t.words) > 6:
        return None
    text = t.text
    # (the first "good morning" of the day is answered by briefing.py, which sits earlier in main.py's order)
    if _MORNING_RE.fullmatch(text):
        logger.info("Hindi: small talk good morning (%r)", text)
        return "सुप्रभात। मैं तैयार हूँ।"
    if _AFTERNOON_RE.fullmatch(text):
        logger.info("Hindi: small talk good afternoon (%r)", text)
        return "नमस्कार। मैं तैयार हूँ।"
    if _EVENING_RE.fullmatch(text):
        logger.info("Hindi: small talk good evening (%r)", text)
        return "शुभ संध्या। मैं तैयार हूँ।"
    if _GREET_RE.fullmatch(text):
        logger.info("Hindi: small talk greeting (%r)", text)
        return "नमस्ते। मैं तैयार हूँ।"
    if _THANKS_RE.fullmatch(text):
        logger.info("Hindi: small talk thanks (%r)", text)
        return "कभी भी।"
    if _PRAISE_RE.fullmatch(text):
        logger.info("Hindi: small talk praise (%r)", text)
        return "शुक्रिया।"
    if _HOWRU_RE.fullmatch(text):
        logger.info("Hindi: small talk how-are-you (%r)", text)
        return "मैं ठीक हूँ। आप बताइए।"
    return None


# --------------------------------------------------------------- the matcher

_FAILED = "माफ़ कीजिए, यह काम नहीं हो पाया।"


def _act(call: Callable) -> str:
    """Runs the tools.py call. A failure becomes a spoken apology, never a traceback."""
    try:
        import tools
    except Exception as e:                       # pragma: no cover - import guard
        logger.error("Hindi: tools unavailable: %s", e)
        return _FAILED
    try:
        reply = call(tools)
    except Exception as e:
        logger.error("Hindi: tool call failed: %s", e, exc_info=True)
        return _FAILED
    reply = (reply or "").strip()
    return reply or _FAILED


_HINGLISH_HINTS = {
    "kholo", "khol", "kholiye", "chalao", "chala", "bajao", "baja", "lagao", "laga",
    "karo", "kardo", "kar", "bhejo", "bhej", "likho", "dikhao", "dikha", "batao",
    "bata", "sunao", "roko", "rok", "ruko", "jodo", "dalo", "kya", "hai", "hain",
    "baje", "gaana", "gana", "gaane", "geet", "milte", "jao", "suno", "khojo",
    "dhundo", "alvida", "namaste", "shukriya", "dhanyavad", "kaise", "kaisi",
    "abhi", "tareekh", "tarikh", "samay", "mera", "meri", "mere", "kuch", "koi",
    "mujhe", "aaj", "pichla", "agla", "chalu", "shuru", "bhi", "wala", "wali",
    "lo", "lelo", "lijiye", "kaun", "kitne", "kitna", "din", "baat",
}


def _is_hindi(t: _Text) -> bool:
    if any(lang.has_devanagari(w) for w in t.words):
        return True
    if lang.detect(" ".join(t.words)) == "hi":
        return True
    return any(f in _HINGLISH_HINTS for f in t.folded)


_HANDLERS: Tuple[Callable, ...] = (
    _h_screenshot,
    _h_datetime,
    _h_whatsapp,
    _h_music_control,
    _h_queue,
    _h_search,
    _h_play,
    _h_open,
    _h_smalltalk,
)


def try_handle(transcript: str, now: Optional[datetime] = None) -> Optional[str]:
    """
    A Hindi command for one of Raziel's existing features -> the action is done and the
    finished Hindi sentence comes back. Anything else (English, chit-chat, another
    module's job) -> None, and nothing happened. Never raises.
    """
    try:
        if not transcript or not isinstance(transcript, str):
            return None
        if not _enabled():
            return None
        whole = _make(transcript)
        if not whole.text or len(whole.words) > _MAX_WORDS:
            return None
        if not _is_hindi(whole):
            return None
        core = _strip_fillers(whole)
        if not core.text:
            return None
        for handler in _HANDLERS:
            for variant in ((core,) if core.text == whole.text else (core, whole)):
                reply = handler(variant, now)
                if reply:
                    return reply
        logger.info("Hindi: no match for %r", whole.text)
        return None
    except Exception as e:                       # pragma: no cover - safety net
        logger.warning("Hindi: try_handle failed on %r: %s", transcript, e, exc_info=True)
        return None


# --------------------------------------------------------------- sleep phrases

_SLEEP_RE = _c(r"^(?:अलविदा|गुडबाय|गुड बाय|गुड बाई|गुडबाई|गुड़बाय|बाय|बाय बाय|बाई|"
               r"टाटा|ता ता|alvida|bye|bye bye|goodbye|good bye|tata)"
               r"(?: (?:रज़ील|रेज़ील|जी|यार|raziel))?$"
               r"|^(?:अब )?(?:सो जाओ|सो जाइए|सो जाइये|सो जा|सोजाओ|सो जाओ अब|आराम करो|"
               r"so jao|soja|so ja)(?: (?:रज़ील|जी|यार))?$"
               r"|^(?:फिर मिलते हैं|फिर मिलेंगे|बाद में मिलते हैं|phir milte hain)$"
               r"|^(?:गुड नाइट|गुडनाइट|शुभ रात्रि|शुभ रात्री|good night)$")


def is_sleep_hindi(transcript: str) -> bool:
    """True when the whole short utterance is a Hindi goodbye ("अलविदा", "सो जाओ", "बाय बाय")."""
    try:
        if not transcript or not isinstance(transcript, str):
            return False
        whole = _make(transcript)
        if not whole.text or len(whole.words) > _SLEEP_WORDS:
            return False
        core = _strip_fillers(whole)
        for t in {whole.text, core.text}:
            if t and _SLEEP_RE.fullmatch(t):
                logger.info("Hindi: sleep phrase %r", transcript)
                return True
        return False
    except Exception as e:                       # pragma: no cover - safety net
        logger.warning("Hindi: is_sleep_hindi failed on %r: %s", transcript, e)
        return False


# --------------------------------------------------------------- localizing tool replies

# The exact sentences tools.py / spotify_queue.py / main.py / llm_brain.py return.
# Specific patterns come first; "I couldn't ..." is the last resort.
_TEMPLATES: List[Tuple["re.Pattern", str]] = [(re.compile(p), h) for p, h in (
    # --- apps, folders, websites
    (r"Opened the (.+?) folder\.", "{0} फ़ोल्डर खोल दिया।"),
    (r"Opened (.+?) in your browser\.", "{0} ब्राउज़र में खोल दिया।"),
    (r"Opened (.+?), but I don't know how to search directly on that site yet\.",
     "{0} खोल दिया, पर उस साइट पर सीधे सर्च करना मुझे अभी नहीं आता।"),
    (r"Opened (.+?)\.", "{0} खोल दिया।"),
    (r"Searched (.+?) for (.+?)\.", "{0} पर {1} सर्च कर दिया।"),
    (r"I don't know how to search on (.+?)\.", "{0} पर सर्च करना मुझे नहीं आता।"),
    (r"I couldn't find (.+?) on this computer\.", "मुझे इस कंप्यूटर पर {0} नहीं मिला।"),
    (r"I couldn't find a folder called (.+?)\.", "मुझे {0} नाम का कोई फ़ोल्डर नहीं मिला।"),
    # --- Spotify
    (r"Playing your playlist (.+?) on Spotify\.", "स्पॉटिफाई पर आपकी प्लेलिस्ट {0} चल रही है।"),
    (r"Playing (.+?) by (.+?) on Spotify\.", "स्पॉटिफाई पर {1} का गाना {0} चल रहा है।"),
    (r"Playing music on Spotify\.", "स्पॉटिफाई पर म्यूज़िक चल रहा है।"),
    (r"Playing (.+?) on Spotify\.", "स्पॉटिफाई पर {0} चल रहा है।"),
    (r"Paused\.", "रोक दिया।"),
    (r"Resumed\.", "फिर से चालू कर दिया।"),
    (r"Skipped to the next track\.", "अगला गाना चला दिया।"),
    (r"Went back to the previous track\.", "पिछला गाना चला दिया।"),
    (r"Spotify doesn't seem to be open right now, so there is no queue\.",
     "लगता है स्पॉटिफाई अभी खुला नहीं है, तो क्यू भी नहीं है।"),
    (r"Spotify doesn't seem to be open right now\.", "लगता है स्पॉटिफाई अभी खुला नहीं है।"),
    (r"Added (.+?) by (.+?) to your queue\.", "{1} का गाना {0} आपके क्यू में जोड़ दिया।"),
    (r"Added (.+?) to your queue\.", "{0} आपके क्यू में जोड़ दिया।"),
    (r"Nothing is playing on Spotify right now, so there is no queue\.",
     "अभी स्पॉटिफाई पर कुछ नहीं चल रहा, तो कोई क्यू नहीं है।"),
    (r"Start a song first\.", "पहले कोई गाना चलाइए।"),
    (r"Specify which song to add to the queue\.", "बताइए कौन सा गाना क्यू में डालना है।"),
    (r"I couldn't find a song matching '(.+?)' on Spotify\.",
     "स्पॉटिफाई पर '{0}' जैसा कोई गाना नहीं मिला।"),
    (r"I opened Spotify but it isn't showing up as an active device yet\.",
     "स्पॉटिफाई खोल तो दिया, पर वह अभी एक्टिव डिवाइस की तरह नहीं दिख रहा।"),
    (r"Try asking again in a few seconds\.", "कुछ सेकंड बाद फिर कहिए।"),
    # --- screenshots
    (r"Saved a screenshot as (.+?) in the (.+?) folder\.",
     "स्क्रीनशॉट {0} नाम से {1} फ़ोल्डर में सेव कर दिया।"),
    (r"Here's the screenshot\.", "यह रहा स्क्रीनशॉट।"),
    (r"I don't have a screenshot to show yet - take one first\.",
     "अभी दिखाने के लिए कोई स्क्रीनशॉट नहीं है, पहले एक लीजिए।"),
    (r"I couldn't take the screenshot: (.+)", "मैं स्क्रीनशॉट नहीं ले पाई।"),
    # --- WhatsApp
    (r"Sent your message to (.+?) on WhatsApp\.", "आपका मैसेज {0} को व्हाट्सएप पर भेज दिया।"),
    (r"What should the message to (.+?) say\?", "{0} को क्या मैसेज भेजना है?"),
    (r"I don't have a phone number saved for (.+?)\.", "मेरे पास {0} का फ़ोन नंबर सेव नहीं है।"),
    (r"Which one do you mean\?", "आप किसकी बात कर रहे हैं?"),
    (r"Sending it now\.", "अभी भेज रही हूँ।"),
    (r"Please don't touch the keyboard or mouse for a few seconds\.",
     "कुछ सेकंड तक कीबोर्ड या माउस को हाथ मत लगाइए।"),
    # --- memory
    (r"I'll remember that: (.+)", "याद रखूँगी: {0}"),
    (r"Forgot: (.+)", "भूल गई: {0}"),
    (r"I didn't find anything remembered matching '(.+?)'\.",
     "'{0}' से मिलता-जुलता कुछ याद नहीं मिला।"),
    (r"I don't have anything remembered about '(.+?)'\.",
     "'{0}' के बारे में मुझे कुछ याद नहीं है।"),
    (r"Got it, I'll remember your favorite (.+?) is (.+?)\.",
     "समझ गई, याद रखूँगी कि आपका पसंदीदा {0} {1} है।"),
    # --- short, frequent sentences
    (r"Understood\.", "समझ गई।"),
    (r"Acknowledged\.", "ठीक है।"),
    (r"Done\.", "हो गया।"),
    (r"Goodbye\.", "अलविदा।"),
    (r"Listening\.", "सुन रही हूँ।"),
    (r"Systems online\.", "सिस्टम ऑनलाइन।"),
    (r"Task complete\.", "काम हो गया।"),
    (r"That request failed\.", "वह काम नहीं हो पाया।"),
    (r"Anytime\.", "कभी भी।"),
    (r"Hello\.", "नमस्ते।"),
    (r"Standing by\.", "मैं तैयार हूँ।"),
    (r"I did that, but had trouble summarizing the result\.",
     "काम कर दिया, पर नतीजा बताने में दिक्कत हुई।"),
    (r"I did not go through with the pending request\.",
     "जो काम रुका हुआ था, वह मैंने नहीं किया।"),
    # --- last resort
    (r"I couldn't (.+)", "मैं यह नहीं कर पाई।"),
)]

_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _localize_sentence(sentence: str) -> Optional[str]:
    s = sentence.strip()
    if not s:
        return None
    for rx, hindi in _TEMPLATES:
        m = rx.fullmatch(s)
        if not m:
            continue
        out = hindi
        for i, g in enumerate(m.groups()):
            out = out.replace("{%d}" % i, (g or "").strip())
        return out
    return None


def localize(text: str, code: Optional[str] = None) -> str:
    """
    The English sentence a tool returned -> the same thing in Hindi, while the turn is
    Hindi. Anything not in the table (and every English turn) comes back unchanged.
    Multi-sentence replies are translated sentence by sentence. Never raises.
    """
    try:
        if not text or not isinstance(text, str):
            return text
        if (code or lang.current()) != "hi":
            return text
        if lang.has_devanagari(text):
            return text
        parts = _SPLIT_RE.split(text)
        out, changed = [], False
        for part in parts:
            hindi = _localize_sentence(part)
            if hindi is None:
                out.append(part)
            else:
                out.append(hindi)
                changed = True
        if not changed:
            return text
        result = " ".join(p for p in out if p)
        logger.info("Hindi: localized %r -> %r", text, result)
        return result
    except Exception as e:                       # pragma: no cover - safety net
        logger.warning("Hindi: localize failed on %r: %s", text, e)
        return text
