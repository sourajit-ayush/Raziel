"""
universes.py - Raziel's universe: she is the NEXUS at the centre, and six beings each look after a
part of what she can do. Say a being's name first and it takes the request:

    "Raziel"  ->  "Listening."
    "Melody, play Kalyani"                  music      Spotify, YouTube, playlists, the queue
    "Hermes, message Fazal saying hi"       messages   WhatsApp, Gmail, Calendar, your phone
    "Athena, what is virtual memory?"       knowledge  your files, notes, the web, ChatGPT / Gemini
    "Atlas, open my COA folder"             forge      apps, folders, typing, screenshots, PC controls
    "Chronos, remind me at 6"               time       reminders, timers, to-do lists, weather, news
    "Sentinel, lock my laptop"              shield     Voice ID, quiet mode, locking, the phone link

Without a name she decides herself: whatever handles the request (a matcher in main.py, or the tool the
model calls) is the universe that lights up on the map. A named being answers in its own style and is
told which of her tools are its own. (The model still gets the same full tool list every time: Ollama
keeps the start of the request - system prompt and tools - cached, and a different list would make it
read everything again, 10+ s on this PC.)

The look (avatar_orb.html, form "universe"): the Nexus star in the middle, one galaxy per being around
it with its name and symbol, each being's helpers as small stars. When a being works, the camera flies
into its galaxy. "Show me the universe" opens it big.

Settings (config.py, all optional): UNIVERSES_ENABLED, UNIVERSE_HOME ("universe" | "orb"),
UNIVERSE_NAMES ({"music": "Melody", ...} to rename a being), UNIVERSE_BIG_SECONDS.
"""
import logging
import re
import threading
import time
from typing import Dict, Iterable, List, Optional, Tuple

import config

logger = logging.getLogger("voice_assistant")


def _cfg(name: str, default):
    return getattr(config, name, default)


# ---------------------------------------------------------------- who is who
# glyph: the symbol on the map (音 sound, 信 message, 知 knowledge, 創 create, 時 time, 盾 shield, 核 core).
NEXUS = {
    "key": "nexus", "name": "Raziel", "title": "The Nexus", "role": "everything runs through here",
    "color": "#ffb84d", "glyph": "核", "aliases": (),
    "helpers": ["Memory", "Conversation", "Calculator", "Briefing"],
    "tools": None,                                       # all of them
}
BEINGS: List[Dict] = [
    {"key": "music", "name": "Melody", "title": "The Music", "role": "music",
     "color": "#ff5cb8", "glyph": "音",
     "aliases": ("melody", "melodie", "melodi", "melodee", "mellody", "malady", "मेलोडी", "मेलडी"),
     "helpers": ["Spotify", "YouTube", "Playlists", "Queue", "Dance"],
     "tools": ["play_music", "play_playlist", "pause_music", "resume_music", "next_track", "previous_track",
               "queue_music", "open_website_search", "open_app"],
     "persona": "the music being: Spotify, YouTube, playlists and the queue. Lively, a few words"},
    {"key": "messages", "name": "Hermes", "title": "The Messenger", "role": "messages",
     "color": "#4be38a", "glyph": "信",
     "aliases": ("hermes", "hermez", "hermis", "hermies", "herms", "hermès", "hermess", "her miss", "हर्मीस", "हर्मेस", "हरमीस"),
     "helpers": ["WhatsApp", "Gmail", "Calendar", "Contacts", "Phone"],
     "tools": ["send_whatsapp_message", "compose_email", "email_action", "calendar_action", "control_phone"],
     "persona": "the messenger: WhatsApp, Gmail, the calendar and the phone. Quick and precise"},
    {"key": "knowledge", "name": "Athena", "title": "The Knowledge", "role": "knowledge",
     "color": "#b28cff", "glyph": "知",
     "aliases": ("athena", "athina", "atheena", "athene", "a thena", "atena", "athenaa", "एथेना", "अथीना", "एथीना"),
     "helpers": ["Files", "Notes", "Web", "ChatGPT", "Gemini", "Screen"],
     "tools": ["web_search", "search_my_files", "open_file", "type_into_ai_site", "describe_screen",
               "clipboard_action", "manage_notes", "calculate", "recall"],
     "persona": "the knowledge being: your files and notes, the web, ChatGPT and Gemini, study help. "
                "Clear and patient; explains simply"},
    {"key": "forge", "name": "Atlas", "title": "The Forge", "role": "the PC",
     "color": "#ff7a33", "glyph": "創",
     "aliases": ("atlas", "atlus", "atlass", "atles", "atlis", "एटलस", "ऐटलस"),
     "helpers": ["Apps", "Folders", "Typing", "Screenshots", "Volume", "Windows"],
     "tools": ["open_app", "open_folder", "open_file", "write_file", "take_screenshot", "show_last_screenshot",
               "dictation_control", "system_control"],
     "persona": "the forge: apps, folders, typing, screenshots and the PC's controls. Strong and direct"},
    {"key": "time", "name": "Chronos", "title": "The Keeper of Time", "role": "time",
     "color": "#47d4ff", "glyph": "時",
     "aliases": ("chronos", "kronos", "cronos", "cronus", "kronus", "chronus", "khronos", "chronose", "क्रोनोस", "क्रोनॉस", "क्रोनस"),
     "helpers": ["Reminders", "Timers", "To-do", "Weather", "News", "Briefing"],
     "tools": ["set_reminder", "manage_reminders", "manage_notes", "get_weather", "get_news", "daily_briefing",
               "calendar_action", "get_current_datetime"],
     "persona": "the keeper of time: reminders, timers, to-do lists, weather, news and the day's plan. Calm"},
    {"key": "shield", "name": "Sentinel", "title": "The Shield", "role": "security",
     "color": "#ff4d5e", "glyph": "盾",
     "aliases": ("sentinel", "sentinal", "centinel", "sentenel", "sentinels", "sentinell", "सेंटिनल", "सेंटीनल", "सेंटिनेल"),
     "helpers": ["Voice ID", "Lock", "Quiet mode", "Phone link", "Camera"],
     "tools": ["system_control", "control_phone"],
     "persona": "the shield: locking the PC, quiet mode, Voice ID and the phone link. Watchful, brief"},
]
_COMMON_TOOLS = ("get_current_datetime", "remember", "recall")

