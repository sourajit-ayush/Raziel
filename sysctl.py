"""
sysctl.py - voice control of the PC itself, in English and Hindi. Everything is decided in code (no LLM).

    Volume       "volume up / down", "turn it up", "louder", "a bit quieter", "set the volume to 40 percent",
                 "max volume", "mute", "unmute", "what's the volume".      Hindi: "आवाज़ बढ़ाओ", "वॉल्यूम 40 करो", "म्यूट करो"
    Brightness   "brightness up / down", "dim the screen", "set the brightness to 60".   Hindi: "ब्राइटनेस कम करो"
    Battery      "what's my battery", "is my laptop charging".                        Hindi: "बैटरी कितनी है"
    Lock / sleep "lock the computer", "put the PC to sleep" (spoken FIRST, done ~2 s later, no question asked)
    Shutdown     "shut down the computer", "restart the PC" (asks yes/no first: kinds `shutdown_pc`, `restart_pc`;
                 then `shutdown /s /t 30`), "cancel the shutdown" (`shutdown /a`)
    Windows      "close chrome" (asks first: kind `close_app`), "close this window", "switch to spotify",
                 "next window", "show the desktop", "minimize everything", "minimize this window"

Deliberately NOT handled here (other matchers own them, or the LLM does): a bare "go to sleep" / "sleep" /
"goodbye" (Raziel herself stops listening: PC sleep needs the words computer / PC / laptop / system / machine),
"open X" (the open-app matcher), "close the tab" (a browser shortcut), "turn it off", "shut up", "restart the
song", "lock the door", "close the door", "pause / next song / play X", "what is the volume of a cylinder".

Public API
----------
  try_handle(transcript) -> Optional[str]         the finished sentence to speak (action already done), else None
  system_control(action, value, **_) -> str       the single LLM tool: volume_up, volume_down, volume_set, mute, unmute,
                                                  volume_get, brightness_up, brightness_down, brightness_set,
                                                  brightness_get, battery, lock, sleep, shutdown, restart,
                                                  cancel_shutdown, close_app, switch_window, show_desktop, minimize_all

Every OS-facing step is a small module-level function so tests can replace it (_get_volume, _set_volume,
_get_muted, _set_muted, _press_volume_key, _get_brightness, _set_brightness, _battery_info, _run_powershell,
_later, _run_shutdown, _suspend, _lock_now, _list_windows, _foreground, _focus, _close, _minimize, _hotkey).
Nothing Windows-specific runs on another OS: those functions report "unavailable" and the caller says so.

Settings read with getattr(config, NAME, default): SYSTEM_CONTROL_ENABLED (True), VOLUME_STEP (10),
BRIGHTNESS_STEP (10), SHUTDOWN_DELAY_SECONDS (30), SYSTEM_ACTION_DELAY_SECONDS (2.0, lock / sleep delay).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import threading
import time
import unicodedata
from typing import Callable, Dict, List, Optional, Tuple

import config
import confirmation
import lang
import winutil

logger = logging.getLogger("voice_assistant")


# ------------------------------------------------------------------ settings

def _cfg(name: str, default):
    return getattr(config, name, default)


def _cfg_int(name: str, default: int, lo: int = 0, hi: int = 10 ** 6) -> int:
    try:
        return max(lo, min(hi, int(float(_cfg(name, default)))))
    except (TypeError, ValueError):
        return default


def _is_windows() -> bool:
    return sys.platform == "win32"


BRIGHTNESS_FLOOR = 5          # never dim the panel to fully black by voice (some panels treat 0 as "off")

# module state (tests reset it)
_state = {"pycaw_missing": False, "pycaw_hint_given": False}


# ------------------------------------------------------------------ strings (English / Hindi)

_T: Dict[str, Dict[str, str]] = {
    # volume
    "vol_up": {"en": "Volume up, now at {p} percent.", "hi": "आवाज़ बढ़ा दी, अब {p} प्रतिशत है।"},
    "vol_down": {"en": "Volume down, now at {p} percent.", "hi": "आवाज़ कम कर दी, अब {p} प्रतिशत है।"},
    "vol_at_max": {"en": "The volume is already at maximum.", "hi": "आवाज़ पहले से ही पूरी है।"},
    "vol_at_min": {"en": "The volume is already at zero.", "hi": "आवाज़ पहले से ही शून्य है।"},
    "vol_set": {"en": "Volume set to {p} percent.", "hi": "आवाज़ {p} प्रतिशत कर दी।"},
    "vol_get": {"en": "The volume is at {p} percent.", "hi": "आवाज़ {p} प्रतिशत है।"},
    "vol_get_muted": {"en": "The volume is at {p} percent, but the sound is muted.",
                      "hi": "आवाज़ {p} प्रतिशत है, लेकिन साउंड म्यूट है।"},
    "muted": {"en": "Muted.", "hi": "म्यूट कर दिया।"},
    "already_muted": {"en": "The sound is already muted.", "hi": "साउंड पहले से म्यूट है।"},
    "unmuted": {"en": "Unmuted. The volume is at {p} percent.", "hi": "अनम्यूट कर दिया। आवाज़ {p} प्रतिशत है।"},
    "unmuted_plain": {"en": "Unmuted.", "hi": "अनम्यूट कर दिया।"},
    "unmuted_zero": {"en": "Unmuted, but the volume is at zero.", "hi": "अनम्यूट कर दिया, लेकिन आवाज़ शून्य है।"},
    "not_muted": {"en": "The sound isn't muted.", "hi": "साउंड म्यूट नहीं है।"},
    "key_up": {"en": "I turned the volume up, but I can't read the level.",
               "hi": "मैंने आवाज़ बढ़ा दी, लेकिन मैं वॉल्यूम पढ़ नहीं सकती।"},
    "key_down": {"en": "I turned the volume down, but I can't read the level.",
                 "hi": "मैंने आवाज़ कम कर दी, लेकिन मैं वॉल्यूम पढ़ नहीं सकती।"},
    "key_mute": {"en": "I pressed the mute key, but I can't tell whether that muted or unmuted the sound.",
                 "hi": "मैंने म्यूट बटन दबाया, लेकिन मैं देख नहीं सकती कि साउंड म्यूट हुआ या अनम्यूट।"},
    "key_set": {"en": "I set the volume to about {p} percent with the volume keys, but I can't read it back.",
                "hi": "मैंने वॉल्यूम कीज़ से आवाज़ करीब {p} प्रतिशत कर दी, लेकिन मैं उसे पढ़ नहीं सकती।"},
    "no_read_vol": {"en": "I can't read the volume on this computer.",
                    "hi": "मैं इस कंप्यूटर पर वॉल्यूम पढ़ नहीं सकती।"},
    "pycaw_hint": {"en": " If you install the pycaw package with pip, I can tell you the exact level.",
                   "hi": " अगर आप pip से pycaw पैकेज इंस्टॉल कर लें, तो मैं सही स्तर बता पाऊँगी।"},
    "vol_fail": {"en": "I couldn't change the volume on this computer.",
                 "hi": "मैं इस कंप्यूटर पर आवाज़ नहीं बदल पाई।"},
    "ask_vol": {"en": "What should I set the volume to, from 0 to 100?", "hi": "आवाज़ कितने प्रतिशत करूँ?"},
    # brightness
    "br_up": {"en": "Brightness up, now at {p} percent.", "hi": "ब्राइटनेस बढ़ा दी, अब {p} प्रतिशत है।"},
    "br_down": {"en": "Brightness down, now at {p} percent.", "hi": "ब्राइटनेस कम कर दी, अब {p} प्रतिशत है।"},
    "br_at_max": {"en": "The brightness is already at maximum.", "hi": "ब्राइटनेस पहले से ही पूरी है।"},
    "br_at_min": {"en": "The brightness is already at the lowest setting.",
                  "hi": "ब्राइटनेस पहले से ही सबसे कम है।"},
    "br_set": {"en": "Brightness set to {p} percent.", "hi": "ब्राइटनेस {p} प्रतिशत कर दी।"},
    "br_get": {"en": "The brightness is at {p} percent.", "hi": "ब्राइटनेस {p} प्रतिशत है।"},
    "br_na": {"en": "I can't control the brightness on this display. External monitors usually need their own buttons.",
              "hi": "मैं इस डिस्प्ले की ब्राइटनेस नहीं बदल सकती। बाहरी मॉनिटर में आमतौर पर अपने बटन होते हैं।"},
    "ask_br": {"en": "What should I set the brightness to, from 0 to 100?", "hi": "ब्राइटनेस कितने प्रतिशत करूँ?"},
    # battery
    "bat_desktop": {"en": "This looks like a desktop computer, so there's no battery.",
                    "hi": "यह डेस्कटॉप कंप्यूटर लगता है, इसलिए इसमें बैटरी नहीं है।"},
    "bat_unknown": {"en": "I couldn't read the battery.", "hi": "मैं बैटरी की जानकारी नहीं पढ़ पाई।"},
    "bat_charging": {"en": "Your battery is at {p} percent and charging.",
                     "hi": "आपकी बैटरी {p} प्रतिशत है और चार्ज हो रही है।"},
    "bat_plugged": {"en": "Your battery is at {p} percent and plugged in.",
                    "hi": "आपकी बैटरी {p} प्रतिशत है और चार्जर लगा है।"},
    "bat_full": {"en": "Your battery is full and plugged in.", "hi": "आपकी बैटरी फुल है और चार्जर लगा है।"},
    "bat_on": {"en": "Your battery is at {p} percent and not charging.",
               "hi": "आपकी बैटरी {p} प्रतिशत है और चार्ज नहीं हो रही।"},
    "bat_time": {"en": " About {t} left.", "hi": " करीब {t} चलेगी।"},
    "bat_low": {"en": " That's low, so you may want to plug in.", "hi": " यह कम है, चार्जर लगा लीजिए।"},
    # lock / sleep
    "locking": {"en": "Locking the computer.", "hi": "कंप्यूटर लॉक कर रही हूँ।"},
    "sleeping": {"en": "Putting the computer to sleep.", "hi": "कंप्यूटर को स्लीप मोड में डाल रही हूँ।"},
    # shutdown / restart
    "shutdown_q": {"en": "Shut down the computer? Unsaved work will be lost. Say yes to shut down, or no to cancel.",
                   "hi": "कंप्यूटर बंद कर दूँ? बिना सेव किया हुआ काम खो जाएगा। बंद करने के लिए हाँ बोलिए, रद्द करने के लिए ना।"},
    "restart_q": {"en": "Restart the computer? Unsaved work will be lost. Say yes to restart, or no to cancel.",
                  "hi": "कंप्यूटर रीस्टार्ट कर दूँ? बिना सेव किया हुआ काम खो जाएगा। रीस्टार्ट के लिए हाँ बोलिए, रद्द करने के लिए ना।"},
    "shutdown_summary": {"en": "shut down the computer", "hi": "कंप्यूटर बंद करना"},
    "restart_summary": {"en": "restart the computer", "hi": "कंप्यूटर रीस्टार्ट करना"},
    "shutdown_go": {"en": "Shutting down in {t}. Say cancel the shutdown to stop it.",
                    "hi": "{t} में कंप्यूटर बंद हो जाएगा। रोकने के लिए शटडाउन रद्द करो बोलिए।"},
    "restart_go": {"en": "Restarting in {t}. Say cancel the restart to stop it.",
                   "hi": "{t} में कंप्यूटर रीस्टार्ट हो जाएगा। रोकने के लिए रीस्टार्ट रद्द करो बोलिए।"},
    "shutdown_now": {"en": "Shutting down now.", "hi": "कंप्यूटर अभी बंद हो रहा है।"},
    "restart_now": {"en": "Restarting now.", "hi": "कंप्यूटर अभी रीस्टार्ट हो रहा है।"},
    "shutdown_fail": {"en": "I couldn't start the shutdown.", "hi": "मैं शटडाउन शुरू नहीं कर पाई।"},
    "restart_fail": {"en": "I couldn't start the restart.", "hi": "मैं रीस्टार्ट शुरू नहीं कर पाई।"},
    "cancelled": {"en": "Cancelled. The computer will stay on.", "hi": "रद्द कर दिया। कंप्यूटर चालू रहेगा।"},
    "cancel_none": {"en": "There's no shutdown in progress.", "hi": "अभी कोई शटडाउन चल नहीं रहा।"},
    "cancel_fail": {"en": "I couldn't cancel it.", "hi": "मैं उसे रद्द नहीं कर पाई।"},
    # windows
    "desktop": {"en": "Showing the desktop.", "hi": "डेस्कटॉप दिखा रही हूँ।"},
    "min_all": {"en": "Minimized all windows.", "hi": "सारी विंडो मिनिमाइज़ कर दीं।"},
    "min_this": {"en": "Minimized {name}.", "hi": "{name} मिनिमाइज़ कर दिया।"},
    "next_window": {"en": "Switched windows.", "hi": "विंडो बदल दी।"},
    "switching": {"en": "Switching to {name}.", "hi": "{name} पर जा रही हूँ।"},
    "already_front": {"en": "{name} is already in front.", "hi": "{name} पहले से सामने है।"},
    "not_open": {"en": "{name} isn't open right now. Say open {name} if you want me to start it.",
                 "hi": "{name} अभी खुला नहीं है। खोलना हो तो {name} खोलो बोलिए।"},
    "switch_fail": {"en": "I couldn't bring {name} to the front. Windows sometimes blocks that.",
                    "hi": "मैं {name} को सामने नहीं ला पाई। कभी-कभी विंडोज़ इसे रोक देता है।"},
    "no_front": {"en": "I don't see a window in front.", "hi": "मुझे सामने कोई विंडो नहीं दिख रही।"},
    "win_na": {"en": "I can't control windows on this computer.", "hi": "मैं इस कंप्यूटर पर विंडो नहीं संभाल सकती।"},
    "key_fail": {"en": "I couldn't send that key press.", "hi": "मैं वह कुंजी नहीं दबा पाई।"},
    "close_q_one": {"en": "Close {name}? Say yes or no.", "hi": "{name} बंद कर दूँ? हाँ या ना बोलिए।"},
    "close_q_many": {"en": "Close {name}? It has {n} windows open. Say yes or no.",
                     "hi": "{name} बंद कर दूँ? इसकी {n} विंडो खुली हैं। हाँ या ना बोलिए।"},
    "close_q_this": {"en": "Close this window, {name}? Say yes or no.",
                     "hi": "यह विंडो, {name}, बंद कर दूँ? हाँ या ना बोलिए।"},
    "close_summary": {"en": "close {name}", "hi": "{name} बंद करना"},
    "closed": {"en": "Closed {name}.", "hi": "{name} बंद कर दिया।"},
    "close_pending": {"en": "I asked {name} to close, but it's still open. It may be waiting for you to save your work.",
                      "hi": "मैंने {name} को बंद करने को कहा, लेकिन वह अभी खुला है। शायद वह आपसे सेव करने के लिए रुका है।"},
    "close_gone": {"en": "{name} looks like it's already closed.", "hi": "{name} पहले से बंद लगता है।"},
    "cant_close": {"en": "I can't close {name}.", "hi": "मैं {name} को बंद नहीं कर सकती।"},
    "no_window": {"en": "I can't see a window for {name}.", "hi": "मुझे {name} की कोई विंडो नहीं दिख रही।"},
    "ask_close": {"en": "Which app should I close?", "hi": "कौन सा ऐप बंद करूँ?"},
    "ask_switch": {"en": "Which app should I switch to?", "hi": "किस ऐप पर जाऊँ?"},
    "this_window": {"en": "this window", "hi": "यह विंडो"},
    # generic
    "sorry": {"en": "Sorry, something went wrong with that.", "hi": "माफ़ कीजिए, इसमें कुछ गड़बड़ हो गई।"},
    "disabled": {"en": "System control is turned off.", "hi": "सिस्टम कंट्रोल बंद है।"},
    "unknown": {"en": "I can change the volume and brightness, check the battery, lock or sleep the computer, "
                      "and close or switch windows.",
                "hi": "मैं आवाज़ और ब्राइटनेस बदल सकती हूँ, बैटरी बता सकती हूँ, कंप्यूटर लॉक या स्लीप कर सकती हूँ, "
                      "और विंडो बंद या बदल सकती हूँ।"},
}


def _tr(key: str, l: Optional[str] = None, **kw) -> str:
    return lang.tr(_T, key, lang=l, **kw)


# ------------------------------------------------------------------ text folding and normalising

_DROP = {0x0901, 0x0902, 0x093C, 0x200C, 0x200D}            # chandrabindu, anusvara, nukta, ZWNJ, ZWJ
_DEV_DIGITS = "०१२३४५६७८९"
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate(_DEV_DIGITS)}


def _strip_marks(s: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFC", s) if ord(ch) not in _DROP)


def _fold(text: str) -> str:
    """Lower case, Hindi spelling variants folded (हाँ = हा, बढ़ाओ = बढाओ), Devanagari digits -> ASCII."""
    t = _strip_marks(text or "").translate(_DIGIT_MAP).lower()
    return t.replace("।", " ").replace("॥", " ")


def _c(pattern: str) -> "re.Pattern":
    """A pattern written in natural Hindi spelling, folded the same way as the utterance."""
    return re.compile(_strip_marks(pattern))


def _L(*patterns: str) -> List["re.Pattern"]:
    return [_c(p) for p in patterns]


def _first(pats: List["re.Pattern"], t: str) -> Optional["re.Match"]:
    for p in pats:
        m = p.fullmatch(t)
        if m:
            return m
    return None


_LEAD = _c(
    r"^(?:(?:hey|hi|hello|yo|ok|okay|so|well|um+|uh+|alright|all right|please|kindly|raziel|razil|razeel|actually|"
    r"now|and|just|also|then|listen)\s+"
    r"|(?:can|could|would|will) you(?: please)?\s+|(?:i|we) (?:want|need|would like|'d like) you to\s+"
    r"|i'd like you to\s+|go ahead and\s+|you should\s+"
    r"|कृपया\s+|प्लीज़\s+|ज़रा\s+|जरा\s+|रज़ील\s+|रेज़ील\s+|अच्छा\s+|सुनो\s+|अरे\s+|क्या आप\s+|क्या तुम\s+|आप\s+|तुम\s+"
    r"|ठीक है\s+|मेरे लिए\s+)+"
)
_TRAIL = _c(
    r"(?:\s+(?:please|now|right now|for me|thanks|thank you|thanks a lot|okay|ok|raziel|"
    r"प्लीज़|कृपया|अभी|ज़रा|जरा|मेरे लिए|जी|यार|रज़ील))+$"
)


def _norm(transcript) -> Optional[str]:
    """Folded, punctuation-free, filler-free text ready for the fullmatch grammars; None if not usable."""
    if not isinstance(transcript, str):
        return None
    t = unicodedata.normalize("NFC", transcript)
    if not t.strip() or len(t) > 400:
        return None
    t = t.replace("’", "'").replace("‘", "'").replace("`", "'").replace("%", " percent ")
    t = _fold(t)
    t = re.sub(r"[^\w\sऀ-ॿ']+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    for _ in range(3):
        before = t
        t = _LEAD.sub("", t, count=1).strip()
        t = _TRAIL.sub("", t, count=1).strip()
        if t == before:
            break
    if not t or len(t.split()) > 16:
        return None
    return t


# ------------------------------------------------------------------ numbers and amounts

_EN_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_EN_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
            "eighty": 80, "ninety": 90}
_SPECIAL_NUMS = {"half": 50, "आधा": 50, "आधी": 50, "आधे": 50, "full": 100, "max": 100, "maximum": 100,
                 "पूरा": 100, "पूरी": 100, "फुल": 100, "मैक्स": 100, "मैक्सिमम": 100, "अधिकतम": 100}
_SPECIAL_NUMS = {_fold(k): v for k, v in _SPECIAL_NUMS.items()}


def _en_words_to_int(s: str) -> Optional[int]:
    total, seen = 0, False
    for w in s.replace("-", " ").split():
        if w in ("and", "a"):
            continue
        if w in _EN_UNITS:
            total += _EN_UNITS[w]
        elif w in _EN_TENS:
            total += _EN_TENS[w]
        elif w == "hundred":
            total = (total or 1) * 100
        else:
            return None
        seen = True
    return total if seen else None


def _parse_number(s) -> Optional[int]:
    """'40', '40.0', 'forty five', 'चालीस', 'half', 'full' -> 0..100+ (caller clamps); None if not a number."""
    if s is None:
        return None
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return int(round(s))
    s = _fold(str(s)).strip().strip(".,!?%").strip()
    s = re.sub(r"\s*(?:per ?cent|percent|प्रतिशत|परसेंट|पर्सेंट|फीसदी|%)$", "", s).strip()
    if not s:
        return None
    if re.fullmatch(r"-?\d{1,4}(?:\.\d+)?", s):
        return int(round(float(s)))
    if s in _SPECIAL_NUMS:
        return _SPECIAL_NUMS[s]
    if lang.has_devanagari(s):
        conv = lang.hindi_to_ascii_numbers(s).strip()
        if re.fullmatch(r"\d{1,4}", conv):
            return int(conv)
        return None
    return _en_words_to_int(s)


def _bound(pat: str) -> str:
    return rf"(?:(?<= )|^)(?:{pat})(?= |$)"


_AMT_BY = re.compile(_bound(r"by (\d{1,3})(?: percent| points?)?"))
_AMT_SMALL = re.compile(_bound(
    r"(?:just )?(?:a )?(?:little bit|little|tad|touch|notch|smidge|hair|wee bit|tiny bit)|slightly|somewhat|"
    + _strip_marks(r"थोड़ा(?: सा)?|थोड़ी(?: सी)?|थोड़े(?: से)?|ज़रा सा|जरा सा")))
_AMT_BIT = re.compile(_bound(r"(?:just )?(?:a )?bit"))            # "a bit louder" = the normal step
_AMT_BIG = re.compile(_bound(
    r"(?:a )?(?:whole )?lot|much|way|(?:a )?ton|(?:a )?bunch|significantly|considerably|drastically|hugely|"
    + _strip_marks(r"बहुत|काफ़ी|काफी")))
_AND_HI = re.compile(_bound(_strip_marks(r"और")))


def _take_amount(t: str) -> Tuple[Optional[int], str, bool]:
    """('turn it up a little') -> (5, 'turn it up', False). 'a lot' = 25, 'a little' = 5, 'by 20' = 20, 'a bit' = the
    normal step (amount None).
    The third value says a Hindi amount word or 'और' (more) was present."""
    amount: Optional[int] = None
    marker = False
    m = _AMT_BY.search(t)
    if m:
        amount = max(1, min(100, int(m.group(1))))
        t = (t[:m.start()] + " " + t[m.end():])
    else:
        for rx, val in ((_AMT_SMALL, 5), (_AMT_BIG, 25)):
            m = rx.search(t)
            if m:
                amount = val
                marker = marker or lang.has_devanagari(m.group(0))
                t = (t[:m.start()] + " " + t[m.end():])
                break
        else:
            m = _AMT_BIT.search(t)
            if m:
                t = (t[:m.start()] + " " + t[m.end():])
    m = _AND_HI.search(t)
    if m:
        marker = True
        t = (t[:m.start()] + " " + t[m.end():])
    return amount, re.sub(r"\s+", " ", t).strip(), marker


# ------------------------------------------------------------------ grammar pieces

# "do it" verbs: करो / कर दो / कीजिए ... and the Latin (Hinglish) spellings
_DO = (r"(?:करो|करें|करना|कर ?दो|कर ?दीजिए|कर ?दीजिये|कर ?दे|कर ?दें|कीजिए|कीजिये|कीजिएगा|कर|"
       r"karo|kar do|kardo|kar dijiye|kijiye|karna|kar de)")
_ART = r"(?:(?:the|my|our|this|your) )?"
_PC = (r"(?:(?:the|my|this|our) )?(?:(?:laptop|desktop|windows|whole|entire) )?"
       r"(?:computer|pc|laptop|system|machine|desktop|windows|workstation)")
_HPC = (r"(?:(?:मेरा|मेरी|मेरे|अपना|अपनी|इस|यह|mera|meri|apna) )?"
        r"(?:स्क्रीन|कंप्यूटर|पीसी|लैपटॉप|सिस्टम|मशीन|डेस्कटॉप|विंडोज|computer|pc|laptop|screen|system)(?: को| ko)?")
_HPC2 = (r"(?:(?:मेरा|मेरी|मेरे|अपना|अपनी|इस|यह|mera|meri|apna) )?"           # the machine, never "screen"
         r"(?:कंप्यूटर|पीसी|लैपटॉप|सिस्टम|मशीन|विंडोज|computer|pc|laptop|system)(?: को| ko)?")


# ------------------------------------------------------------------ lock / sleep / shutdown / restart / cancel

_P_LOCK = _L(
    rf"lock (?:down )?{_PC}",
    rf"lock (?:the |my |this |our )?(?:screen|display|monitor)",
    rf"lock {_PC} (?:screen|display)",
    rf"{_HPC} (?:लॉक|lock) {_DO}",
)
_P_SLEEP = _L(
    rf"(?:put|send|set|let|get|make) {_PC}(?: (?:to|into|in))? sleep(?: mode)?",
    rf"sleep {_PC}",
    rf"suspend {_PC}",
    rf"{_PC} (?:go to sleep|goes to sleep|go to sleep mode)",
    rf"{_HPC2} सो (?:जाओ|जा|जाइए|जाइये)",
    rf"{_HPC2} (?:सुला दो|सुला दे|सुलाओ|सुला दीजिए|सुला दीजिये|सुला दें|सुलाइए|"
    rf"स्लीप (?:मोड )?(?:में|पर) (?:डालो|डाल दो|डाल दीजिए|करो|कर दो)|स्लीप {_DO}|स्लीप मोड में {_DO}|सस्पेंड {_DO})",
)
_P_SHUTDOWN = _L(
    rf"(?:shut ?down|power off|power down|shut off|turn off|switch off|close down) {_PC}",
    rf"(?:turn|switch|shut|power) {_PC} (?:off|down)",
    rf"{_HPC2} (?:बंद|बन्द|शटडाउन|शट डाउन|ऑफ|band|shutdown|shut down|off) {_DO}",
    rf"(?:शटडाउन|शट डाउन) {_DO}",
)
_P_RESTART = _L(
    rf"(?:restart|reboot) {_PC}",
    r"reboot",
    rf"{_HPC2} (?:रीस्टार्ट|रिस्टार्ट|री स्टार्ट|रीबूट|रिबूट|री बूट|restart|reboot) {_DO}",
    rf"(?:रीबूट|रिबूट|रीस्टार्ट|रिस्टार्ट) {_DO}",
)
_HI_CANCEL_VERB = (r"(?:रद्द|कैंसिल|कैंसल|कैन्सल|केंसल|रोक|रोको|रोकिए|रुकवाओ|स्टॉप|abort|cancel)"
                   r"(?: {do}| दो| दीजिए| दे)?").replace("{do}", _DO)
_CANCEL_WORD = r"(?:cancel|abort|stop|undo|halt|call off|kill|कैंसिल|कैंसल|कैन्सल|केंसल|रद्द|रोको|रोक|स्टॉप|एबॉर्ट)"
_SHUT_WORD = (r"(?:shutdown|shut down|restart|reboot|shutting down|rebooting|restarting|शटडाउन|शट डाउन|रीस्टार्ट|"
              r"रिस्टार्ट|री स्टार्ट|रीबूट|रिबूट|री बूट)")
_P_CANCEL = _L(
    r"(?:cancel|abort|stop|undo|halt|call off|kill)(?: the| my| that| this| it)? "
    r"(?:shutdown|shut down|restart|reboot|shutting down|rebooting|restarting)",
    rf"(?:don't|do not|dont) (?:shut ?down|restart|reboot)(?: {_PC})?",
    rf"(?:शटडाउन|शट डाउन|रीस्टार्ट|रिस्टार्ट|रीबूट|रिबूट)(?: को)? {_HI_CANCEL_VERB}",
    r"(?:कंप्यूटर|पीसी|लैपटॉप|सिस्टम)(?: को)? (?:बंद|शटडाउन|रीस्टार्ट|रीबूट) मत (?:करो|करना)",
    rf"(?:cancel|abort) (?:shutdown|restart|reboot) (?:{_DO})",
    # word order / spelling variants a speech recogniser produces (also English spoken in a Hindi session:
    # "cancel the shutdown" is written "कैंसल द शटडाउन")
    rf"{_CANCEL_WORD}(?: the| द| my| that| this| it| इस| इसे| को| का)? {_SHUT_WORD}(?: {_DO}| प्लीज| please)?",
    rf"{_SHUT_WORD}(?: को| का)? {_CANCEL_WORD}(?: {_DO}| दो| दीजिए| दे| please| प्लीज)?",
)
# While a shutdown / restart she scheduled is still counting down, a bare "cancel" / "stop" / "रुको" cancels it.
_P_BARE_CANCEL = _L(
    r"(?:(?:oh |wait |no |hey |please |raziel )+)?(?:cancel|abort|stop|wait|hold on|hold it|undo|never ?mind|"
    r"कैंसिल|कैंसल|रद्द|रुको|रुकिए|रुक जाओ|रोको|स्टॉप|एबॉर्ट|रहने दो)(?: it| that| this| please| now| प्लीज| करो| दो)?",
)
_ARMED_UNTIL = 0.0                 # time.time() until which a shutdown she scheduled can still be cancelled


def _shutdown_armed() -> bool:
    return time.time() < _ARMED_UNTIL

# ------------------------------------------------------------------ battery (read-only, matched by vocabulary)

_BAT_START = {"what", "whats", "what's", "how", "hows", "how's", "is", "are", "am", "check", "tell", "show", "give",
              "read", "get", "do", "does", "will", "battery", "my", "the", "our", "your", "laptop", "computer", "pc",
              "charging", "batterys", "battery's"}
_BAT_VOCAB = {"what", "whats", "what's", "how", "hows", "how's", "is", "are", "am", "the", "my", "our", "your", "this",
              "laptop", "laptops", "laptop's", "computer", "computer's", "pc", "pc's", "machine", "system", "battery",
              "battery's", "level", "percent", "percentage", "status", "life", "charge", "charged", "charging",
              "plugged", "in", "on", "much", "many", "left", "remaining", "remain", "do", "does", "i", "we", "have",
              "got", "it", "its", "now", "right", "currently", "check", "tell", "me", "show", "give", "read", "out",
              "at", "power", "ac", "connected", "low", "full", "dead", "ok", "okay", "fine", "dying", "long", "will",
              "last", "time", "until", "dies", "die", "run", "runs", "doing", "looking", "like", "to", "get"}
_BAT_KEY = {"battery", "battery's", "charging", "plugged"}
_HI_BAT_VOCAB = {_fold(w) for w in (
    "बैटरी मेरी मेरे मेरा लैपटॉप कंप्यूटर पीसी की का कितनी कितने कितना बची बचा बाकी है हैं क्या कैसी कैसा लेवल स्तर "
    "स्टेटस प्रतिशत परसेंट पर्सेंट फीसदी चेक बताओ बताइए दिखाओ चार्ज चार्जिंग चार्जर हो रहा रही पर प्लग इन अभी इस तो "
    "मुझे जानकारी जरा ज़रा कब खत्म होगी").split()}
_HI_BAT_KEY = {_fold(w) for w in ("बैटरी", "चार्ज", "चार्जिंग", "चार्जर")}
_HG_BAT_VOCAB = {"battery", "kitni", "kitna", "kitne", "hai", "bachi", "bacha", "baaki", "baki", "kya", "mera", "meri",
                 "mere", "laptop", "computer", "pc", "charge", "charging", "ho", "raha", "rahi", "abhi", "level",
                 "percent", "batao", "bata", "do", "check", "karo", "status", "mujhe", "ka", "ki"}
_HG_BAT_ASK = {"kitni", "kitna", "kitne", "batao", "bachi", "bacha", "baaki", "baki"}


def _is_battery_query(t: str) -> bool:
    toks = t.split()
    if not toks or len(toks) > 10:
        return False
    if toks[0] == "how" and len(toks) > 1 and toks[1] in ("do", "does", "can", "to", "should"):
        return False                            # "how do I charge my battery" is a how-to, not a status question
    if not lang.has_devanagari(t) and any(w in _HG_BAT_ASK for w in toks):
        return all(w in _HG_BAT_VOCAB for w in toks) and "battery" in toks or (
            all(w in _HG_BAT_VOCAB for w in toks) and bool({"charge", "charging"} & set(toks)))
    if lang.has_devanagari(t):
        t2 = t.replace(_fold("चेक करो"), _fold("चेक")).replace(_fold("चेक कर दो"), _fold("चेक"))
        toks = t2.split()
        return all(w in _HI_BAT_VOCAB for w in toks) and any(w in _HI_BAT_KEY for w in toks)
    if toks[0] not in _BAT_START:
        return False
    if not all(w in _BAT_VOCAB for w in toks):
        return False
    if not any(w in _BAT_KEY for w in toks):
        return False
    return len(toks) >= 2 or toks[0] in ("battery", "batterys", "battery's")


# ------------------------------------------------------------------ volume

_VN = (rf"{_ART}(?:(?:system|computer|pc|laptop|master|speaker|main|overall) )?"
       r"(?:volume|sound|audio|music|speakers?|sound level|volume level)")
_VN2 = rf"{_ART}(?:(?:system|computer|pc|laptop|master) )?(?:volume|sound level|volume level)"   # no "music"/"sound"
_VOBJ = rf"(?:{_VN}|it|this|that)"
_HVN = (r"(?:(?:गाने की|गानों की|म्यूज़िक की|संगीत की|कंप्यूटर की|पीसी की|लैपटॉप की|स्पीकर की|सिस्टम की|साउंड की) )?"
        r"(?:आवाज़|अवाज़|वॉल्यूम|वोल्यूम|वाल्यूम|वॉल्युम|साउंड|स्पीकर्स|स्पीकर|awaaz|awaz|aawaz|volume|sound)(?: को| ko)?")
_HI_UP = (r"(?:बढ़ा(?:ओ|इए|इये|ना|एं|ए)?(?: (?:दो|दीजिए|दीजिये|दे|दें|देना))?|badhao|badha do|badhaao|badha de|"
          rf"(?:तेज़|ऊँचा|ऊंचा|ज़्यादा|ज्यादा|फुल|ऊपर|tez) {_DO})")
_HI_DOWN = (r"(?:घटा(?:ओ|इए|इये|ना|एं|ए)?(?: (?:दो|दीजिए|दीजिये|दे|दें|देना))?|ghatao|ghata do|"
            rf"(?:कम|धीरे|धीमा|धीमी|धीमे|नीचे|हल्का|हल्की|हल्के|kam|dheere|dheeray) {_DO})")
_MUTE_OBJ = (r"(?:the |my |our |this )?(?:volume|sound|audio|speakers?|computer|pc|laptop|system|music|everything|"
             r"it|this|that)")
_SND = r"(?:the |my )?(?:sound|volume|audio|speakers?)"
_HMUTE_OBJ = (r"(?:(?:कंप्यूटर|पीसी|लैपटॉप|साउंड|आवाज़|वॉल्यूम|स्पीकर|स्पीकर्स|सिस्टम|सब कुछ|computer|sound)"
              r"(?: को| ko)? )?")

_VQ_START = {"what", "whats", "what's", "how", "check", "tell", "show", "give", "read", "volume", "current", "the",
             "my", "audio", "sound", "get"}
_VQ_VOCAB = {"what", "whats", "what's", "how", "loud", "is", "it", "the", "current", "volume", "level", "at", "right",
             "now", "check", "tell", "me", "my", "sound", "on", "currently", "set", "to", "speaker", "speakers",
             "system", "computer", "pc", "laptop", "percent", "percentage", "this", "audio", "are", "status",
             "get", "show", "give", "read", "out"}
_VQ_KEY = {"volume", "loud"}
_HI_VQ_VOCAB = {_fold(w) for w in (
    "आवाज़ वॉल्यूम साउंड कितनी कितना कितने है हैं क्या का की लेवल स्तर अभी प्रतिशत परसेंट चेक बताओ बताइए मुझे मेरी इस यह "
    "पर तेज़ चल रही रहा").split()}
_HI_VQ_KEY = {_fold(w) for w in ("आवाज़", "वॉल्यूम", "साउंड")}
_HI_VQ_ASK = {_fold(w) for w in ("कितनी", "कितना", "कितने", "क्या", "लेवल", "स्तर", "बताओ", "बताइए", "चेक", "प्रतिशत")}


def _is_definition_question(toks: List[str]) -> bool:
    """'what is volume', 'what is brightness': a knowledge question, not 'what is THE volume (of my PC)'."""
    return (len(toks) <= 3 and toks[0] in ("what", "whats", "what's") and toks[-1] in ("volume", "brightness", "sound")
            and not {"the", "my", "current", "it", "now"} & set(toks))


def _is_volume_query(t: str) -> bool:
    toks = t.split()
    if not toks or len(toks) > 9:
        return False
    if lang.has_devanagari(t):
        t2 = t.replace(_fold("चेक करो"), _fold("चेक"))
        toks = t2.split()
        return (all(w in _HI_VQ_VOCAB for w in toks) and any(w in _HI_VQ_KEY for w in toks)
                and any(w in _HI_VQ_ASK for w in toks))
    if _is_definition_question(toks):
        return False
    return (toks[0] in _VQ_START and all(w in _VQ_VOCAB for w in toks) and any(w in _VQ_KEY for w in toks))


_NUM_EN = r"(?P<num>\d{1,4}|[a-z]+(?: [a-z]+){0,2})(?: percent)?"
_VN3 = rf"{_ART}(?:(?:system|computer|pc|laptop|master|speaker|main|overall) )?(?:volume|sound|audio)"   # + a number
_VPRO = r"(?:it|this|that)"                       # a bare pronoun only ever goes with "turn / crank ..." (see below)
_V_SET_EN = _L(
    rf"(?:set|change|put|make|turn|bring|adjust|get|move|take|push|crank|lower|raise|drop|decrease|increase) "
    rf"{_VN}(?: (?:up|down))? (?:to|at|on) {_NUM_EN}",
    rf"(?:turn|crank) {_VPRO} (?:up|down) (?:to|at) {_NUM_EN}",
    rf"(?:set|change|adjust) {_VN} {_NUM_EN}",
    rf"{_VN3}(?: level)?(?: (?:to|at|is|on))? {_NUM_EN}",
    r"(?P<num>\d{1,3}) ?(?:percent )?(?:volume|sound)",
)
_V_MAX_EN = _L(
    r"(?:max|maximum|full|highest|loudest) (?:volume|sound|audio)",
    rf"(?:set |put |turn |bring |crank |change |get )?(?:the |my )?(?:volume|sound|audio)(?: up)? "
    rf"(?:to |at |on )?(?:max|maximum|full|the max|the maximum|its max|its maximum)",
    rf"(?:turn|crank|bring|put|pump|push|set|get|take|make) {_VN}(?: up)? "
    rf"(?:all the way up|to the max|to max|to maximum|to full|to the maximum|as high as it (?:goes|will go|can go))",
    rf"(?:turn|crank|pump) {_VPRO}(?: up)? (?:all the way up|all the way|to the max|to max|to maximum|to full)",
    rf"{_VN} all the way up",
)
_V_MIN_EN = _L(r"(?:min|minimum|lowest|quietest) (?:volume|sound|audio)",
               rf"(?:set |put |turn |bring )?(?:the |my )?(?:volume|sound|audio)(?: down)? (?:to |at )?(?:min|minimum|the minimum|its minimum|the lowest)")
_V_SET_HI = _L(
    rf"{_HVN}(?: (?:में|पर))?(?: सेट)? (?P<num>\S+(?: \S+){{0,2}}?)(?: (?:प्रतिशत|परसेंट|पर्सेंट|फीसदी|percent))?"
    rf"(?: (?:पर|तक))?(?: (?:सेट|फिक्स))?(?: {_DO})?",
    rf"{_HVN} सेट {_DO} (?P<num>\S+(?: \S+){{0,2}}?)(?: (?:प्रतिशत|परसेंट|पर्सेंट|percent))?",
)
_V_UP_EN = _L(
    rf"(?:turn|crank|pump|bump|kick|push|bring|raise|boost|lift|step|ramp|amp|jack) up {_VN}",
    rf"(?:turn|crank|pump|bump|kick|push|bring|put|raise|lift|boost|get|make|take|ramp|have) {_VN} "
    rf"(?:up|higher|louder)",
    rf"(?:turn|crank|pump|bump|kick|boost|ramp) {_VPRO} up",
    rf"(?:turn|crank|pump|bump|boost|make|get|have) {_VPRO} louder",
    rf"(?:increase|raise|boost|lift|amplify|louden) {_VN}",
    rf"(?:up|more|extra) {_VN2}",
    rf"{_VN} (?:up|higher|louder|increase|increases|raise|rise|goes up|go up)",
    r"(?:even |still )?louder",
    r"(?:that's|that is|it's|it is|this is)? ?too (?:quiet|soft)(?: to hear)?",
)
_V_DOWN_EN = _L(
    rf"(?:turn|bring|put|push|take|drop|cut|lower|bump|pull|knock|dial|step|ramp) down {_VN}",
    rf"(?:turn|bring|put|push|take|drop|cut|lower|bump|pull|knock|dial|make|get|have) {_VN} "
    rf"(?:down|lower|quieter|softer|quiet)",
    rf"(?:turn|bump|knock|dial) {_VPRO} down",
    rf"(?:turn|make|get|have) {_VPRO} (?:quieter|softer|quiet)",
    rf"(?:decrease|reduce|lower|quieten|soften) {_VN}",
    rf"(?:down|less|lower|cut) {_VN2}",
    rf"{_VN} (?:down|lower|quieter|softer|decrease|reduce|drop|goes down|go down)",
    r"(?:even |still )?(?:quieter|softer)",
    r"(?:that's|that is|it's|it is|this is)? ?too (?:loud|noisy)",
)
_V_UP_HI = _L(
    rf"{_HVN} {_HI_UP}",
    rf"(?:{_HI_UP}) {_HVN}",
)
_V_DOWN_HI = _L(
    rf"{_HVN} {_HI_DOWN}",
    rf"(?:{_HI_DOWN}) {_HVN}",
    rf"(?:धीरे|धीमे|dheere) {_DO}",
)
_V_UP_HI_BARE = _L(rf"(?:तेज़|ऊँचा|ऊंचा|tez) {_DO}")
_V_MUTE = _L(
    rf"mute(?: {_MUTE_OBJ})?",
    rf"(?:turn|switch|shut|kill) off {_SND}",
    rf"(?:turn|switch|shut) {_SND} off",
    r"(?:sound|audio|volume) off",
    rf"kill {_SND}",
    rf"(?:put|set|switch|turn) {_MUTE_OBJ} (?:on|to) mute",
    rf"{_HMUTE_OBJ}(?:म्यूट|mute)(?: {_DO})?",
    # "आवाज़ बंद करो" = turn the sound off (NOT "close an app called awaaz")
    rf"(?:साउंड|आवाज़|वॉल्यूम|स्पीकर|स्पीकर्स)(?: को)?(?: (?:पूरा|पूरी|बिल्कुल|एकदम|पूरी तरह से))? (?:बंद|बन्द|ऑफ)(?: {_DO})?",
)
_V_UNMUTE = _L(
    rf"un ?mute(?: {_MUTE_OBJ})?",
    rf"(?:turn|switch) on {_SND}",
    rf"(?:turn|switch) {_SND} (?:back )?on",
    r"(?:sound|audio|volume) on",
    rf"(?:bring|get|turn) {_SND} back(?: on)?",
    rf"(?:restore|enable) {_SND}",
    r"(?:take|remove|get) (?:it |the sound |the computer )?off (?:of )?mute",
    r"(?:turn|switch) off (?:the )?mute",
    r"mute off",
    rf"{_HMUTE_OBJ}(?:अनम्यूट|अन म्यूट|unmute|un mute)(?: {_DO})?",
    rf"म्यूट (?:हटाओ|हटा दो|हटाइए|खोलो|खोल दो|बंद करो|बंद कर दो)",
    rf"(?:साउंड|आवाज़|वॉल्यूम|स्पीकर|स्पीकर्स)(?: को)? (?:चालू|ऑन|वापस चालू|फिर से चालू) {_DO}",
)
_V_TOGGLE = _L(r"(?:toggle|switch|flip) (?:the )?mute", r"mute toggle", r"toggle (?:the )?(?:sound|volume|audio)")


def _p_volume(t: str) -> Optional[Tuple[str, str]]:
    if _is_volume_query(t):
        return "volume_get", ""
    if _first(_V_UNMUTE, t):
        return "unmute", ""
    if _first(_V_TOGGLE, t):
        return "toggle_mute", ""
    if _first(_V_MUTE, t):
        return "mute", ""
    if _first(_V_MAX_EN, t):
        return "volume_set", "100"
    if _first(_V_MIN_EN, t):
        return "volume_set", "0"
    for pats in (_V_SET_EN, _V_SET_HI):
        for p in pats:
            m = p.fullmatch(t)
            if m:
                n = _parse_number(m.group("num"))
                if n is not None:
                    return "volume_set", str(n)
    amount, rest, marker = _take_amount(t)
    val = str(amount) if amount else ""
    if _first(_V_UP_EN, rest) or _first(_V_UP_HI, rest) or (marker and _first(_V_UP_HI_BARE, rest)):
        return "volume_up", val
    if _first(_V_DOWN_EN, rest) or _first(_V_DOWN_HI, rest):
        return "volume_down", val
    if rest != t:
        # the amount phrase was part of the sentence's meaning ("too loud"): try once more unchanged
        if _first(_V_UP_EN, t) or _first(_V_UP_HI, t):
            return "volume_up", ""
        if _first(_V_DOWN_EN, t) or _first(_V_DOWN_HI, t):
            return "volume_down", ""
    return None


# ------------------------------------------------------------------ brightness

_BN = rf"{_ART}(?:(?:screen|display|monitor|laptop) )?(?:brightness|backlight)(?: level)?"
_SCR = r"(?:the |my |our |this )?(?:screen|display|monitor)"
_HBN = (r"(?:(?:स्क्रीन|डिस्प्ले|मॉनिटर)(?: की| का)? (?:ब्राइटनेस|रोशनी|चमक|लाइट)|ब्राइटनेस|brightness|स्क्रीन की चमक)"
        r"(?: को| ko)?")
_BQ_START = {"what", "whats", "what's", "how", "check", "tell", "show", "give", "read", "brightness", "current", "the",
             "my", "screen", "get", "is"}
_BQ_VOCAB = {"what", "whats", "what's", "how", "bright", "is", "it", "the", "current", "brightness", "level", "at",
             "right", "now", "check", "tell", "me", "my", "on", "currently", "set", "to", "screen", "display",
             "monitor", "laptop", "percent", "percentage", "this", "get", "show", "give", "read", "out"}
_BQ_KEY = {"brightness", "bright"}
_HI_BQ_VOCAB = {_fold(w) for w in (
    "ब्राइटनेस स्क्रीन की का रोशनी चमक कितनी कितना कितने है हैं क्या लेवल स्तर अभी प्रतिशत परसेंट चेक बताओ बताइए मुझे मेरी "
    "इस यह पर").split()}
_HI_BQ_KEY = {_fold(w) for w in ("ब्राइटनेस", "रोशनी", "चमक")}
_HI_BQ_ASK = _HI_VQ_ASK


def _is_brightness_query(t: str) -> bool:
    toks = t.split()
    if not toks or len(toks) > 9:
        return False
    if lang.has_devanagari(t):
        t2 = t.replace(_fold("चेक करो"), _fold("चेक"))
        toks = t2.split()
        return (all(w in _HI_BQ_VOCAB for w in toks) and any(w in _HI_BQ_KEY for w in toks)
                and any(w in _HI_BQ_ASK for w in toks))
    return (toks[0] in _BQ_START and all(w in _BQ_VOCAB for w in toks) and any(w in _BQ_KEY for w in toks)
            and not _is_definition_question(toks)
            and not (toks[0] == "is" and "bright" in toks and "brightness" not in toks))


_B_SET_EN = _L(
    rf"(?:set|change|put|make|turn|bring|adjust|get|move|take|push|crank|lower|raise|drop|decrease|increase) "
    rf"{_BN}(?: (?:up|down))? (?:to|at|on) {_NUM_EN}",
    rf"(?:set|change|adjust) {_BN} {_NUM_EN}",
    rf"{_BN}(?: (?:to|at|is|on))? {_NUM_EN}",
    r"(?P<num>\d{1,3}) ?(?:percent )?(?:brightness)",
)
_B_MAX_EN = _L(
    r"(?:max|maximum|full|highest|brightest) brightness",
    rf"(?:set |put |turn |bring |crank |change |get )?{_BN}(?: up)? (?:to |at |on )?"
    r"(?:max|maximum|full|the max|the maximum|its max|its maximum)",
    rf"(?:turn|crank|bring|put|push|set|get|take|make) {_BN}(?: up)? (?:all the way up|to the max|to max|to maximum|to full)",
)
_B_MIN_EN = _L(r"(?:min|minimum|lowest|dimmest) brightness",
               rf"(?:set |put |turn |bring )?{_BN}(?: down)? (?:to |at )?(?:min|minimum|the minimum|its minimum|the lowest)")
_B_SET_HI = _L(
    rf"{_HBN}(?: (?:में|पर))?(?: सेट)? (?P<num>\S+(?: \S+){{0,2}}?)(?: (?:प्रतिशत|परसेंट|पर्सेंट|फीसदी|percent))?"
    rf"(?: (?:पर|तक))?(?: (?:सेट|फिक्स))?(?: {_DO})?",
    rf"{_HBN} सेट {_DO} (?P<num>\S+(?: \S+){{0,2}}?)(?: (?:प्रतिशत|परसेंट|पर्सेंट|percent))?",
)
_B_UP_EN = _L(
    rf"(?:turn|crank|bring|push|bump|raise|increase|boost|lift|put) up {_BN}",
    rf"(?:turn|bring|push|bump|put|get|make|take|raise) {_BN} (?:up|higher|brighter)",
    rf"(?:increase|raise|boost|lift|up|brighten) {_BN}",
    rf"{_BN} (?:up|higher|increase|brighter|raise)",
    rf"(?:brighten|light up) (?:{_SCR}|it)",
    rf"(?:make|get|turn) (?:{_SCR}|it) brighter",
    r"(?:even |still )?brighter",
    rf"(?:more|extra) {_BN}",
    rf"{_SCR}(?:'s| is)? too (?:dark|dim)",
)
_B_DOWN_EN = _L(
    rf"(?:turn|bring|push|take|drop|cut|lower|bump|pull|knock|dial) down {_BN}",
    rf"(?:turn|bring|push|take|drop|cut|lower|bump|pull|knock|dial|make|get|put) {_BN} (?:down|lower|dimmer|darker)",
    rf"(?:decrease|reduce|lower|dim) {_BN}",
    rf"{_BN} (?:down|lower|decrease|dimmer|darker|drop|reduce)",
    rf"dim {_SCR}",
    rf"(?:make|get|turn) (?:{_SCR}|it) (?:dimmer|darker)",
    r"(?:even |still )?(?:dimmer|darker)",
    rf"(?:less|lower) {_BN}",
    rf"{_SCR}(?:'s| is)? too bright",
)
_B_UP_HI = _L(rf"{_HBN} {_HI_UP}", rf"(?:{_HI_UP}) {_HBN}")
_B_DOWN_HI = _L(rf"{_HBN} {_HI_DOWN}", rf"(?:{_HI_DOWN}) {_HBN}")


def _p_brightness(t: str) -> Optional[Tuple[str, str]]:
    if _is_brightness_query(t):
        return "brightness_get", ""
    if _first(_B_MAX_EN, t):
        return "brightness_set", "100"
    if _first(_B_MIN_EN, t):
        return "brightness_set", str(BRIGHTNESS_FLOOR)
    for pats in (_B_SET_EN, _B_SET_HI):
        for p in pats:
            m = p.fullmatch(t)
            if m:
                n = _parse_number(m.group("num"))
                if n is not None:
                    return "brightness_set", str(n)
    amount, rest, _marker = _take_amount(t)
    val = str(amount) if amount else ""
    if _first(_B_UP_EN, rest) or _first(_B_UP_HI, rest):
        return "brightness_up", val
    if _first(_B_DOWN_EN, rest) or _first(_B_DOWN_HI, rest):
        return "brightness_down", val
    if rest != t:
        if _first(_B_UP_EN, t):
            return "brightness_up", ""
        if _first(_B_DOWN_EN, t):
            return "brightness_down", ""
    return None


# ------------------------------------------------------------------ windows: grammar

_WIN_NOUN = r"(?:window|app|application|program)"
_HWIN = r"(?:विंडो|विंडोज|ऐप|एप|एप्लिकेशन|एप्लीकेशन|प्रोग्राम)"
_P_SHOW_DESKTOP = _L(
    r"(?:show|display|reveal|go to|take me to|switch to|bring up|get to|back to)(?: me)?(?: (?:the|my))? desktop",
    rf"डेस्कटॉप (?:दिखाओ|दिखा दो|दिखाइए|दिखा दीजिए|पर जाओ|पर चलो|पर ले चलो)",
    r"desktop dikhao",
)
_P_MIN_ALL = _L(
    r"(?:minimi[sz]e|hide) (?:everything|all(?: of)?(?: (?:the|my|these))?(?: (?:open|running))? ?"
    r"(?:windows|apps|applications|programs)?|all of them|them all|every window|the windows|windows)",
    r"(?:सब कुछ|सभी|सारी|सारे|सब)(?: (?:खुली|खुले))?(?: (?:विंडो|विंडोज|ऐप्स|प्रोग्राम्स|ऐप|प्रोग्राम))?(?: को)? "
    rf"(?:(?:मिनिमाइज|मिनीमाइज|मिनिमाईज|हाइड) (?:{_DO})?|छिपाओ|छिपा दो|छोटी {_DO}|छोटा {_DO})",
)
_P_MIN_THIS = _L(
    r"(?:minimi[sz]e|shrink)(?: (?:this|the|the current|current|the active|active|my current))?(?: " + _WIN_NOUN + r")?",
    rf"(?:(?:इस|यह|ये|मौजूदा) )?(?:विंडो|ऐप)(?: को)? (?:मिनिमाइज|मिनीमाइज|छोटी|छोटा) {_DO}",
    r"(?:(?:इस|यह|ये) )?विंडो(?: को)? (?:छिपाओ|छिपा दो)",
)
_P_NEXT_WINDOW = _L(
    r"alt ?tab(?: key)?",
    r"(?:press|hit) alt ?tab",
    rf"switch(?: the)?(?: between)? (?:windows?|apps?|programs?|applications?)",
    r"(?:next|previous|last|other|another|different|prior) windows?",
    rf"(?:switch|go|change|move|flip|jump|cycle)(?: over)? (?:to |back to )?(?:the |my )?"
    rf"(?:next|previous|last|other|another|different|prior) {_WIN_NOUN}",
    r"(?:change|swap) windows?",
    r"(?:अगली|अगला|पिछली|पिछला|दूसरी|दूसरा|दूसरे) (?:विंडो|ऐप|एप)(?: पर)?(?: (?:जाओ|चलो|स्विच करो|दिखाओ|बदलो))?",
    rf"(?:विंडो|ऐप) (?:बदलो|बदल दो|बदलें|स्विच करो|स्विच कर दो)",
    r"(?:ऑल्ट|अल्ट|आल्ट) ?(?:टैब|टेब)",
)
_TAILW = rf"(?: {_WIN_NOUN})?"
_P_SWITCH = _L(
    rf"(?:switch|go|change|jump|flip|move|navigate)(?: back| over)? (?:to|onto) (?:the |my )?(?P<name>.+?){_TAILW}",
    rf"(?:bring up|pull up|bring forward|bring to the front|bring to front|focus on|focus) (?:the |my )?(?P<name>.+?){_TAILW}",
    rf"bring (?:the |my )?(?P<name>.+?){_TAILW} (?:to the front|to front|forward|up)",
    rf"(?:show me|show|display)(?: the| my)? (?P<name>.+?) {_WIN_NOUN}",
    rf"(?:back|return) to (?:the |my )?(?P<name>.+?){_TAILW}",
    rf"(?P<name>.+?)(?: की| का)?(?: {_HWIN})?(?: पर| में| को) (?:जाओ|जाइए|चलो|स्विच करो|स्विच कर दो|स्विच करें|स्विच कीजिए|आओ|वापस जाओ)",
    rf"(?P<name>.+?)(?: की| का)? {_HWIN} (?:दिखाओ|दिखा दो|सामने लाओ|सामने करो|ऊपर लाओ)",
    rf"(?P<name>.+?)(?: को)? (?:सामने|आगे|ऊपर) (?:लाओ|करो|कर दो|ले आओ)",
    rf"(?P<name>.+?)(?: पर)? स्विच {_DO}",
    rf"(?P<name>.+?) (?:pe|par) (?:jao|chalo|switch karo)",
)
_P_CLOSE = _L(
    r"(?:close down|close|quit|exit) (?P<what>.+)",
    rf"(?P<what>.+?)(?: को| ko)? (?:बंद|बन्द|क्लोज|band|close) {_DO}",
)
_FG_RE = _c(
    r"(?:this|the current|current|the active|active|the front|the top|the foreground|my current) "
    rf"{_WIN_NOUN}|(?:the )?{_WIN_NOUN} (?:in front|on top)"
    rf"|(?:इस|यह|ये|मौजूदा|वर्तमान|सामने वाली|सामने की) {_HWIN}"
)
_LEAD_DET = re.compile(r"^(?:(?:the|my|our|that|an?|यह|ये|वो|मेरा|मेरी|मेरे|अपना|अपनी) )+")
_TAIL_WORD = re.compile(_strip_marks(
    r"(?: (?:app|application|program|software|window|windows|ऐप|एप|एप्लिकेशन|एप्लीकेशन|प्रोग्राम|सॉफ्टवेयर|विंडो|विंडोज))+$"))
_NAME_STOP = {
    "tab", "tabs", "new tab", "the tab", "browser tab", "all", "everything", "every", "other", "others", "both", "some",
    "any", "each", "another", "more", "up", "down", "out", "off", "it", "them", "that", "those", "these", "this",
    "window", "windows", "door", "doors", "eyes", "eye", "mouth", "gap", "deal", "case", "loop", "call", "sleep", "bed",
    "sleep mode", "app", "application", "program", "here", "there", "now", "again", "back", "in", "on", "to", "and", "or", "but", "the", "a", "an",
}
_NEVER_TARGETS = {"sleep", "bed", "sleep mode"}


# ------------------------------------------------------------------ windows: what is a known app, what is protected

_BROWSER_STEMS = ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "iexplore", "arc", "chromium")
_HOST_STEMS = ("applicationframehost", "")      # UWP frame windows and windows whose process can't be read

_APPS: Dict[str, dict] = {}


def _reg(names, stems, pretty, titles=None, weak=False):
    for n in names:
        _APPS[n] = {"stems": tuple(stems), "pretty": pretty, "titles": tuple(titles or ()), "weak": weak}


_reg(("chrome", "google chrome", "chrome browser"), ("chrome",), "Chrome")
_reg(("edge", "microsoft edge", "ms edge"), ("msedge",), "Edge")
_reg(("firefox", "mozilla firefox", "fire fox"), ("firefox",), "Firefox")
_reg(("brave", "brave browser"), ("brave",), "Brave")
_reg(("opera",), ("opera",), "Opera")
_reg(("browser", "web browser", "internet browser"), _BROWSER_STEMS, "the browser")
_reg(("notepad", "note pad"), ("notepad",), "Notepad", titles=("notepad",))
_reg(("calculator", "calc"), ("calculatorapp", "calculator", "calc"), "Calculator", titles=("calculator",))
_reg(("paint", "ms paint", "microsoft paint"), ("mspaint", "paint"), "Paint", titles=("paint",))
_reg(("word", "microsoft word", "ms word"), ("winword",), "Word")
_reg(("excel", "microsoft excel", "ms excel"), ("excel",), "Excel")
_reg(("powerpoint", "power point", "microsoft powerpoint"), ("powerpnt",), "PowerPoint")
_reg(("outlook", "microsoft outlook"), ("outlook", "olk"), "Outlook")
_reg(("onenote", "one note"), ("onenote", "onenoteim"), "OneNote")
_reg(("teams", "microsoft teams", "ms teams"), ("teams", "ms-teams"), "Teams")
_reg(("zoom",), ("zoom",), "Zoom")
_reg(("skype",), ("skype", "skypeapp"), "Skype")
_reg(("slack",), ("slack",), "Slack")
_reg(("discord",), ("discord",), "Discord")
_reg(("telegram",), ("telegram",), "Telegram")
_reg(("whatsapp", "whats app"), ("whatsapp", "whatsapp.root", "whatsappdesktop"), "WhatsApp", titles=("whatsapp",))
_reg(("spotify",), ("spotify",), "Spotify")
_reg(("vlc", "vlc player", "vlc media player"), ("vlc",), "VLC")
_reg(("steam",), ("steam",), "Steam")
_reg(("vs code", "vscode", "visual studio code", "v s code"), ("code",), "VS Code")
_reg(("visual studio",), ("devenv",), "Visual Studio")
_reg(("pycharm", "py charm"), ("pycharm64", "pycharm"), "PyCharm")
_reg(("sublime", "sublime text"), ("sublime_text",), "Sublime Text")
_reg(("obs", "obs studio"), ("obs64", "obs32", "obs"), "OBS")
_reg(("task manager",), ("taskmgr",), "Task Manager", titles=("task manager",))
_reg(("wordpad", "word pad"), ("wordpad",), "WordPad")
_reg(("snipping tool",), ("snippingtool", "screenclippinghost"), "Snipping Tool", titles=("snipping tool",))
_reg(("acrobat", "adobe reader", "adobe acrobat", "pdf reader"), ("acrord32", "acrobat", "acrord64"), "Acrobat")
_reg(("photoshop", "adobe photoshop"), ("photoshop",), "Photoshop")
# protected apps are known so that asking to close them gets a clear "I can't"
_reg(("explorer", "file explorer", "windows explorer", "files"), ("explorer",), "File Explorer")
_reg(("terminal", "windows terminal", "command prompt", "cmd", "powershell", "console"),
     ("windowsterminal", "wt", "cmd", "powershell", "pwsh", "conhost", "openconsole"), "the terminal")
_reg(("python",), ("python", "pythonw", "py"), "Python")
# weak names only count when a matching window is really open
_reg(("settings",), ("systemsettings",), "Settings", titles=("settings",), weak=True)
_reg(("photos",), ("microsoft.photos",), "Photos", titles=("photos",), weak=True)
_reg(("camera",), ("windowscamera",), "Camera", titles=("camera",), weak=True)
_reg(("calendar",), (), "Calendar", titles=("calendar",), weak=True)
_reg(("mail",), ("hxoutlook", "olk"), "Mail", titles=("mail",), weak=True)
_reg(("store", "microsoft store"), ("winstore.app",), "Microsoft Store", titles=("microsoft store",), weak=True)
_reg(("maps",), (), "Maps", titles=("maps",), weak=True)
_reg(("clock", "alarms"), (), "Clock", titles=("clock",), weak=True)

_PRETTY_EXE: Dict[str, str] = {}
for _a in _APPS.values():                         # the first (most specific) name registered for an exe wins
    if _a["stems"]:
        _PRETTY_EXE.setdefault(_a["stems"][0], _a["pretty"])
_PRETTY_EXE.update({"msedge": "Edge", "winword": "Word", "powerpnt": "PowerPoint", "code": "VS Code",
                    "calculatorapp": "Calculator", "mspaint": "Paint", "taskmgr": "Task Manager"})

# Never closed by voice: the desktop shell and the Windows shell hosts, Raziel herself, and the terminals she may
# be running in (closing those would end her process).
_NEVER_STEMS = {"explorer", "shellexperiencehost", "startmenuexperiencehost", "searchhost", "searchapp", "searchui",
                "textinputhost", "lockapp", "dwm", "winlogon", "csrss", "sihost", "ctfmon", "widgets", "taskhostw",
                "python", "pythonw", "py"}
_TERMINAL_STEMS = {"windowsterminal", "wt", "cmd", "powershell", "pwsh", "conhost", "openconsole"}

_HI_EXTRA = {_fold(k): v for k, v in {
    "ब्राउज़र": "browser", "ब्राउजर": "browser", "ब्रेव": "brave", "ओपेरा": "opera", "आउटलुक": "outlook",
    "स्लैक": "slack", "ओबीएस": "obs", "टीम": "teams", "पावर पॉइंट": "powerpoint", "विज़ुअल स्टूडियो कोड": "visual studio code",
    "वीएस कोड": "vs code", "टास्क मैनेजर": "task manager", "फाइल एक्सप्लोरर": "file explorer",
}.items()}


def _latin_name(name: str) -> str:
    """The spoken app name as lower-case Latin letters ('क्रोम' -> 'chrome')."""
    n = _fold(name).strip()
    if lang.has_devanagari(n):
        for k, v in sorted(_HI_EXTRA.items(), key=lambda kv: -len(kv[0])):
            n = n.replace(k, v)
        n = lang.hint_latin(n)
    n = re.sub(r"[^a-z0-9 .+_-]", " ", n.lower())
    n = re.sub(r"\.exe$", "", re.sub(r"\s+", " ", n).strip())
    return n


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _stem(exe: str) -> str:
    e = (exe or "").lower()
    return e[:-4] if e.endswith(".exe") else e


def _is_protected(w: dict) -> bool:
    """A window that is never closed by voice."""
    exe = _stem(w.get("exe", ""))
    title = (w.get("title") or "").lower()
    if exe in _NEVER_STEMS or exe in _TERMINAL_STEMS:
        return True
    if "raziel" in title or title == "program manager":
        return True
    return False


def _match_windows(q: str, windows: List[dict], loose: bool = False,
                   browser_titles: bool = False) -> Tuple[List[dict], bool]:
    """Windows that belong to the app spoken as `q` (Latin, lower case). Returns (matches, matched_by_exe).
    Exe names always count; the window title only counts for the app's own title words on UWP frame / unreadable
    windows, and (loose, i.e. an explicit tool call) for any non-browser window whose title has the whole word
    (browser windows too when browser_titles: switching to "gmail" may mean the browser window showing it)."""
    qn = _alnum(q)
    if not qn:
        return [], False
    alias = _APPS.get(q)
    stems = tuple(_alnum(s) for s in alias["stems"]) if alias else ()
    titles = tuple(alias["titles"]) if alias else ()
    by_exe: List[dict] = []
    by_title: List[dict] = []
    for w in windows:
        stem = _stem(w.get("exe", ""))
        sn = _alnum(stem)
        exe_hit = False
        if sn:
            if alias:
                exe_hit = sn in stems
            else:
                exe_hit = (sn == qn or (len(qn) >= 4 and sn.startswith(qn)) or (len(qn) >= 5 and qn in sn))
        if exe_hit:
            by_exe.append(w)
            continue
        if stem in _BROWSER_STEMS and not browser_titles:
            continue
        title = (w.get("title") or "").lower()
        if alias and titles and stem in _HOST_STEMS:
            if any(re.search(rf"(?:^|\W){re.escape(t)}(?:\W|$)", title) for t in titles):
                by_title.append(w)
                continue
        if loose and len(q) >= 3 and re.search(rf"(?:^|\W){re.escape(q)}(?:\W|$)", title):
            by_title.append(w)
    if by_exe:
        return by_exe, True
    return by_title, False


def _pretty_window(w: dict) -> str:
    stem = _stem(w.get("exe", ""))
    if stem in _PRETTY_EXE:
        return _PRETTY_EXE[stem]
    if stem in ("applicationframehost", ""):
        title = (w.get("title") or "").strip()
        return title[:30] if title else "that window"
    return stem.replace("_", " ").replace("-", " ").title()


def _pretty_query(q: str, matches: List[dict]) -> str:
    alias = _APPS.get(q)
    if alias:
        return alias["pretty"]
    if matches:
        return _pretty_window(matches[0])
    return q.title()


# ------------------------------------------------------------------ OS layer (all replaceable in tests)

def _run_powershell(script: str, timeout: float = 10.0) -> Optional[str]:
    """Runs a PowerShell snippet without a console window. stdout (stripped) on success, None on any failure."""
    if not _is_windows():
        return None
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
             "$ErrorActionPreference='Stop'; " + script],
            capture_output=True, text=True, timeout=timeout, creationflags=0x08000000,
            encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - timeout, no powershell, ...
        logger.warning("sysctl: PowerShell failed to run: %s", e)
        return None
    if proc.returncode != 0:
        logger.warning("sysctl: PowerShell exited %s: %s", proc.returncode, (proc.stderr or "").strip()[:200])
        return None
    return (proc.stdout or "").strip()


def _later(delay_s: float, fn: Callable[[], None]):
    """Runs fn after delay_s seconds on a daemon timer thread (so she can finish her sentence first)."""
    timer = threading.Timer(float(delay_s), fn)
    timer.daemon = True
    timer.start()
    return timer


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _pycaw_endpoint():
    """A fresh IAudioEndpointVolume (COM objects are per thread), or None: pycaw missing / not Windows / no device."""
    if not _is_windows():
        return None
    try:
        import comtypes
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError:
        _state["pycaw_missing"] = True
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: pycaw import failed: %s", e)
        return None
    try:
        try:
            comtypes.CoInitialize()
        except Exception:                       # already initialised on this thread, possibly in another mode
            pass
        dev = AudioUtilities.GetSpeakers()
        ev = getattr(dev, "EndpointVolume", None) or dev.Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)
        return ev
    except Exception as e:  # noqa: BLE001 - no audio device, COM error ...
        logger.warning("sysctl: couldn't get the audio endpoint: %s", e)
        return None


def _get_volume() -> Optional[int]:
    """Master volume 0-100, or None if it can't be read (no pycaw)."""
    ev = _pycaw_endpoint()
    if ev is None:
        return None
    try:
        return int(round(float(ev.GetMasterVolumeLevelScalar()) * 100))
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: reading the volume failed: %s", e)
        return None


