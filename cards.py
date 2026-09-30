"""
cards.py - the mini cards under the orb (avatar_orb.html draws them; avatar_server.py carries them).

  timer     every running timer with a live countdown (the page counts down itself; this only tells
            it when a timer starts, changes, finishes or is cancelled)
  music     the song Spotify is playing: album art, title, artist, progress
  weather   today's weather while she gives the morning briefing (and when you ask for the weather)
  note      pop-ups from quiet_mode.py (a timer that finished during a game or a call)
  status    the little game / call / quiet mode chip (quiet_mode.py)

One background thread polls the timers (every second, from the reminders database) and Spotify
(every few seconds, and only while the orb window is open). Spotify needs the login Raziel already
has (.spotify_cache); without it the song still shows, from the Spotify window's title, just
without the album art.

Settings: CARDS_ENABLED, CARDS_TIMERS, CARDS_MUSIC, CARDS_WEATHER, CARDS_SPOTIFY_POLL_SECONDS.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Dict, Optional

import config

logger = logging.getLogger("voice_assistant")

TIMER_DONE_SHOW_SECONDS = 8
MUSIC_PAUSED_HIDE_SECONDS = 90


def _cfg(name: str, default):
    return getattr(config, name, default)


def _server():
    import avatar_server
    return avatar_server


def _orb_on() -> bool:
    if not _cfg("AVATAR_ENABLED", True) or not _cfg("CARDS_ENABLED", True):
        return False
    try:
        import avatar_window
        return getattr(avatar_window, "STYLE", "orb") == "orb"
    except Exception:  # noqa: BLE001
        return str(_cfg("AVATAR_STYLE", "orb")).lower() == "orb"


# ------------------------------------------------------------------ timers

def timer_cards(rows, now: float) -> Dict[str, dict]:
    """reminders.list_active('timer') rows -> cards keyed by id."""
    out = {}
    for r in rows:
        try:
            due, created = float(r["due"]), float(r.get("created") or r["due"])
        except (KeyError, TypeError, ValueError):
            continue
        title = str(r.get("label") or r.get("text") or "").strip() or "Timer"
        title = title[:1].upper() + title[1:]
        out[f"timer:{r['id']}"] = {"id": f"timer:{r['id']}", "kind": "timer", "title": title[:40],
                                   "due": int(due * 1000), "total": max(1, int(due - created))}
    return out


class Timers:
    def __init__(self, list_timers=None, now=time.time):
        self._list = list_timers
        self._now = now
        self.shown: Dict[str, dict] = {}
        self.done_at: Dict[str, float] = {}

    def _rows(self):
        if self._list is not None:
            return self._list()
        import reminders
        return reminders.list_active("timer")

    def tick(self):
        srv = _server()
        now = self._now()
        want = timer_cards(self._rows(), now)
        for cid, card in want.items():
            if self.shown.get(cid) != card:
                srv.show_card(card)
                self.shown[cid] = card
        for cid in [c for c in self.shown if c not in want]:
            card = self.shown[cid]
            if cid not in self.done_at and card["due"] <= (now + 2) * 1000:
                self.done_at[cid] = now                     # it rang: show "done" for a moment
                srv.show_card({**card, "done": True})
            elif cid not in self.done_at:
                srv.hide_card(cid)                          # cancelled
                self.shown.pop(cid, None)
        for cid, t in list(self.done_at.items()):
            if now - t >= TIMER_DONE_SHOW_SECONDS:
                srv.hide_card(cid)
                self.shown.pop(cid, None)
                self.done_at.pop(cid, None)


# ------------------------------------------------------------------ Spotify

def _spotify_ready() -> bool:
    cid = str(_cfg("SPOTIFY_CLIENT_ID", "") or "")
    if not cid or "your" in cid.lower():
        return False
    cache = os.path.join(getattr(config, "_SCRIPT_DIR", "."), ".spotify_cache")
    return os.path.isfile(cache)          # never open a login page from a background thread


def _spotify_client():
    """A Spotify client from the saved login that can NEVER ask to log in (no browser, no prompt) -
    this runs in a background thread. None when the saved login can't be used. spotify_auth keeps the
    saved login's permissions as they are when it refreshes the token (it used to shrink them, and the
    next "play X" opened the login page again)."""
    import spotify_auth
    return spotify_auth.saved_login_client(timeout=5)


def music_card(pb: Optional[dict], now_ms: int) -> Optional[dict]:
    """Spotify's current_playback() -> the music card (None when nothing is loaded)."""
    if not pb or not isinstance(pb, dict):
        return None
    item = pb.get("item") or {}
    if not item:
        return None
    images = (item.get("album") or {}).get("images") or []
    art = ""
    if images:
        small = sorted(images, key=lambda im: abs((im.get("width") or 300) - 128))
        art = small[0].get("url", "")
    artists = ", ".join(a.get("name", "") for a in item.get("artists") or [] if a.get("name"))
    return {"id": "music", "kind": "music", "title": str(item.get("name") or "")[:80], "artist": artists[:80],
            "art": art if art.startswith("https://") else "", "playing": bool(pb.get("is_playing")),
            "progress": int(pb.get("progress_ms") or 0), "duration": int(item.get("duration_ms") or 0),
            "at": now_ms}


