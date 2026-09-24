"""
notes.py - voice notes and named lists (to-do, shopping, grocery, packing ...), English and Hindi.

Everything is decided in code (no LLM): `try_handle(transcript)` recognises the request, does the
work in SQLite (memory.connect(), same file as facts / sessions) and returns the sentence to speak.

Notes      "take a note: buy milk", "note that the meeting is at 5", "write this down ...",
           "read my notes", "read my last note", "what notes do I have", "delete my last note",
           "delete all my notes" (asks first: confirmation kind `clear_notes`).
           Hindi: "नोट करो कि कल मीटिंग है", "एक नोट बनाओ: दूध लाना है", "मेरे नोट्स पढ़ो", ...
Lists      "add milk, eggs and bread to my shopping list", "put X on my list", "what's on my to-do
           list", "mark buy milk as done", "tick off eggs", "remove X from my list", "how many tasks
           are left", "clear my to-do list" (asks first, only if the list has items: kind `clear_list`).
           The default list is "to-do". Hindi: "शॉपिंग लिस्ट में अंडे और ब्रेड जोड़ो", ...

Deliberately NOT handled (they belong to other modules or to the LLM): "notes app", "open notepad",
"list of songs", "add this song to my playlist / queue" ("playlist" and "queue" are not lists here),
"add 5 and 7", "add me to the group", "take notes on this" (plural), "write a note to my boss".

How "and" is split: commas always separate items ("milk, eggs and bread"). For the to-do list a
lone "and" with no comma stays ONE item ("call mom and dad"); for every other list ("shopping",
"grocery" ...) a final "and" separates items ("eggs and bread" = two items).

Matching works on a folded copy of the sentence (lower case, Hindi chandrabindu / anusvara / nukta
dropped, so हाँ = हा, पढ़ो = पढो) with an index map back to the original text, so a saved note or item
keeps the exact spelling Whisper produced.

Hooks: briefing_line() (for the morning briefing), todo_count(list_name), save_note(text) (used by clipboard_tool).
Tool: manage_notes().
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import confirmation
import lang
import memory

logger = logging.getLogger("voice_assistant")

DEFAULT_LIST = "to-do"

# ------------------------------------------------------------------ strings (English / Hindi)

_T: Dict[str, Dict[str, str]] = {
    "note_saved": {"en": "Noted: {t}.", "hi": "ठीक है, नोट कर लिया: {t}।"},
    "note_saved_long": {"en": "Noted.", "hi": "नोट कर लिया।"},
    "note_hint": {"en": "What should the note say? Try: take a note, buy milk.",
                  "hi": "नोट में क्या लिखूँ? ऐसे कहिए: नोट करो, दूध लाना है।"},
    "notes_none": {"en": "You don't have any notes yet.", "hi": "आपके पास अभी कोई नोट नहीं है।"},
    "notes_one": {"en": "You have 1 note: {items}.", "hi": "आपके पास 1 नोट है: {items}।"},
    "notes_few": {"en": "You have {n} notes: {items}.", "hi": "आपके पास {n} नोट हैं: {items}।"},
    "notes_many": {"en": "You have {n} notes. The last {k} are: {items}.",
                   "hi": "आपके पास {n} नोट हैं। आख़िरी {k} ये हैं: {items}।"},
    "notes_many_more": {"en": "You have {n} notes. Here are the last {k}: {items}.",
                        "hi": "आपके पास {n} नोट हैं। आख़िरी {k} ये हैं: {items}।"},
    "notes_count_one": {"en": "You have 1 note.", "hi": "आपके पास 1 नोट है।"},
    "notes_count_many": {"en": "You have {n} notes.", "hi": "आपके पास {n} नोट हैं।"},
    "last_note": {"en": "Your last note is: {t}.", "hi": "आपका आख़िरी नोट है: {t}।"},
    "note_deleted": {"en": "Deleted your last note: {t}.", "hi": "आपका आख़िरी नोट मिटा दिया: {t}।"},
    "clear_notes_q1": {"en": "Delete your note? Say yes to delete it, or no to keep it.",
                       "hi": "क्या आपका नोट मिटा दूँ? मिटाने के लिए हाँ कहिए, रखने के लिए ना।"},
    "clear_notes_q": {"en": "Delete all {n} of your notes? Say yes to delete them, or no to keep them.",
                      "hi": "क्या आपके सारे {n} नोट मिटा दूँ? मिटाने के लिए हाँ कहिए, रखने के लिए ना।"},
    "clear_notes_summary": {"en": "delete all your notes", "hi": "आपके सारे नोट मिटाना"},
    "clear_notes_done": {"en": "Deleted {n} notes.", "hi": "{n} नोट मिटा दिए।"},
    "clear_notes_done1": {"en": "Deleted your note.", "hi": "आपका नोट मिटा दिया।"},

    "list_added": {"en": "Added {items} to {ref}.", "hi": "{ref} में {items} जोड़ दिया।"},
    "list_added_many": {"en": "Added {n} items to {ref}: {items}.",
                        "hi": "{ref} में {n} चीज़ें जोड़ दीं: {items}।"},
    "list_already": {"en": "Already on {ref}: {items}.", "hi": "{items} पहले से {ref} में है।"},
    "list_already_many": {"en": "Already on {ref}: {items}.", "hi": "{items} पहले से {ref} में हैं।"},
    "list_partial": {"en": "Added {items} to {ref}. Already there: {dupes}.",
                     "hi": "{ref} में {items} जोड़ दिया। {dupes} पहले से था।"},
    "list_partial_many": {"en": "Added {items} to {ref}. Already there: {dupes}.",
                          "hi": "{ref} में {items} जोड़ दिया। {dupes} पहले से थे।"},
    "list_empty": {"en": "{Ref} is empty.", "hi": "{ref} खाली है।"},
    "list_all_done": {"en": "Everything on {ref} is done.", "hi": "{ref} का सारा काम हो चुका है।"},
    "list_read": {"en": "You have {n} on {ref}: {items}.", "hi": "{ref} में {n} हैं: {items}।"},
    "list_read_more": {"en": "You have {n} on {ref}. The first {k}: {items}.",
                       "hi": "{ref} में {n} हैं। पहले {k} ये हैं: {items}।"},
    "things_one": {"en": "1 thing", "hi": "1 चीज़"},
    "things_many": {"en": "{n} things", "hi": "{n} चीज़ें"},
    "left_none": {"en": "There is nothing left on {ref}.", "hi": "{ref} में कुछ भी बाकी नहीं है।"},
    "left_one": {"en": "You have 1 thing left on {ref}.", "hi": "{ref} में 1 काम बाकी है।"},
    "left_many": {"en": "You have {n} things left on {ref}.", "hi": "{ref} में {n} काम बाकी हैं।"},
    "marked": {"en": "Marked {t} as done.", "hi": "{t} पूरा हो गया, मैंने टिक कर दिया।"},
    "marked_left": {"en": "Marked {t} as done. {n} left on {ref}.",
                    "hi": "{t} पूरा हो गया, मैंने टिक कर दिया। {ref} में {n} बाकी हैं।"},
    "marked_all": {"en": "Marked {t} as done. That's everything on {ref}.",
                   "hi": "{t} पूरा हो गया। {ref} का सारा काम हो गया।"},
    "done_already": {"en": "That one is already marked as done: {t}.", "hi": "वो पहले से पूरा हो चुका है: {t}।"},
    "removed": {"en": "Removed {t} from {ref}.", "hi": "{ref} से {t} हटा दिया।"},
    "not_found": {"en": "I couldn't find {q} on {ref}.", "hi": "मुझे {ref} में {q} नहीं मिला।"},
    "not_found_any": {"en": "I couldn't find {q} on any of your lists.",
                      "hi": "मुझे आपकी किसी लिस्ट में {q} नहीं मिला।"},
    "which": {"en": "Which one do you mean: {opts}?", "hi": "आपका मतलब कौन सा है: {opts}?"},
    "which_list": {"en": "I found {t} on more than one list: {lists}. Say it again with the list name.",
                   "hi": "{t} एक से ज़्यादा लिस्ट में है: {lists}। लिस्ट का नाम बताकर दोबारा कहिए।"},
    "clear_list_q": {"en": "Clear {ref}? It has {n} {w}. Say yes to clear it, or no to keep it.",
                     "hi": "क्या {ref} साफ़ कर दूँ? इसमें {n} आइटम हैं। साफ़ करने के लिए हाँ कहिए, रखने के लिए ना।"},
    "clear_list_summary": {"en": "clear {ref}", "hi": "{ref} साफ़ करना"},
    "clear_list_done": {"en": "Cleared {ref}.", "hi": "{ref} साफ़ कर दी।"},
    "clear_list_empty": {"en": "{Ref} is already empty.", "hi": "{ref} पहले से खाली है।"},
    "item_one": {"en": "item", "hi": "आइटम"},
    "item_many": {"en": "items", "hi": "आइटम"},
    "need_text": {"en": "What should I add?", "hi": "क्या जोड़ूँ?"},
    "need_item": {"en": "Which item do you mean?", "hi": "कौन सा आइटम?"},
    "sorry": {"en": "Sorry, something went wrong with your notes and lists.",
              "hi": "माफ़ कीजिए, नोट या लिस्ट में कुछ गड़बड़ हो गई।"},
    "unknown_action": {"en": "I can add or read notes, and manage your to-do and shopping lists.",
                       "hi": "मैं नोट जोड़ या पढ़ सकती हूँ, और आपकी टू-डू और शॉपिंग लिस्ट संभाल सकती हूँ।"},
    "briefing": {"en": "You have {n} on your to-do list: {items}; the first is {first}.",
                 "hi": "आपकी टू-डू लिस्ट में {n} हैं: {items}; पहला {first} है।"},
    "briefing_one": {"en": "You have 1 thing on your to-do list: {first}.",
                     "hi": "आपकी टू-डू लिस्ट में 1 काम है: {first}।"},
}


def _tr(key: str, l: Optional[str] = None, **kw) -> str:
    return lang.tr(_T, key, lang=l, **kw)


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


# ------------------------------------------------------------------ text folding with an index map

_DROP = {0x0901, 0x0902, 0x093C, 0x200C, 0x200D}          # chandrabindu, anusvara, nukta, ZWNJ, ZWJ
_DEV_DIGITS = "०१२३४५६७८९"
_DIGIT_MAP = {ord(c): str(i) for i, c in enumerate(_DEV_DIGITS)}


def _fold_pat(p: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFC", p) if ord(ch) not in _DROP)


def _c(p: str) -> "re.Pattern":
    return re.compile(_fold_pat(p), re.S)


def _fold_map(text: str) -> Tuple[str, List[int]]:
    """Folded text and, for every folded char, its index in `text`."""
    out: List[str] = []
    idx: List[int] = []
    for i, ch in enumerate(text):
        if ord(ch) in _DROP:
            continue
        if ch in "।॥":
            ch = " "
        ch = ch.translate(_DIGIT_MAP).lower()
        for c in ch:
            out.append(c)
            idx.append(i)
    return "".join(out), idx


def _fold(text: str) -> str:
    return _fold_map(unicodedata.normalize("NFC", text or ""))[0]


class _U:
    """One utterance: the original text, a folded copy for matching and the way back."""

    def __init__(self, orig: str):
        self.orig = orig
        self.f, self.m = _fold_map(orig)
        self.off = 0

    def strip_leading(self, rx) -> None:
        mt = rx.match(self.f)
        if mt and mt.end():
            self.off = mt.end()
            self.f = self.f[mt.end():]

    def strip_trailing(self, rx) -> None:
        mt = rx.search(self.f)
        if mt:
            self.f = self.f[:mt.start()]

    def span(self, s: int, e: int) -> str:
        a = s + self.off
        b = e + self.off
        ia = self.m[a] if a < len(self.m) else len(self.orig)
        ib = self.m[b] if b < len(self.m) else len(self.orig)
        return self.orig[ia:ib]

    def group(self, mt, name: str) -> str:
        if mt.group(name) is None:
            return ""
        return self.span(mt.start(name), mt.end(name))


_FILLER = _c(
    r"(?:(?:hey|hi|hello|ok|okay|so|well|um+|uh+|alright|all right|please|kindly|raziel|razil|razeel|"
    r"actually|now|and|just|also|then)[,.!]?\s+"
    r"|(?:can|could|would|will) you(?: please)?[,]?\s+"
    r"|(?:i|we) (?:want|need|would like|'d like) you to\s+|i'd like you to\s+|go ahead and\s+|you should\s+"
    r"|कृपया\s+|प्लीज़\s+|ज़रा\s+|जरा\s+|रज़ील[,]?\s+|रेज़ील[,]?\s+|अच्छा[,]?\s+|सुनो[,]?\s+|अरे[,]?\s+"
    r"|क्या आप\s+|क्या तुम\s+|आप\s+|तुम\s+|ठीक है[,]?\s+|मेरे लिए\s+)+"
)
_TRAIL = _c(
    r"(?:[\s,]+(?:please|thanks|thank you|for me|प्लीज़|कृपया|मेरे लिए|ज़रा|जरा|"
    r"सकते हैं|सकती हैं|सकते हो|सकती हो|सकते|सकती))+$"
)
_END_PUNCT = re.compile(r"[\s.!?।॥]+$")


def _prep(transcript) -> Optional[_U]:
    if not isinstance(transcript, str):
        return None
    t = unicodedata.normalize("NFC", transcript)
    t = t.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"\s+", " ", t).strip()
    t = _END_PUNCT.sub("", t)
    if not t or len(t) > 1500:
        return None
    u = _U(t)
    u.strip_leading(_FILLER)
    u.strip_trailing(_TRAIL)
    if not u.f.strip():
        return None
    return u


_EDGE = re.compile(r"^[\s,;:\-–—\"'“”]+|[\s,;:\-–—\"'“”]+$")


def _clean(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = _END_PUNCT.sub("", s)
    s = _EDGE.sub("", s)
    return s.strip()


_PRONOUNS = {"it", "this", "that", "them", "these", "those", "something", "stuff", "things", "me", "him",
             "her", "us", "you", "यह", "ये", "इसे", "इसको", "इस", "वह", "वो", "इसे", "उसे", "बात", "यह बात",
             "इस बात को"}
_PRONOUNS_F = {_fold(p) for p in _PRONOUNS}
# "note to self" / "take a note of what I said" / "नोट कर लिया": there is no text yet, so she asks for it
_NO_TEXT_YET = {_fold(p) for p in ("self", "myself", "to self", "what i said", "of what i said", "what i just said",
                                    "लिया", "लिए", "लीजिए")}
_MUSIC_ITEM = re.compile(r"^(?:(?:this|that|the|current|my|a|an) )?(?:song|songs|track|tracks|album|artist|playlist|"
                         r"podcast|music)\b")


def _is_pronoun(c: str) -> bool:
    return _fold(c).strip() in _PRONOUNS_F


# ------------------------------------------------------------------ list names

_TODO_NAMES = {"to do", "to-do", "todo", "to dos", "to-dos", "todos", "task", "tasks", "task list", "chores",
               "things to do", "टू डू", "टू-डू", "टूडू", "टु डू", "टु-डू", "टू डु", "काम", "कामों", "काम की",
               "कार्य", "कार्यों", "टास्क", "टास्क्स", "करने वाले काम"}
_ALIASES = {
    "shop": "shopping", "shopping": "shopping", "groceries": "grocery", "grocery": "grocery",
    "शॉपिंग": "shopping", "शौपिंग": "shopping", "शापिंग": "shopping", "खरीदारी": "shopping",
    "खरीददारी": "shopping", "ग्रोसरी": "grocery", "किराना": "grocery", "किराने": "grocery",
    "राशन": "grocery", "पैकिंग": "packing", "packing": "packing",
}
_TODO_F = {_fold(n) for n in _TODO_NAMES}
_ALIASES_F = {_fold(k): v for k, v in _ALIASES.items()}
_NOT_LISTS = re.compile(
    r"\b(?:play|playlist|playlists|song|songs|music|queue|playing|spotify|youtube|contact|contacts|mailing|"
    r"email|emails|recipient|blocked|block|black|calendar|channel|phone|call|calls|message|messages|whatsapp|"
    r"group|chat|browser|history|bookmark|bookmarks|download|downloads|file|files|program|programs|app|apps|"
    r"startup|process|processes|video|videos|album|albums|podcast|podcasts|radio|tv|window|windows)\b")
_NOT_LISTS_HI = ("प्ले", "गाना", "गाने", "गानों", "म्यूज़िक", "संगीत", "कॉन्टैक्ट", "क्यू", "ईमेल", "व्हाट्सएप",
                 "व्हाट्सऐप", "कॉल", "मैसेज", "यूट्यूब", "स्पॉटिफ़ाई", "स्पॉटिफाई", "ऐप", "वीडियो", "एलबम")
_NOT_LISTS_HI_F = tuple(_fold(w) for w in _NOT_LISTS_HI)
_NAME_STOP = {"my", "the", "our", "your", "this", "that", "a", "an", "मेरी", "मेरे", "मेरा", "हमारी", "इस", "उस",
              "यह", "वह", "की", "का", "के", "सारी", "पूरी", "सभी"}
_NAME_STOP_F = {_fold(w) for w in _NAME_STOP}


def _canon_list(raw: str) -> str:
    """'to do' / 'टू-डू' / 'task' -> 'to-do'; 'groceries' -> 'grocery'; other names stay as spoken."""
    n = re.sub(r"\s+", " ", _fold(raw or "")).strip().strip(".,:;!?")
    for w in ("list", "लिस्ट", "सूची"):
        if n.endswith(" " + w):
            n = n[: -len(w)].strip()
    if not n or n in _NAME_STOP_F:
        return DEFAULT_LIST
    if n in _TODO_F:
        return DEFAULT_LIST
    if n in _ALIASES_F:
        return _ALIASES_F[n]
    return n


def _name_ok(name: str) -> bool:
    """False for 'playlist'-like or pronoun 'names' that must not count as a list."""
    n = _fold(name)
    if not n.strip():
        return True
    if _NOT_LISTS.search(n):
        return False
    return not any(w in n for w in _NOT_LISTS_HI_F)


_NOTES_AS_LIST = {"note", "notes", _fold("नोट"), _fold("नोट्स"), _fold("नोटस")}
_HI_NAMES = {"to-do": "टू-डू", "shopping": "शॉपिंग", "grocery": "ग्रोसरी", "packing": "पैकिंग"}


def _ref(name: str, l: Optional[str] = None) -> str:
    """'your to-do list' / 'आपकी टू-डू लिस्ट'."""
    l = l or lang.current()
    if l == "hi":
        return f"आपकी {_HI_NAMES.get(name, name)} लिस्ट"
    return f"your {name} list"


# ------------------------------------------------------------------ database

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS notes ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, created_at TEXT NOT NULL, lang TEXT)",
    "CREATE TABLE IF NOT EXISTS list_items ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, list_name TEXT NOT NULL, text TEXT NOT NULL,"
    " done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)",
)


def _now() -> datetime:
    return datetime.now()


@contextmanager
def _db():
    conn = memory.connect()
    try:
        for stmt in _SCHEMA:
            conn.execute(stmt)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _add_note(text: str) -> int:
    with _db() as c:
        cur = c.execute("INSERT INTO notes (text, created_at, lang) VALUES (?, ?, ?)",
                        (text, _now().isoformat(timespec="seconds"), lang.current()))
        return cur.lastrowid


def _notes() -> List[dict]:
    with _db() as c:
        return [dict(r) for r in c.execute("SELECT id, text, created_at, lang FROM notes ORDER BY id")]


def _delete_last_note() -> Optional[str]:
    with _db() as c:
        row = c.execute("SELECT id, text FROM notes ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        c.execute("DELETE FROM notes WHERE id = ?", (row["id"],))
        return row["text"]


def _delete_all_notes() -> int:
    with _db() as c:
        n = c.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        c.execute("DELETE FROM notes")
        return n


def _items(list_name: Optional[str] = None, done: Optional[bool] = None) -> List[dict]:
    sql, args = "SELECT id, list_name, text, done, created_at FROM list_items", []
    conds = []
    if list_name is not None:
        conds.append("list_name = ?")
        args.append(list_name)
    if done is not None:
        conds.append("done = ?")
        args.append(1 if done else 0)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    with _db() as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY id", args)]


def _add_items(list_name: str, items: List[str]) -> Tuple[List[str], List[str]]:
    """Adds items; an item that is already open on that list is skipped, a done one is re-opened."""
    added, dupes = [], []
    with _db() as c:
        for it in items:
            row = c.execute("SELECT id, done FROM list_items WHERE list_name = ? AND lower(text) = lower(?)"
                            " ORDER BY done, id LIMIT 1", (list_name, it)).fetchone()
            if row and not row["done"]:
                dupes.append(it)
            elif row:
                c.execute("UPDATE list_items SET done = 0 WHERE id = ?", (row["id"],))
                added.append(it)
            else:
                c.execute("INSERT INTO list_items (list_name, text, done, created_at) VALUES (?, ?, 0, ?)",
                          (list_name, it, _now().isoformat(timespec="seconds")))
                added.append(it)
    return added, dupes


def _set_done(ids: List[int]) -> None:
    with _db() as c:
        c.executemany("UPDATE list_items SET done = 1 WHERE id = ?", [(i,) for i in ids])


def _delete_items(ids: List[int]) -> None:
    with _db() as c:
        c.executemany("DELETE FROM list_items WHERE id = ?", [(i,) for i in ids])


def _clear_list(list_name: str) -> int:
    with _db() as c:
        n = c.execute("SELECT COUNT(*) FROM list_items WHERE list_name = ?", (list_name,)).fetchone()[0]
        c.execute("DELETE FROM list_items WHERE list_name = ?", (list_name,))
        return n


# ------------------------------------------------------------------ fuzzy item matching

_STOPW = {_fold(w) for w in (
    "the", "a", "an", "my", "some", "to", "of", "for", "on", "in", "from", "and", "task", "item", "one", "that",
    "this", "it", "list", "done", "वाला", "वाली", "वाले", "का", "की", "के", "को", "काम", "टास्क", "आइटम", "से",
    "में", "और", "मेरा", "मेरी", "मेरे", "पर")}


def _toks(s: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+|[ऀ-ॿ]+", _fold(s)) if t not in _STOPW]


def _teq(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 3:
        return False
    if a.rstrip("s") == b.rstrip("s"):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _score(query: str, item: str) -> float:
    qt, it = _toks(query), _toks(item)
    qn, itn = " ".join(qt), " ".join(it)
    if not qt or not it:
        return 0.0
    if qn == itn:
        return 1.0
    mq = sum(1 for q in qt if any(_teq(q, i) for i in it))
    mi = sum(1 for i in it if any(_teq(q, i) for q in qt))
    best = 0.0
    if mq == len(qt):                                    # everything asked for is in the item
        best = max(best, 0.75 + 0.15 * len(qt) / len(it))
    if mi == len(it):                                    # the whole item is inside what was said
        best = max(best, 0.75 + 0.15 * len(it) / len(qt))
    frac = max(mq, mi) / max(len(qt), len(it))
    if frac > 0.5:
        best = max(best, 0.45 + 0.5 * frac)
    ratio = difflib.SequenceMatcher(None, qn, itn).ratio()
    if ratio >= 0.75:
        best = max(best, ratio)
    return best


def _resolve(query: str, rows: List[dict]) -> Tuple[str, List[dict]]:
    """('one', rows) | ('ambiguous', rows) | ('none', [])."""
    scored = sorted(((_score(query, r["text"]), r) for r in rows), key=lambda x: -x[0])
    scored = [(s, r) for s, r in scored if s >= 0.7]
    if not scored:
        return "none", []
    top = scored[0][0]
    if top >= 0.99:
        exact = [r for s, r in scored if s >= 0.99]
        if len({r["list_name"] for r in exact}) > 1:
            return "ambiguous", exact
        return "one", exact
    near = [r for s, r in scored if s >= top - 0.06]
    if len(near) > 1:
        return "ambiguous", near
    return "one", [scored[0][1]]


# ------------------------------------------------------------------ item splitting

def _split_items(raw: str, list_name: str) -> List[str]:
    parts = [p for p in re.split(r"\s*[,;،]\s*(?:and\s+|और\s+)?", raw or "") if p and p.strip()]
    todo_like = list_name == DEFAULT_LIST
    sep = re.compile(r"\s+(?:and|और|&)\s+")
    out: List[str] = []
    for i, p in enumerate(parts):
        if (not todo_like) or (len(parts) > 1 and i == len(parts) - 1):
            out.extend(sep.split(p))
        else:
            out.append(p)
    clean, seen = [], set()
    for it in out:
        it = _clean(re.sub(r"^(?:and|और)\s+", "", it.strip(), flags=re.I))
        it = re.sub(r"\s+(?:also|too|as well)$", "", it, flags=re.I)
        k = it.lower()
        if it and k not in seen and not _is_pronoun(it):
            seen.add(k)
            clean.append(it)
    return clean[:20]


# ------------------------------------------------------------------ patterns

_TAIL = r"(?: (?:for )?(?:now|right now|today|tonight))?"

# --- notes (English)
_SEP_EN = r"(?:\s*[:,;]|\s+[-–—]\s|\s+(?:that|to|saying|says|about|of|which says)\b)?"
_N_VERB = _c(
    r"^(?:take|make|add|create|save)(?: me)?(?: an?| another| one)?(?: (?:new|quick|short|little|voice))? "
    r"note(?![\w\-])(?: to (?:self|myself))?(?: for (?:later|myself))?" + _SEP_EN + r"\s*(?P<c>.*)$")
_N_NOTE_SEP = _c(r"^note(?: to (?:self|myself))?\s*(?:[:,;]|\s(?:that|this|to)\b)\s*(?P<c>.*)$")
_N_NOTE_DOWN = _c(r"^note (?:(?:this|that|it|these) )?down\b(?: that\b)?\s*[:,;]?\s*(?P<c>.*)$")
_N_NOTE_SELF = _c(r"^note to (?:self|myself)\s*[:,;]?\s*(?P<c>.*)$")
_N_WRITE = _c(r"^(?:write|jot)(?: (?:this|that|it|these))? down\b(?: that\b)?\s*(?:[:,;]|\s[-–—]\s)?\s*(?P<c>.*)$")
_N_HINGLISH = (
    _c(r"^(?:ek |1 )?(?:naya )?note (?:banao|bana do|bana lo|likho|likh do|likh lo|kar(?:o| lo| do| dijiye)|"
       r"le lo|lo)\b(?: ki\b)?\s*[:,]?\s*(?P<c>.*)$"),
    _c(r"^(?:yeh |ye |isse )?note kar(?:o| lo| do)\b(?: ki\b)?\s*[:,]?\s*(?P<c>.*)$"),
    _c(r"^(?P<c>.+?),?\s+(?:yeh |ye |isse )?note kar (?:lo|do|dijiye)$"),
)
_NOT_NOTE_CONTENT = re.compile(
    r"^(?:(?:for|from|on|in|into|at|using|with|via|by|between|onto)\b|"
    r"(?:my|the|our|your)\s+(?:[a-z'\-]+\s+)?(?:list|calendar|reminders?|playlist|queue|file|folder|phone|"
    r"desktop|clipboard)\b)")

# --- notes (Hindi)
_HB = r"(?![ऀ-ॿ])"                                  # not followed by another Devanagari letter
_H_NOTES = r"नोट(?:्स|स|ों)?"
_H_READV = (r"(?:पढ़(?:ो|ें|िए|िये| दो| दीजिए| दीजिये|कर सुनाओ|कर सुनाइए|कर बताओ)?|"
            r"सुना(?:ओ|इए|इये| दो| दीजिए| दीजिये)|बता(?:ओ|इए|इये| दो| दीजिए| दीजिये)|"
            r"दिखा(?:ओ|इए|इये| दो| दीजिए| दीजिये))")
_H_DEL = (r"(?:हटा(?:ओ|ें| दो| दीजिए| दीजिये| दें|इए|इये)|मिटा(?:ओ|ें| दो| दीजिए| दीजिये| दें|इए|इये)|"
          r"निकाल(?:ो|ें| दो| दीजिए| दीजिये| दें)|(?:डिलीट|रिमूव|डिलिट) (?:करो|कर दो|कर दीजिए|कर दीजिये|कर दें|करें))")
_H_CLR = (r"(?:(?:साफ़|खाली|ख़ाली) (?:करो|कर दो|कर दीजिए|कर दीजिये|कर दें|करें|कीजिए)|" + _H_DEL[3:-1] + r")")
_H_LAST = r"(?:आखिरी|आखरी|आख़िरी|आख़री|अंतिम|पिछला|पिछले|आखिर वाला|सबसे नया|सबसे ताज़ा)"

_N_HI = (
    _c(r"^(?:यह |ये |इसे |इसको |यह बात |इस बात को )?नोट (?:करो|करें|कीजिए|कीजिये|कर लीजिए|कर लीजिये|कर दीजिए|"
       r"कर लो|कर दो|कर ले|कर)" + _HB + r"(?: (?:कि|की|के)" + _HB + r")?\s*[:,;]?\s*(?P<c>.*)$"),
    _c(r"^(?:एक |एक नया |नया |एक छोटा )?नोट (?:बनाओ|बना दो|बना लो|बनाइए|बनाइये|बना दीजिए|लिखो|लिख लो|लिख दो|"
       r"लिखिए|ले लो|लो)" + _HB + r"(?: कि" + _HB + r")?\s*[:,;]?\s*(?P<c>.*)$"),
    _c(r"^(?:यह |ये |इसे |इसको )?लिख (?:लो|दो|लीजिए|लीजिये|ले)" + _HB + r"(?: कि" + _HB + r")?\s*[:,;]?\s*(?P<c>.*)$"),
)
_N_HI_SUFFIX = _c(r"^(?P<c>.+?),?\s+(?:यह |ये |इसे |इसको )?(?:नोट|लिख) (?:कर लो|कर दो|करो|कर लीजिए|लो|लीजिए|ले|दो)$")

_N_DEL_ALL = (
    _c(r"^(?:delete|clear|erase|remove|wipe|empty|get rid of|trash)(?: (?:all|every|each))?(?: of)?"
       r"(?: (?:my|the|our))?(?: all)? (?:notes|note)(?: list)?" + _TAIL + r"$"),
    _c(r"^(?:delete|clear|erase|remove|wipe|empty|get rid of)(?: out)? everything(?: in| from| on)? "
       r"(?:my|the) notes" + _TAIL + r"$"),
    _c(r"^(?:मेरे |मेरी )?(?:सारे |सभी |सब )?(?:मेरे )?" + _H_NOTES + r" (?:को )?(?:" + _H_DEL[3:-1] + r"|"
       + _H_CLR[3:-1] + r")$"),
)
_N_DEL_LAST = (
    _c(r"^(?:delete|remove|erase|discard|scratch|trash|undo)(?: (?:my|the))? (?:last|latest|most recent|"
       r"previous|newest) note" + _TAIL + r"$"),
    _c(r"^(?:delete|remove|erase|discard|scratch) that note" + _TAIL + r"$"),
    _c(r"^(?:मेरा |मेरे |मेरी )?" + _H_LAST + r" नोट (?:को )?" + _H_DEL + r"$"),
)
_N_READ_LAST = (
    _c(r"^(?:read|show|tell|play|give)(?: me)?(?: back)?(?: out)?(?: (?:my|the))? (?:last|latest|most recent|"
       r"previous|newest) note(?: back)?(?: out loud| aloud)?" + _TAIL + r"$"),
    _c(r"^what(?:'s| is| was) (?:my|the) (?:last|latest|most recent|previous|newest) note(?: i (?:took|made|saved|"
       r"wrote|added))?$"),
    _c(r"^what did i (?:just )?(?:note|write) down(?: last)?$"),
    _c(r"^(?:मेरा |मेरे |मेरी )?" + _H_LAST + r" नोट (?:को )?" + _H_READV + r"$"),
    _c(r"^(?:मेरा |मेरे )?" + _H_LAST + r" नोट क्या (?:था|है)$"),
)
_N_COUNT = (
    _c(r"^how many notes (?:do i have|have i (?:saved|taken|made|written)|are there|are saved)$"),
    _c(r"^(?:मेरे )?(?:पास )?कितने " + _H_NOTES + r" (?:हैं|है|हुए|बने|सेव हैं)$"),
)
_N_READ = (
    _c(r"^(?:read|list|show|tell|give|recite)(?: me)?(?: out)?(?: back)?(?P<all> all)?(?: of)?"
       r"(?: (?:my|the|our))?(?P<all2> all)?(?: saved)? notes(?: list)?(?: back)?(?: out loud| aloud)?" + _TAIL + r"$"),
    _c(r"^what notes (?:do i have|have i (?:saved|taken|made|written|got)|did i (?:save|take|make|write))"
       r"(?: saved| so far)?$"),
    _c(r"^what(?:'s| is| are) (?:in |on )?(?:my|the) notes$"),
    _c(r"^(?:do i have any|any|have i got any) (?:saved )?notes$"),
    _c(r"^(?:मेरे |मेरी |हमारे )?(?P<all>(?:सारे|सभी|सब) )?(?:मेरे )?" + _H_NOTES + r" (?:को )?" + _H_READV + r"$"),
    _c(r"^(?:मेरे |मेरी )?(?P<all>(?:सारे|सभी|सब) )?(?:मेरे )?" + _H_NOTES + r" क्या (?:क्या )?(?:हैं|है)$"),
    _c(r"^(?:मेरे )?(?:पास )?कौन से " + _H_NOTES + r" (?:हैं|है)$"),
    _c(r"^(?:क्या )?(?:मेरे )?(?:पास )?(?:कोई )?" + _H_NOTES + r" (?:हैं|है)(?: क्या)?$"),
)

# --- lists (English)
_NM = r"(?:(?P<name>[a-z][a-z'\-]*(?: [a-z][a-z'\-]*)?) )?"
_LREF = (r"(?:(?:my|the|our|your|this|that) )?(?:" + _NM + r"list|(?P<bare>to-?dos?|to dos?|todos?|tasks))")
_A_VERB = r"(?:add|put|place|stick|throw|write|jot down|write down|include|append)"
_L_ADD = (
    _c(r"^" + _A_VERB + r"(?: in| on)? (?P<items>.+?) (?:to|on|onto|in|into|at) " + _LREF + r"$"),
    _c(r"^add (?:to|onto|in) " + _LREF + r"\s*[:,;]?\s+(?P<items>.+)$"),
    _c(r"^(?:add|create|make|new|put)(?: an?| another| one)?(?: new)? (?:task|to-?do|to do|todo)(?: item)?"
       r"(?:\s*[:,;]|\s+[-–—]\s|\s+(?:to|for|called|named|that says|saying)\b)?\s*(?P<items>.+)$"),
)
_L_ADD_HING = _c(r"^(?:meri |mere )?" + _NM + r"list (?:mein|me|mai) (?P<items>.+?) "
                 r"(?:add karo|add kar do|jodo|jod do|daalo|dalo|daal do|likho)$")
_BAD_ITEMS_FIRST = re.compile(r"^(?:in|on|at|for|with|using|from|into|to (?:my|the|our)\b)\b")
_ITEM_LOOKS_LIKE_LIST = re.compile(r"\blist\b")
_LISTISH = re.compile(r"\b(?:list|lists)$|(?:लिस्ट|सूची)$")               # "mark the shopping list as done": not an item
_NOT_ITEM = re.compile(r"^(?:an? |the |my )?(?:reminders?|notes?|alarms?|timers?|events?|appointments?|meetings? invite)$")

_L_READ = (
    _c(r"^(?:what(?:'s| is| are)|whats) (?:on|in) " + _LREF + _TAIL + r"$"),
    _c(r"^(?:do i have|have i got|is there) (?:anything|something|any (?:tasks|items|things)) (?:left )?(?:on|in) "
       + _LREF + r"$"),
    _c(r"^(?:read|show|tell|give|list)(?: me)?(?: out)?(?: back)?(?: (?:what(?:'s| is) (?:on|in)))? " + _LREF
       + r"(?: back)?(?: out loud| aloud)?" + _TAIL + r"$"),
    _c(r"^what do i (?:still |currently )?have (?:on|in) " + _LREF + r"$"),
    _c(r"^what(?:'s| is)(?: still)? left (?:on|in) " + _LREF + r"$"),
    _c(r"^what do i (?:still |now )?(?:have|need) (?:left )?to (?:do|buy|get)(?: now| still)?" + _TAIL + r"$"),
    _c(r"^what(?:'s| is| are)(?: still)? (?:left|outstanding|remaining) to do" + _TAIL + r"$"),
    _c(r"^what(?:'s| are) my (?:to-?dos?|tasks|to dos?)$"),
    _c(r"^what tasks do i (?:still )?have(?: left)?$"),
)
_L_COUNT = (
    _c(r"^how many (?:tasks|things|items|to-?dos?|to dos?|things to do|things left) (?:are|do i have|have i got)"
       r"(?: still)?(?: left| remaining| outstanding| to do)?(?: (?:on|in) " + _LREF + r")?$"),
    _c(r"^how many (?:tasks|things|items|to-?dos?) (?:are )?(?:left|remaining|outstanding|still to do)"
       r"(?: to do)?(?: (?:on|in) " + _LREF + r")?$"),
    _c(r"^how many (?:tasks|things|items|to-?dos?|chores) do i (?:still |currently )?have(?: left)? to do$"),
    _c(r"^how many (?:things|items|tasks) (?:are )?(?:on|in) " + _LREF + r"$"),
    _c(r"^how much (?:is |do i have )?(?:left )?(?:on|in) " + _LREF + r"$"),
)
_L_DONE_SCOPE = r"(?: (?:(?:on|in|from|off|of) )?" + _LREF + r")?"
_L_DONE = (
    _c(r"^mark (?P<x>.+?) (?:as )?(?:done|complete|completed|finished|checked|ticked)" + _L_DONE_SCOPE + r"$"),
    _c(r"^(?:tick|check|cross|strike) off (?P<x>.+?)" + _L_DONE_SCOPE + r"$"),
    _c(r"^(?:tick|check|cross|strike) (?P<x>.+?) off" + _L_DONE_SCOPE + r"$"),
    _c(r"^(?:mark|set|tick|check) (?:the )?(?P<x>.+?) (?:task |item )?(?:as )?(?:done|complete|completed|finished)"
       + _L_DONE_SCOPE + r"$"),
    _c(r"^(?:complete|finish) (?:the )?(?:task|to-?do|item) (?P<x>.+?)" + _L_DONE_SCOPE + r"$"),
)
_L_REMOVE = (
    _c(r"^(?:remove|delete|erase|drop|scratch|get rid of|take) (?P<x>.+?) "
       r"(?:from|off of|off|out of) " + _LREF + r"$"),
)
_L_CLEAR = (
    _c(r"^(?:clear|empty|wipe|delete|erase|reset)(?: out)?(?: (?:all|everything)(?: (?:on|in|from|off))?)?(?: of)?"
       r"(?: (?:whole|entire|complete))? " + _LREF + _TAIL + r"$"),
    _c(r"^(?:clear|empty|wipe|delete|erase|reset)(?: out)?(?: (?:all|everything)(?: (?:on|in|from|off))?)?(?: of)?"
       r"(?: (?:my|the|our|your))?(?: (?:whole|entire|complete))? " + _NM + r"list" + _TAIL + r"$"),
)

# --- lists (Hindi)
_HNM = r"(?:(?P<name>[^\s,:]+(?: [^\s,:]+)?) (?:की )?)?"
_HLIST = r"(?:(?:मेरी|मेरे|हमारी|इस|उस|अपनी) )?(?:" + _HNM + r"(?:लिस्ट|सूची)|(?P<bare>टू[\- ]?डू|टूडू|टु[\- ]?डू))"
_H_ADDV = (r"(?:जोड़(?:ो| दो| दीजिए| दीजिये| दें|ें|िए|िये)|डाल(?:ो| दो| दीजिए| दीजिये| दें|ें)|"
           r"(?:ऐड|एड) (?:करो|कर दो|कर दीजिए|कर दें|करें)|शामिल (?:करो|कर दो|कर दीजिए)|रखो|रख दो|"
           r"लिख(?:ो| दो| लो|िए))")
_H_L_ADD = (
    _c(r"^" + _HLIST + r" (?:में|पर) (?P<items>.+?) (?:को )?" + _H_ADDV + r"$"),
    _c(r"^(?P<items>.+?) (?:को |भी )?" + _HLIST + r" (?:में|पर) " + _H_ADDV + r"$"),
)
_H_L_READ = (
    _c(r"^" + _HLIST + r" (?:में )?(?:क्या|क्या क्या|क्या-क्या|कौन सी चीज़ें|कौन कौन सी चीज़ें|कौन से काम) "
       r"(?:है|हैं|लिखा है|बाकी है|बाकी हैं)$"),
    _c(r"^" + _HLIST + r" (?:को )?" + _H_READV + r"$"),
    _c(r"^(?:मुझे )?(?:अभी |और |अब )(?:क्या|क्या क्या|क्या-क्या|कौन से काम|कौन कौन से काम) (?:करना|करने) "
       r"(?:बाकी )?(?:है|हैं)$"),
    _c(r"^(?:मुझे )?(?:क्या|क्या क्या|क्या-क्या|कौन से काम|कौन कौन से काम) (?:करना|करने) बाकी (?:है|हैं)$"),
    _c(r"^(?:कौन से|कौन कौन से|कौन-कौन से) (?:काम|टास्क) (?:बाकी|बचे|रह गए|रहते) (?:हैं|है)$"),
)
_H_L_COUNT = (
    _c(r"^(?:अभी |और )?कितने (?:काम|टास्क|आइटम|चीज़ें|चीजें|कार्य) (?:बाकी|बचे|रह गए|रहते) (?:हैं|है)$"),
    _c(r"^" + _HLIST + r" (?:में )?कितने (?:काम|टास्क|आइटम|चीज़ें|चीजें|कार्य) (?:हैं|है|बाकी हैं|बचे हैं)$"),
    _c(r"^(?:मेरी |मेरे )?(?:अभी )?कितने (?:काम|टास्क) (?:हैं|है)$"),
)
_H_L_DONE = (
    _c(r"^(?P<x>.+?)(?: वाला| वाली| वाले| का| की| के)? (?:काम|टास्क|आइटम) (?:हो गया|हो गई|हो चुका|पूरा हो गया|"
       r"पूरा हो गई|पूरा हो चुका|पूरा कर दिया|पूरा हुआ|ख़त्म हो गया|खत्म हो गया)$"),
    _c(r"^(?P<x>.+?) (?:को )?(?:पूरा|डन|कम्प्लीट|कंप्लीट) (?:मार्क )?(?:करो|कर दो|कर दीजिए|कर दीजिये|करें)$"),
    _c(r"^(?P<x>.+?) (?:को |पर )?(?:टिक|चेक) (?:करो|कर दो|कर दीजिए|लगाओ|लगा दो|कर दें)$"),
    _c(r"^(?P<x>.+?) (?:को )?(?:डन|पूरा|हो गया) मार्क (?:करो|कर दो|कर दीजिए|कर दें)$"),
)
_H_L_REMOVE = (
    _c(r"^" + _HLIST + r" से (?P<x>.+?) (?:को )?" + _H_DEL + r"$"),
    _c(r"^(?P<x>.+?) (?:को )?(?:अपनी |मेरी |मेरे )?(?:" + _HNM + r"(?:लिस्ट|सूची)) से " + _H_DEL + r"$"),
)
_H_L_CLEAR = (
    _c(r"^(?:(?:मेरी|मेरे|हमारी|अपनी) )?(?:(?:सारी|पूरी|सभी) )?" + _HNM + r"(?:(?:सारी|पूरी) )?(?:लिस्ट|सूची) (?:को )?"
       + _H_CLR + r"$"),
    _c(r"^(?:(?:मेरी|मेरे|हमारी|अपनी) )?(?P<bare>टू[\- ]?डू|टूडू) (?:को )?" + _H_CLR + r"$"),
)


def _list_name(mt) -> Optional[str]:
    """Canonical list name of a match (None if the 'name' is really something else, e.g. a playlist)."""
    gd = mt.groupdict()
    if gd.get("bare"):
        return DEFAULT_LIST
    raw = gd.get("name") or ""
    if not raw:
        return DEFAULT_LIST
    if not _name_ok(raw):
        return None
    name = _canon_list(raw)
    if name in _NOTES_AS_LIST:
        return None                                   # "clear my notes list" is about the notes
    return name


# ------------------------------------------------------------------ parsing (no side effects)

Intent = Tuple[str, dict]


def _first(rxs, f: str):
    for rx in (rxs if isinstance(rxs, (list, tuple)) else (rxs,)):
        mt = rx.match(f)
        if mt:
            return mt
    return None


def _parse_note_add(u: _U) -> Optional[Intent]:
    f = u.f
    mt = (_N_VERB.match(f) or _N_NOTE_DOWN.match(f) or _N_NOTE_SEP.match(f) or _N_NOTE_SELF.match(f)
          or _N_WRITE.match(f))
    if mt is None:
        for rx in _N_HINGLISH + _N_HI:
            mt = rx.match(f)
            if mt:
                break
    if mt is None:
        mt = _N_HI_SUFFIX.match(f)
    if mt is None:
        return None
    content = _clean(u.group(mt, "c"))
    if not content or _fold(content).strip() in _NO_TEXT_YET:
        return "note_hint", {}
    if _is_pronoun(content):
        return None                                   # "make a note of that": the LLM knows the context
    if re.match(r"(?:called|named|titled)\b", _fold(content)):
        return None                                   # "create a note called X": a title, not the text
    if _NOT_NOTE_CONTENT.match(_fold(content)) and mt.re is _N_VERB:
        return None
    if mt.re is _N_HI_SUFFIX and _fold(content).split()[-1:] in (["नोट"], ["नोटस"]):
        return None
    if _NOT_NOTE_CONTENT.match(_fold(content)) and re.match(r"(?:my|the|our|your)\s", _fold(content)):
        return None                                   # "add a note to my calendar / list"
    return "add_note", {"text": content}


def _parse_notes_other(u: _U) -> Optional[Intent]:
    f = u.f
    if _first(_N_DEL_ALL, f):
        return "clear_notes", {}
    if _first(_N_DEL_LAST, f):
        return "delete_last_note", {}
    if _first(_N_READ_LAST, f):
        return "last_note", {}
    if _first(_N_COUNT, f):
        return "notes_count", {}
    mt = _first(_N_READ, f)
    if mt:
        gd = mt.groupdict()
        return "read_notes", {"all": bool(gd.get("all") or gd.get("all2"))}
    return None


def _parse_lists(u: _U) -> Optional[Intent]:
    f = u.f
    # -- clear
    for rx in _L_CLEAR + _H_L_CLEAR:
        mt = rx.match(f)
        if mt:
            name = _list_name(mt)
            if name is not None:
                return "clear_list", {"list": name}
    # -- remove
    for rx in _L_REMOVE + _H_L_REMOVE:
        mt = rx.match(f)
        if mt:
            gd = mt.groupdict()
            explicit = bool(gd.get("bare") or gd.get("name"))
            name = _list_name(mt)
            x = _clean(u.group(mt, "x"))
            if name is None or not x or _is_pronoun(x) or _MUSIC_ITEM.match(_fold(x)) or _LISTISH.search(_fold(x)):
                continue
            return "remove", {"list": name if explicit else None, "text": x}
    # -- done
    for rx in _L_DONE + _H_L_DONE:
        mt = rx.match(f)
        if mt:
            gd = mt.groupdict()
            explicit = bool(gd.get("bare") or gd.get("name"))
            name = _list_name(mt) if explicit else None
            if explicit and name is None:
                continue
            x = _clean(u.group(mt, "x"))
            if not x or _is_pronoun(x) or _LISTISH.search(_fold(x)):
                continue
            return "done", {"list": name, "text": x}
    # -- count / read
    for rx in _L_COUNT + _H_L_COUNT:
        mt = rx.match(f)
        if mt:
            name = _list_name(mt)
            if name is not None:
                return "count", {"list": name}
    for rx in _L_READ + _H_L_READ:
        mt = rx.match(f)
        if mt:
            name = _list_name(mt)
            if name is not None:
                return "read_list", {"list": name}
    # -- add
    for rx in _L_ADD + _H_L_ADD + (_L_ADD_HING,):
        mt = rx.match(f)
        if not mt:
            continue
        name = _list_name(mt)
        raw = u.group(mt, "items")
        if name is None or not raw.strip():
            continue
        fr = _fold(raw).strip()
        if rx is _L_ADD[2] and (_BAD_ITEMS_FIRST.match(fr) or _ITEM_LOOKS_LIKE_LIST.search(fr)):
            continue
        if _is_pronoun(raw) or _MUSIC_ITEM.match(fr) or _NOT_ITEM.match(fr):
            continue
        items = _split_items(raw, name)
        if not items:
            continue
        return "add_todo", {"list": name, "items": items}
    return None


def _parse(u: _U) -> Optional[Intent]:
    for fn in (_parse_notes_other, _parse_lists, _parse_note_add):
        it = fn(u)
        if it is not None:
            return it
    return None


# ------------------------------------------------------------------ actions (return the sentence to speak)

def _short(t: str, n: int = 90) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."


def _join(items: List[str], l: Optional[str] = None) -> str:
    return lang.join_list(items, l)


def _do_add_note(text: str) -> str:
    text = _clean(text)
    if not text:
        return _tr("note_hint")
    _add_note(text)
    logger.info("notes: saved a note (%d chars)", len(text))
    if len(text) > 140:
        return _tr("note_saved_long")
    return _tr("note_saved", t=text)


def _do_read_notes(all_: bool = False) -> str:
    notes = _notes()
    n = len(notes)
    if not n:
        return _tr("notes_none")
    take = 10 if all_ else 3
    shown = [x["text"] for x in notes[-take:]]
    k = len(shown)
    stop = "। " if lang.current() == "hi" else ". "
    items = stop.join(_END_PUNCT.sub("", _short(t, 200)) for t in shown)
    if n == 1:
        return _tr("notes_one", items=items)
    if n <= take:
        return _tr("notes_few", n=n, items=items)
    return _tr("notes_many", n=n, k=k, items=items)


def _do_last_note() -> str:
    notes = _notes()
    if not notes:
        return _tr("notes_none")
    return _tr("last_note", t=notes[-1]["text"])


def _do_notes_count() -> str:
    n = len(_notes())
    if not n:
        return _tr("notes_none")
    return _tr("notes_count_one") if n == 1 else _tr("notes_count_many", n=n)


def _do_delete_last_note() -> str:
    text = _delete_last_note()
    if text is None:
        return _tr("notes_none")
    logger.info("notes: deleted the last note")
    return _tr("note_deleted", t=_short(text, 80))


def _do_clear_notes() -> str:
    n = len(_notes())
    if not n:
        return _tr("notes_none")
    l = lang.current()

    def execute() -> str:
        cnt = _delete_all_notes()
        logger.info("notes: deleted all notes (%d)", cnt)
        return _tr("clear_notes_done1", l) if cnt == 1 else _tr("clear_notes_done", l, n=cnt)

    question = _tr("clear_notes_q1", l) if n == 1 else _tr("clear_notes_q", l, n=n)
    return confirmation.request("clear_notes", question, execute, summary=_tr("clear_notes_summary", l),
                                yes_phrases=("delete them", "delete all", "delete them all"))


def _things(n: int) -> str:
    return _tr("things_one") if n == 1 else _tr("things_many", n=n)


def _do_add_todo(list_name: str, items: List[str]) -> str:
    list_name = list_name or DEFAULT_LIST
    items = [i for i in (items or []) if i and i.strip()]
    if not items:
        return _tr("need_text")
    added, dupes = _add_items(list_name, items)
    ref = _ref(list_name)
    logger.info("notes: added %d item(s) to list %r (%d already there)", len(added), list_name, len(dupes))
    if not added:
        return _tr("list_already" if len(dupes) == 1 else "list_already_many", items=_join(dupes), ref=ref)
    if dupes:
        return _tr("list_partial" if len(dupes) == 1 else "list_partial_many", items=_join(added), ref=ref,
                   dupes=_join(dupes))
    if len(added) <= 3:
        return _tr("list_added", items=_join(added), ref=ref)
    return _tr("list_added_many", n=len(added), items=_join(added), ref=ref)


def _do_read_list(list_name: str) -> str:
    list_name = list_name or DEFAULT_LIST
    ref = _ref(list_name)
    allrows = _items(list_name)
    if not allrows:
        return _tr("list_empty", Ref=_cap(ref), ref=ref)
    open_ = [r["text"] for r in allrows if not r["done"]]
    if not open_:
        return _tr("list_all_done", ref=ref)
    n = len(open_)
    if n > 8:
        return _tr("list_read_more", n=_things(n), k=8, items=_join(open_[:8]), ref=ref)
    return _tr("list_read", n=_things(n), items=_join(open_), ref=ref)


def _do_count(list_name: str) -> str:
    list_name = list_name or DEFAULT_LIST
    ref = _ref(list_name)
    n = todo_count(list_name)
    if n == 0:
        return _tr("left_none", ref=ref)
    return _tr("left_one", ref=ref) if n == 1 else _tr("left_many", n=n, ref=ref)


def _find(text: str, list_name: Optional[str], want_done: bool = False):
    rows = _items(list_name, done=want_done)
    return _resolve(text, rows)


def _which(rows: List[dict], text: str, scoped: bool) -> str:
    lists = []
    for r in rows:
        if r["list_name"] not in lists:
            lists.append(r["list_name"])
    if len(lists) > 1 and len({r["text"].lower() for r in rows}) == 1:
        names = [_HI_NAMES.get(x, x) if lang.current() == "hi" else x for x in lists]
        return _tr("which_list", t=rows[0]["text"], lists=_join(names))
    opts, seen = [], set()
    for r in rows:
        if r["text"].lower() not in seen:
            seen.add(r["text"].lower())
            opts.append(r["text"])
    word = "या" if lang.current() == "hi" else "or"
    return _tr("which", opts=(", ".join(opts[:-1]) + f" {word} " + opts[-1]) if len(opts) > 1 else opts[0])


def _do_done(text: str, list_name: Optional[str]) -> str:
    text = _clean(text)
    if not text:
        return _tr("need_item")
    state, rows = _find(text, list_name)
    if state == "none":
        state2, rows2 = _find(text, list_name, want_done=True)
        if state2 != "none":
            return _tr("done_already", t=rows2[0]["text"])
        if list_name:
            return _tr("not_found", q=text, ref=_ref(list_name))
        return _tr("not_found_any", q=text)
    if state == "ambiguous":
        return _which(rows, text, bool(list_name))
    _set_done([r["id"] for r in rows])
    target = rows[0]["list_name"]
    logger.info("notes: marked %r done on list %r", rows[0]["text"], target)
    left = todo_count(target)
    ref = _ref(target)
    if left > 0:
        return _tr("marked_left", t=rows[0]["text"], n=left, ref=ref)
    return _tr("marked_all", t=rows[0]["text"], ref=ref)


def _do_remove(text: str, list_name: Optional[str]) -> str:
    text = _clean(text)
    if not text:
        return _tr("need_item")
    state, rows = _resolve(text, _items(list_name))
    if state == "none":
        if list_name:
            return _tr("not_found", q=text, ref=_ref(list_name))
        return _tr("not_found_any", q=text)
    if state == "ambiguous":
        return _which(rows, text, bool(list_name))
    _delete_items([r["id"] for r in rows])
    logger.info("notes: removed %r from list %r", rows[0]["text"], rows[0]["list_name"])
    return _tr("removed", t=rows[0]["text"], ref=_ref(rows[0]["list_name"]))


def _do_clear_list(list_name: str) -> str:
    list_name = list_name or DEFAULT_LIST
    n = len(_items(list_name))
    l = lang.current()
    ref = _ref(list_name, l)
    if not n:
        return _tr("clear_list_empty", l, Ref=_cap(ref), ref=ref)

    def execute() -> str:
        _clear_list(list_name)
        logger.info("notes: cleared list %r", list_name)
        return _tr("clear_list_done", l, ref=_ref(list_name, l))

    word = _tr("item_one", l) if n == 1 else _tr("item_many", l)
    return confirmation.request("clear_list", _tr("clear_list_q", l, ref=ref, n=n, w=word), execute,
                                summary=_tr("clear_list_summary", l, ref=ref),
                                yes_phrases=("clear it", "clear the list"))


def _execute(kind: str, a: dict) -> str:
    if kind == "add_note":
        return _do_add_note(a["text"])
    if kind == "note_hint":
        return _tr("note_hint")
    if kind == "read_notes":
        return _do_read_notes(a.get("all", False))
    if kind == "last_note":
        return _do_last_note()
    if kind == "notes_count":
        return _do_notes_count()
    if kind == "delete_last_note":
        return _do_delete_last_note()
    if kind == "clear_notes":
        return _do_clear_notes()
    if kind == "add_todo":
        return _do_add_todo(a["list"], a["items"])
    if kind == "read_list":
        return _do_read_list(a["list"])
    if kind == "count":
        return _do_count(a["list"])
    if kind == "done":
        return _do_done(a["text"], a.get("list"))
    if kind == "remove":
        return _do_remove(a["text"], a.get("list"))
    if kind == "clear_list":
        return _do_clear_list(a["list"])
    raise ValueError(f"unknown intent {kind}")


# ------------------------------------------------------------------ public API

def try_handle(transcript: str) -> Optional[str]:
    """The finished sentence to speak if `transcript` is a note / list request (already done), else None."""
    try:
        u = _prep(transcript)
        if u is None:
            return None
        intent = _parse(u)
    except Exception:
        logger.exception("notes: matching failed")
        return None
    if intent is None:
        return None
    kind, args = intent
    logger.info("notes: matched %s %s", kind, {k: v for k, v in args.items() if k != "text"} or "")
    try:
        return _execute(kind, args)
    except Exception:
        logger.exception("notes: %s failed", kind)
        return _tr("sorry")


_ACTIONS = {
    "add_note": "add_note", "note": "add_note", "save_note": "add_note", "create_note": "add_note",
    "take_note": "add_note", "make_note": "add_note", "write_note": "add_note",
    "read_notes": "read_notes", "read_note": "read_notes", "list_notes": "read_notes",
    "get_notes": "read_notes", "show_notes": "read_notes", "notes": "read_notes",
    "add_todo": "add_todo", "add_item": "add_todo", "add_task": "add_todo", "add_to_list": "add_todo",
    "todo": "add_todo", "add_todo_item": "add_todo", "add_to_do": "add_todo", "add_items": "add_todo",
    "read_list": "read_list", "show_list": "read_list", "get_list": "read_list", "list": "read_list",
    "read_todo": "read_list", "read_todos": "read_list", "list_items": "read_list",
    "done": "done", "complete": "done", "mark_done": "done", "tick": "done", "check": "done",
    "finish": "done", "mark_complete": "done", "check_off": "done", "tick_off": "done",
    "remove": "remove", "remove_item": "remove", "delete_item": "remove", "delete": "remove",
    "clear": "clear", "clear_list": "clear", "clear_notes": "clear", "delete_all": "clear",
    "delete_last_note": "delete_last_note", "delete_note": "delete_last_note", "delete_last": "delete_last_note",
}
_NOTE_WORDS = {"note", "notes", "नोट", "नोट्स", "नोटस"}


def manage_notes(action: str = "", text: str = "", list_name: str = "", **_ignored) -> str:
    """
    LLM tool: notes and lists. action = add_note | read_notes | add_todo | read_list | done | remove |
    clear | delete_last_note. `text` is the note / item; `list_name` defaults to "to-do".
    """
    try:
        items_arg = None
        if isinstance(text, (list, tuple)):
            items_arg = [str(t).strip() for t in text if str(t).strip()]
            text = ", ".join(items_arg)
        text = (str(text) if text is not None else "").strip()
        act = re.sub(r"[\s\-]+", "_", str(action or "").strip().lower())
        act = _ACTIONS.get(act, act)
        if act == "add" or act == "":
            act = "add_todo" if (list_name or "").strip() else ("add_note" if text else "")
        raw_list = (str(list_name) if list_name is not None else "").strip()
        name = _canon_list(raw_list) if raw_list else DEFAULT_LIST
        logger.info("notes tool: %s text=%r list=%r", act, text[:40], name)
        if act == "add_note":
            return _do_add_note(text)
        if act == "read_notes":
            if _fold(text).strip() in ("last", "latest", "recent", "most recent", "previous", "newest"):
                return _do_last_note()
            return _do_read_notes(_fold(text).strip() in ("all", "everything"))
        if act == "delete_last_note":
            return _do_delete_last_note()
        if act == "add_todo":
            items = items_arg if items_arg else _split_items(text, name)
            return _do_add_todo(name, items)
        if act == "read_list":
            return _do_read_list(name)
        if act == "done":
            return _do_done(text, name if raw_list else None)
        if act == "remove":
            return _do_remove(text, name if raw_list else None)
        if act == "clear":
            if _fold(raw_list).strip() in {_fold(w) for w in _NOTE_WORDS}:
                return _do_clear_notes()
            return _do_clear_list(name)
        return _tr("unknown_action")
    except Exception:
        logger.exception("notes tool failed")
        return _tr("sorry")


def save_note(text: str) -> int:
    """Stores a note without any speech (used by clipboard_tool: "save my clipboard to a note"). Returns its id.
    Raises ValueError for an empty text and lets database errors through - the caller words the apology."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty note")
    nid = _add_note(text)
    logger.info("notes: saved a note through save_note (%d chars)", len(text))
    return nid


def todo_count(list_name: str = DEFAULT_LIST) -> int:
    """Number of open (not done) items on a list. Never raises."""
    try:
        return len(_items(_canon_list(list_name) if list_name else DEFAULT_LIST, done=False))
    except Exception:
        logger.exception("notes: todo_count failed")
        return 0


def briefing_line() -> str:
    """One or two sentences for the morning briefing about the to-do list ('' if there is nothing)."""
    try:
        open_ = [r["text"] for r in _items(DEFAULT_LIST, done=False)]
        n = len(open_)
        if not n:
            return ""
        if n == 1:
            return _tr("briefing_one", first=open_[0])
        shown = open_[:3]
        if n > 3:
            if lang.current() == "hi":
                listing = ", ".join(shown) + f" और {n - 3} और"
            else:
                listing = ", ".join(shown) + f" and {n - 3} more"
        else:
            listing = _join(shown)
        return _tr("briefing", n=_things(n), items=listing, first=open_[0])
    except Exception:
        logger.exception("notes: briefing_line failed")
        return ""