def _set_volume(pct: int) -> bool:
    ev = _pycaw_endpoint()
    if ev is None:
        return False
    try:
        ev.SetMasterVolumeLevelScalar(max(0, min(100, int(pct))) / 100.0, None)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: setting the volume failed: %s", e)
        return False


def _get_muted() -> Optional[bool]:
    ev = _pycaw_endpoint()
    if ev is None:
        return None
    try:
        return bool(ev.GetMute())
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: reading mute failed: %s", e)
        return None


def _set_muted(flag: bool) -> bool:
    ev = _pycaw_endpoint()
    if ev is None:
        return False
    try:
        ev.SetMute(1 if flag else 0, None)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: setting mute failed: %s", e)
        return False


def _press_volume_key(kind: str, times: int = 1) -> None:
    """Sends the keyboard volume key 'up' | 'down' | 'mute' `times` times (each press is about 2 percent)."""
    vk = {"up": winutil.VK_VOLUME_UP, "down": winutil.VK_VOLUME_DOWN, "mute": winutil.VK_VOLUME_MUTE}[kind]
    winutil.press_volume_key(vk, times)


def _sbc():
    try:
        import screen_brightness_control as sbc
        return sbc
    except Exception:                           # not installed, or it failed to initialise
        return None


def _get_brightness() -> Optional[int]:
    """Screen brightness 0-100, or None when this display can't be read."""
    if not _is_windows():
        return None
    sbc = _sbc()
    if sbc is not None:
        try:
            vals = sbc.get_brightness()
            vals = vals if isinstance(vals, (list, tuple)) else [vals]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if vals:
                return int(round(vals[0]))
        except Exception as e:  # noqa: BLE001
            logger.info("sysctl: screen_brightness_control couldn't read: %s", e)
    out = _run_powershell("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness")
    if out:
        m = re.search(r"\d+", out)
        if m:
            return int(m.group(0))
    return None


