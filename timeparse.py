"""
timeparse.py - spoken durations and clock times, English and Hindi (Devanagari and Hinglish).

Pure functions, no I/O, no clock of their own (time is always passed in as `now`), so it is trivially
testable and other modules (reminders, calendar, briefing) can import it freely.

Public API (frozen):
  parse_duration(text)               -> seconds (float) or None      "an hour and a half", "डेढ़ घंटा"
  parse_when(text, now=None)         -> naive local datetime or None  "tomorrow at 9", "शाम 5 बजे"
  extract_when(text, now=None)       -> (datetime | None, text without the time words)
  describe_when(dt, now=None, lang=None) -> speech-friendly phrase    "in 20 minutes", "कल सुबह 9 बजे"

How it works
------------
1. The sentence is cut into tokens, each token is folded (Hindi spelling variants) and mapped onto a small
   canonical vocabulary ("मिनट" / "minutes" / "minit" -> "min", "बजे" / "baje" -> "baje", "कल" -> "tomorrow",
   "बीस" / "twenty" -> "20"). Every canonical token remembers where it came from in the original text.
2. Scanners recognise chunks on the canonical tokens: a duration ("in 20 min", "1 hr 30 min"), a clock time
   ("at 5 pm", "half past 5", "साढ़े 5 baje"), a day ("tomorrow", "wd0", "25th"), a part of the day
   ("evening"). Hindi word order (postfix "20 मिनट में", "एक घंटे बाद") is handled in the same scanners.
3. The chunks are combined into one datetime (resolve). Rules that matter to callers:
   * No AM/PM and hour <= 12 -> the EARLIEST FUTURE occurrence among AM and PM.
   * An explicit clock time that already passed today rolls to tomorrow ("at 5 am" at 6 am -> tomorrow 5 am),
     unless a date word (today / tomorrow / a weekday / a date) was said: then the date is honoured, even if
     that moment is already in the past (the caller decides what to tell the user).
   * "tomorrow morning" 09:00, "this afternoon" 15:00, "this evening" 18:00, "tonight" 20:00, a bare day 09:00.
   * "next Friday" / "on Friday" = the soonest such day after today (today itself only counts for a bare
     weekday whose time is still ahead).
   * "midnight" said with a day means the END of that day ("tomorrow at midnight" = the day after, 00:00).
   * When a "this evening"-style phrase is already late but the part of the day is still running, the
     result is now + 30 minutes instead of a time in the past.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import lang

# ------------------------------------------------------------------ vocabulary

_PUNCT = ".,!?;:।॥\"'()[]{}“”‘’-–—…"

_EN_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
             "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
             "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
             "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90}
_HINGLISH_NUM = {"ek": 1, "teen": 3, "char": 4, "paanch": 5, "panch": 5, "chhe": 6, "chhah": 6, "cheh": 6,
                 "saat": 7, "aath": 8, "nau": 9, "das": 10, "dus": 10, "gyarah": 11, "barah": 12,
                 "terah": 13, "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17, "atharah": 18,
                 "unnis": 19, "bees": 20, "pachees": 25, "tees": 30, "paintees": 35, "chalis": 40,
                 "pachas": 50, "saath": 60}

_SIMPLE: Dict[str, str] = {}


def _reg(canon: str, words: str) -> None:
    for w in words.split():
        _SIMPLE[lang.fold_hindi(w)] = canon


_reg("min", "minute minutes min mins minit minat minits मिनट मिनिट मिनटों मिनटो मिनट्स मिनिटों मिनिट्स")
_reg("hr", "hour hours hr hrs ghanta ghante ghanty ghantey ghanton घंटा घंटे घंटों घण्टा घण्टे "
           "घन्टा घन्टे घंटो घंटी")
_reg("sec", "second seconds sec secs sekand sekend सेकंड सेकेंड सेकण्ड सैकंड सेकेण्ड सेकंड्स सेकंडों "
            "सेकेंडों सैकेंड")
_reg("day", "day days din दिन दिनों")
_reg("wk", "week weeks hafta hafte हफ्ता हफ्ते हफ्तों सप्ताह हफ़्ता हफ़्ते")
_reg("am", "am")
_reg("pm", "pm")
_reg("oclock", "oclock o'clock")
_reg("noon", "noon")
_reg("midnight", "midnight")
_reg("today", "today aaj आज")
_reg("tomorrow", "tomorrow tomorow tommorow tmrw tomoro kal कल")
_reg("dayafter", "parso parson parsoon परसों परसो")
_reg("morning", "morning subah subha सुबह सवेरे सवेरा तड़के प्रातः")
_reg("afternoon", "afternoon dopahar dophar dupahar दोपहर दुपहर दोपहरी")
_reg("evening", "evening shaam sham शाम संध्या")
_reg("night", "night raat रात रात्रि")
_reg("baje", "baje baj bajke बजे")
_reg("bajkar", "bajkar bajkarr बजकर बजके")
_reg("half", "half adha aadha aadhe adhe aadhi adhi आधा आधे आधी")
_reg("dedh", "dedh der डेढ़ डेढ")
_reg("dhai", "dhai dhaai ढाई")
_reg("sava", "sava sawa सवा")
_reg("sadhe", "sadhe saadhe saade साढ़े साढे साढ़ें")
_reg("paune", "paune paun pauna पौने पौन पौना")
_reg("quarter", "quarter quarters")
_reg("couple", "couple")
_reg("in", "in")
_reg("after", "after within")
_reg("mein", "mein में")
_reg("baad", "baad बाद")
_reg("ke", "ke ka ki के का की")
_reg("ko", "ko को")
_reg("and", "and aur और")
_reg("a", "a an")
_reg("next", "next agle agla agli अगले अगला अगली coming upcoming")
_reg("this", "this इस")
_reg("tarikh", "tarikh tareekh तारीख तारीख़")
_reg("later", "later")
_reg("from", "from")
_reg("now", "now abhi अभी")

_MONTH_EN = {}
for _i, _n in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                         "september", "october", "november", "december"]):
    _MONTH_EN[_n] = _i + 1
    if _n not in ("may", "june", "july"):
        _MONTH_EN[_n[:3]] = _i + 1
_MONTH_EN["sept"] = 9
_MONTH_EN["may"] = 0        # only a month next to a number (handled in _post)
_WEEKDAY_EN = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5,
               "sunday": 6, "somvar": 0, "somwar": 0, "mangalvar": 1, "mangalwar": 1, "budhvar": 2,
               "budhwar": 2, "guruvar": 3, "guruwar": 3, "veervar": 3, "brihaspativar": 3,
               "shukravar": 4, "shukrawar": 4, "shanivar": 5, "shaniwar": 5, "ravivar": 6,
               "raviwar": 6, "itwar": 6, "itvar": 6, "tuesdays": 1}

_SUFFIX_UNITS = {"h": "hr", "m": "min", "s": "sec", "d": "day"}
_UNIT_SECS = {"sec": 1, "min": 60, "hr": 3600, "day": 86400, "wk": 604800}
_UNIT_RANK = {"wk": 5, "day": 4, "hr": 3, "min": 2, "sec": 1}
_UNITS = frozenset(_UNIT_SECS)
_PERIODS = ("morning", "afternoon", "evening", "night")

_NUM_RE = re.compile(r"\d+(?:\.\d+)?$")
_INT_RE = re.compile(r"\d+$")
_ORD_RE = re.compile(r"(\d+)th$")
_SPLIT_RE = re.compile(r"\d+(?:[.:]\d+)?|[A-Za-z']+|[ऀ-ॿ]+")
_AMPM_DOTS_RE = re.compile(r"(?<![a-z])([ap])\.m\.?(?![a-z])")


class _T:
    """One canonical token and the span of the original text it came from."""
    __slots__ = ("c", "s", "e", "kind")

    def __init__(self, c: str, s: int, e: int, kind: str = ""):
        self.c, self.s, self.e, self.kind = c, s, e, kind    # kind: "" | "en" | "hi" (number words)

    def __repr__(self):
        return f"{self.c}"


def _word_canon(p: str) -> Tuple[List[str], str]:
    """One Latin or Devanagari word (already folded) -> (canonical tokens, kind)."""
    if p in _SIMPLE:
        return [_SIMPLE[p]], ""
    if p == "tonight" or p == "tonite":
        return ["today", "night"], ""
    if p in _EN_UNITS:
        return [str(_EN_UNITS[p])], "en"
    if p in _HINGLISH_NUM:
        return [str(_HINGLISH_NUM[p])], "hi"
    if p in _WEEKDAY_EN:
        return ["wd%d" % _WEEKDAY_EN[p]], ""
    if p in _MONTH_EN:
        return (["mo%d" % _MONTH_EN[p]] if _MONTH_EN[p] else ["may"]), ""
    if p and "ऀ" <= p[0] <= "ॿ":
        wd = lang.hindi_weekday_index(p)
        if wd is not None and (p.endswith("वार") or p == lang.fold_hindi("इतवार")):
            return ["wd%d" % wd], ""
        mo = lang.hindi_month_number(p)
        if mo:
            return ["mo%d" % mo], ""
        num = lang.hindi_to_ascii_numbers(p)
        if num.isdigit():
            return [num], "hi"
    return [p], ""


def _canon(text: str) -> List[_T]:
    toks: List[_T] = []
    for m in re.finditer(r"\S+", text or ""):
        raw, s, e = m.group(0), m.start(), m.end()
        w = lang.fold_hindi(raw.strip(_PUNCT)).strip(_PUNCT)
        w = _AMPM_DOTS_RE.sub(r"\1m", w)
        if not w:
            continue
        pieces = _SPLIT_RE.findall(w)
        prev_digit = False
        for idx, p in enumerate(pieces):
            if p[0].isdigit():
                toks.append(_T(p, s, e))
                prev_digit = True
                continue
            p = p.strip("'")
            if not p:
                continue
            if prev_digit and p in ("st", "nd", "rd", "th") and toks and _INT_RE.match(toks[-1].c):
                toks[-1].c += "th"
                prev_digit = False
                continue
            if prev_digit and p in _SUFFIX_UNITS:
                toks.append(_T(_SUFFIX_UNITS[p], s, e))
                prev_digit = False
                continue
            prev_digit = False
            canon, kind = _word_canon(p)
            for c in canon:
                # English "twenty" + "five" -> 25
                if (kind == "en" and toks and toks[-1].kind == "en" and _INT_RE.match(toks[-1].c)
                        and _INT_RE.match(c) and int(toks[-1].c) in (20, 30, 40, 50, 60, 70, 80, 90)
                        and 1 <= int(c) <= 9):
                    toks[-1].c = str(int(toks[-1].c) + int(c))
                    toks[-1].e = e
                    continue
                toks.append(_T(c, s, e, kind))
    return _post(toks)


def _isnum(c: str) -> bool:
    return bool(_NUM_RE.match(c))


def _isint(c: str) -> bool:
    return bool(_INT_RE.match(c))


def _post(toks: List[_T]) -> List[_T]:
    """Multi-token fixes: 'day after tomorrow', 'may' as a month, Hindi 'do' as 2, 'N tarikh'."""
    out: List[_T] = []
    i = 0
    n = len(toks)
    while i < n:
        t = toks[i]
        if (t.c == "day" and i + 2 < n and toks[i + 1].c == "after" and toks[i + 2].c == "tomorrow"):
            out.append(_T("dayafter", t.s, toks[i + 2].e))
            i += 3
            continue
        if t.c == "may":
            nxt = toks[i + 1].c if i + 1 < n else ""
            prv = out[-1].c if out else ""
            if _isint(nxt) or _ORD_RE.match(nxt) or _isint(prv) or _ORD_RE.match(prv):
                t = _T("mo5", t.s, t.e)
        if t.c == "do" and i + 1 < n and toks[i + 1].c in ("baje", "bajkar", "min", "hr", "sec", "day"):
            t = _T("2", t.s, t.e, "hi")
        if t.c == "tarikh" and out and _isint(out[-1].c):
            out[-1].c += "th"
            out[-1].e = t.e
            i += 1
            continue
        out.append(t)
        i += 1
    return out


# ------------------------------------------------------------------ scanners (on canonical tokens)

def _c(toks: List[_T], i: int) -> str:
    return toks[i].c if 0 <= i < len(toks) else ""


def _qty(toks: List[_T], i: int) -> Optional[Tuple[float, int]]:
    """A quantity that stands before a unit word: '10', 'a', 'half an', 'डेढ़', 'साढ़े 3', 'a couple of' ..."""
    c = _c(toks, i)
    if _isnum(c):
        v, j = float(c), i + 1
        if _c(toks, j) == "and":                       # "2 and a half hours"
            k = j + 1
            if _c(toks, k) == "a":
                k += 1
            if _c(toks, k) == "half" and _c(toks, k + 1) in _UNITS:
                return v + 0.5, k + 1
            if _c(toks, k) == "quarter" and _c(toks, k + 1) in _UNITS:
                return v + 0.25, k + 1
        return v, j
    if c == "a":
        j = i + 1
        if _c(toks, j) == "couple":
            j += 1
            if _c(toks, j) == "of":
                j += 1
            return 2.0, j
        if _c(toks, j) == "half":
            return 0.5, j + 1
        if _c(toks, j) == "quarter":
            j += 1
            if _c(toks, j) == "of":
                j += 1
                if _c(toks, j) == "a":
                    j += 1
            return 0.25, j
        return 1.0, j
    if c == "couple":
        j = i + 1
        if _c(toks, j) == "of":
            j += 1
        return 2.0, j
    if c == "half":
        j = i + 1
        if _c(toks, j) == "a":
            j += 1
        return 0.5, j
    if c == "quarter":
        j = i + 1
        if _c(toks, j) == "of":
            j += 1
        if _c(toks, j) == "a":
            j += 1
        return 0.25, j
    if c == "dedh":
        return 1.5, i + 1
    if c == "dhai":
        return 2.5, i + 1
    if c == "sava":
        if _isnum(_c(toks, i + 1)):
            return float(_c(toks, i + 1)) + 0.25, i + 2
        return 1.25, i + 1
    if c == "sadhe":
        if _isnum(_c(toks, i + 1)):
            return float(_c(toks, i + 1)) + 0.5, i + 2
        return None
    if c == "paune":
        if _isnum(_c(toks, i + 1)):
            return float(_c(toks, i + 1)) - 0.25, i + 2
        return 0.75, i + 1
    return None


def _dur_core(toks: List[_T], i: int) -> Optional[Tuple[float, int]]:
    """'1 hr 30 min', 'a hr and a half', 'half a hr', 'dedh hr', '2 hr and 30 min' -> (seconds, end index)."""
    total, j, segs, last_rank = 0.0, i, 0, 99
    while True:
        jj = j + 1 if (segs and _c(toks, j) == "and") else j
        q = _qty(toks, jj)
        if q is None:
            break
        v, k = q
        # "three quarter of an hour"
        if _c(toks, k) == "quarter" and _c(toks, k + 1) == "of" and _c(toks, k + 2) == "a" \
                and _c(toks, k + 3) == "hr" and not segs:
            return v * 0.25 * 3600, k + 4
        u = _c(toks, k)
        if u not in _UNITS or _UNIT_RANK[u] >= last_rank:
            break
        total += v * _UNIT_SECS[u]
        last_rank = _UNIT_RANK[u]
        j = k + 1
        segs += 1
        if _c(toks, j) == "and":                        # "an hour and a half"
            k = j + 1
            if _c(toks, k) == "a":
                k += 1
            frac = {"half": 0.5, "quarter": 0.25}.get(_c(toks, k))
            if frac and _c(toks, k + 1) not in _UNITS:
                total += frac * _UNIT_SECS[u]
                return total, k + 1
    if not segs:
        return None
    return total, j


_FILLER_IN = ("about", "around", "roughly", "approximately", "just", "only")


def _dur_expr(toks: List[_T], i: int) -> Optional[Tuple[float, int, bool]]:
    """A duration with its Hindi/English markers. marked = it said 'in / after / from now / later /
    में / बाद', i.e. it points at a moment rather than just naming an amount of time."""
    j, marked = i, False
    if _c(toks, j) in ("in", "after"):
        j += 1
        marked = True
        while _c(toks, j) in _FILLER_IN:
            j += 1
    core = _dur_core(toks, j)
    if core is None:
        return None
    secs, k = core
    if _c(toks, k) == "from" and _c(toks, k + 1) == "now":
        k, marked = k + 2, True
    elif _c(toks, k) == "later":
        k, marked = k + 1, True
    elif marked and _c(toks, k) == "time":
        k += 1
    else:
        kk = k + 1 if _c(toks, k) == "ke" else k
        if _c(toks, kk) in ("mein", "baad", "me", "main"):
            k, marked = kk + 1, True
    return secs, k, marked


