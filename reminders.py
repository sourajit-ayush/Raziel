"""
reminders.py - timers, reminders and alarms that survive a restart and are announced out loud
(English and Hindi).

Everything that can be decided in code is decided here, before the LLM is asked:
  "set a timer for 10 minutes"       "remind me to call mom in 20 minutes"     "wake me up at 7"
  "what timers do I have"            "cancel all my reminders"                  "snooze for 10 minutes"
  "10 मिनट का टाइमर लगाओ"            "मुझे शाम 5 बजे दवाई की याद दिलाना"          "कल सुबह 7 बजे का अलार्म लगाओ"

Layout of this file
-------------------
  1. settings, texts (every sentence in en + hi), small helpers
  2. the SQLite table and the data API  (add / list_active / cancel / remaining / snooze_last / active_count)
  3. Scheduler: a daemon thread that announces due items exactly once (lock + mark-before-announce),
     waits while the assistant is busy, reports missed items, reschedules daily / weekday repeats
  4. the matcher try_handle() and the tool functions set_reminder / list_reminders / cancel_reminder
  5. hooks for other modules: briefing_line(), active_count()

Time is always injected (`now` parameters, Scheduler(now=...)), the database path comes from memory.connect(),
so tests use a fake clock, a fake announce() and a temporary database. Windows-only code (winsound) is imported
lazily inside _default_beep().

Settings (config.py, all optional, read with getattr):
  REMINDERS_ENABLED = True         master switch of the matcher (the scheduler keeps announcing what is stored)
  REMINDER_BEEP = True             three short beeps before an announcement (Windows only)
  REMINDER_LATE_SECONDS = 120      an item overdue by more than this is announced as "I missed a reminder from ..."
  TIMER_MAX_SECONDS = 172800       longest timer (2 days)
"""

from __future__ import annotations

import logging
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import config
import lang
import memory
import timeparse

logger = logging.getLogger("voice_assistant")

_L = lang            # `lang` is also a parameter name in add()

KINDS = ("timer", "reminder", "alarm")
REPEATS = ("", "daily", "weekdays")
PENDING_SECONDS = 60          # how long "When should I remind you?" waits for the answer
BUSY_MAX_WAIT = 600           # a due item waits at most this long for a busy assistant
KEEP_FIRED_SECONDS = 7 * 86400
MAX_ATTEMPTS = 3
SNOOZE_WINDOW = 3600          # "snooze" only makes sense soon after something fired
DEFAULT_SNOOZE_MINUTES = 10


def _cfg(name: str, default):
    return getattr(config, name, default)


def _enabled() -> bool:
    return bool(_cfg("REMINDERS_ENABLED", True))


def _now_ts() -> float:
    return time.time()


# ------------------------------------------------------------------ texts

_T: Dict[str, Dict[str, str]] = {
    # ---- confirmations
    "timer_set": {"en": "Okay, timer set for {d}.", "hi": "ठीक है, {d} का टाइमर लगा दिया।"},
    "timer_set_label": {"en": "Okay, {label} timer set for {d}.",
                        "hi": "ठीक है, {label} का टाइमर {d} के लिए लगा दिया।"},
    "timer_long": {"en": "That's too long for a timer. I can go up to {m}. A reminder might suit that better.",
                   "hi": "टाइमर के लिए यह बहुत लंबा है। मैं ज़्यादा से ज़्यादा {m} का टाइमर लगा सकती हूँ। शायद रिमाइंडर बेहतर रहेगा।"},
    "timer_ask": {"en": "For how long?", "hi": "कितनी देर के लिए?"},
    "alarm_set": {"en": "Okay, alarm set for {when}.", "hi": "ठीक है, {when} का अलार्म लगा दिया।"},
    "alarm_in": {"en": "Okay, alarm set to go off {when}.", "hi": "ठीक है, अलार्म {when} बजेगा।"},
    "alarm_set_rep": {"en": "Okay, alarm set for {when}, {rep}.", "hi": "ठीक है, {when} का अलार्म लगा दिया, {rep}।"},
    "alarm_ask": {"en": "What time should I set the alarm for?", "hi": "अलार्म कितने बजे के लिए लगाऊँ?"},
    "remind_set": {"en": "I'll remind you {what} {when}.", "hi": "ठीक है, {when} मैं आपको याद दिलाऊँगी: {what}।"},
    "remind_set_rep": {"en": "I'll remind you {what} {when}, {rep}.",
                       "hi": "ठीक है, {when} से मैं आपको {rep} याद दिलाऊँगी: {what}।"},
    "remind_ask": {"en": "When should I remind you?", "hi": "कब याद दिलाऊँ?"},
    "remind_ask_rep": {"en": "At what time should I remind you?", "hi": "किस समय याद दिलाऊँ?"},
    "time_passed": {"en": "That time has already passed. Tell me a time in the future.",
                    "hi": "वह समय तो निकल चुका है। कोई आने वाला समय बताइए।"},
    "no_time": {"en": "I didn't catch the time. When should I remind you?",
                "hi": "मुझे समय समझ नहीं आया। कब याद दिलाऊँ?"},
    "repeat_unsupported": {"en": "I can only repeat reminders every day or on weekdays.",
                           "hi": "मैं रिमाइंडर सिर्फ़ रोज़ या सोमवार से शुक्रवार तक दोहरा सकती हूँ।"},
    "failed": {"en": "Sorry, I couldn't do that. Please try again.",
               "hi": "माफ़ कीजिए, मैं यह नहीं कर पाई। कृपया दोबारा कोशिश कीजिए।"},
    "rep_daily": {"en": "every day", "hi": "रोज़"},
    "rep_weekdays": {"en": "on weekdays", "hi": "सोमवार से शुक्रवार"},
    # ---- spoken when an item fires
    "timer_done": {"en": "Your {d} timer is done.", "hi": "आपका {d} का टाइमर पूरा हो गया।"},
    "timer_done_long": {"en": "Your timer for {d} is done.", "hi": "आपका {d} का टाइमर पूरा हो गया।"},
    "timer_done_label": {"en": "Your {label} timer is done.", "hi": "आपका {label} का टाइमर पूरा हो गया।"},
    "reminder_due": {"en": "Reminder: {what}.", "hi": "याद दिलाना: {what}।"},
    "alarm_due": {"en": "It's {t}. Time to wake up.", "hi": "अभी {t} हैं। उठने का समय हो गया।"},
    "missed_timer": {"en": "I missed your {name} from {ago}.", "hi": "{ago} का आपका {name} छूट गया था।"},
    "missed_reminder": {"en": "I missed a reminder from {ago}: {what}.", "hi": "{ago} का एक रिमाइंडर छूट गया था: {what}।"},
    "missed_alarm": {"en": "I missed your {t} alarm from {ago}.", "hi": "{ago} का आपका {t} का अलार्म छूट गया था।"},
    "ago": {"en": "{d} ago", "hi": "{d} पहले"},
    # ---- lists and questions
    "none_timer": {"en": "You have no timers running.", "hi": "आपका कोई टाइमर नहीं चल रहा है।"},
    "none_reminder": {"en": "You have no reminders.", "hi": "आपका कोई रिमाइंडर नहीं है।"},
    "none_alarm": {"en": "You have no alarms set.", "hi": "आपका कोई अलार्म सेट नहीं है।"},
    "left_one": {"en": "Your {name} has {left} left.", "hi": "आपके {name} में {left} बचे हैं।"},
    "left_many": {"en": "You have {n} timers. {items}.", "hi": "आपके {n} टाइमर चल रहे हैं। {items}।"},
    "left_item": {"en": "the {name} has {left} left", "hi": "{name} में {left} बचे हैं"},
    "list_one_reminder": {"en": "You have one reminder: {items}.", "hi": "आपका एक रिमाइंडर है: {items}।"},
    "list_many_reminder": {"en": "You have {n} reminders: {items}.", "hi": "आपके {n} रिमाइंडर हैं: {items}।"},
    "list_one_alarm": {"en": "You have one alarm: {items}.", "hi": "आपका एक अलार्म लगा है: {items}।"},
    "list_many_alarm": {"en": "You have {n} alarms: {items}.", "hi": "आपके {n} अलार्म लगे हैं: {items}।"},
    "list_many_timer": {"en": "You have {n} timers: {items}.", "hi": "आपके {n} टाइमर हैं: {items}।"},
    "more": {"en": "{list}, and {n} more", "hi": "{list}, और {n} अन्य"},
    "none_all": {"en": "You have no timers, reminders or alarms.", "hi": "आपका कोई टाइमर, रिमाइंडर या अलार्म नहीं है।"},
    "remind_set_nowhat": {"en": "I'll remind you {when}.", "hi": "ठीक है, {when} मैं आपको याद दिलाऊँगी।"},
    "brief_one": {"en": "You have one reminder today: {items}.", "hi": "आज आपका एक रिमाइंडर है: {items}।"},
    "brief_many": {"en": "You have {n} reminders today: {items}.", "hi": "आज आपके {n} रिमाइंडर हैं: {items}।"},
    # ---- cancel / snooze
    "cancel_one_timer": {"en": "Okay, timer cancelled.", "hi": "ठीक है, टाइमर रद्द कर दिया।"},
    "cancel_one_timer_label": {"en": "Okay, {label} timer cancelled.", "hi": "ठीक है, {label} टाइमर रद्द कर दिया।"},
    "cancel_one_reminder": {"en": "Okay, reminder cancelled.", "hi": "ठीक है, रिमाइंडर रद्द कर दिया।"},
    "cancel_one_alarm": {"en": "Okay, alarm cancelled.", "hi": "ठीक है, अलार्म रद्द कर दिया।"},
    "cancel_many_timer": {"en": "Okay, cancelled {n} timers.", "hi": "ठीक है, {n} टाइमर रद्द कर दिए।"},
    "cancel_many_reminder": {"en": "Okay, cancelled {n} reminders.", "hi": "ठीक है, {n} रिमाइंडर रद्द कर दिए।"},
    "cancel_many_alarm": {"en": "Okay, cancelled {n} alarms.", "hi": "ठीक है, {n} अलार्म रद्द कर दिए।"},
    "cancel_nomatch": {"en": "I couldn't find a {kind} matching {sel}.", "hi": "{sel} वाला कोई {kind} नहीं मिला।"},
    "cancel_any": {"en": "Okay, cancelled.", "hi": "ठीक है, रद्द कर दिया।"},
    "snoozed": {"en": "Okay, snoozed for {d}.", "hi": "ठीक है, {d} के लिए स्नूज़ कर दिया।"},
    "nothing_snooze": {"en": "I don't have anything to snooze right now.", "hi": "अभी स्नूज़ करने के लिए कुछ नहीं है।"},
}

