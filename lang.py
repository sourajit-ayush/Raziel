"""
lang.py - which language Raziel is speaking right now, plus a small Hindi toolkit.

The language is decided PER TURN from the script of what the user said:
Devanagari in the transcript = Hindi, anything else = English. main.py calls
set_current(detect(transcript)) after every transcription; every module that
words a reply (reminders, weather, news...) asks current() so the answer comes
back in the language the user just used. Nothing here needs the OS, the network
or any audio library, so it is trivially testable.

What lives here
---------------
  * current()/set_current()/detect()      the per-turn language state
  * tr(table, key, **vars)                one-line i18n: {"key": {"en": "...", "hi": "..."}}
  * fold_hindi()/normalize_hindi()        spelling-variant folding (हाँ = हां = हा, नहीं = नही)
  * hindi_to_ascii_numbers()              Devanagari digits and Hindi number words -> digits
  * split_runs()                          Hindi/English code-switching for the TTS
  * to_latin()/hint_latin()               Devanagari -> letters (TTS fallback, app / contact names)
"""

from __future__ import annotations

import re
import threading
import unicodedata
from typing import Dict, List, Optional, Tuple

# ------------------------------------------------------------------ language state

_lock = threading.Lock()
_current = "en"


def current() -> str:
    """'en' or 'hi': the language of the user's latest utterance."""
    return _current


def set_current(code: str) -> str:
    global _current
    with _lock:
        _current = "hi" if code == "hi" else "en"
    return _current


def is_hindi() -> bool:
    return _current == "hi"


# ------------------------------------------------------------------ script detection