def _hour_at(toks: List[_T], j: int) -> Optional[Tuple[int, int]]:
    c = _c(toks, j)
    if _isint(c) and 0 <= int(c) <= 24:
        return int(c), j + 1
    return None


def _clock(h: int, m: int, ap: Optional[str] = None, mid: bool = False) -> dict:
    return {"h": h, "m": m, "ap": ap, "h24": h == 0 or h > 12, "mid": mid}


def _ampm_tail(toks: List[_T], j: int) -> Tuple[Optional[str], int]:
    if _c(toks, j) in ("am", "pm"):
        return _c(toks, j), j + 1
    if _c(toks, j) == "oclock":
        return None, j + 1
    return None, j


def _clock_at(toks: List[_T], i: int) -> Optional[Tuple[dict, int]]:
    j, pref = i, False
    if _c(toks, j) in ("at", "by", "around"):
        j, pref = j + 1, True
    c = _c(toks, j)
    if c == "noon":
        ck = _clock(12, 0)
        ck["h24"] = True
        return ck, j + 1
    if c == "midnight":
        return _clock(0, 0, mid=True), j + 1
    # half past five / quarter past 7 / quarter to 6 / twenty past 5 / ten to 6
    rel = None
    if c in ("half", "quarter") and _c(toks, j + 1) in ("past", "to"):
        rel = ({"half": 30, "quarter": 15}[c], _c(toks, j + 1), j + 2)
    elif _isint(c) and 1 <= int(c) <= 29 and _c(toks, j + 1) in ("past", "to") and \
            _isint(_c(toks, j + 2)):
        rel = (int(c), _c(toks, j + 1), j + 2)
    if rel:
        mins, word, k = rel
        if _c(toks, k) == "noon":
            hh, k = 12, k + 1
        else:
            r = _hour_at(toks, k)
            if not r or not 1 <= r[0] <= 12:
                return None
            hh, k = r
        if word == "to":
            hh, mins = (hh - 1) or 12, 60 - mins
        ap, k = _ampm_tail(toks, k)
        return _clock(hh, mins, ap), k
    # Hindi: साढ़े पाँच बजे / सवा पाँच बजे / पौने छह बजे / डेढ़ बजे / ढाई बजे / सवा बजे
    if c in ("sadhe", "sava", "paune", "dedh", "dhai"):
        if c in ("dedh", "dhai", "sava") and _c(toks, j + 1) == "baje":
            hh, mm = {"dedh": (1, 30), "dhai": (2, 30), "sava": (1, 15)}[c]
            return _clock(hh, mm), j + 2
        r = _hour_at(toks, j + 1)
        if r and c in ("sadhe", "sava", "paune") and 1 <= r[0] <= 12:
            hh, k = r
            if _c(toks, k) in _UNITS:
                return None
            if _c(toks, k) == "baje":
                k += 1
            elif not pref and _c(toks, k) in ("mein", "baad", "ke"):
                return None
            hh, mm = {"sadhe": (hh, 30), "sava": (hh, 15), "paune": ((hh - 1) or 12, 45)}[c]
            return _clock(hh, mm), k
        return None
    # 5:30 / 17:30 / 5.30 (+ pm / baje)
    mt = re.match(r"(\d{1,2})([:.])(\d{2})$", c)
    if mt:
        hh, mm = int(mt.group(1)), int(mt.group(3))
        nxt = _c(toks, j + 1)
        if hh <= 24 and mm < 60 and (mt.group(2) == ":" or pref or nxt in ("am", "pm", "baje", "oclock")):
            k = j + 1
            ap, k = _ampm_tail(toks, k)
            if _c(toks, k) == "baje" and ap is None:
                k += 1
            return _clock(hh, mm, ap), k
        return None
    # 5 pm / 5 baje / 5 o'clock / at 5 / 10 bajkar 30 min
    r = _hour_at(toks, j)
    if r:
        hh, k = r
        nxt = _c(toks, k)
        if nxt in ("am", "pm"):
            return _clock(hh, 0, nxt), k + 1
        if nxt in ("baje", "oclock"):
            return _clock(hh, 0), k + 1
        if nxt == "bajkar":
            k += 1
            mm = 0
            if _isint(_c(toks, k)) and _c(toks, k + 1) == "min" and int(_c(toks, k)) < 60:
                mm, k = int(_c(toks, k)), k + 2
            return _clock(hh, mm), k
        if pref and nxt not in _UNITS and nxt not in ("percent", "mein", "baad", "ke") and 1 <= hh <= 24:
            return _clock(hh, 0), k
    return None