_KIND_WORD = {"timer": ("timer", "टाइमर"), "reminder": ("reminder", "रिमाइंडर"), "alarm": ("alarm", "अलार्म")}


def _t(key: str, L: str, **kw) -> str:
    return _L.tr(_T, key, lang=L, **kw)


# ------------------------------------------------------------------ small helpers

def _norm_kind(kind) -> str:
    k = str(kind or "").strip().lower()
    for base in KINDS:
        if k.startswith(base):
            return base
    return "reminder" if k else ""


def _tokens(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+|[ऀ-ॿ]+", _L.fold_hindi(text or ""))


def _fmt_dur(seconds: float, L: str) -> str:
    """lang.fmt_duration, but a 10 minute 30 second timer keeps its seconds (fmt_duration drops them after 10 minutes)."""
    total = max(0, int(round(seconds)))
    rem = total % 60
    if total > 600 and rem:
        base = _L.fmt_duration(total - rem, L)
        return base + (f" {rem} सेकंड" if L == "hi" else f" {rem} second{'s' if rem != 1 else ''}")
    return _L.fmt_duration(total, L)


def _dur_adj(seconds: float, L: str) -> Optional[str]:
    """'10 minute' for a round single-unit duration (used as 'your 10 minute timer'), else None."""
    total = int(round(seconds))
    if total >= 86400 and total % 86400 == 0:
        return f"{total // 86400} day"
    if total >= 3600 and total % 3600 == 0:
        return f"{total // 3600} hour"
    if total >= 60 and total % 60 == 0:
        return f"{total // 60} minute"
    if 0 < total < 60:
        return f"{total} second"
    return None


def _ago(seconds: float, L: str) -> str:
    total = max(60, int(round(seconds / 60.0)) * 60)
    if total >= 6 * 3600:
        total = int(round(total / 3600.0)) * 3600
    return _t("ago", L, d=_fmt_dur(total, L))


def _second_person(text: str, L: str) -> str:
    """'call my mom' -> 'call your mom' (the reminder is spoken back to the user)."""
    if L == "hi":
        table = {"मेरा": "आपका", "मेरी": "आपकी", "मेरे": "आपके", "मुझे": "आपको", "मुझसे": "आपसे"}
        return " ".join(table.get(w, w) for w in (text or "").split())
    table = {"my": "your", "myself": "yourself", "mine": "yours"}
    return re.sub(r"\b(my|myself|mine)\b", lambda m: table[m.group(1).lower()], text or "", flags=re.I)


_ABOUT_FIRST = frozenset({"the", "a", "an", "my", "your", "our", "this", "that", "these", "those", "his", "her",
                          "their", "meeting", "appointment", "dentist", "doctor", "birthday", "exam", "interview",
                          "deadline", "flight", "train", "class", "lecture", "anniversary", "game", "match"})


def _connective(what: str) -> str:
    """'to call mom' or 'about the meeting' (English reminders read better with the right small word)."""
    first = (what or "").split(" ", 1)[0].lower()
    if first in _ABOUT_FIRST or first.isdigit():
        return "about"
    return "to"


def _hi_infinitive(text: str) -> str:
    """'मम्मी को फोन करने की' -> 'मम्मी को फोन करना' (what a Hindi speaker says after 'याद दिलाना:')."""
    words = (text or "").split()
    while words and words[-1] in ("की", "के", "का", "को"):
        words.pop()
    if words and words[-1].endswith("ने") and len(words[-1]) > 3:
        words[-1] = words[-1][:-2] + "ना"
    return " ".join(words)


def _when_phrase(dt: datetime, now: datetime, L: str) -> str:
    """describe_when, but 'today at 5 PM' reads 'at 5 PM' after 'remind you to call mom'."""
    p = timeparse.describe_when(dt, now, L)
    if L == "en" and p.startswith("today at "):
        return p[len("today "):]
    return p


def _from_ts(ts: float) -> datetime:
    return datetime.fromtimestamp(ts)


# ------------------------------------------------------------------ the table and the data API

_SCHEMA = """CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT, text TEXT, label TEXT, due REAL, created REAL, repeat TEXT, lang TEXT,
    done INTEGER DEFAULT 0, fired_at REAL)"""

_lock = threading.RLock()          # database writers + scheduler claims + the pending state


def _connect() -> sqlite3.Connection:
    conn = memory.connect()
    conn.execute(_SCHEMA)
    return conn


def _row(r) -> Dict[str, object]:
    return {k: r[k] for k in r.keys()}


def add(kind: str, text: str, due_ts: float, repeat: str = "", label: str = "", lang: Optional[str] = None,
        created: Optional[float] = None) -> int:
    """Stores one timer / reminder / alarm and returns its id. `created` defaults to the real clock (tests pass
    their fake one); for timers due - created is the length of the timer."""
    kind = _norm_kind(kind) or "reminder"
    repeat = repeat if repeat in REPEATS else ""
    L = lang if lang in ("en", "hi") else _L.current()
    now = float(created) if created is not None else _now_ts()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO reminders (kind, text, label, due, created, repeat, lang, done, fired_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)",
                (kind, (text or "").strip(), (label or "").strip(), float(due_ts), now, repeat, L))
            rid = cur.lastrowid
            conn.execute("DELETE FROM reminders WHERE done = 1 AND fired_at IS NOT NULL AND fired_at < ?",
                         (now - KEEP_FIRED_SECONDS,))
            conn.commit()
        finally:
            conn.close()
    logger.info("reminders: added %s #%s %r label=%r due=%s repeat=%r lang=%s", kind, rid, text, label,
                _from_ts(due_ts).strftime("%Y-%m-%d %H:%M:%S"), repeat, L)
    return rid


def get(rid: int) -> Optional[Dict[str, object]]:
    try:
        conn = _connect()
        try:
            r = conn.execute("SELECT * FROM reminders WHERE id = ?", (rid,)).fetchone()
        finally:
            conn.close()
        return _row(r) if r else None
    except Exception:
        logger.exception("reminders: get failed")
        return None


def list_active(kind: str = "") -> List[Dict[str, object]]:
    """Every item that has not fired yet, soonest first (optionally only one kind). [] on any database error."""
    try:
        k = _norm_kind(kind) if kind else ""
        with _lock:
            conn = _connect()
            try:
                if k:
                    rows = conn.execute("SELECT * FROM reminders WHERE done = 0 AND kind = ? ORDER BY due, id",
                                        (k,)).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM reminders WHERE done = 0 ORDER BY due, id").fetchall()
            finally:
                conn.close()
        return [_row(r) for r in rows]
    except Exception:
        logger.exception("reminders: list_active failed")
        return []


def active_count() -> int:
    """How many items are waiting. Never raises."""
    try:
        with _lock:
            conn = _connect()
            try:
                return int(conn.execute("SELECT COUNT(*) FROM reminders WHERE done = 0").fetchone()[0])
            finally:
                conn.close()
    except Exception:
        return 0


_SEL_STOP = frozenset("""the my a an all of to about for that this every any please reminder reminders timer timers alarm
alarms one me i have set on at it and remind reminding से का की के को सब सारे सभी मेरा मेरी मेरे एक""".split())


def _time_tokens(row: Dict[str, object]) -> List[str]:
    try:
        dt = _from_ts(float(row["due"]))
        return _tokens(_L.fmt_time(dt, "en")) + _tokens(_L.fmt_time(dt, "hi"))
    except Exception:
        return []


def _selector_matches(row: Dict[str, object], sel: str) -> bool:
    words = [w for w in _tokens(sel) if w not in _SEL_STOP]
    if not words:
        return True
    hay = set(_tokens(f"{row.get('label') or ''} {row.get('text') or ''}")) | set(_time_tokens(row))
    hay_text = " ".join(_tokens(f"{row.get('label') or ''} {row.get('text') or ''}"))
    return all(w in hay or (len(w) >= 3 and w in hay_text) for w in words)


def cancel(selector: str = "", kind: str = "") -> List[Dict[str, object]]:
    """Deletes active items and returns them. Empty selector = every item of that kind (or of every kind when
    `kind` is empty too); otherwise items whose id, label or words match the selector."""
    sel = str(selector or "").strip()
    k = _norm_kind(kind) if kind else ""
    with _lock:
        rows = list_active(k)
        if sel:
            by_id = [r for r in rows if sel.isdigit() and int(sel) == r["id"]]
            rows = by_id or [r for r in rows if _selector_matches(r, sel)]
        if not rows:
            return []
        conn = _connect()
        try:
            conn.executemany("DELETE FROM reminders WHERE id = ? AND done = 0", [(r["id"],) for r in rows])
            conn.commit()
        finally:
            conn.close()
    logger.info("reminders: cancelled %d item(s) selector=%r kind=%r ids=%s", len(rows), sel, k,
                [r["id"] for r in rows])
    return rows


def remaining(kind: str = "timer", now: Optional[float] = None) -> List[Tuple[str, float]]:
    """[(label or text, seconds left)] for the active items of one kind, soonest first."""
    now_ts = float(now) if now is not None else _now_ts()
    return [(str(r["label"] or r["text"] or ""), max(0.0, float(r["due"]) - now_ts))
            for r in list_active(kind)]