_DEV_RANGE = "ऀ-ॿ"
_DEV_RE = re.compile(f"[{_DEV_RANGE}]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_DANDA = "।॥"                     # । ॥ (punctuation, not letters)
_DEV_DIGITS = "०१२३४५६७८९"
_DEV_WORD_RE = re.compile("[\u0900-\u0963\u0971-\u097F]+")   # letters + vowel signs (no danda, no digits)


def has_devanagari(text: str) -> bool:
    return bool(_DEV_RE.search(text or ""))


def devanagari_ratio(text: str) -> float:
    """Share of Devanagari among the Devanagari + Latin LETTERS of `text` (0.0 - 1.0)."""
    text = text or ""
    dev = sum(1 for c in text if "ऀ" <= c <= "ॿ" and c not in _DANDA
              and c not in _DEV_DIGITS)
    lat = len(_LATIN_RE.findall(text))
    total = dev + lat
    return dev / total if total else 0.0


# Latin-script Hindi ("Hinglish") words that are NOT also common English words.
# Two or more of them in one utterance = the user is speaking Hindi.
HINGLISH_MARKERS = frozenset({
    "kya", "hai", "hain", "nahi", "nahin", "karo", "kardo", "batao", "bolo", "kholo", "chalao",
    "bajao", "lagao", "mujhe", "mera", "meri", "mere", "tum", "aap", "kal", "aaj", "subah",
    "shaam", "raat", "baje", "ghanta", "ghante", "abhi", "jaldi", "kaisa", "kaise", "kitna",
    "kitne", "kaun", "kahan", "kyun", "kyu", "theek", "accha", "acha", "bhai", "yaar", "suno",
    "dikhao", "sunao", "chalu", "khabar", "mausam", "dobara", "phir", "mein", "ko", "ka",
    "ki", "se", "par", "aur", "lekin", "wala", "wali", "chahiye", "hoga", "hua", "hui",
})
_STRONG_HINGLISH = HINGLISH_MARKERS - {"ko", "ka", "ki", "se", "par", "aur", "mein", "phir"}


def detect(text: str) -> str:
    """'hi' if the transcript is Hindi (Devanagari, or clearly Hinglish), else 'en'."""
    text = text or ""
    if has_devanagari(text):
        # Compare WORDS, not letters: a Devanagari word is several code points (vowel signs), so a
        # letter count would let one Hindi word outweigh a whole English sentence.
        dev_words = len(_DEV_WORD_RE.findall(text))
        lat_words = len(re.findall(r"[A-Za-z]+", text))
        if dev_words and dev_words / (dev_words + lat_words) >= 0.4:
            return "hi"
    words = re.findall(r"[a-z]+", text.lower())
    if len(words) >= 2:
        strong = sum(1 for w in words if w in _STRONG_HINGLISH)
        weak = sum(1 for w in words if w in HINGLISH_MARKERS)
        if strong >= 2 or (strong >= 1 and weak >= 3):
            return "hi"
    return "en"


# ------------------------------------------------------------------ tiny i18n

def tr(table: Dict[str, object], key: str, lang: Optional[str] = None, **fmt) -> str:
    """
    table = {"set": {"en": "Timer set for {t}.", "hi": "{t} का टाइमर लग गया।"}, "x": "plain"}
    tr(table, "set", t="10 minutes") -> text in the current language (English fallback).
    A missing key returns the key itself, so a typo is visible instead of silent.
    """
    entry = table.get(key, key)
    if isinstance(entry, dict):
        text = entry.get(lang or _current) or entry.get("en") or next(iter(entry.values()), key)
    else:
        text = str(entry)
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return text
    return text


# ------------------------------------------------------------------ Hindi spelling folding

_COMBINING_DROP = dict.fromkeys(map(ord, "ँं़‌‍"), None)  # ँ ं ़ ZWNJ ZWJ


def fold_hindi(text: str) -> str:
    """
    Compares Hindi spellings that mean the same: हाँ / हां / हा, नहीं / नही,
    ज़रूर / जरूर. Drops chandrabindu, anusvara, nukta and joiners, folds the
    long ी at the end of a word onto ि-less spelling variants only where safe.
    Also lower-cases Latin and turns Devanagari digits into ASCII.
    """
    text = unicodedata.normalize("NFC", text or "")
    text = unicodedata.normalize("NFD", text).translate(_COMBINING_DROP)
    text = unicodedata.normalize("NFC", text)
    text = text.translate(str.maketrans(_DEV_DIGITS, "0123456789"))
    text = text.replace("।", " ").replace("॥", " ")
    return text.lower()


_PUNCT_RE = re.compile(rf"[^\w\s'{_DEV_RANGE}]+", re.UNICODE)


def normalize_hindi(text: str) -> str:
    """fold_hindi() + punctuation removed (matras kept) + spaces collapsed."""
    text = fold_hindi(text).replace("’", "'")
    text = _PUNCT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_any(text: str) -> str:
    """Lower-cased, punctuation-free, Devanagari-safe text for keyword matching."""
    return normalize_hindi(text)


# ------------------------------------------------------------------ Hindi numbers

_UNITS_0_99 = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस "
    "बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस "
    "पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चौवालीस पैंतालीस छियालीस "
    "सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ "
    "इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर "
    "पचहत्तर छिहत्तर सतहत्तर अठहत्तर उनासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सतासी "
    "अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पंचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()
assert len(_UNITS_0_99) == 100

_EXTRA_WORDS = {           # spelling variants, colloquial forms
    "पांच": 5, "पाँच": 5, "छः": 6, "छे": 6, "पन्द्रह": 15, "पंदरह": 15, "अट्ठाईस": 28,
    "अठाईस": 28, "अट्ठाइस": 28, "अठ्ठाईस": 28, "चौंतीस": 34, "ज़ीरो": 0, "जीरो": 0,
    "ग्यारा": 11, "बारा": 12, "तेरा": 13, "चौदा": 14, "पंद्रा": 15, "सोला": 16, "सत्रा": 17,
    "अठारा": 18, "बिस": 20, "तिस": 30, "पैंतिस": 35, "चालिस": 40, "पचाश": 50, "साट": 60,
    "सत्तार": 70, "अस्सि": 80, "नब्बे": 90, "एकसौ": 100,
}
HINDI_NUMBER_WORDS: Dict[str, int] = {w: i for i, w in enumerate(_UNITS_0_99)}
HINDI_NUMBER_WORDS.update(_EXTRA_WORDS)
_FOLDED_NUMBER_WORDS: Dict[str, int] = {fold_hindi(w): v for w, v in HINDI_NUMBER_WORDS.items()}
_MULTIPLIERS = {"सौ": 100, "हज़ार": 1000, "हजार": 1000, "लाख": 100000, "करोड़": 10000000, "करोड": 10000000}
_FOLDED_MULT = {fold_hindi(w): v for w, v in _MULTIPLIERS.items()}
_FRACTION_WORDS = {"डेढ़": 1.5, "डेढ": 1.5, "ढाई": 2.5, "आधा": 0.5, "आधे": 0.5, "आधी": 0.5, "सवा": 1.25,
                   "पौन": 0.75, "पौना": 0.75, "पौने": 0.75}
_FOLDED_FRACTIONS = {fold_hindi(w): v for w, v in _FRACTION_WORDS.items()}


def devanagari_digits_to_ascii(text: str) -> str:
    return (text or "").translate(str.maketrans(_DEV_DIGITS, "0123456789"))


def hindi_to_ascii_numbers(text: str) -> str:
    """
    'बीस मिनट' -> '20 मिनट', 'एक सौ पच्चीस' -> '125', 'दो हज़ार पाँच सौ' -> '2500',
    '२० मिनट' -> '20 मिनट'. Fraction words (डेढ़, ढाई, सवा, साढ़े, पौने, आधा) are left alone
    because their meaning depends on the phrase (timeparse.py handles them).
    """
    text = devanagari_digits_to_ascii(text or "")
    tokens = re.findall(r"\s+|\S+", text)
    out: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        core = fold_hindi(tok.strip(".,!?;:।"))
        if core in _FOLDED_NUMBER_WORDS or (core in _FOLDED_MULT and core != ""):
            j, total, current_part, consumed_any = i, 0, 0, False
            last_end = i
            while j < len(tokens):
                t = tokens[j]
                if t.isspace():
                    j += 1
                    continue
                c = fold_hindi(t.strip(".,!?;:।"))
                if c in _FOLDED_NUMBER_WORDS:
                    if consumed_any and current_part and current_part < 100 and current_part % 10 != 0 \
                            and _FOLDED_NUMBER_WORDS[c] < 100:
                        break                       # "पाँच सात": two separate numbers
                    current_part += _FOLDED_NUMBER_WORDS[c]
                elif c in _FOLDED_MULT:
                    mult = _FOLDED_MULT[c]
                    if mult == 100:
                        current_part = (current_part or 1) * 100
                    else:
                        total += (current_part or 1) * mult
                        current_part = 0
                else:
                    break
                consumed_any = True
                last_end = j
                j += 1
            out.append(str(total + current_part))
            i = last_end + 1
            continue
        out.append(tok)
        i += 1
    return "".join(out)


def fraction_value(word: str) -> Optional[float]:
    return _FOLDED_FRACTIONS.get(fold_hindi(word.strip(".,!?;:।")))


# ------------------------------------------------------------------ Devanagari -> letters

# Hindi spellings of English words and app names -> the English name. Used before
# the letter-by-letter fallback, so "क्रोम खोलो" gives "chrome" and not "krom".
HINDI_APP_WORDS: Dict[str, str] = {
    "क्रोम": "chrome", "गूगल क्रोम": "chrome", "यूट्यूब": "youtube", "यू ट्यूब": "youtube",
    "व्हाट्सएप": "whatsapp", "व्हाट्सऐप": "whatsapp", "वाट्सएप": "whatsapp", "वॉट्सऐप": "whatsapp",
    "वट्सऐप": "whatsapp", "व्हाट्सअप": "whatsapp", "स्पॉटिफ़ाई": "spotify", "स्पॉटिफाई": "spotify",
    "स्पोटिफाई": "spotify", "नोटपैड": "notepad", "नोटपेड": "notepad", "कैलकुलेटर": "calculator",
    "कैलक्युलेटर": "calculator", "गूगल": "google", "गुगल": "google", "जीमेल": "gmail",
    "जी मेल": "gmail", "इंस्टाग्राम": "instagram", "फेसबुक": "facebook", "फ़ेसबुक": "facebook",
    "टेलीग्राम": "telegram", "ट्विटर": "twitter", "एक्स": "x", "वर्ड": "word", "एक्सेल": "excel",
    "पावरपॉइंट": "powerpoint", "पेंट": "paint", "सेटिंग्स": "settings", "सेटिंग": "settings",
    "कैमरा": "camera", "एज": "edge", "माइक्रोसॉफ्ट एज": "edge", "फायरफॉक्स": "firefox",
    "डिस्कॉर्ड": "discord", "ज़ूम": "zoom", "जूम": "zoom", "टीम्स": "teams", "स्काइप": "skype",
    "नेटफ्लिक्स": "netflix", "अमेज़न": "amazon", "अमेजन": "amazon", "फ्लिपकार्ट": "flipkart",
    "विकिपीडिया": "wikipedia", "मैप्स": "maps", "गूगल मैप्स": "google maps", "ईमेल": "email",
    "एक्सप्लोरर": "explorer", "फ़ाइल एक्सप्लोरर": "file explorer", "टास्क मैनेजर": "task manager",
    "कमांड प्रॉम्प्ट": "command prompt", "टर्मिनल": "terminal", "वीएस कोड": "vs code",
    "विजुअल स्टूडियो": "visual studio", "फोटोशॉप": "photoshop", "वीएलसी": "vlc", "स्टीम": "steam",
    "डेस्कटॉप": "desktop", "डाउनलोड्स": "downloads", "डाउनलोड": "downloads", "डॉक्यूमेंट्स": "documents",
    "पिक्चर्स": "pictures", "म्यूज़िक": "music", "वीडियो": "video", "वीडियोज": "videos",
    "स्क्रीनशॉट": "screenshot", "क्लिपबोर्ड": "clipboard", "टाइमर": "timer", "अलार्म": "alarm",
    "रिमाइंडर": "reminder", "कैलेंडर": "calendar", "नोट": "note", "नोट्स": "notes", "मौसम": "weather",
    "न्यूज़": "news", "न्यूज": "news", "वॉल्यूम": "volume", "ब्राइटनेस": "brightness",
    "बैटरी": "battery", "वाईफाई": "wifi", "वाई-फाई": "wifi", "ब्लूटूथ": "bluetooth",
}
_FOLDED_APP_WORDS: Dict[str, str] = {fold_hindi(k): v for k, v in HINDI_APP_WORDS.items()}

_CONS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh",
    "ञ": "ny", "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d",
    "ध": "dh", "न": "n", "प": "p", "फ": "f", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r",
    "ल": "l", "व": "v", "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ळ": "l",
    "क़": "q", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "r", "ढ़": "rh", "फ़": "f", "य़": "y",
}
_INDEP_VOWELS = {"अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo", "ऋ": "ri", "ए": "e",
                 "ऐ": "ai", "ओ": "o", "औ": "au", "ऑ": "o", "ऍ": "e"}