def _skip_ko(toks: List[_T], j: int) -> int:
    return j + 1 if _c(toks, j) == "ko" else j


def _day_at(toks: List[_T], i: int) -> Optional[Tuple[dict, int]]:
    j, nxt_flag = i, False
    if _c(toks, j) == "on":
        j += 1
    if _c(toks, j) == "the":
        j += 1
    while _c(toks, j) in ("next", "this"):
        nxt_flag = nxt_flag or _c(toks, j) == "next"
        j += 1
    c = _c(toks, j)
    if c in ("today", "tomorrow", "dayafter"):
        return {"kind": "rel", "n": {"today": 0, "tomorrow": 1, "dayafter": 2}[c]}, _skip_ko(toks, j + 1)
    if re.match(r"wd[0-6]$", c):
        return {"kind": "wd", "wd": int(c[2]), "next": nxt_flag}, _skip_ko(toks, j + 1)
    mo = re.match(r"mo(\d+)$", c)
    if mo:                                                   # december 25 / december the 25th
        k = j + 1
        if _c(toks, k) == "the":
            k += 1
        d = _c(toks, k)
        om = _ORD_RE.match(d)
        day = int(om.group(1)) if om else (int(d) if _isint(d) else 0)
        if 1 <= day <= 31:
            return {"kind": "date", "d": day, "m": int(mo.group(1))}, _skip_ko(toks, k + 1)
        return None
    om = _ORD_RE.match(c)
    d = int(om.group(1)) if om else (int(c) if _isint(c) else 0)
    if 1 <= d <= 31:
        k = j + 1
        if _c(toks, k) == "of":
            k += 1
        mo2 = re.match(r"mo(\d+)$", _c(toks, k))
        if mo2:                                              # 25 december / the 25th of december
            return {"kind": "date", "d": d, "m": int(mo2.group(1))}, _skip_ko(toks, k + 1)
        if om:                                               # the 25th
            return {"kind": "date", "d": d, "m": None}, _skip_ko(toks, j + 1)
    return None