def snooze_last(minutes, now: Optional[float] = None) -> Optional[Dict[str, object]]:
    """Re-adds the item that fired last, due in `minutes`. Returns the new item, None if nothing has fired."""
    try:
        mins = float(minutes)
    except (TypeError, ValueError):
        mins = DEFAULT_SNOOZE_MINUTES
    if mins <= 0:
        mins = DEFAULT_SNOOZE_MINUTES
    now_ts = float(now) if now is not None else _now_ts()
    with _lock:
        conn = _connect()
        try:
            last = conn.execute("SELECT * FROM reminders WHERE done = 1 AND fired_at IS NOT NULL "
                                "ORDER BY fired_at DESC, id DESC LIMIT 1").fetchone()
            if not last:
                return None
            last = _row(last)
            # a second "snooze" replaces the first snooze instead of stacking a copy
            conn.execute("DELETE FROM reminders WHERE done = 0 AND kind = ? AND text = ? AND label = ? "
                         "AND created >= ? AND id > ?",
                         (last["kind"], last["text"], last["label"], last["fired_at"], last["id"]))
            conn.commit()
        finally:
            conn.close()
        rid = add(str(last["kind"]), str(last["text"]), now_ts + mins * 60.0, "", str(last["label"] or ""),
                  str(last["lang"] or "en"), created=now_ts)
    logger.info("reminders: snoozed %s #%s for %.0f minutes as #%s", last["kind"], last["id"], mins, rid)
    return get(rid)


def last_fired(now: Optional[float] = None) -> Optional[Dict[str, object]]:
    """The item that fired most recently, only if that was within SNOOZE_WINDOW seconds."""
    now_ts = float(now) if now is not None else _now_ts()
    try:
        conn = _connect()
        try:
            r = conn.execute("SELECT * FROM reminders WHERE done = 1 AND fired_at IS NOT NULL "
                             "ORDER BY fired_at DESC, id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if r and now_ts - float(r["fired_at"]) <= SNOOZE_WINDOW:
            return _row(r)
    except Exception:
        logger.exception("reminders: last_fired failed")
    return None


# ------------------------------------------------------------------ what is spoken when an item fires

def _timer_seconds(row: Dict[str, object]) -> float:
    return max(1.0, float(row["due"]) - float(row["created"] or row["due"]))


def _timer_name(row: Dict[str, object], L: str) -> str:
    """'pasta timer' / '10 minute timer' (Hindi: 'पास्ता टाइमर' / '10 मिनट का टाइमर')."""
    label = str(row.get("label") or "").strip()
    secs = _timer_seconds(row)
    if L == "hi":
        return f"{label} टाइमर" if label else f"{_fmt_dur(secs, 'hi')} का टाइमर"
    if label:
        return f"{label} timer"
    adj = _dur_adj(secs, "en")
    return f"{adj} timer" if adj else f"timer for {_fmt_dur(secs, 'en')}"


def announce_text(row: Dict[str, object], now_ts: float, missed: bool = False) -> str:
    """The sentence Raziel says when `row` fires, in the language stored with the item."""
    L = row.get("lang") if row.get("lang") in ("en", "hi") else "en"
    kind = str(row.get("kind") or "reminder")
    ago = _ago(now_ts - float(row["due"]), L)
    if kind == "timer":
        if missed:
            return _t("missed_timer", L, name=_timer_name(row, L), ago=ago)
        label = str(row.get("label") or "").strip()
        secs = _timer_seconds(row)
        if label:
            return _t("timer_done_label", L, label=label)
        if L == "hi":
            return _t("timer_done", L, d=_fmt_dur(secs, "hi"))
        adj = _dur_adj(secs, "en")
        if adj:
            return _t("timer_done", L, d=adj)
        return _t("timer_done_long", L, d=_fmt_dur(secs, "en"))
    if kind == "alarm":
        t = _L.fmt_time(_from_ts(float(row["due"])), L)
        if missed:
            return _t("missed_alarm", L, t=t, ago=ago)
        return _t("alarm_due", L, t=t)
    what = _second_person(str(row.get("text") or "").strip(), L)
    if not what:
        what = "यह आपका रिमाइंडर है" if L == "hi" else "this is your reminder"
    if missed:
        return _t("missed_reminder", L, ago=ago, what=what)
    return _t("reminder_due", L, what=what)


def _next_due(due: float, repeat: str, now_ts: float) -> Optional[float]:
    """The next daily / weekday occurrence of `due` that lies after now (same wall-clock time)."""
    dt = _from_ts(due)
    for _ in range(800):
        dt = dt + timedelta(days=1)
        if repeat == "weekdays" and dt.weekday() >= 5:
            continue
        ts = dt.timestamp()
        if ts > now_ts:
            return ts
    return None


def _default_beep() -> None:
    """Three short beeps on Windows, nothing anywhere else."""
    if sys.platform != "win32":
        return
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 150)
            time.sleep(0.08)
    except Exception:
        logger.debug("reminders: beep failed", exc_info=True)


# ------------------------------------------------------------------ the scheduler

class Scheduler:
    """
    Announces due items. announce(text) speaks a sentence (may block while it is spoken); is_busy() says whether
    the assistant is in the middle of a conversation / speaking (the item then waits, at most BUSY_MAX_WAIT
    seconds); beep() plays the attention sound (default: _default_beep when config.REMINDER_BEEP). `now` is the
    clock (seconds since the epoch), injected so tests can move time by hand.

    Exactly once: an item is claimed with UPDATE ... WHERE done = 0 under a lock BEFORE anything is spoken, so two
    ticks racing each other (or two Scheduler objects) cannot both announce it. If announce() raises, the claim is
    given back and the item is retried on the next tick, at most MAX_ATTEMPTS times in all.
    """

    def __init__(self, announce: Callable[[str], object], is_busy: Optional[Callable[[], bool]] = None,
                 beep: Optional[Callable[[], object]] = None, poll: float = 1.0,
                 now: Callable[[], float] = time.time,
                 guard: Optional[Callable[[float], object]] = None):
        self._announce = announce
        self._is_busy = is_busy
        self._beep = beep
        self._guard = guard          # guard(seconds): mute the microphone for that long (covers the beep)
        self._poll = max(0.05, float(poll))
        self._now = now
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._state = threading.Lock()
        self._attempts: Dict[int, int] = {}
        self._blocked: Dict[int, float] = {}       # id -> moment the busy wait is counted from
        self._delayed: set = set()                # ids that waited for a quiet moment (so not "missed")

    # -- lifecycle
    def start(self) -> None:
        with self._state:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="reminders-scheduler", daemon=True)
            self._thread.start()
        logger.info("reminders: scheduler started (poll %.2fs)", self._poll)

    def stop(self) -> None:
        self._stop.set()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout=3.0)
        self._thread = None
        logger.info("reminders: scheduler stopped")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("reminders: scheduler tick failed")
            self._stop.wait(self._poll)

    # -- one pass
    def tick(self) -> int:
        """Announces everything that is due now. Returns how many items were announced."""
        fired = 0
        try:
            now_ts = float(self._now())
            with _lock:
                conn = _connect()
                try:
                    rows = [_row(r) for r in conn.execute(
                        "SELECT * FROM reminders WHERE done = 0 AND due <= ? ORDER BY due, id", (now_ts,))]
                finally:
                    conn.close()
        except Exception:
            logger.exception("reminders: could not read due items")
            return 0
        for row in rows:
            try:
                if self._fire_one(row, float(self._now())):
                    fired += 1
            except Exception:
                logger.exception("reminders: firing #%s failed", row.get("id"))
        return fired

    def _busy(self) -> bool:
        if self._is_busy is None:
            return False
        try:
            return bool(self._is_busy())
        except Exception:
            logger.exception("reminders: is_busy() raised, assuming not busy")
            return False

    def _do_beep(self) -> None:
        fn = self._beep
        if fn is None and _cfg("REMINDER_BEEP", True):
            fn = _default_beep
        if fn is None:
            return
        try:
            fn()
        except Exception:
            logger.exception("reminders: beep failed")

    def _fire_one(self, row: Dict[str, object], now_ts: float) -> bool:
        rid = int(row["id"])
        late = float(_cfg("REMINDER_LATE_SECONDS", 120))
        overdue = now_ts - float(row["due"])
        if self._busy():
            ref = self._blocked.get(rid)
            if ref is None:
                ref = float(row["due"]) if overdue <= late else now_ts
                self._blocked[rid] = ref
                if overdue <= late:
                    self._delayed.add(rid)
                logger.info("reminders: %s #%s is due but the assistant is busy, waiting", row["kind"], rid)
            if now_ts - ref <= BUSY_MAX_WAIT:
                return False
            logger.info("reminders: %s #%s waited %.0fs for a quiet moment, announcing anyway", row["kind"], rid,
                        now_ts - ref)
        next_id = None
        with _lock:
            conn = _connect()
            try:
                cur = conn.execute("UPDATE reminders SET done = 1, fired_at = ? WHERE id = ? AND done = 0",
                                   (now_ts, rid))
                if cur.rowcount != 1:
                    conn.commit()
                    return False                                   # somebody else already took it
                if row.get("repeat") in ("daily", "weekdays"):
                    nxt = _next_due(float(row["due"]), str(row["repeat"]), now_ts)
                    if nxt is not None:
                        c2 = conn.execute(
                            "INSERT INTO reminders (kind, text, label, due, created, repeat, lang, done, fired_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, NULL)",
                            (row["kind"], row["text"], row["label"], nxt, now_ts, row["repeat"], row["lang"]))
                        next_id = c2.lastrowid
                conn.commit()
            finally:
                conn.close()
        missed = overdue > late and rid not in self._delayed
        text = announce_text(row, now_ts, missed)
        logger.info("reminders: firing %s #%s (%s) -> %r", row["kind"], rid,
                    "missed" if missed else "on time", text)
        if self._guard is not None:
            try:
                self._guard(1.5)                       # the beeps must not be heard as the user talking
            except Exception:
                logger.debug("reminders: guard failed", exc_info=True)
        self._do_beep()
        try:
            self._announce(text)
        except Exception:
            n = self._attempts.get(rid, 0) + 1
            self._attempts[rid] = n
            logger.exception("reminders: announce failed for #%s (attempt %d of %d)", rid, n, MAX_ATTEMPTS)
            if n < MAX_ATTEMPTS:
                with _lock:
                    conn = _connect()
                    try:
                        conn.execute("UPDATE reminders SET done = 0, fired_at = NULL WHERE id = ?", (rid,))
                        if next_id is not None:
                            conn.execute("DELETE FROM reminders WHERE id = ? AND done = 0", (next_id,))
                        conn.commit()
                    finally:
                        conn.close()
            else:
                logger.error("reminders: giving up on #%s after %d failed announcements", rid, n)
            return False
        self._attempts.pop(rid, None)
        self._blocked.pop(rid, None)
        self._delayed.discard(rid)
        return True