# Which universe did it, when the name wasn't said: main.py's matchers ...
MATCHER_UNIVERSE = {
    "play-music": "music", "queue-music": "music", "youtube-nth": "music", "quick-command": "music",
    "open-site-action": "music",
    "whatsapp": "messages", "calendar-mail": "messages",
    "file-search": "knowledge", "open-ai-chat": "knowledge", "ai-send": "knowledge", "ai-followup": "knowledge",
    "ai-answer": "knowledge", "screen-vision": "knowledge", "clipboard": "knowledge", "web-search": "knowledge",
    "live-question": "knowledge", "calculator": "knowledge",
    "open-app": "forge", "open-folder": "forge", "dictation": "forge", "screenshot": "forge",
    "system-control": "forge", "avatar-place": "forge",
    "reminders": "time", "notes": "time", "weather": "time", "news": "time", "date-time": "time", "briefing": "time",
    "quiet-mode": "shield", "gestures": "shield",
}
# ... and the model's tools (the first tool it calls decides).
TOOL_UNIVERSE: Dict[str, str] = {}
for _b in BEINGS:
    for _t in _b["tools"]:
        TOOL_UNIVERSE.setdefault(_t, _b["key"])
TOOL_UNIVERSE.update({"system_control": "forge", "calendar_action": "messages", "manage_notes": "time",
                      "calculate": "knowledge", "open_app": "forge", "open_file": "forge",
                      "get_current_datetime": "time"})
# "Sentinel, lock my laptop" is the shield; "turn the volume up" is the forge
_SHIELD_WORDS = re.compile(r"\b(?:lock|quiet mode|do not disturb|voice id|camera|stop watching|watch my hands)\b", re.I)


def enabled() -> bool:
    return bool(_cfg("UNIVERSES_ENABLED", True))


def beings() -> List[Dict]:
    """The six beings, with any names renamed in config.UNIVERSE_NAMES."""
    names = _cfg("UNIVERSE_NAMES", {}) or {}
    out = []
    for b in BEINGS:
        b = dict(b)
        new = str(names.get(b["key"], "") or "").strip()
        if new and new.lower() != b["name"].lower():
            b["name"] = new
            b["aliases"] = (new.lower(),)
        out.append(b)
    return out


def being(key: Optional[str]) -> Optional[Dict]:
    if key == "nexus":
        return dict(NEXUS)
    for b in beings():
        if b["key"] == key:
            return b
    return None


def names() -> List[str]:
    """The beings' names, for Whisper's spelling hints (and its bare-name check)."""
    return [b["name"] for b in beings()] if enabled() else []