def _period_at(toks: List[_T], i: int) -> Optional[Tuple[dict, int]]:
    j, this = i, False
    c = _c(toks, j)
    if c == "in":
        j += 1
        if _c(toks, j) == "the":
            j += 1
    elif c == "this":
        j, this = j + 1, True
    elif c in ("at", "during", "of"):
        j += 1
        if _c(toks, j) == "the":
            j += 1
    elif c == "the":
        j += 1
    if _c(toks, j) in _PERIODS:
        return {"p": _c(toks, j), "this": this}, j + 1
    return None


# ------------------------------------------------------------------ chunks -> one moment

_L = lang        # `lang` is also a parameter name in describe_when()

_PERIOD_DEFAULT = {"morning": (9, 0), "afternoon": (15, 0), "evening": (18, 0), "night": (20, 0)}
_PERIOD_WINDOW = {"morning": (4, 12), "afternoon": (12, 17), "evening": (16, 21), "night": (19, 24)}


def _scan(toks: List[_T]) -> Tuple[List[dict], List[dict]]:
    """(chunks that point at a moment, bare durations like '10 minutes')."""
    chunks: List[dict] = []
    bare: List[dict] = []
    i, n = 0, len(toks)
    while i < n:
        r = _clock_at(toks, i)
        if r:
            chunks.append({"k": "clock", "d": r[0], "s": i, "e": r[1]})
            i = r[1]
            continue
        r = _dur_expr(toks, i)
        if r:
            secs, e, marked = r
            (chunks if marked else bare).append({"k": "dur", "d": secs, "s": i, "e": e})
            i = e
            continue
        r = _day_at(toks, i)
        if r:
            chunks.append({"k": "day", "d": r[0], "s": i, "e": r[1]})
            i = r[1]
            continue
        r = _period_at(toks, i)
        if r:
            chunks.append({"k": "period", "d": r[0], "s": i, "e": r[1]})
            i = r[1]
            continue
        i += 1
    return chunks, bare