_MATRAS = {"ा": "aa", "ि": "i", "ी": "ee", "ु": "u", "ू": "oo", "ृ": "ri",
           "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
           "ॆ": "e", "ॊ": "o"}
_VIRAMA = "्"
_NUKTA = "़"


def _romanize_word(word: str) -> str:
    """One Devanagari word -> letters. (Nukta is already folded away by fold_hindi.)"""
    word = unicodedata.normalize("NFC", word).replace(_NUKTA, "")
    out: List[str] = []
    n = len(word)
    i = 0
    while i < n:
        ch = word[i]
        if ch in _CONS:
            nxt = word[i + 1] if i + 1 < n else ""
            out.append(_CONS[ch])
            if nxt == _VIRAMA:              # conjunct: no vowel
                i += 2
                continue
            if nxt in _MATRAS:
                out.append(_MATRAS[nxt])
                i += 2
                continue
            if i + 1 < n:                   # inherent 'a', dropped at word end (कमल -> kamal)
                out.append("a")
            i += 1
            continue
        if ch in _INDEP_VOWELS:
            out.append(_INDEP_VOWELS[ch])
        elif ch in ("\u0902", "\u0901"):
            out.append("n")
        elif ch == "\u0903":
            out.append("h")
        elif ch in _DEV_DIGITS:
            out.append(str(_DEV_DIGITS.index(ch)))
        elif ch in _DANDA:
            out.append(".")
        elif ch in _MATRAS:
            out.append(_MATRAS[ch])
        elif ch == _VIRAMA:
            pass
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def to_latin(text: str) -> str:
    """
    Devanagari -> readable letters. Known app / loan words first (क्रोम -> chrome),
    the rest letter by letter (नमस्ते -> namaste). Latin text passes through.
    Approximate on purpose: it feeds the TTS fallback and name matching, not display.
    """
    text = text or ""
    if not has_devanagari(text):
        return text
    tokens = re.findall(r"\s+|[^\s]+", text)
    out: List[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.isspace():
            out.append(tok)
            i += 1
            continue
        core = tok.strip(".,!?;:\u0964\u0965")
        # two-word names first ("गूगल मैप्स", "टास्क मैनेजर")
        if i + 2 < len(tokens) and tokens[i + 1].isspace() and not tokens[i + 2].isspace():
            core2 = tokens[i + 2].strip(".,!?;:\u0964\u0965")
            pair = fold_hindi(core) + " " + fold_hindi(core2)
            if pair in _FOLDED_APP_WORDS:
                out.append(_FOLDED_APP_WORDS[pair])
                i += 3
                continue
        folded = fold_hindi(core)
        if folded in _FOLDED_APP_WORDS:
            out.append(tok.replace(core, _FOLDED_APP_WORDS[folded]) if core else tok)
        elif has_devanagari(tok):
            out.append(_romanize_word(tok))
        else:
            out.append(tok)
        i += 1
    return "".join(out)


def hint_latin(text: str) -> str:
    """Latin form of a (possibly Devanagari) spoken name, for matching contacts / apps."""
    return to_latin(text).strip()


# ------------------------------------------------------------------ code-switching for the TTS

def split_runs(text: str) -> List[Tuple[str, str]]:
    """
    Splits mixed text into consecutive ("hi" | "en", chunk) runs so each can be spoken by
    the voice that can pronounce it:
      "आपके मैसेज में Fazal का नाम है" -> [("hi", "आपके मैसेज में "), ("en", "Fazal "), ("hi", "का नाम है")]
    Digits, spaces and punctuation stay with the run before them (or the first run).
    Pure English or pure Hindi text comes back as one run; text with no letters at all is one "en" run.
    """
    text = text or ""
    if not text:
        return []
    kinds: List[Optional[str]] = []
    for ch in text:
        if "ऀ" <= ch <= "ॿ":
            kinds.append(None if ch in _DEV_DIGITS or ch in _DANDA else "hi")
        elif ch.isalpha():
            kinds.append("en")
        else:
            kinds.append(None)
    first = next((k for k in kinds if k), None)
    if first is None:
        return [("en", text)]
    last = first
    resolved: List[str] = []
    for k in kinds:
        if k:
            last = k
        resolved.append(last)
    # a neutral char at the very start takes the first letter's language (already true
    # because `last` starts as `first`)
    runs: List[Tuple[str, str]] = []
    for ch, k in zip(text, resolved):
        if runs and runs[-1][0] == k:
            runs[-1] = (k, runs[-1][1] + ch)
        else:
            runs.append((k, ch))
    return runs


def sentence_end_chars() -> str:
    """Characters that end a spoken sentence in either language."""
    return ".!?।॥"


# ------------------------------------------------------------------ dates, times, durations (both languages)

WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_HI = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
             "October", "November", "December"]