# ------------------------------------------------------------------ the matcher: text preparation

def _hx(pattern: str) -> str:
    """Hindi patterns are written in natural spelling and folded the way the transcript is (no anusvara / nukta).
    Never use \\b or \\w on Hindi words: matras are not \\w, so use whitespace lookarounds instead."""
    return _L.fold_hindi(pattern)


def _prep(text: str) -> str:
    """Lower-case, Hindi spelling folded, punctuation gone (':' and '.' kept inside numbers), one space between words."""
    t = (text or "").replace("’", "'").replace("‘", "'").replace("`", "'")
    t = _L.fold_hindi(t)
    t = re.sub(r"(?<![a-z])([ap])\s?\.\s?m\b\.?", r"\1m ", t)
    t = re.sub(r"(?<!\d)[:.]|[:.](?!\d)", " ", t)
    t = re.sub(r"[^\w\sऀ-ॿ':.]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


_LEAD_EN = re.compile(
    r"^(?:(?:hey|hi|hello|okay|ok|so|alright|well|um+|uh+|oh|raziel|razil|please|kindly|now|and|also|just|actually|"
    r"then|you know)\s+|(?:can|could|would|will) you(?: please| kindly)?\s+|would you mind\s+|"
    r"(?:i|we) (?:want|need|would like|'d like|wanna|want you|need you|would like you|'d like you) (?:you )?to\s+|"
    r"i wanna\s+|go ahead and\s+|let's\s+|lets\s+|you can\s+|you should\s+)+")
_TRAIL_EN = re.compile(r"\s+(?:please|for me|now|right now|thanks|thank you|okay|ok|will you|can you)$")
_LEAD_HI = re.compile(_hx(
    r"^(?:(?:रजील|राजील|रेजील|रेजियल|हे|ओके|ठीक है|अच्छा|सुनो|सुनिए|कृपया|जरा|प्लीज|क्या आप|क्या तुम|एक काम करो|और|तो|फिर)"
    r"\s+)+"))
_TRAIL_HI = re.compile(_hx(r"\s+(?:कृपया|प्लीज|जरा|अभी|फटाफट|जल्दी|धन्यवाद|शुक्रिया)$"))


def _strip_fillers(t: str) -> str:
    for _ in range(4):
        before = t
        t = _LEAD_EN.sub("", t, count=1).strip()
        t = _LEAD_HI.sub("", t, count=1).strip()
        t = _TRAIL_EN.sub("", t, count=1).strip()
        t = _TRAIL_HI.sub("", t, count=1).strip()
        if t == before:
            break
    return t


_HINGLISH_RE = re.compile(r"\b(?:laga ?do|lagao|laga de|laga dena|karo|kar do|kar dena|ke liye|yaad dila\w*|baje|mujhe|"
                          r"batao|bata do|band karo|rad karo|shuru karo|dikhao|sunao|kaun se|kitne|kitna)\b")


def _language_of(raw: str) -> str:
    """The language to answer in: Devanagari or clear Hinglish = Hindi, else English."""
    try:
        if _L.detect(raw) == "hi":
            return "hi"
        if _HINGLISH_RE.search((raw or "").lower()):
            return "hi"
    except Exception:
        pass
    return "en"


def _as_dt(now) -> datetime:
    if isinstance(now, datetime):
        return now
    if isinstance(now, (int, float)):
        return _from_ts(float(now))
    return _from_ts(_now_ts())


def _unfold(words: str, raw: str) -> str:
    """Puts the original spelling (capitals, Hindi nasal marks) back into words taken from the folded text."""
    orig: Dict[str, str] = {}
    for w in re.findall(r"[^\s]+", raw or ""):
        core = w.strip(".,!?;:।॥\"'()[]{}“”‘’-–—…")
        if core:
            orig.setdefault(_prep(core), core)
    return " ".join(orig.get(x, x) for x in (words or "").split())


# ---- repeat words ("every day", "on weekdays", "रोज़")

_DAY_NAMES = "monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
_NUM_WORDS = "one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|a|an|other|\\d+"
_REP_WEEKDAYS = re.compile("|".join([
    r"(?:every|each|on|all)\s+(?:week ?days?|working days?|business days?)",
    r"week ?days",
    r"(?:monday|mon) (?:to|through|thru|till|until) (?:friday|fri)",
    _hx(r"हर\s+(?:वीकडे|वीकडेज|कार्यदिवस|वर्किंग डे|वर्किंग डेज)"),
    _hx(r"सोमवार\s+से\s+शुक्रवार(?:\s+तक)?"),
    _hx(r"(?:सप्ताह|हफ्ते)\s+के\s+(?:हर\s+)?कार्यदिवस"),
]))
_REP_DAILY = re.compile("|".join([
    r"every ?day", r"everyday", r"daily", r"each day", r"every single day",
    r"every (?=(?:morning|afternoon|evening|night)\b)", r"each (?=(?:morning|afternoon|evening|night)\b)",
    _hx(r"(?<![^\s])हर\s+(?:रोज|दिन)(?![^\s])"), _hx(r"(?<![^\s])रोजाना(?![^\s])"),
    _hx(r"(?<![^\s])प्रतिदिन(?![^\s])"), _hx(r"(?<![^\s])रोज(?![^\s])"),
    _hx(r"(?<![^\s])हर\s+(?=(?:सुबह|शाम|रात|दोपहर)(?:\s|$))"),
    r"\broz(?:ana)?\b", r"\bhar roz\b", r"\bhar din\b",
]))
_REP_OTHER = re.compile("|".join([
    rf"every\s+(?:(?:{_NUM_WORDS})\s+)?(?:minutes?|hours?|seconds?|weeks?|months?|years?)\b",
    rf"every\s+(?:other|{_DAY_NAMES})\b",
    rf"(?:each|every)\s+(?:week|month|year)\b",
    _hx(r"हर\s+(?:(?:\d+|दो|तीन|चार|पाँच|पांच)\s+)?(?:घंटे|घंटा|घण्टे|मिनट|हफ्ते|हफ्ता|महीने|महीना|साल|सोमवार|मंगलवार|बुधवार|"
        r"गुरुवार|शुक्रवार|शनिवार|रविवार|इतवार)"),
]))


def _extract_repeat(t: str) -> Tuple[str, str]:
    """(text without the repeat words, '' | 'daily' | 'weekdays' | 'other')."""
    if _REP_OTHER.search(t):
        return re.sub(r"\s+", " ", _REP_OTHER.sub(" ", t)).strip(), "other"
    if _REP_WEEKDAYS.search(t):
        return re.sub(r"\s+", " ", _REP_WEEKDAYS.sub(" ", t)).strip(), "weekdays"
    if _REP_DAILY.search(t):
        return re.sub(r"\s+", " ", _REP_DAILY.sub(" ", t)).strip(), "daily"
    return t, ""


# ---- answers to "when?" / "for how long?"

_FILLER_WORDS = frozenset("""at in on for around about by make it set so say lets let's like um uh the how what please okay ok
remind me again then to after and maybe let that would be said sure yes yeah alright just is its it's then
में बजे के लिए बाद रखो कर करो दो दीजिए दीजिये रख को पर लगा सेट अभी ठीक है हाँ हां हा ओके फिर से तक कीजिए बोलो
ke liye baje mein baad karo kar do""".split())


_FILLER_FOLDED = frozenset(_L.fold_hindi(f) for f in _FILLER_WORDS)


def _filler_only(rest: str) -> bool:
    return all(w in _FILLER_FOLDED for w in _tokens(rest))


def _answer_when(t: str, now: datetime, bias: Optional[str]) -> Optional[datetime]:
    """The time in an answer like 'at 5 pm', 'tomorrow morning', 'in 20 minutes', '20 minutes', 'शाम 5 बजे' - only
    when nothing else of substance is in the sentence."""
    dt, rest = timeparse.extract_when(t, now, bias=bias)
    if dt is None:
        secs, rest = timeparse.extract_duration(t)
        if not secs:
            return None
        dt = now + timedelta(seconds=secs)
    return dt if _filler_only(rest) else None


def _answer_duration(t: str) -> Optional[float]:
    secs, rest = timeparse.extract_duration(t)
    if secs and secs > 0 and _filler_only(rest):
        return secs
    return None


# ---- the question waiting for its answer

_pending: Optional[Dict[str, object]] = None


def _set_pending(**kw) -> None:
    global _pending
    with _lock:
        _pending = dict(kw)


def _clear_pending() -> Optional[Dict[str, object]]:
    """Takes the waiting question (None if there is none or it timed out)."""
    global _pending
    with _lock:
        p, _pending = _pending, None
    return p


def pending_state() -> Optional[Dict[str, object]]:
    """The question Raziel is waiting for an answer to (tests / debugging)."""
    with _lock:
        return dict(_pending) if _pending else None


# ------------------------------------------------------------------ the matcher: which kind of thing is meant

_NOUN_RES = (
    ("timer", re.compile(_hx(r"^(?:timers?|टा[इईय]?मर[^\s]*)$"))),
    ("alarm", re.compile(_hx(r"^(?:alarms?|[अएआ]ला(?:र्म|रम)[^\s]*)$"))),
    ("reminder", re.compile(_hx(r"^(?:reminders?|र[िी]मा[इई](?:न्)?डर[^\s]*)$"))),
)


def _kind_of_token(tok: str) -> str:
    for kind, rx in _NOUN_RES:
        if rx.match(tok):
            return kind
    return ""


def _find_noun(words: List[str]) -> Tuple[int, str]:
    for i, w in enumerate(words):
        k = _kind_of_token(w)
        if k:
            return i, k
    return -1, ""


_QUICK = re.compile(_hx(
    r"timer|alarm|remind|wake me|get me up|snooze|more (?:minutes?|mins?|hours?)|another|count ?down|time (?:is )?left|"
    r"time remaining|how long is left|how much time|left on|yaad dila|"
    r"टा[इईय]?मर|[अएआ]ला(?:र्म|रम)|र[िी]मा[इई]|याद\s+दिला|जगा|उठा|स्नूज|कितना\s+(?:समय|वक्त|टाइम)|"
    r"बचा|बाकी|और\s+\d|\d\s+और|(?:मिनट|घंटे?)\s+और|और\s+[^\s]+\s+(?:मिनट|घंटे?)|थोड़ी\s+देर"))


_DET_PRE = frozenset("the my a an all of that this these those every any our your both".split())
_HI_STOP = frozenset(_L.fold_hindi(w) for w in """सब सारे सभी सारी तमाम मेरे मेरा मेरी अपना अपने अपनी ये यह वो वह इस उस वाला वाली वाले का की
के को से में एक जो है हैं सेट लगा लगे लगी लगाया लगाओ करो कर दो दीजिए कीजिए करें करना देना दें दे बद रद्द कैसल कैसिल कैन्सिल कैन्सल हटा हटाओ मिटा मिटाओ
डिलीट रोक रोको खत्म आज कृपया मुझे प्लीज""".split())
_HI_CANCEL_EXACT = frozenset(_L.fold_hindi(w) for w in
                             "बंद बन्द बदकर रद्द रद्द कैंसल कैसल कैंसिल कैन्सिल कैन्सल खत्म समाप्त डिलीट".split())
_HIN_CANCEL = re.compile(r"\b(?:band|rad|hata|mita|cancel|delete|rok)\s+(?:kar|karo|do|de|dena|kardo)\b")
_HI_CANCEL_RE = re.compile(_hx(r"^(?:हटा|मिटा|डिलीट|रोक|निकाल|कैन्?सि?ल|rad|hata|mita|rok)"))
_HI_LIST_RE = re.compile(_hx(r"^(?:बता|दिखा|सुना|पढ|देख|चेक|लिस्ट|batao|bata|dikhao|dikha|sunao)"))
_HI_ALL = frozenset(_L.fold_hindi(w) for w in "सब सारे सभी सारी तमाम all".split())


def _is_hindi_sentence(t: str) -> bool:
    return _L.has_devanagari(t) or bool(_HINGLISH_RE.search(t))


def _is_hi_cancel_word(w: str) -> bool:
    return w in _HI_CANCEL_EXACT or bool(_HI_CANCEL_RE.match(w))


# ------------------------------------------------------------------ handlers (each returns a sentence or None)

def _kind_phrase(kind: str, plural: bool, L: str) -> str:
    en, hi = _KIND_WORD[kind]
    return hi if L == "hi" else (en + ("s" if plural else ""))


_EN_CANCEL = re.compile(r"^(?:cancel|delete|remove|stop|clear|turn off|switch off|shut off|dismiss|kill|discard|"
                        r"disable|get rid of|silence)\s+(?P<body>.+)$")
_EN_STOP_REMINDING = re.compile(r"^stop reminding me(?: (?:to|about|of|that))?\s+(?P<x>.+)$")
_EN_POST_OK = re.compile(r"^(?:for|to|about|at|on|of|that|called|named|labeled|labelled|from|in|with|i|which|please)\b")


def _handle_cancel(t: str, words: List[str], L: str) -> Optional[str]:
    kind, sel, every = "", "", False
    m = _EN_STOP_REMINDING.match(t)
    if m:
        kind, sel = "reminder", m.group("x")
    else:
        m = _EN_CANCEL.match(t)
        if m:
            body = m.group("body")
            bw = body.split()
            i, k = _find_noun(bw)
            if i < 0 or i > 6:
                return None
            pre, post = bw[:i], " ".join(bw[i + 1:])
            if post and not _EN_POST_OK.match(post):
                return None
            kind = k
            every = any(w in ("all", "every", "everything", "both") for w in pre)
            sel = "" if every else " ".join(w for w in pre if w not in _DET_PRE) + " " + post
        else:
            if "मत" in words:
                return None
            i, k = _find_noun(words)
            if i < 0 or not (_is_hindi_sentence(t)):
                return None
            if not (any(_is_hi_cancel_word(w) for w in words) or _HIN_CANCEL.search(t)):
                return None
            if any(_HI_LIST_RE.match(w) for w in words):
                return None
            kind = k
            every = any(w in _HI_ALL for w in words)
            sel = "" if every else " ".join(w for w in words if w not in _HI_STOP and w not in _HI_CANCEL_EXACT
                                            and not _is_hi_cancel_word(w) and not _kind_of_token(w))
    sel = sel.strip()
    _matched("cancel kind=%s selector=%r all=%s" % (kind, sel, every))
    rows = cancel(sel, kind)
    if rows:
        return _cancel_reply(rows, kind, L)
    if sel and list_active(kind):
        return _t("cancel_nomatch", L, kind=_kind_phrase(kind, False, L), sel=sel)
    return _t("none_" + kind, L)


def _cancel_reply(rows: List[Dict[str, object]], kind: str, L: str) -> str:
    n = len(rows)
    if n == 1:
        if kind == "timer" and str(rows[0].get("label") or "").strip():
            return _t("cancel_one_timer_label", L, label=str(rows[0]["label"]).strip())
        return _t("cancel_one_" + kind, L)
    return _t("cancel_many_" + kind, L, n=n)


_tl = threading.local()


def _matched(what: str) -> None:
    """A handler recognised the request: log it, and remember it so an exception later becomes an apology."""
    _tl.matched = True
    logger.info("reminders: matched %s", what)


# ---- lists and "how much is left"

_K = r"(?P<k>timers?|alarms?|reminders?)"
_TAILQ = (r"(?: (?:do|does|are|is|i|i've|have|got|set|running|active|pending|scheduled|today|right now|now|currently|"
          r"coming up|for today|left|going|on|still|we|you|there|created|upcoming))*")
_LAB = r"(?:(?P<lab>[a-z0-9 ]+?) )?"
_MY = r"(?:(?:my|the|that|this|our) )*"
_LIST_RES = [re.compile(p) for p in (
    rf"(?:what|which)(?: are| is)?(?: (?:my|the|all|any|of my|of the))* {_K}{_TAILQ}",
    rf"(?:list|show|read|tell|check|give|display|see)(?: me)?(?: (?:all|out|of|about))*(?: (?:my|the|all|any|our))*"
    rf"(?: (?:active|current|pending|upcoming|running|scheduled))? {_K}{_TAILQ}",
    rf"(?:do|have) i (?:have|got|set) (?:any|an?) (?:(?:active|running|pending) )?{_K}{_TAILQ}",
    rf"(?:is|are) there (?:any|an?) {_K}{_TAILQ}",
    rf"(?:any|how many) {_K}{_TAILQ}",
    rf"(?:my|the|all my|all) (?P<k>timers|alarms|reminders){_TAILQ}",
    rf"what(?:'s| is) on my {_K}(?: list)?",
    rf"how (?:much|long) (?:time )?(?:is )?(?:left|remaining|to go)(?: (?:on|for|in|of|till|until|before) {_MY}{_LAB}(?P<k>timer|alarm))?",
    rf"how (?:much|long) (?:time )?(?:do i have|have i got|does (?:it|my timer) have) left(?: (?:on|for|in) {_MY}{_LAB}(?P<k>timer|alarm))?",
    rf"how (?:much time|long) (?:until|till|before|to) {_MY}{_LAB}(?P<k>timer|alarm)"
    rf"(?: (?:goes off|rings|ends|is done|finishes|is up|beeps|buzzes|expires|go off|ring|end))?",
    rf"when (?:does|will|is|do) {_MY}{_LAB}(?P<k>timer|alarm)"
    rf"(?: (?:go off|goes off|ring|rings|end|ends|due|expire|expires|finish|finishes|set for|going off|ringing|done|ready))?",
    rf"(?:time|how much time) (?:is )?(?:left|remaining)(?: (?:on|for))? {_MY}{_LAB}(?P<k>timer)",
    rf"(?:check|status of|what's the status of)(?: (?:on|of))? {_MY}(?P<k>timers?)",
    rf"is {_MY}{_LAB}(?P<k>timer) (?:still )?(?:running|going|on)",
)]


def _handle_list(t: str, words: List[str], L: str, now: datetime) -> Optional[str]:
    kind, lab, ok = "", "", False
    for rx in _LIST_RES:
        m = rx.fullmatch(t)
        if m:
            gd = m.groupdict()
            kind = _kind_of_token(gd.get("k") or "") or "timer"
            lab = (gd.get("lab") or "").strip()
            lab = " ".join(w for w in lab.split() if w not in _DET_PRE)
            ok = True
            break
    if not ok:
        i, k = _find_noun(words)
        hi_query = False
        if not _is_hindi_sentence(t):
            return None
        if i >= 0:
            has_verb = any(_HI_LIST_RE.match(w) for w in words)
            has_q = any(w in ("कौन", "कितने", "कितना", "कोई", "कब", "kaun", "kitne") for w in words)
            setting = any(w.startswith(("लगा", "सेट", "बना", "बजेगा")) and w not in ("लगे", "लगी") for w in words) \
                and not has_verb
            if (has_verb or has_q) and not setting and "कैसे" not in words and "kaise" not in words:
                if not any(_is_hi_cancel_word(w) for w in words) and timeparse.parse_when(t, now) is None:
                    kind, hi_query = k, True
            if not hi_query and re.search(_hx(r"कब\s+बज"), t):
                kind, hi_query = k, True
        elif re.search(_hx(r"कितना\s+(?:समय|वक्त|टाइम)?\s*(?:बचा|बाकी|रह गया|रहा)"), t) or \
                re.search(_hx(r"(?:समय|वक्त|टाइम)\s+(?:कितना\s+)?(?:बचा|बाकी)"), t):
            kind, hi_query = "timer", True
        if not hi_query:
            return None
    _matched("list kind=%s label=%r" % (kind, lab))
    now_ts = now.timestamp()
    if kind == "timer":
        rows = list_active("timer")
        if lab:
            rows = [r for r in rows if _selector_matches(r, lab)]
            if not rows:
                return _t("none_timer", L)
        return _timers_reply(rows, L, now_ts)
    return _list_reply(list_active(kind), kind, L, now)


def _timers_reply(rows: List[Dict[str, object]], L: str, now_ts: float) -> str:
    if not rows:
        return _t("none_timer", L)
    items = [(_timer_name(r, L), _L.fmt_duration(max(0.0, float(r["due"]) - now_ts), L)) for r in rows[:4]]
    if len(rows) == 1:
        name = items[0][0]
        if L == "hi" and name.endswith(" का टाइमर"):
            name = name[:-len(" का टाइमर")] + " के टाइमर"
        return _t("left_one", L, name=name, left=items[0][1])
    if L == "hi":
        items = [(n[:-len(" का टाइमर")] + " के टाइमर" if n.endswith(" का टाइमर") else n, lf) for n, lf in items]
    parts = [_t("left_item", L, name=n, left=lf) for n, lf in items]
    text = _L.join_list(parts, L)
    text = text[:1].upper() + text[1:] if L == "en" else text
    if len(rows) > len(items):
        text = _t("more", L, list=text, n=len(rows) - len(items))
    return _t("left_many", L, n=len(rows), items=text)


def _list_reply(rows: List[Dict[str, object]], kind: str, L: str, now: datetime) -> str:
    if not rows:
        return _t("none_" + kind, L)
    shown = rows[:5]
    parts = []
    for r in shown:
        when = _when_phrase(_from_ts(float(r["due"])), now, L)
        if kind == "reminder":
            what = _second_person(str(r.get("text") or ""), L)
            parts.append((f"{what} {when}" if what else when).strip())
        else:
            parts.append(when)
    text = _L.join_list(parts, L)
    if len(rows) > len(shown):
        text = _t("more", L, list=", ".join(parts), n=len(rows) - len(shown))
    key = ("list_one_" if len(rows) == 1 else "list_many_") + ("reminder" if kind == "reminder" else "alarm")
    if kind == "timer":
        key = "list_many_timer"
    return _t(key, L, n=len(rows), items=text)


# ---- snooze

_NUMW = r"(?:\d+|a|one|two|three|four|five|six|ten|fifteen|twenty|thirty|forty five|forty)"
_SNOOZE_RES = (
    re.compile(r"snooze(?: it| that| this| the (?:alarm|reminder|timer))?(?: for)?(?: (?P<d>.+))?"),
    re.compile(r"(?:remind me|ring|wake me|alert me|ping me|tell me|buzz me|call me)(?: up)? "
               r"(?:again|later|in a bit|in a while)(?: (?:in|after|for) (?P<d>.+))?"),
    re.compile(rf"(?:(?:give me|gimme|i need|let me have|just|can i have|i want|i'll need) )*(?P<d>{_NUMW}(?: and a half)?) "
               rf"more (?P<u>minutes?|mins?|hours?)(?: please)?"),
    re.compile(rf"(?:(?:give me|i need|just|can i have) )?another (?P<d>{_NUMW}) (?P<u>minutes?|mins?|hours?)"),
)
_HI_SNOOZE_WORD = re.compile(_hx(r"(?<![^\s])(?:स्नूज[^\s]*|snooze)(?![^\s])"))


def _handle_snooze(t: str, words: List[str], L: str, now: datetime) -> Optional[str]:
    secs: Optional[float] = None
    explicit = False
    matched = False
    for i, rx in enumerate(_SNOOZE_RES):
        m = rx.fullmatch(t)
        if not m:
            continue
        d, u = m.groupdict().get("d"), m.groupdict().get("u")
        if u:
            secs = timeparse.parse_duration(f"{d} {u}")
        elif d:
            secs = timeparse.parse_duration(d)
            if secs is None:
                continue
        matched, explicit = True, i == 0
        break
    if not matched:
        if _HI_SNOOZE_WORD.search(t) and _is_hindi_sentence(t):
            secs, matched, explicit = timeparse.parse_duration(t), True, True
        elif re.search(_hx(r"(?:फिर|दोबारा|दुबारा|फिरसे)"), t) and re.search(_hx(r"याद\s+दिला"), t):
            secs, matched = timeparse.parse_duration(t), True
        else:
            m = re.fullmatch(_hx("(?:(?:अभी )?और)") + r"\s+(?P<d>.+)|(?P<e>.+)\s+" + _hx("और"), t)
            if m:
                secs = timeparse.parse_duration(m.group("d") or m.group("e"))
                matched = secs is not None
            elif re.fullmatch(_hx(r"(?:थोड़ी|थोडी) देर बाद(?: फिर)?(?: याद दिला\w*)?"), t):
                matched = True
    if not matched:
        return None
    fired = last_fired(now.timestamp())
    if fired is None:
        if not explicit:
            return None                                # "5 more minutes" with nothing to snooze is not ours
        logger.info("reminders: snooze asked but nothing fired recently")
        return _t("nothing_snooze", L)
    minutes = (secs or DEFAULT_SNOOZE_MINUTES * 60) / 60.0
    _matched("snooze %.1f minutes" % minutes)
    new = snooze_last(minutes, now.timestamp())
    if new is None:
        return _t("nothing_snooze", L)
    return _t("snoozed", L, d=_fmt_dur(minutes * 60, L))


# ---- creating things (shared by the matcher, the pending answers and the tool functions)

def _create_timer(secs: float, label: str, L: str, now: datetime) -> str:
    mx = float(_cfg("TIMER_MAX_SECONDS", 172800))
    if secs > mx:
        return _t("timer_long", L, m=_fmt_dur(mx, L))
    secs = max(1.0, float(secs))
    now_ts = now.timestamp()
    label = (label or "").strip()
    add("timer", label, now_ts + secs, label=label, lang=L, created=now_ts)
    d = _fmt_dur(secs, L)
    return _t("timer_set_label", L, label=label, d=d) if label else _t("timer_set", L, d=d)


def _create_alarm(dt: datetime, rep: str, L: str, now: datetime) -> str:
    if dt <= now:
        return _t("time_passed", L)
    add("alarm", "", dt.timestamp(), repeat=rep, lang=L, created=now.timestamp())
    desc = timeparse.describe_when(dt, now, L)
    if desc.startswith("in ") or desc.endswith(" में"):
        return _t("alarm_in", L, when=desc)
    when = desc[len("today at "):] if (L == "en" and desc.startswith("today at ")) else desc
    if rep:
        return _t("alarm_set_rep", L, when=when, rep=_t("rep_" + rep, L))
    return _t("alarm_set", L, when=when)


def _create_reminder(what: str, dt: datetime, rep: str, L: str, now: datetime) -> str:
    if dt <= now:
        return _t("time_passed", L)
    what = (what or "").strip()
    add("reminder", what, dt.timestamp(), repeat=rep, lang=L, created=now.timestamp())
    when = _when_phrase(dt, now, L)
    if L == "hi":
        if not what:
            return _t("remind_set_nowhat", L, when=when)
        if rep:
            return _t("remind_set_rep", L, when=when, rep=_t("rep_" + rep, L), what=what)
        return _t("remind_set", L, when=when, what=what)
    core = f"{_connective(what)} {_second_person(what, 'en')} " if what else ""
    if rep:
        return _t("remind_set_rep", L, what=core.strip(), when=when, rep=_t("rep_" + rep, L)).replace("  ", " ")
    return _t("remind_set", L, what=core.strip(), when=when).replace("  ", " ")


# ---- timers and alarms: recognising the words around the noun

_EN_SETV = frozenset("set start create make begin run put add get give need want turn launch".split())
_EN_OK = _EN_SETV | frozenset("me us up a an the my another new one please now i would like to can could will you and it "
                              "that so on some also just".split())
_HI_VERBS = frozenset(_L.fold_hindi(w) for w in """लगा लगाओ लगाइए लगादो लगादीजिए लगाना लगाए लगाइये सेट करो कर कीजिए कीजिये करें करना
करदो बना बनाओ बनादो बनाइए चालू शुरू स्टार्ट चला चलाओ चलादो रख रखो रखदो दो दीजिए दीजिये दें दे देना सकते सकती सकता सकोगे सके पाएंगे
पाओगे""".split())
_HI_OK = _HI_VERBS | frozenset(_L.fold_hindi(w) for w in "मुझे एक का की के लिए मेरा मेरे मेरी नया नई कृपया अभी जरा मैं हमें आप है हैं हो".split())
_HING_VERBS = frozenset("laga lagao do de dena karo kar dijiye dijie set shuru chalu start kardo lagado".split())
_HING_OK = _HING_VERBS | frozenset("ke ka ki liye ek mujhe mera meri mere naya please zara ji".split())
_ALL_VERBS = _EN_SETV | _HI_VERBS | _HING_VERBS | {"wake"}
_ALL_OK = _EN_OK | _HI_OK | _HING_OK


def _has_setverb(words: List[str]) -> bool:
    return any(w in _ALL_VERBS or w.startswith(("लगा", "सेट", "बना")) for w in words)


_GENITIVE = frozenset(_L.fold_hindi(w) for w in "का की के वाला वाली वाले".split()) | {"ka", "ki", "ke", "wala", "wali"}
_QUESTION_WORDS = frozenset("how what why when where who which whose if whether".split())


def _frame(words: List[str], idx: int) -> Optional[Tuple[str, bool]]:
    """Checks the words around the noun at `idx`: (label, has_setting_verb), or None when the sentence carries other
    content ('the timer for the pasta is ready', 'how do I set a timer'). A label is the unknown words that stand
    directly before the noun ('set a PASTA timer') or after 'for' / 'called' ('timer for the OVEN')."""
    if any(w in _QUESTION_WORDS for w in words):
        return None
    left, right = words[:idx], words[idx + 1:]
    while left and left[-1] in _GENITIVE:                 # "पास्ता का टाइमर": the label is the word before का
        left = left[:-1]
    j = len(left)
    while j > 0 and left[j - 1] not in _ALL_OK and left[j - 1] not in _DET_PRE:
        j -= 1
    label_words = left[j:]
    if any(w not in _ALL_OK and w not in _DET_PRE for w in left[:j]):
        return None
    if len(right) > 1 and right[0] in ("for", "called", "named", "labeled", "labelled"):
        label_words = label_words + [w for w in right[1:] if w not in _DET_PRE]
    elif any(w not in _ALL_OK and w not in _DET_PRE for w in right):
        return None
    if len(label_words) > 3:
        return None
    label = " ".join(label_words)
    if label.isdigit():
        label = ""
    return label, _has_setverb(words)


_COUNTDOWN = re.compile(r"(?:(?:start|set) )?(?:a )?count ?down(?: from| for| of)? (?P<d>.+)")


def _handle_timer(t: str, words: List[str], L: str, now: datetime, raw: str) -> Optional[str]:
    m = _COUNTDOWN.fullmatch(t)
    if m:
        secs = timeparse.parse_duration(m.group("d"))
        if not secs:
            return None
        _matched("timer (countdown) %.0f s" % secs)
        return _create_timer(secs, "", L, now)
    secs, rest = timeparse.extract_duration(t)
    if secs is None:
        m = re.fullmatch(r"(?:(?:set|start|create|make) )?(?:a |the |my )?timer (?:for )?(\d{1,3})", t)
        if m and int(m.group(1)) > 0:                  # "set a timer for 10": a bare number means minutes
            secs, rest = int(m.group(1)) * 60.0, "timer"
    rwords = rest.split()
    idx, kind = _find_noun(rwords)
    if kind != "timer":
        return None
    if any(_kind_of_token(w) not in ("", "timer") for w in rwords):
        return None
    fr = _frame(rwords, idx)
    if fr is None:
        return None
    label, has_verb = fr
    if label and timeparse.parse_when(label, now) is not None:
        return None                                    # "timer for tomorrow" is not a timer called "tomorrow"
    label = _unfold(label, raw)
    if secs is None:
        if not has_verb:
            return None
        _matched("timer without a duration -> asking")
        _set_pending(kind="timer", await_="duration", label=label, repeat="", lang=L,
                     expires=now.timestamp() + PENDING_SECONDS)
        return _t("timer_ask", L)
    _matched("timer %.0f s label=%r" % (secs, label))
    return _create_timer(secs, label, L, now)


_WAKE_RE = re.compile(r"^(?:wake|get) me(?: up)?(?: |$)")
_FOR_BEFORE_TIME = re.compile(r"\bfor (?=(?:\d{1,2}(?::\d{2})?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
                              r"twelve|noon|midnight)\b(?!\s*(?:min|mins|minutes?|hours?|hrs?|sec|seconds?|days?|weeks?)))")
_BARE_AFTER_ALARM = re.compile(r"\b(alarms?) (?=\d{1,2}(?::\d{2})?\b(?! ?(?:min|mins|minutes?|hours?|hrs?|sec|seconds?)))")
_ALARM_OK = _ALL_OK | frozenset("wake up get ring go off will in at on the morning".split())


def _alarm_prep(t: str) -> str:
    t = _FOR_BEFORE_TIME.sub("at ", t)
    return _BARE_AFTER_ALARM.sub(r"\1 at ", t)


def _handle_alarm(t: str, words: List[str], L: str, now: datetime, raw: str) -> Optional[str]:
    idx, kind = _find_noun(words)
    wake = bool(_WAKE_RE.match(t))
    hi_wake = (_L.fold_hindi("मुझे") in words) and any(w.startswith(("जगा", "उठा")) for w in words)
    if kind != "alarm" and not wake and not hi_wake:
        return None
    if kind not in ("", "alarm") or any(_kind_of_token(w) not in ("", "alarm") for w in words):
        return None
    t1, rep = _extract_repeat(t)
    t2 = _alarm_prep(t1)
    dt, rest = timeparse.extract_when(t2, now, bias="am")
    rwords = rest.split()
    if any(not (w in _ALARM_OK or _kind_of_token(w) == "alarm" or w.startswith(("जगा", "उठा"))) for w in rwords):
        return None
    if rep == "other":
        _matched("alarm with an unsupported repeat")
        return _t("repeat_unsupported", L)
    if dt is None:
        if kind == "alarm" and _has_setverb(rwords):
            _matched("alarm without a time -> asking")
            _set_pending(kind="alarm", await_="when", repeat=rep, lang=L, expires=now.timestamp() + PENDING_SECONDS)
            return _t("alarm_ask", L)
        return None
    _matched("alarm at %s repeat=%r" % (dt.strftime("%Y-%m-%d %H:%M"), rep))
    return _create_alarm(dt, rep, L, now)


# ---- reminders

_REM_START = re.compile(
    r"^(?:remind me|(?:set|create|make|add|schedule|put|put in|start|new)(?: me| us)?(?: up)?"
    r"(?: (?:a|an|the|my|another|new|one))* (?:new )?reminder|reminder|(?:give|get) me (?:a|an) reminder|"
    r"i need a reminder|i want a reminder)(?: |$)")
_LATER_WORDS = frozenset(["later", "again", "in a bit", "in a while", "again later", "soon", "sometime", "some time"])
_QUESTION_START = re.compile(r"^(?:how|what|why|when|where|who|which|if|whether|of|is|are|do|does|did)\b")
_OTHER_MODULE_RE = re.compile(r"\b(?:to ?do list|todo list|shopping list|grocery list|my list|the list|calendar|my notes?)\b")
_PAST_RE = re.compile(r"\b(?:yesterday|ago|last (?:night|week|month|year)|earlier today)\b")
_EN_LEAD_JUNK = re.compile(r"^(?:(?:for|on|at|in|by|around|about|that|to|of|me|and|then|so|it)\s+)+")
_EN_TRAIL_JUNK = re.compile(r"(?:\s+(?:please|for me|again|then|okay|thanks))+$")
_HI_FRAME_RES = [re.compile(_hx(p)) for p in (
    r"(?<![^\s])याद\s+दिला[^\s]*(?:\s+(?:दो|देना|दीजिए|दीजिये|दें|देंगे|सकते|सकती|सकता|सकोगे|हैं|है|जाना|पाओगे|दोगे|दोगी))*(?![^\s])",
    r"(?<![^\s])र[िी]मा[इई](?:न्)?डर[^\s]*(?:\s+(?:सेट|लगा[^\s]*|बना[^\s]*|कर[^\s]*|जोड[^\s]*|जोड़[^\s]*|डाल[^\s]*|रख[^\s]*|"
    r"कीजि[^\s]*|दो|दीजिए))*",
    r"(?<![^\s])(?:मुझे|मैं|प्लीज|कृपया|जरा|फिर से|दोबारा|अभी|चाहता|चाहती|चाहिए|चाहते|हूँ|है|हैं)(?![^\s])",
    r"\byaad dila\w*(?: (?:do|dena|dijiye|de))?", r"\b(?:mujhe|karo|kar do)\b",
)]
_HI_EDGE = frozenset(_L.fold_hindi(w) for w in "की के का को लिए ताकि कि से पर और तो".split()) | {"ki", "ka", "ke"}


def _clean_what_hi(what: str) -> str:
    for rx in _HI_FRAME_RES:
        what = rx.sub(" ", what)
    words = what.split()
    while words and words[0] in _HI_EDGE:
        words.pop(0)
    while words and words[-1] in _HI_EDGE:
        words.pop()
    return " ".join(words)


def _clean_what_en(what: str) -> str:
    what = _EN_LEAD_JUNK.sub("", what.strip())
    what = _EN_TRAIL_JUNK.sub("", what)
    return what.strip()


_HI_SETVERB_TOK = ("लगा", "सेट", "बना", "जोड", "डाल", "रख", "कर")


def _handle_reminder(t: str, words: List[str], L: str, now: datetime, raw: str) -> Optional[str]:
    m = _REM_START.match(t)
    hindi_form = False
    if m:
        body = t[m.end():]
    else:
        has_yaad = bool(re.search(_hx(r"याद\s+दिला"), t) or re.search(r"\byaad dila", t))
        idx, kind = _find_noun(words)
        has_set = any(w.startswith(_HI_SETVERB_TOK) or w in ("laga", "lagao", "karo", "kar") for w in words)
        if not (has_yaad or (kind == "reminder" and has_set)):
            return None
        if any(_kind_of_token(w) not in ("", "reminder") for w in words):
            return None
        body, hindi_form = t, True
    body, rep = _extract_repeat(body)
    if _OTHER_MODULE_RE.search(body):
        return None                                    # "add a reminder to my calendar" belongs to the calendar / notes
    if _PAST_RE.search(body):
        _matched("reminder about the past")
        return _t("time_passed", L)
    dt, what = timeparse.extract_when(body, now, bias="day")
    if dt is None and not hindi_form and re.match(r"^of\b", what.strip()):
        return None                                    # "remind me of a song" is chit-chat, not a request
    what = _clean_what_hi(what) if hindi_form else _clean_what_en(what)
    if what in _LATER_WORDS or (not hindi_form and _QUESTION_START.match(what)):
        return None
    if rep == "other":
        _matched("reminder with an unsupported repeat")
        return _t("repeat_unsupported", L)
    if len(what) > 240:
        return None                                    # a whole paragraph is not a reminder text
    what = _unfold(what, raw)
    if hindi_form:
        what = _hi_infinitive(what)
    if dt is None:
        if not what:
            return None
        _matched("reminder %r without a time -> asking" % what)
        _set_pending(kind="reminder", await_="when", what=what, repeat=rep, lang=L,
                     expires=now.timestamp() + PENDING_SECONDS)
        return _t("remind_ask_rep" if rep else "remind_ask", L)
    _matched("reminder %r at %s repeat=%r" % (what, dt.strftime("%Y-%m-%d %H:%M"), rep))
    return _create_reminder(what, dt, rep, L, now)


# ---- answering "When should I remind you?" / "For how long?"

_EN_EVIDENCE = re.compile(r"\b(?:at|in|on|tomorrow|today|tonight|minutes?|hours?|the|please|morning|evening|afternoon|noon|"
                          r"midnight|next)\b")


def _complete_pending(p: Dict[str, object], raw: str, t: str, now: datetime) -> Optional[str]:
    L = _language_of(raw)
    if L == "en" and p.get("lang") == "hi" and not _EN_EVIDENCE.search(t):
        L = "hi"
    kind = str(p.get("kind"))
    if p.get("await_") == "duration":
        secs = _answer_duration(t)
        if not secs:
            return None
        _matched("answer: timer %.0f s" % secs)
        return _create_timer(secs, str(p.get("label") or ""), L, now)
    if kind == "alarm":
        dt = _answer_when(t, now, "am")
        if dt is None:
            return None
        _matched("answer: alarm at %s" % dt.strftime("%Y-%m-%d %H:%M"))
        return _create_alarm(dt, str(p.get("repeat") or ""), L, now)
    dt = _answer_when(t, now, "day")
    if dt is None:
        return None
    _matched("answer: reminder %r at %s" % (p.get("what"), dt.strftime("%Y-%m-%d %H:%M")))
    return _create_reminder(str(p.get("what") or ""), dt, str(p.get("repeat") or ""), L, now)


# ------------------------------------------------------------------ the entry point

_HANDLERS = (
    ("cancel", lambda t, w, L, n, r: _handle_cancel(t, w, L)),
    ("list", lambda t, w, L, n, r: _handle_list(t, w, L, n)),
    ("snooze", lambda t, w, L, n, r: _handle_snooze(t, w, L, n)),
    ("timer", _handle_timer),
    ("alarm", _handle_alarm),
    ("reminder", _handle_reminder),
)


def _take_pending(now: datetime) -> Optional[Dict[str, object]]:
    p = _clear_pending()
    if p and float(p.get("expires", 0)) >= now.timestamp():
        return p
    return None


def try_handle(transcript: str, now=None) -> Optional[str]:
    """The finished sentence if `transcript` is about timers / reminders / alarms (the action is already done),
    else None. Never raises. `now` (datetime or epoch seconds) is injected for tests."""
    if not _enabled():
        return None
    L = "en"
    try:
        raw = str(transcript or "")
        if not raw.strip():
            return None
        t = _strip_fillers(_prep(raw))
        if not t:
            return None
        now_dt = _as_dt(now)
        pend = _take_pending(now_dt)
        words = t.split()
        L = _language_of(raw)
    except Exception:
        logger.exception("reminders: could not prepare %r", transcript)
        return None
    if _QUICK.search(t):
        for name, fn in _HANDLERS:
            _tl.matched = False
            try:
                reply = fn(t, words, L, now_dt, raw)
            except Exception:
                logger.exception("reminders: %s handler failed on %r", name, raw)
                return _t("failed", L) if getattr(_tl, "matched", False) else None
            if reply is not None:
                logger.info("reminders: %r -> %r", raw, reply)
                return reply
    if pend:
        _tl.matched = False
        try:
            reply = _complete_pending(pend, raw, t, now_dt)
        except Exception:
            logger.exception("reminders: answering the pending question failed on %r", raw)
            return _t("failed", L) if getattr(_tl, "matched", False) else None
        if reply is not None:
            logger.info("reminders: %r -> %r", raw, reply)
            return reply
    return None


# ------------------------------------------------------------------ tool functions (LLM schemas)

def set_reminder(what: str = "", when: str = "", kind: str = "", **_ignored) -> str:
    """Sets a timer, reminder or alarm. `when` is free text ('in 20 minutes', 'tomorrow at 9', 'every day at 8 am');
    with no `what` and a duration it is a timer, with no `what` and a clock time it is an alarm."""
    L = _L.current()
    try:
        now = _as_dt(None)
        what = re.sub(r"\s+", " ", str(what or "")).strip()
        when = str(when or "").strip()
        k = _norm_kind(kind)
        wt = _strip_fillers(_prep(when))
        wt, rep = _extract_repeat(wt)
        secs, drest = timeparse.extract_duration(wt) if wt else (None, "")
        dur_only = bool(secs) and _filler_only(drest)
        what = _EN_LEAD_JUNK.sub("", what) if what[:3].lower() == "to " else what
        if k == "timer" or (not k and not what and dur_only):
            if not secs:
                _set_pending(kind="timer", await_="duration", label=what, repeat="", lang=L,
                             expires=now.timestamp() + PENDING_SECONDS)
                return _t("timer_ask", L)
            logger.info("reminders: set_reminder -> timer %.0f s label=%r", secs, what)
            return _create_timer(secs, what, L, now)
        if k == "alarm" or (not k and not what):
            dt = timeparse.parse_when(wt, now, bias="am") if wt else None
            if dt is None:
                _set_pending(kind="alarm", await_="when", repeat=rep if rep != "other" else "", lang=L,
                             expires=now.timestamp() + PENDING_SECONDS)
                return _t("alarm_ask", L)
            logger.info("reminders: set_reminder -> alarm %s", dt)
            return _create_alarm(dt, rep if rep in REPEATS else "", L, now)
        if rep == "other":
            return _t("repeat_unsupported", L)
        dt = timeparse.parse_when(wt, now, bias="day") if wt else None
        if dt is None:
            _set_pending(kind="reminder", await_="when", what=what, repeat=rep, lang=L,
                         expires=now.timestamp() + PENDING_SECONDS)
            return _t("no_time", L)
        logger.info("reminders: set_reminder -> reminder %r at %s repeat=%r", what, dt, rep)
        return _create_reminder(what, dt, rep, L, now)
    except Exception:
        logger.exception("reminders: set_reminder failed")
        return _t("failed", L)


def list_reminders(**_ignored) -> str:
    """Everything that is waiting: timers with time left, reminders, alarms."""
    L = _L.current()
    try:
        now = _as_dt(None)
        parts = []
        timers = list_active("timer")
        if timers:
            parts.append(_timers_reply(timers, L, now.timestamp()))
        for kind in ("reminder", "alarm"):
            rows = list_active(kind)
            if rows:
                parts.append(_list_reply(rows, kind, L, now))
        return " ".join(parts) if parts else _t("none_all", L)
    except Exception:
        logger.exception("reminders: list_reminders failed")
        return _t("failed", L)


def cancel_reminder(which: str = "", **_ignored) -> str:
    """Cancels by words ('the pasta timer', 'call mom', 'all reminders', '7 am alarm'); empty = everything."""
    L = _L.current()
    try:
        words = _prep(str(which or "")).split()
        idx, kind = _find_noun(words)
        every = any(w in ("all", "every", "everything") or w in _HI_ALL for w in words)
        sel = "" if every else " ".join(w for w in words if w not in _DET_PRE and not _kind_of_token(w))
        rows = cancel(sel, kind)
        logger.info("reminders: cancel_reminder(%r) -> %d item(s)", which, len(rows))
        if rows:
            return _cancel_reply(rows, kind or str(rows[0]["kind"]), L) if len(
                {r["kind"] for r in rows}) == 1 else _t("cancel_any", L)
        if sel and list_active(kind):
            return _t("cancel_nomatch", L, kind=_kind_phrase(kind or "reminder", False, L), sel=sel)
        return _t("none_" + kind, L) if kind else _t("none_all", L)
    except Exception:
        logger.exception("reminders: cancel_reminder failed")
        return _t("failed", L)


# ------------------------------------------------------------------ hooks

def briefing_line(now=None) -> str:
    """'You have 2 reminders today: call mom at 5 PM and take medicine at 9 PM.' Future reminders due today only;
    '' when there are none. Never raises."""
    try:
        now_dt = _as_dt(now)
        L = _L.current()
        start = now_dt.timestamp()
        end = (datetime(now_dt.year, now_dt.month, now_dt.day) + timedelta(days=1)).timestamp()
        rows = [r for r in list_active("reminder") if start < float(r["due"]) < end]
        if not rows:
            return ""
        parts = []
        for r in rows[:3]:
            tm = _L.fmt_time(_from_ts(float(r["due"])), L)
            what = _second_person(str(r.get("text") or "").strip(), L)
            if L == "hi":
                parts.append(f"{what} {tm}".strip())
            else:
                parts.append(f"{what} at {tm}" if what else f"at {tm}")
        text = _L.join_list(parts, L)
        if len(rows) > 3:
            text = _t("more", L, list=", ".join(parts), n=len(rows) - 3)
        return _t("brief_one" if len(rows) == 1 else "brief_many", L, n=len(rows), items=text)
    except Exception:
        logger.exception("reminders: briefing_line failed")
        return ""