def _select(chunks: List[dict], bare: List[dict], bare_ok: bool) -> Dict[str, dict]:
    first: Dict[str, dict] = {}
    for c in chunks:
        first.setdefault(c["k"], c)
    dur = first.get("dur")
    if dur:
        days_only = dur["d"] >= 86400 and dur["d"] % 86400 == 0
        if days_only and ("clock" in first or "period" in first):          # "in 2 days at 9"
            return {k: v for k, v in first.items() if k in ("dur", "clock", "period")}
        return {"dur": dur}
    if first:
        return first
    if bare_ok and bare:
        return {"dur": bare[0]}
    return {}


def _cand_times(clock: Optional[dict], period: Optional[str]) -> List[Tuple[int, int]]:
    if clock is None:
        return [_PERIOD_DEFAULT[period] if period else (9, 0)]
    h, m, ap = clock["h"], clock["m"], clock["ap"]
    if ap == "am":
        return [(h % 12, m)]
    if ap == "pm":
        return [((h % 12) + 12 if h <= 12 else h, m)]
    if clock["h24"]:
        return [(h % 24, m)]
    if period:
        if period == "morning":
            return [(h % 12, m)]
        if period in ("afternoon", "evening"):
            return [((h % 12) + 12, m)]
        if h == 12:
            return [(0, m)]
        return [(h + 12 if h >= 6 else h, m)]
    return [(h % 12, m), ((h % 12) + 12, m)]