MONTHS_HI = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर",
             "नवंबर", "दिसंबर"]
# Alternative spellings Whisper / people use, folded for lookup. Monday = 0 (datetime.weekday()).
_WEEKDAY_LOOKUP = {}
for _i, _names in enumerate([
        ["सोमवार", "सोम"], ["मंगलवार", "मंगल"], ["बुधवार", "बुध"],
        ["गुरुवार", "बृहस्पतिवार", "वीरवार", "गुरु"], ["शुक्रवार", "शुक्र"], ["शनिवार", "शनि"],
        ["रविवार", "इतवार", "रवि"]]):
    for _n in _names:
        _WEEKDAY_LOOKUP[fold_hindi(_n)] = _i
_MONTH_LOOKUP = {}
for _i, _names in enumerate([
        ["जनवरी"], ["फरवरी", "फ़रवरी"], ["मार्च"], ["अप्रैल", "अप्रेल"], ["मई"], ["जून"], ["जुलाई"],
        ["अगस्त"], ["सितंबर", "सितम्बर", "सितमबर"], ["अक्टूबर", "अक्तूबर"], ["नवंबर", "नवम्बर"],
        ["दिसंबर", "दिसम्बर"]]):
    for _n in _names:
        _MONTH_LOOKUP[fold_hindi(_n)] = _i + 1