def _set_brightness(pct: int) -> bool:
    if not _is_windows():
        return False
    pct = max(0, min(100, int(pct)))
    sbc = _sbc()
    if sbc is not None:
        try:
            sbc.set_brightness(pct)
            return True
        except Exception as e:  # noqa: BLE001
            logger.info("sysctl: screen_brightness_control couldn't set: %s", e)
    out = _run_powershell(
        "Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
        f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1;Brightness={pct}}} | Out-Null")
    return out is not None


def _battery_info() -> Optional[dict]:
    """{'percent': int, 'plugged': bool|None, 'charging': bool|None, 'secs': int|None}; {} = no battery (desktop);
    None = couldn't find out."""
    try:
        import psutil
    except Exception:
        psutil = None
    if psutil is not None:
        try:
            b = psutil.sensors_battery()
            if b is None:
                return {}
            secs = b.secsleft if isinstance(b.secsleft, (int, float)) and b.secsleft > 0 else None
            return {"percent": int(round(b.percent)), "plugged": None if b.power_plugged is None else bool(b.power_plugged),
                    "charging": None, "secs": int(secs) if secs else None}
        except Exception as e:  # noqa: BLE001
            logger.info("sysctl: psutil battery failed: %s", e)
    out = _run_powershell("Get-CimInstance Win32_Battery | Select-Object EstimatedChargeRemaining,BatteryStatus | "
                          "ConvertTo-Json -Compress")
    if out is None:
        return None
    if not out.strip():
        return {}
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict) or data.get("EstimatedChargeRemaining") is None:
        return {} if not data else None
    status = data.get("BatteryStatus")
    return {"percent": int(data["EstimatedChargeRemaining"]),
            "plugged": status in (2, 3, 6, 7, 8, 9, 11) if isinstance(status, int) else None,
            "charging": status in (6, 7, 8, 9) if isinstance(status, int) else None, "secs": None}