# ---------------------------------------------------------------- "Melody, play Kalyani"
_LEAD = r"^(?:(?:ok|okay|hey|hi|so|and|now|please|raziel|yo)[\s,.!]+)*"
# Without a comma / pause after the name, the next word must start a request: "Melody play Kalyani" is
# for Melody, "Atlas cycle price" or "Hermes bag price" are just sentences.
_REQUEST_START = set("""
play pause stop resume next skip previous shuffle queue open close launch start run message text send whatsapp
email mail call remind set add delete remove clear cancel snooze what what's whats who whom how when where why
which explain tell show search find look lock unlock turn switch mute unmute volume brightness type write take
read check create make put give go can could please list help summarize summarise translate calculate define
describe answer note save copy paste schedule book
""".split())
_HI_REQUEST = re.compile(r"[\u0900-\u097F]")                    # a Hindi sentence after the name: a request


def _alias_re() -> Tuple[re.Pattern, Dict[str, str]]:
    table = {}
    for b in beings():
        for a in (b["name"].lower(),) + tuple(b.get("aliases") or ()):
            table[a] = b["key"]
    alts = "|".join(re.escape(a).replace(r"\ ", r"\s+") for a in sorted(table, key=len, reverse=True))
    # "ask Athena ...", "tell Hermes to ..." (not "tell Melody I'm late" - that's a message to a person
    # called Melody - and not "call Melody")
    return re.compile(_LEAD + rf"(?:(?P<ask>ask)\s+|(?P<tell>tell)\s+)?(?P<name>{alts})(?:'s)?"
                      r"(?=[\s,.!?:;\u0964-]|$)(?P<rest>.*)$", re.I | re.S), table


def split_name(transcript: str) -> Tuple[Optional[str], str]:
    """ "Melody, play Kalyani" -> ("music", "play Kalyani"); "Melody." -> ("music", ""); no being -> (None, text)."""
    text = (transcript or "").strip()
    if not enabled() or not text:
        return None, text
    rx, table = _alias_re()
    m = rx.match(text)
    if not m:
        return None, text
    key = table.get(re.sub(r"\s+", " ", m.group("name").lower()))
    raw = m.group("rest")
    if not key:
        return None, text
    paused = bool(re.match(r"^\s*[,.!?:;\u0964-]", raw)) or not raw.strip()
    rest = re.sub(r"^[\s,.!?:;\u0964-]+", "", raw).strip()
    if m.group("tell"):
        if not re.match(r"^to\s+", rest, re.I):
            return None, text                                # "tell Melody I'm running late": a person
        rest = rest[3:].strip()
    elif m.group("ask"):
        rest = re.sub(r"^(?:to|please)\s+", "", rest, flags=re.I).strip()   # "ask Athena to explain ..."
    elif not paused:
        first = rest.split()[0].lower().strip(",.!?") if rest else ""
        if first not in _REQUEST_START and not _HI_REQUEST.match(rest):
            return None, text                                # "Atlas cycle price", "melody of this song ..."
    if not rest and (m.group("ask") or m.group("tell")):
        return None, text
    return key, rest


# ---------------------------------------------------------------- the tools a named being gets
def tool_schemas(key: Optional[str], all_schemas: Iterable[Dict]) -> Optional[List[Dict]]:
    """A being's own tools (for its hint and the tests). The model is still sent all of them (see above)."""
    b = being(key)
    if not b or not b.get("tools"):
        return None
    wanted = set(b["tools"]) | set(_COMMON_TOOLS)
    out = [s for s in all_schemas if (s.get("function") or {}).get("name") in wanted]
    return out or None


def persona_hint(key: Optional[str]) -> str:
    """Added to the user's message for the model (the system prompt must stay the same for Ollama's cache)."""
    b = being(key)
    if not b or key == "nexus":
        return ""
    return (f"[This turn {b['name']} answers - {b['persona']}. Speak as {b['name']}, one of the beings in "
            f"Raziel's universe. {b['name']}'s own tools: {', '.join(b['tools'])}.]")


# ---------------------------------------------------------------- what the map shows
_state = {"focus": None, "turn": None, "pending": None, "pending_until": 0.0, "prev_form": None,
          "big": False, "saved_rect": None, "big_timer": None, "save_timer": None}
_lock = threading.RLock()


def _avatar():
    try:
        import avatar_server
        return avatar_server
    except Exception:  # noqa: BLE001
        return None