def _title_card() -> Optional[dict]:
    """No Spotify login: the song from the Spotify window's title ("Artist - Song")."""
    try:
        import winutil
        for w in winutil.list_windows():
            if str(w.get("exe", "")).lower() != "spotify.exe":
                continue
            title = str(w.get("title") or "")
            if " - " in title and not re.match(r"^spotify(?: premium| free)?$", title, re.I):
                artist, _, song = title.partition(" - ")
                return {"id": "music", "kind": "music", "title": song[:80], "artist": artist[:80], "art": "",
                        "playing": True, "progress": 0, "duration": 0, "at": int(time.time() * 1000)}
    except Exception:  # noqa: BLE001
        pass
    return None


class Music:
    def __init__(self, playback=None, now=time.time):
        self._playback = playback
        self._now = now
        self.last_key = None
        self.paused_since = None
        self.backoff_until = 0.0
        self.shown = False

    def _current(self) -> Optional[dict]:
        if self._playback is not None:
            return self._playback()
        if not _spotify_ready():
            return None
        sp = _spotify_client()
        return sp.current_playback() if sp is not None else None

    def tick(self):
        now = self._now()
        if now < self.backoff_until:
            return
        try:
            card = music_card(self._current(), int(now * 1000))
        except Exception as e:  # noqa: BLE001
            self.backoff_until = now + 60
            logger.debug("cards: Spotify check failed (%s) - trying again in a minute", e)
            card = None
        if card is None and self._playback is None:
            card = _title_card()
        srv = _server()
        if card is None:
            if self.shown:
                srv.hide_card("music")
                self.shown = False
            return
        if not card["playing"]:
            self.paused_since = self.paused_since or now
            if now - self.paused_since > MUSIC_PAUSED_HIDE_SECONDS:
                if self.shown:
                    srv.hide_card("music")
                    self.shown = False
                return
        else:
            self.paused_since = None
        key = (card["title"], card["artist"], card["playing"], card["progress"] // 5000)
        if key != self.last_key or not self.shown:
            srv.show_card(card)
            self.last_key = key
            self.shown = True


# ------------------------------------------------------------------ weather (called by webinfo.py)

_WEATHER_ICONS = [((0,), "☀️"), ((1, 2), "🌤️"), ((3,), "☁️"), ((45, 48), "🌫️"),
                  ((51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82), "🌧️"),
                  ((71, 73, 75, 77, 85, 86), "❄️"), ((95, 96, 99), "⛈️")]


def weather_icon(code, is_day: bool = True) -> str:
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "🌡️"
    for codes, icon in _WEATHER_ICONS:
        if c in codes:
            return "🌙" if icon == "☀️" and not is_day else icon
    return "🌡️"


def show_weather(place: str, data: dict, condition: str = "", seconds: float = 40):
    """webinfo.py hands over an Open-Meteo forecast it just fetched; this shows today's card."""
    if not _orb_on() or not _cfg("CARDS_WEATHER", True):
        return
    try:
        cur = data.get("current") or {}
        daily = data.get("daily") or {}

        def first(key):
            arr = daily.get(key)
            return arr[0] if isinstance(arr, list) and arr else None

        def deg(v):
            return None if v is None else int(round(float(v)))

        card = {"id": "weather", "kind": "weather", "place": str(place or "")[:40],
                "temp": deg(cur.get("temperature_2m")), "hi": deg(first("temperature_2m_max")),
                "lo": deg(first("temperature_2m_min")), "rain": deg(first("precipitation_probability_max")),
                "cond": str(condition or "")[:40],
                "icon": weather_icon(cur.get("weather_code"), bool(cur.get("is_day", 1))),
                "ttl": seconds}
        _server().show_card(card)
    except Exception:  # noqa: BLE001
        logger.debug("cards: weather card failed", exc_info=True)


# ------------------------------------------------------------------ the thread

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def start():
    global _thread
    if not _orb_on() or (_thread and _thread.is_alive()):
        return
    timers, music = Timers(), Music()
    poll_music = float(_cfg("CARDS_SPOTIFY_POLL_SECONDS", 5))

    def loop():
        last_music = 0.0
        while not _stop.is_set():
            try:
                srv = _server()
                if srv.is_connected():
                    if _cfg("CARDS_TIMERS", True):
                        timers.tick()
                    if _cfg("CARDS_MUSIC", True) and time.time() - last_music >= poll_music:
                        last_music = time.time()
                        music.tick()
            except Exception:  # noqa: BLE001
                logger.exception("cards: update failed")
            _stop.wait(1.0)

    _stop.clear()
    _thread = threading.Thread(target=loop, name="orb-cards", daemon=True)
    _thread.start()
    try:
        import shutdown
        shutdown.on_shutdown(_stop.set)
    except Exception:  # noqa: BLE001
        pass