def _lock_now() -> None:
    try:
        ok = winutil.lock_workstation()
        logger.info("sysctl: LockWorkStation -> %s", ok)
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: lock failed: %s", e)


def _suspend() -> bool:
    """Puts the PC to sleep. powrprof.SetSuspendState first (blocks until the PC wakes up), PowerShell if that
    returns 0. NB: with hibernation enabled or Modern Standby Windows may hibernate instead of sleeping."""
    if not _is_windows():
        return False
    try:
        import ctypes
        dll = ctypes.WinDLL("powrprof")
        fn = dll.SetSuspendState
        fn.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ubyte]
        fn.restype = ctypes.c_ubyte
        if fn(0, 0, 0):
            return True
        logger.warning("sysctl: SetSuspendState returned 0, trying PowerShell")
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: SetSuspendState failed (%s), trying PowerShell", e)
    out = _run_powershell("Add-Type -AssemblyName System.Windows.Forms; "
                          "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)", timeout=20)
    return out is not None


def _run_shutdown(mode: str, delay: int = 0) -> int:
    """`shutdown /s|/r /t <delay>` or `shutdown /a`. Returns the exit code (0 = ok), -1 if it could not run.
    NB: on Windows a timeout above 0 implies /f (running apps are force-closed when the time is up)."""
    args = ["shutdown", "/a"] if mode == "a" else ["shutdown", f"/{mode}", "/t", str(int(delay))]
    if not _is_windows():
        return -1
    try:
        return subprocess.run(args, capture_output=True, timeout=15, creationflags=0x08000000).returncode
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: %s failed: %s", " ".join(args), e)
        return -1