def page_config() -> Dict:
    def pack(b):
        return {"key": b["key"], "name": b["name"], "title": b["title"], "color": b["color"], "glyph": b["glyph"],
                "helpers": list(b.get("helpers") or [])[:6]}
    return {"type": "universe", "nexus": pack(NEXUS), "beings": [pack(b) for b in beings()],
            "home": str(_cfg("UNIVERSE_HOME", "universe") or "universe").lower()}


def start():
    """At start-up: the page learns the beings; her resting look becomes the universe (UNIVERSE_HOME)."""
    if not enabled():
        return
    a = _avatar()
    if a is None or not hasattr(a, "set_universe"):
        return
    a.set_universe(page_config())
    if page_config()["home"] == "universe" and a.current_form() == "orb":
        a.set_form("universe")


def _show(key: Optional[str], switch: bool = True):
    """The camera goes to this being. `switch`: she becomes the universe if she has another shape (the
    shape comes back when she sleeps); False (what did the work, no name said) only moves the camera."""
    a = _avatar()
    if a is None or not hasattr(a, "set_universe"):
        return
    with _lock:
        _state["focus"] = key
        if key and switch and a.current_form() != "universe":
            _state["prev_form"] = _state["prev_form"] or a.current_form()
            a.set_form("universe")
    a.set_universe({"focus": key})


def begin_turn(key: Optional[str]):
    """A new request: `key` = the being named in it (or waiting since "Melody." on its own)."""
    with _lock:
        if key is None and _state["pending"] and time.time() < _state["pending_until"]:
            key = _state["pending"]
        _state["pending"] = None
        _state["turn"] = key
    if key and enabled():
        _show(key)


def turn_being() -> Optional[str]:
    return _state["turn"]


def end_turn():
    """The spoken turn is over: a phone command or an announcement after it is nobody's being."""
    with _lock:
        _state["turn"] = None


def name_only(key: str) -> str:
    """ "Melody." on its own: she answers as that being; the next sentence is for it."""
    with _lock:
        _state["pending"], _state["pending_until"] = key, time.time() + 30
    _show(key)
    b = being(key)
    return f"{b['name']} here." if b else "Yes?"


def note_matcher(label: str, transcript: str = ""):
    """A matcher in main.py handled it: that universe lights up (unless a being was named)."""
    if not enabled() or _state["turn"]:
        return
    key = MATCHER_UNIVERSE.get(label)
    if key == "forge" and label == "system-control" and _SHIELD_WORDS.search(transcript or ""):
        key = "shield"
    if key:
        _show(key, switch=False)


def note_tools(tool_names: Iterable[str]):
    """The model called these tools: the first one's universe lights up (unless a being was named)."""
    if not enabled() or _state["turn"]:
        return
    for name in tool_names or ():
        key = TOOL_UNIVERSE.get(name)
        if key:
            _show(key, switch=False)
            return


def end_session():
    """She went back to sleep: the camera flies out to the whole universe (or back to her previous shape)."""
    with _lock:
        _state["pending"], _state["turn"] = None, None
        prev, _state["prev_form"] = _state["prev_form"], None
        had_focus = _state["focus"] is not None
        _state["focus"] = None
    a = _avatar()
    if a is None or not hasattr(a, "set_universe"):
        return
    if had_focus:
        a.set_universe({"focus": None})
    if prev and prev != "universe" and a.current_form() == "universe":
        a.set_form(prev)                                    # the shape she had (the orb, a dragon...)


# ---------------------------------------------------------------- "show me the universe" (big)
def _controller():
    try:
        import avatar_place
        return avatar_place.controller()
    except Exception:  # noqa: BLE001
        return None