def _at(d: date, hm: Tuple[int, int]) -> datetime:
    return datetime(d.year, d.month, d.day, hm[0], hm[1])


def _resolve(sel: Dict[str, dict], now: datetime, bias: Optional[str] = None) -> Optional[datetime]:
    dur = sel.get("dur")
    clock = sel["clock"]["d"] if "clock" in sel else None
    pdata = sel["period"]["d"] if "period" in sel else None
    period = pdata["p"] if pdata else None
    if dur and not (dur["d"] >= 86400 and (clock or period)):
        return now + timedelta(seconds=dur["d"])
    times = _cand_times(clock, period)
    today = now.date()
    if dur:
        spec: Optional[dict] = {"kind": "rel", "n": int(dur["d"] // 86400)}
    elif "day" in sel:
        spec = sel["day"]["d"]
    elif pdata and pdata["this"]:
        spec = {"kind": "rel", "n": 0}
    else:
        spec = None
    shift = timedelta(days=1) if (clock and clock["mid"] and spec is not None) else timedelta(0)

    def future(base: date) -> List[datetime]:
        ts = times
        if bias and clock and len(times) == 2:               # "7" with no AM/PM
            if bias == "am":                                 # alarms: "wake me up at 7" = 7 AM
                ts = [times[0]]
            elif bias == "day" and base > today:             # reminders on a later day: 5 = 5 PM, 9 = 9 AM
                ts = [times[1]] if (clock["h"] <= 6 or clock["h"] == 12) else [times[0]]
        return [c for c in (_at(base + shift, t) for t in ts) if c > now]

    if spec is None:                                        # earliest future among today / tomorrow
        for offs in (0, 1, 2):
            fut = future(today + timedelta(days=offs))
            if fut:
                return min(fut)
        return None
    kind = spec["kind"]
    if kind == "rel":
        base = today + timedelta(days=spec["n"])
        fut = future(base)
        if fut:
            return min(fut)
        allc = [_at(base + shift, t) for t in times]
        if period and clock is None and spec["n"] == 0:
            lo, hi = _PERIOD_WINDOW[period]
            if lo <= now.hour < hi:                          # "this evening" said at 7 PM
                return (now + timedelta(minutes=30)).replace(second=0, microsecond=0)
        return max(allc)
    if kind == "wd":
        start = today + timedelta(days=1 if spec["next"] else 0)
        d0 = start + timedelta(days=(spec["wd"] - start.weekday()) % 7)
        fut = future(d0)
        while not fut:
            d0 += timedelta(days=7)
            fut = future(d0)
        return min(fut)
    if kind == "date":
        day, month = spec["d"], spec["m"]
        y, mo = today.year, (month or today.month)
        for _ in range(60):
            if day <= calendar.monthrange(y, mo)[1]:
                d0 = date(y, mo, day)
                if d0 >= today:
                    fut = future(d0)
                    if fut:
                        return min(fut)
            if month:
                y += 1
            else:
                mo += 1
                if mo > 12:
                    mo, y = 1, y + 1
        return None
    return None


# ------------------------------------------------------------------ public API

def parse_duration(text: str) -> Optional[float]:
    """'ten min', 'an hour and a half', '1h30m', 'डेढ़ घंटा', 'aadha ghanta' -> seconds; None if no duration."""
    try:
        toks = _canon(text)
        for i in range(len(toks)):
            r = _dur_expr(toks, i)
            if r:
                return float(r[0])
    except Exception:
        return None
    return None


def parse_when(text: str, now: Optional[datetime] = None, bias: Optional[str] = None) -> Optional[datetime]:
    """A naive local datetime for a spoken time expression ('tomorrow at 9', 'शाम 5 बजे'), else None.
    bias (optional, for callers that know the context): None = earliest future AM/PM; "am" = an hour
    with no AM/PM means AM (alarms); "day" = on a later day 1-6 means PM and 7-11 means AM (reminders)."""
    try:
        now = now or datetime.now()
        chunks, bare = _scan(_canon(text))
        sel = _select(chunks, bare, True)
        return _resolve(sel, now, bias) if sel else None
    except Exception:
        return None


_DANGLING_END = re.compile(r"(?:\s+|^)(?:at|on|in|by|for|and|around|the|of|to|को|पर|में)\s*$", re.IGNORECASE)


def _tidy(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip(" \t,;:-–—")
    for _ in range(3):
        t2 = _DANGLING_END.sub("", t).strip(" \t,;:-–—")
        if t2 == t:
            break
        t = t2
    return t


def extract_when(text: str, now: Optional[datetime] = None,
                 bias: Optional[str] = None) -> Tuple[Optional[datetime], str]:
    """Finds the time expression inside a sentence: (datetime, sentence without it) or (None, text)."""
    try:
        now = now or datetime.now()
        toks = _canon(text)
        chunks, bare = _scan(toks)
        sel = _select(chunks, bare, False)
        if not sel:
            return None, text
        dt = _resolve(sel, now, bias)
        if dt is None:
            return None, text
        spans = sorted((toks[c["s"]].s, toks[c["e"] - 1].e) for c in sel.values())
        out, pos = [], 0
        for a, b in spans:
            if a < pos:
                a = pos
            out.append(text[pos:a])
            pos = max(pos, b)
        out.append(text[pos:])
        return dt, _tidy(" ".join(out))
    except Exception:
        return None, text


def describe_when(dt: datetime, now: Optional[datetime] = None, lang: Optional[str] = None) -> str:
    """Speech-friendly: 'in 20 minutes', 'today at 5 PM', 'tomorrow at 9 AM', 'on Monday at 3 PM',
    'on September 25 at 10 AM'; Hindi: '20 मिनट में', 'आज शाम 5 बजे', 'कल सुबह 9 बजे', 'सोमवार को दोपहर 3 बजे'."""
    L = lang if lang in ("en", "hi") else _L.current()
    now = now or datetime.now()
    delta = (dt - now).total_seconds()
    hi = L == "hi"
    if delta <= 0:
        return "अभी" if hi else "right now"
    if delta < 3600:
        secs = int(round(delta)) if delta < 60 else max(1, int(round(delta / 60))) * 60
        dur = _L.fmt_duration(secs, L)
        return f"{dur} में" if hi else f"in {dur}"
    t = _L.fmt_time(dt, L)
    dd = (dt.date() - now.date()).days
    if dd == 0:
        return f"आज {t}" if hi else f"today at {t}"
    if dd == 1:
        return f"कल {t}" if hi else f"tomorrow at {t}"
    if dd == 2:
        return f"परसों {t}" if hi else f"the day after tomorrow at {t}"
    if 3 <= dd <= 6:
        wd = (_L.WEEKDAYS_HI if hi else _L.WEEKDAYS_EN)[dt.weekday()]
        return f"{wd} को {t}" if hi else f"on {wd} at {t}"
    ds = _L.fmt_date(dt, L, with_year=dt.year != now.year, with_weekday=False)
    return f"{ds} को {t}" if hi else f"on {ds} at {t}"


def extract_duration(text: str) -> Tuple[Optional[float], str]:
    """The first duration inside a sentence, marked or not: (seconds, sentence without it) or (None, text).
    'set a pasta timer for 12 minutes' -> (720.0, 'set a pasta timer'). A duration word 'for' / 'of' / 'in' in front
    of it goes with it. Used by callers that know the sentence is about an amount of time (timers)."""
    try:
        toks = _canon(text)
        for i in range(len(toks)):
            r = _dur_expr(toks, i)
            if not r:
                continue
            secs, k, _marked = r
            a = i
            if a > 0 and toks[a - 1].c in ("for", "of", "in", "after", "within"):
                a -= 1
            start, end = toks[a].s, toks[k - 1].e
            return float(secs), _tidy(text[:start] + " " + text[end:])
    except Exception:
        return None, text
    return None, text