def _list_windows() -> List[dict]:
    """Visible top-level windows, front first: [{hwnd, title, exe, minimized}]. Raises OSError off Windows."""
    return winutil.list_windows()


def _foreground() -> dict:
    return winutil.foreground_window()


def _focus(hwnd: int) -> bool:
    return bool(winutil.focus_window(hwnd))


def _close(hwnd: int) -> bool:
    return bool(winutil.close_window(hwnd))


def _minimize(hwnd: int) -> bool:
    return bool(winutil.minimize_window(hwnd))


def _hotkey(*vks: int) -> None:
    winutil.send_hotkey(*vks)


# ------------------------------------------------------------------ actions: volume

def _pycaw_hint() -> str:
    if _state["pycaw_missing"] and not _state["pycaw_hint_given"]:
        _state["pycaw_hint_given"] = True
        return _tr("pycaw_hint")
    return ""


def _amount(value: str, default: int) -> int:
    n = _parse_number(value) if str(value or "").strip() else None
    if n is None:
        v = _fold(str(value or ""))
        if re.search(r"little|bit|slight|थोड", v):
            return 5
        if re.search(r"\blot\b|much|बहुत|काफी", v):
            return 25
        return default
    return max(1, min(100, n))


def _volume_step(direction: int, value: str = "") -> str:
    step = _amount(value, _cfg_int("VOLUME_STEP", 10, 1, 100))
    cur = _get_volume()
    if cur is not None:
        target = max(0, min(100, cur + direction * step))
        if target == cur:
            if direction > 0 and _get_muted():
                _set_muted(False)
            return _tr("vol_at_max" if direction > 0 else "vol_at_min")
        if _set_volume(target):
            if direction > 0 and target > 0 and _get_muted():
                _set_muted(False)               # like the keyboard: turning it up un-mutes
            return _tr("vol_up" if direction > 0 else "vol_down", p=target)
    try:
        _press_volume_key("up" if direction > 0 else "down", max(1, int(round(step / 2))))
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: volume key failed: %s", e)
        return _tr("vol_fail")
    return _tr("key_up" if direction > 0 else "key_down") + _pycaw_hint()