def hindi_weekday_index(word: str) -> Optional[int]:
    """'सोमवार' -> 0 ... 'रविवार' -> 6 (datetime.weekday()); None if not a weekday word."""
    return _WEEKDAY_LOOKUP.get(fold_hindi((word or "").strip(".,!?;:।")))


def hindi_month_number(word: str) -> Optional[int]:
    """'सितंबर' -> 9; None if not a month word."""
    return _MONTH_LOOKUP.get(fold_hindi((word or "").strip(".,!?;:।")))


def _period_hi(hour: int) -> str:
    if 4 <= hour < 12:
        return "सुबह"
    if 12 <= hour < 16:
        return "दोपहर"
    if 16 <= hour < 20:
        return "शाम"
    return "रात"


def fmt_time(dt, lang: Optional[str] = None) -> str:
    """English '5:30 PM' / '5 PM'; Hindi 'शाम 5 बजकर 30 मिनट' / 'शाम 5 बजे' (a Hindi voice reads it naturally)."""
    lang = lang or _current
    hour, minute = dt.hour, dt.minute
    if lang == "hi":
        h12 = hour % 12 or 12
        if minute == 0:
            return f"{_period_hi(hour)} {h12} बजे"
        return f"{_period_hi(hour)} {h12} बजकर {minute} मिनट"
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12} {suffix}" if minute == 0 else f"{h12}:{minute:02d} {suffix}"