def show_big(seconds: Optional[float] = None) -> bool:
    """The window grows to most of the screen for a while (not remembered as her size)."""
    c = _controller()
    a = _avatar()
    if a is not None and hasattr(a, "set_universe"):
        if a.current_form() != "universe":
            with _lock:
                _state["prev_form"] = _state["prev_form"] or a.current_form()
            a.set_form("universe")
        a.set_universe({"focus": None, "big": True})
    if c is None:
        return False
    with _lock:
        if not _state["big"]:
            cur = c.get_rect()
            if cur is None:
                return False
            try:
                import avatar_place
                areas = c.areas()
                area = avatar_place.home_area(cur, areas) or avatar_place.primary_area(areas)
            except Exception:  # noqa: BLE001
                area = None
            if not area:
                return False
            st = _state["save_timer"]
            if st is not None:
                st.cancel()                                  # (a re-enable still pending from the last close)
                _state["save_timer"] = None
            avatar_place.suspend_saving(True)
            left, top, right, bottom = area                  # (a work area: edges, not a size)
            aw, ah = right - left, bottom - top
            side = int(min(aw, ah) * 0.86)
            w, h = side, min(ah, int(side * 1.1))
            new = (left + (aw - w) // 2, top + (ah - h) // 2, w, h)
            _state["saved_rect"], _state["big"] = cur, True
            c.set_rect(new)
        t = _state["big_timer"]
        if t is not None:
            t.cancel()
        secs = float(seconds if seconds is not None else _cfg("UNIVERSE_BIG_SECONDS", 40))
        if secs > 0:
            t = threading.Timer(secs, hide_big)
            t.daemon = True
            t.start()
            _state["big_timer"] = t
    return True


def hide_big() -> bool:
    c = _controller()
    with _lock:
        if not _state["big"]:
            return False
        saved, _state["saved_rect"], _state["big"] = _state["saved_rect"], None, False
        t, _state["big_timer"] = _state["big_timer"], None
    if t is not None:
        t.cancel()
    a = _avatar()
    if a is not None and hasattr(a, "set_universe"):
        a.set_universe({"big": False})
    if c is not None and saved is not None:
        c.set_rect(saved)
    def resume():
        with _lock:
            _state["save_timer"] = None
            if _state["big"]:
                return                                       # opened again meanwhile: stays suspended
        try:
            import avatar_place
            avatar_place.suspend_saving(False)
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        st = threading.Timer(1.5, resume)                    # (after the resize events of the restore)
        st.daemon = True
        _state["save_timer"] = st
        st.start()
    return True


# ---------------------------------------------------------------- voice commands about the universe itself
_SHOW_RE = re.compile(r"^(?:(?:please|raziel|ok(?:ay)?|hey|now)[\s,]+)*(?:show(?: me)?|open|display|let me see|zoom out to)"
                      r"\s+(?:the |your |my )?(?:whole |entire |full )?(?:universe|universes|cosmos|galaxy map|"
                      r"universe map|map of (?:the |your )?universe)(?:\s+(?:please|now))?[.!?]*$", re.I)
_HIDE_RE = re.compile(r"^(?:(?:please|raziel|ok(?:ay)?|now)[\s,]+)*(?:close|hide|shrink|exit|leave)\s+(?:the |your )?"
                      r"(?:universe|universes|cosmos|universe map|big view)(?:\s+(?:please|now))?[.!?]*$|"
                      r"^(?:back to (?:your |the )?normal size|make yourself small again)[.!?]*$", re.I)
_WHO_RE = re.compile(r"\b(?:who (?:lives|live|is|are) in (?:your|the) universe|who are (?:your|the) beings|"
                     r"(?:list|name|introduce) (?:your |the |all )?(?:beings|universes|team|crew)|"
                     r"what (?:beings|universes) do you have|tell me about your universe)\b", re.I)
_VISIT_RE = re.compile(r"^(?:(?:please|raziel|ok(?:ay)?)[\s,]+)*(?:show me|go to|take me to|zoom (?:in )?(?:to|on))\s+"
                       r"(?P<n>[\w\s']+?)(?:'s)?\s+(?:universe|galaxy|world)[.!?]*$", re.I)


def introduce() -> str:
    bs = beings()
    parts = [f"{b['name']} for {b['role']}" for b in bs]
    return (f"{len(bs)} beings live in my universe: " + "; ".join(parts[:-1]) + f"; and {parts[-1]}. "
            f"I'm the Nexus in the middle. Say a name first to call one, like: {bs[0]['name']}, play Kalyani.")


def try_handle(transcript: str) -> Optional[str]:
    if not enabled():
        return None
    t = (transcript or "").strip()
    if not t:
        return None
    if _SHOW_RE.match(t):
        show_big()
        return "Here is my universe."
    if _HIDE_RE.match(t):
        return "Okay." if hide_big() else None
    m = _VISIT_RE.match(t)
    if m:
        key, _rest = split_name(m.group("n"))
        if key:
            _show(key)
            return f"This is {being(key)['name']}'s universe, {being(key)['title'].lower()}."
    if _WHO_RE.search(t):
        _show(None)
        return introduce()
    return None