def _volume_set(value: str) -> str:
    n = _parse_number(value)
    if n is None:
        return _tr("ask_vol")
    pct = max(0, min(100, n))
    if _get_volume() is not None and _set_volume(pct):
        if pct > 0 and _get_muted():
            _set_muted(False)
        return _tr("vol_set", p=pct)
    try:
        _press_volume_key("down", 50)
        if pct:
            _press_volume_key("up", int(round(pct / 2)))
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: volume key failed: %s", e)
        return _tr("vol_fail")
    return _tr("key_set", p=pct) + _pycaw_hint()


def _volume_get() -> str:
    cur = _get_volume()
    if cur is None:
        return _tr("no_read_vol") + _pycaw_hint()
    return _tr("vol_get_muted" if _get_muted() else "vol_get", p=cur)


def _mute_set(want: bool) -> str:
    state = _get_muted()
    if state is None:
        try:
            _press_volume_key("mute")
        except Exception as e:  # noqa: BLE001
            logger.warning("sysctl: mute key failed: %s", e)
            return _tr("vol_fail")
        return _tr("key_mute") + _pycaw_hint()
    if state == want:
        return _tr("already_muted" if want else "not_muted")
    if not _set_muted(want):
        return _tr("vol_fail")
    if want:
        return _tr("muted")
    cur = _get_volume()
    if cur is None:
        return _tr("unmuted_plain")
    return _tr("unmuted_zero") if cur == 0 else _tr("unmuted", p=cur)