def fmt_date(dt, lang: Optional[str] = None, with_year: bool = False, with_weekday: bool = True) -> str:
    """English 'Sunday, September 20'; Hindi 'रविवार, 20 सितंबर' (year optional)."""
    lang = lang or _current
    if lang == "hi":
        base = f"{dt.day} {MONTHS_HI[dt.month - 1]}"
        if with_year:
            base += f" {dt.year}"
        return f"{WEEKDAYS_HI[dt.weekday()]}, {base}" if with_weekday else base
    base = f"{MONTHS_EN[dt.month - 1]} {dt.day}"
    if with_year:
        base += f", {dt.year}"
    return f"{WEEKDAYS_EN[dt.weekday()]}, {base}" if with_weekday else base


def fmt_duration(seconds: float, lang: Optional[str] = None) -> str:
    """90 -> '1 minute 30 seconds' / '1 मिनट 30 सेकंड'; 3900 -> '1 hour 5 minutes' / '1 घंटा 5 मिनट'.
    Seconds are dropped once the duration is over 10 minutes."""
    lang = lang or _current
    total = max(0, int(round(seconds)))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if total > 600:
        secs = 0
    parts = []
    if lang == "hi":
        if days:
            parts.append(f"{days} दिन")
        if hours:
            parts.append(f"{hours} घंटा" if hours == 1 else f"{hours} घंटे")
        if minutes:
            parts.append(f"{minutes} मिनट")
        if secs or not parts:
            parts.append(f"{secs} सेकंड")
    else:
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if secs or not parts:
            parts.append(f"{secs} second{'s' if secs != 1 else ''}")
    return " ".join(parts)


def join_list(items, lang: Optional[str] = None) -> str:
    """['a', 'b', 'c'] -> 'a, b and c' / 'a, b और c'."""
    items = [str(i) for i in items if str(i).strip()]
    lang = lang or _current
    word = "और" if lang == "hi" else "and"
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + f" {word} " + items[-1]