def _mute_toggle() -> str:
    state = _get_muted()
    if state is None:
        return _mute_set(True)                  # key fallback: the key itself toggles
    return _mute_set(not state)


# ------------------------------------------------------------------ actions: brightness

def _brightness_step(direction: int, value: str = "") -> str:
    step = _amount(value, _cfg_int("BRIGHTNESS_STEP", 10, 1, 100))
    cur = _get_brightness()
    if cur is None:
        return _tr("br_na")
    target = max(BRIGHTNESS_FLOOR, min(100, cur + direction * step))
    if target == cur or (direction < 0 and cur <= BRIGHTNESS_FLOOR):
        return _tr("br_at_max" if direction > 0 else "br_at_min")
    if not _set_brightness(target):
        return _tr("br_na")
    return _tr("br_up" if direction > 0 else "br_down", p=target)


def _brightness_set(value: str) -> str:
    n = _parse_number(value)
    if n is None:
        return _tr("ask_br")
    pct = max(BRIGHTNESS_FLOOR, min(100, n))
    if not _set_brightness(pct):
        return _tr("br_na")
    return _tr("br_set", p=pct)


def _brightness_get() -> str:
    cur = _get_brightness()
    if cur is None:
        return _tr("br_na")
    return _tr("br_get", p=cur)


# ------------------------------------------------------------------ actions: battery

def _battery() -> str:
    info = _battery_info()
    if info is None:
        return _tr("bat_unknown")
    if not info:
        return _tr("bat_desktop")
    pct = int(info.get("percent", 0))
    plugged, charging, secs = info.get("plugged"), info.get("charging"), info.get("secs")
    if charging:
        text = _tr("bat_charging", p=pct)
    elif plugged:
        text = _tr("bat_full") if pct >= 100 else _tr("bat_plugged", p=pct)
    else:
        text = _tr("bat_on", p=pct)
        if isinstance(secs, int) and 0 < secs < 48 * 3600:
            text += _tr("bat_time", t=lang.fmt_duration(secs))
        if pct <= 20:
            text += _tr("bat_low")
    return text


# ------------------------------------------------------------------ actions: lock / sleep / shutdown

def _schedule(fn: Callable[[], object], what: str) -> None:
    delay = float(_cfg("SYSTEM_ACTION_DELAY_SECONDS", 2.0))

    def run():
        try:
            logger.info("sysctl: %s now", what)
            fn()
        except Exception:                        # noqa: BLE001
            logger.exception("sysctl: %s failed", what)

    _later(delay, run)


def _lock() -> str:
    _schedule(_lock_now, "lock")
    return _tr("locking")


def _sleep_pc() -> str:
    _schedule(_suspend, "sleep")
    return _tr("sleeping")


def _shutdown_request(restart: bool) -> str:
    l = lang.current()
    delay = _cfg_int("SHUTDOWN_DELAY_SECONDS", 30, 0, 3600)
    kind = "restart_pc" if restart else "shutdown_pc"
    tag = "restart" if restart else "shutdown"

    def execute() -> str:
        rc = _run_shutdown("r" if restart else "s", delay)
        logger.info("sysctl: shutdown /%s /t %d -> %s", "r" if restart else "s", delay, rc)
        if rc != 0:
            return _tr(f"{tag}_fail", l)
        global _ARMED_UNTIL
        _ARMED_UNTIL = time.time() + delay + 5          # a bare "cancel" means this until the timer runs out
        if delay <= 0:
            return _tr(f"{tag}_now", l)
        return _tr(f"{tag}_go", l, t=lang.fmt_duration(delay, l))

    return confirmation.request(kind, _tr(f"{tag}_q", l), execute, summary=_tr(f"{tag}_summary", l))


def _cancel_shutdown() -> str:
    rc = _run_shutdown("a")
    logger.info("sysctl: shutdown /a -> %s", rc)
    if rc == 0:
        global _ARMED_UNTIL
        _ARMED_UNTIL = 0.0
        return _tr("cancelled")
    if rc == 1116:                              # ERROR_NO_SHUTDOWN_IN_PROGRESS
        return _tr("cancel_none")
    return _tr("cancel_fail")


# ------------------------------------------------------------------ actions: windows

def _show_desktop() -> str:
    try:
        _hotkey(winutil.VK_LWIN, winutil.VK_D)
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: show desktop failed: %s", e)
        return _tr("key_fail")
    return _tr("desktop")


def _minimize_all() -> str:
    try:
        _hotkey(winutil.VK_LWIN, winutil.VK_M)
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: minimize all failed: %s", e)
        return _tr("key_fail")
    return _tr("min_all")


def _minimize_this() -> str:
    try:
        w = _foreground()
        if not w or not w.get("hwnd") or not (w.get("title") or "").strip():
            return _tr("no_front")
        _minimize(int(w["hwnd"]))
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: minimize failed: %s", e)
        return _tr("win_na")
    return _tr("min_this", name=_tr("this_window") if not _pretty_window(w) else _pretty_window(w))


def _next_window() -> str:
    try:
        _hotkey(winutil.VK_MENU, winutil.VK_TAB)
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: alt-tab failed: %s", e)
        return _tr("key_fail")
    return _tr("next_window")


def _resolve_target(name: str) -> Tuple[str, str]:
    """(latin query, display name) for a spoken app name."""
    q = _latin_name(name)
    q = _TAIL_WORD.sub("", q).strip()
    return q, q


def _plausible_target(q: str) -> bool:
    if not q or len(q) < 2 or len(q) > 32 or len(q.split()) > 3:
        return False
    if q in _NAME_STOP or q.split()[0] in ("to", "in", "on", "at", "for", "with", "by", "your", "his", "her", "their"):
        return False
    return True


def _clean_target(what: str) -> str:
    what = re.sub(r"\s+", " ", what).strip()
    what = re.sub(r"(?: को| ko)$", "", what).strip()
    what = _LEAD_DET.sub("", what).strip()
    for _ in range(2):
        what = _TAIL_WORD.sub("", what).strip()
    return what


def _close_app(value: str, strict: bool = False) -> Optional[str]:
    l = lang.current()
    raw = (value or "").strip()
    fg = raw == "@foreground" or bool(_FG_RE.fullmatch(_fold(raw)))
    try:
        if fg:
            w = _foreground()
            if not w or not w.get("hwnd") or not (w.get("title") or "").strip():
                return _tr("no_front", l)
            name = _pretty_window(w)
            if _is_protected(w):
                return _tr("cant_close", l, name=name)
            hwnds = [int(w["hwnd"])]
            question = _tr("close_q_this", l, name=name)
        else:
            name_raw = _clean_target(_fold(raw)) if raw else ""
            if not name_raw:
                return None if strict else _tr("ask_close", l)
            q, _ = _resolve_target(name_raw)
            if strict and (not _plausible_target(q) or q in _NEVER_TARGETS):
                return None
            if not q:
                return None if strict else _tr("ask_close", l)
            windows = _list_windows()
            matches, via_exe = _match_windows(q, windows, loose=not strict)
            alias = _APPS.get(q)
            known = bool(alias) and not alias["weak"]
            if not matches:
                if strict and not known:
                    return None
                return _tr("no_window", l, name=_pretty_query(q, matches))
            if strict and not (known or via_exe or (alias and alias["weak"])):
                return None
            name = _pretty_query(q, matches)
            allowed = [m for m in matches if not _is_protected(m)]
            if not allowed:
                return _tr("cant_close", l, name=name)
            hwnds = [int(m["hwnd"]) for m in allowed]
            question = (_tr("close_q_many", l, name=name, n=len(hwnds)) if len(hwnds) > 1
                        else _tr("close_q_one", l, name=name))
    except OSError as e:
        logger.warning("sysctl: window list unavailable: %s", e)
        return _tr("win_na", l)

    def execute() -> str:
        try:
            now = {int(w["hwnd"]): w for w in _list_windows()}
        except OSError:
            return _tr("win_na", l)
        live = [h for h in hwnds if h in now and not _is_protected(now[h])]
        if not live:
            return _tr("close_gone", l, name=name)
        for h in live:
            ok = _close(h)
            logger.info("sysctl: WM_CLOSE %s (%s) -> %s", h, name, ok)
        _sleep(0.7)
        try:
            still = {int(w["hwnd"]) for w in _list_windows()}
        except OSError:
            still = set()
        if any(h in still for h in live):
            return _tr("close_pending", l, name=name)
        return _tr("closed", l, name=name)

    return confirmation.request("close_app", question, execute,
                                summary=_tr("close_summary", l, name=name))


def _switch_to(value: str, strict: bool = False) -> Optional[str]:
    l = lang.current()
    name_raw = _clean_target(_fold(value or ""))
    if not name_raw:
        return None if strict else _tr("ask_switch", l)
    q, _ = _resolve_target(name_raw)
    if not q:
        return None if strict else _tr("ask_switch", l)
    if strict and (not _plausible_target(q) or q in _NEVER_TARGETS):
        return None
    try:
        windows = _list_windows()
    except OSError as e:
        logger.warning("sysctl: window list unavailable: %s", e)
        return None if strict else _tr("win_na", l)
    matches, via_exe = _match_windows(q, windows, loose=not strict, browser_titles=not strict)
    alias = _APPS.get(q)
    known = bool(alias) and not alias["weak"]
    if not matches:
        if strict and not known:
            return None
        return _tr("not_open", l, name=_pretty_query(q, matches))
    if strict and not (known or via_exe or alias):
        return None
    target = matches[0]
    name = _pretty_query(q, matches)
    if windows and target is windows[0] and not target.get("minimized"):
        return _tr("already_front", l, name=name)
    try:
        ok = _focus(int(target["hwnd"]))
    except Exception as e:  # noqa: BLE001
        logger.warning("sysctl: focus failed: %s", e)
        ok = False
    logger.info("sysctl: focus %s (%s) -> %s", target.get("hwnd"), name, ok)
    return _tr("switching", l, name=name) if ok else _tr("switch_fail", l, name=name)


# ------------------------------------------------------------------ the parser

def _p_windows(t: str) -> Optional[Tuple[str, str]]:
    if _first(_P_SHOW_DESKTOP, t):
        return "show_desktop", ""
    if _first(_P_MIN_ALL, t):
        return "minimize_all", ""
    if _first(_P_MIN_THIS, t):
        return "minimize_this", ""
    if _first(_P_NEXT_WINDOW, t):
        return "next_window", ""
    return None


def _p_close(t: str) -> Optional[Tuple[str, str]]:
    for p in _P_CLOSE:
        m = p.fullmatch(t)
        if not m:
            continue
        what = m.group("what").strip()
        if _FG_RE.fullmatch(what):
            return "close_app", "@foreground"
        name = _clean_target(what)
        if name and len(name.split()) <= 3 and name not in _NAME_STOP:
            return "close_app", name
    return None


def _p_switch(t: str) -> Optional[Tuple[str, str]]:
    for p in _P_SWITCH:
        m = p.fullmatch(t)
        if not m:
            continue
        name = _clean_target(m.group("name").strip())
        if name and len(name.split()) <= 3 and name not in _NAME_STOP:
            return "switch_window", name
    return None


def _parse(t: str) -> Optional[Tuple[str, str]]:
    if _first(_P_CANCEL, t):
        return "cancel_shutdown", ""
    if _shutdown_armed() and _first(_P_BARE_CANCEL, t):
        return "cancel_shutdown", ""
    if _first(_P_SHUTDOWN, t):
        return "shutdown", ""
    if _first(_P_RESTART, t):
        return "restart", ""
    if _first(_P_LOCK, t):
        return "lock", ""
    if _first(_P_SLEEP, t):
        return "sleep", ""
    if _is_battery_query(t):
        return "battery", ""
    r = _p_windows(t)
    if r:
        return r
    r = _p_volume(t)
    if r:
        return r
    r = _p_brightness(t)
    if r:
        return r
    r = _p_close(t)
    if r:
        return r
    return _p_switch(t)


# ------------------------------------------------------------------ dispatch

def _execute(action: str, value: str = "", strict: bool = False) -> Optional[str]:
    """Runs one canonical action. Returns the sentence to speak (None only in strict mode when a window name
    turned out not to be an app)."""
    if action == "volume_up":
        return _volume_step(+1, value)
    if action == "volume_down":
        return _volume_step(-1, value)
    if action == "volume_set":
        return _volume_set(value)
    if action == "volume_get":
        return _volume_get()
    if action == "mute":
        return _mute_set(True)
    if action == "unmute":
        return _mute_set(False)
    if action == "toggle_mute":
        return _mute_toggle()
    if action == "brightness_up":
        return _brightness_step(+1, value)
    if action == "brightness_down":
        return _brightness_step(-1, value)
    if action == "brightness_set":
        return _brightness_set(value)
    if action == "brightness_get":
        return _brightness_get()
    if action == "battery":
        return _battery()
    if action == "lock":
        return _lock()
    if action == "sleep":
        return _sleep_pc()
    if action == "shutdown":
        return _shutdown_request(False)
    if action == "restart":
        return _shutdown_request(True)
    if action == "cancel_shutdown":
        return _cancel_shutdown()
    if action == "close_app":
        return _close_app(value, strict)
    if action == "switch_window":
        return _switch_to(value, strict)
    if action == "show_desktop":
        return _show_desktop()
    if action == "minimize_all":
        return _minimize_all()
    if action == "minimize_this":
        return _minimize_this()
    if action == "next_window":
        return _next_window()
    return None


def try_handle(transcript: str) -> Optional[str]:
    """The finished sentence to speak if `transcript` is a PC-control request (already done), else None."""
    try:
        if not _cfg("SYSTEM_CONTROL_ENABLED", True):
            return None
        t = _norm(transcript)
        if t is None:
            return None
        intent = _parse(t)
    except Exception:                            # noqa: BLE001
        logger.exception("sysctl: matching failed")
        return None
    if intent is None:
        return None
    action, value = intent
    logger.info("sysctl: matched %s %r", action, value)
    try:
        reply = _execute(action, value, strict=True)
    except Exception:                            # noqa: BLE001
        logger.exception("sysctl: %s failed", action)
        return _tr("sorry")
    if reply is None:
        logger.info("sysctl: %s %r was not something I can act on - passing", action, value)
    return reply


# ------------------------------------------------------------------ the LLM tool

_ACTIONS = {"volume_up", "volume_down", "volume_set", "mute", "unmute", "volume_get", "brightness_up",
            "brightness_down", "brightness_set", "brightness_get", "battery", "lock", "sleep", "shutdown", "restart",
            "cancel_shutdown", "close_app", "switch_window", "show_desktop", "minimize_all", "toggle_mute",
            "minimize_this", "next_window"}
_SYNONYMS = {
    "turn_up": "volume_up", "louder": "volume_up", "increase_volume": "volume_up", "raise_volume": "volume_up",
    "volume_increase": "volume_up", "turn_up_volume": "volume_up", "quieter": "volume_down",
    "decrease_volume": "volume_down", "lower_volume": "volume_down", "volume_decrease": "volume_down",
    "turn_down": "volume_down", "reduce_volume": "volume_down", "set_volume": "volume_set",
    "get_volume": "volume_get", "check_volume": "volume_get", "volume_level": "volume_get",
    "brighter": "brightness_up", "increase_brightness": "brightness_up", "dim": "brightness_down",
    "dimmer": "brightness_down", "decrease_brightness": "brightness_down", "lower_brightness": "brightness_down",
    "set_brightness": "brightness_set", "get_brightness": "brightness_get", "brightness_level": "brightness_get",
    "battery_level": "battery", "battery_status": "battery", "check_battery": "battery", "get_battery": "battery",
    "lock_screen": "lock", "lock_computer": "lock", "lock_pc": "lock", "lock_workstation": "lock",
    "sleep_pc": "sleep", "sleep_computer": "sleep", "suspend": "sleep", "put_to_sleep": "sleep",
    "shut_down": "shutdown", "shutdown_pc": "shutdown", "shutdown_computer": "shutdown", "power_off": "shutdown",
    "restart_pc": "restart", "restart_computer": "restart", "reboot": "restart",
    "cancel_restart": "cancel_shutdown", "abort_shutdown": "cancel_shutdown", "cancel": "cancel_shutdown",
    "abort": "cancel_shutdown", "close": "close_app", "close_window": "close_app", "quit_app": "close_app",
    "quit": "close_app", "close_application": "close_app", "switch": "switch_window", "switch_to": "switch_window",
    "focus_window": "switch_window", "switch_app": "switch_window", "go_to": "switch_window",
    "desktop": "show_desktop", "show_the_desktop": "show_desktop", "minimize": "minimize_all",
    "minimise_all": "minimize_all", "minimize_windows": "minimize_all", "next": "next_window",
    "alt_tab": "next_window", "mute_toggle": "toggle_mute", "unmute_volume": "unmute", "mute_volume": "mute",
    "minimize_window": "minimize_this",
}
_UP_WORDS = {"up", "increase", "raise", "higher", "louder", "more", "brighter", "boost", "inc"}
_DOWN_WORDS = {"down", "decrease", "lower", "lower", "reduce", "quieter", "softer", "less", "dim", "dimmer",
               "darker", "dec"}


def _canon_action(action: str, value: str) -> Tuple[str, str]:
    """Sloppy tool input -> (canonical action, value)."""
    a = _fold(str(action or "")).strip()
    value = "" if value is None else str(value).strip()
    m = re.search(r"\b\d{1,3}\b", a)
    if m and not value:
        value = m.group(0)
        a = (a[:m.start()] + " " + a[m.end():])
    a = re.sub(r"[^a-z]+", "_", a).strip("_")
    if a in _ACTIONS:
        return a, value
    if a in _SYNONYMS:
        return _SYNONYMS[a], value
    words = set(a.split("_"))
    vw = _fold(value)
    if words & {"volume", "sound", "audio", "speaker", "speakers"} or a in ("volume", "sound", "audio"):
        if "unmute" in words:
            return "unmute", value
        if "mute" in words:
            return "mute", value
        if words & _UP_WORDS or vw in _UP_WORDS:
            return "volume_up", "" if vw in _UP_WORDS else value
        if words & _DOWN_WORDS or vw in _DOWN_WORDS:
            return "volume_down", "" if vw in _DOWN_WORDS else value
        if vw in ("mute", "unmute", "toggle"):
            return ("toggle_mute" if vw == "toggle" else vw), ""
        if "max" in words or vw in ("max", "maximum", "full"):
            return "volume_set", "100"
        if _parse_number(value) is not None or "set" in words:
            return "volume_set", value
        return "volume_get", ""
    if words & {"brightness", "screen", "display", "monitor", "backlight"}:
        if words & _UP_WORDS or vw in _UP_WORDS:
            return "brightness_up", "" if vw in _UP_WORDS else value
        if words & _DOWN_WORDS or vw in _DOWN_WORDS:
            return "brightness_down", "" if vw in _DOWN_WORDS else value
        if _parse_number(value) is not None or "set" in words:
            return "brightness_set", value
        return "brightness_get", ""
    if "mute" in words:
        return ("unmute" if "unmute" in words or "un" in words else "mute"), value
    if "battery" in words or "charging" in words:
        return "battery", value
    return a, value


def system_control(action: str = "", value: str = "", **_ignored) -> str:
    """
    LLM tool: control the PC. action = volume_up | volume_down | volume_set | mute | unmute | volume_get |
    brightness_up | brightness_down | brightness_set | brightness_get | battery | lock | sleep | shutdown |
    restart | cancel_shutdown | close_app | switch_window | show_desktop | minimize_all.
    `value` = a number (percent, for *_set; or how many percent for *_up / *_down) or an app name
    (close_app, switch_window). shutdown, restart and close_app ask the user to confirm first.
    """
    try:
        if not _cfg("SYSTEM_CONTROL_ENABLED", True):
            return _tr("disabled")
        if isinstance(action, str) and len(action.split()) > 1:
            # the model wrote a sentence instead of an action name: "turn the volume up a lot"
            reply = try_handle(f"{action} {value if value is not None else ''}".strip())
            if reply:
                return reply
        act, val = _canon_action(action, value)
        logger.info("sysctl tool: %s %r (from %r %r)", act, val, action, value)
        if act in _ACTIONS:
            if act in ("close_app", "switch_window") and val.lower() in ("this", "this window", "current", "current window",
                                                                         "the current window", "active window"):
                val = "@foreground" if act == "close_app" else ""
            reply = _execute(act, val, strict=False)
            if reply:
                return reply
        # last resort: the sentence grammar ("volume 50", "increase the volume a bit")
        sentence = f"{action or ''} {value or ''}".strip()
        if sentence:
            reply = try_handle(sentence)
            if reply:
                return reply
        return _tr("unknown")
    except Exception:                            # noqa: BLE001
        logger.exception("sysctl tool failed")
        return _tr("sorry")
