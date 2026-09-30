"""
avatar_place.py - Raziel's avatar window remembers where you put her and how big you made her.

    drag her anywhere            -> next time she opens right there
    Ctrl + mouse wheel over her  -> bigger / smaller (remembered too)
    "make yourself bigger" / "a bit smaller" / "much bigger"
    "move to the top left corner" / "go to the right" / "move to the middle"
    "reset your size" / "reset your position" / "go back to your usual place and size"
    "बड़ी हो जाओ" / "छोटी हो जाओ"

The window's place and size are saved in avatar_place.json next to this file (a second after
you stop dragging), separately for the orb and for the 3D avatar. If that place is no longer on
any screen (a monitor was unplugged, the resolution changed), she comes back onto the nearest
screen, shrunk if she no longer fits.

avatar_window.py owns the window and hands this module three small functions (read the window's
rectangle, set it, and the screen scale). Everything here works in the window's own screen
coordinates, so a saved place always round-trips exactly, whatever the display scaling.

Settings (config.py): AVATAR_REMEMBER_PLACE, AVATAR_START_X / AVATAR_START_Y, AVATAR_PLACE_FILE.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from typing import Callable, List, Optional, Sequence, Tuple

import config
import lang

logger = logging.getLogger("voice_assistant")

Rect = Tuple[int, int, int, int]            # x, y, width, height (window pixels)
Area = Tuple[int, int, int, int]            # left, top, right, bottom of a screen's work area

SAVE_DELAY_SECONDS = 1.0                    # saved this long after the last move / resize
MIN_VISIBLE = 120                           # this much of her must stay on some screen
MIN_SIZE = 200                              # never smaller than this (times the screen scale)
STEP = 1.2                                  # "bigger" / "smaller"
BIG_STEP = 1.5                              # "much bigger" / "much smaller"
SMALL_STEP = 1.1                            # "a bit bigger" / "a little smaller"
OLD_SPOT = (1420, 180)                      # where she always opened before this existed

_T = {
    "bigger": {"en": "Okay, a bit bigger.", "hi": "ठीक है, थोड़ी बड़ी हो गई।"},
    "smaller": {"en": "Okay, a bit smaller.", "hi": "ठीक है, थोड़ी छोटी हो गई।"},
    "max": {"en": "That's as big as I can get on this screen.", "hi": "इस स्क्रीन पर इससे बड़ी नहीं हो सकती।"},
    "min": {"en": "That's as small as I go.", "hi": "इससे छोटी नहीं हो सकती।"},
    "moved": {"en": "Okay.", "hi": "ठीक है।"},
    "reset_size": {"en": "Back to my usual size.", "hi": "अपने सामान्य साइज़ में वापस।"},
    "reset_place": {"en": "Back to my usual spot.", "hi": "अपनी सामान्य जगह पर वापस।"},
    "reset_both": {"en": "Back to my usual size and spot.", "hi": "अपने सामान्य साइज़ और जगह पर वापस।"},
    "no_window": {"en": "My window isn't open right now.", "hi": "अभी मेरी विंडो खुली नहीं है।"},
}


def _cfg(name: str, default):
    return getattr(config, name, default)


def _tr(key: str) -> str:
    return lang.tr(_T, key)


# ======================================================================== geometry (pure)

def _overlap(rect: Rect, area: Area) -> Tuple[int, int]:
    x, y, w, h = rect
    l, t, r, b = area
    return max(0, min(x + w, r) - max(x, l)), max(0, min(y + h, b) - max(y, t))


def _distance(rect: Rect, area: Area) -> float:
    x, y, w, h = rect
    cx, cy = x + w / 2, y + h / 2
    l, t, r, b = area
    dx = max(l - cx, 0, cx - r)
    dy = max(t - cy, 0, cy - b)
    return math.hypot(dx, dy)


def home_area(rect: Rect, areas: Sequence[Area]) -> Optional[Area]:
    """The screen she is (mostly) on, or the nearest one."""
    if not areas:
        return None
    return min(areas, key=lambda a: (-(_overlap(rect, a)[0] * _overlap(rect, a)[1]), _distance(rect, a)))


def _clamp_into(rect: Rect, area: Area) -> Rect:
    """Fully inside `area`, shrunk (same shape) if it doesn't fit."""
    x, y, w, h = rect
    l, t, r, b = area
    k = min(1.0, (r - l) / max(1, w), (b - t) / max(1, h))
    if k < 1.0:
        w, h = max(1, int(w * k)), max(1, int(h * k))
    x = min(max(x, l), r - w)
    y = min(max(y, t), b - h)
    return int(x), int(y), int(w), int(h)


def fit_rect(rect: Rect, areas: Sequence[Area], min_visible: int = MIN_VISIBLE) -> Rect:
    """A saved place, made usable on today's screens: kept as it is when enough of it is visible and it
    fits its screen (you may park her half off an edge on purpose), otherwise pulled onto the screen it
    overlaps most / is nearest to."""
    rect = tuple(int(v) for v in rect)
    home = home_area(rect, areas)
    if home is None:
        return rect
    x, y, w, h = rect
    ow, oh = _overlap(rect, home)
    fits = w <= home[2] - home[0] and h <= home[3] - home[1]
    if fits and ow >= min(min_visible, w) and oh >= min(min_visible, h):
        return rect
    return _clamp_into(rect, home)


def primary_area(areas: Sequence[Area]) -> Optional[Area]:
    """The main screen: the one that contains (0, 0), else the first."""
    for a in areas:
        if a[0] <= 0 < a[2] and a[1] <= 0 < a[3]:
            return a
    return areas[0] if areas else None


def default_rect(w: int, h: int, areas: Sequence[Area]) -> Rect:
    """Where she opens when nothing is saved: AVATAR_START_X / _Y if set, else on the right-hand side of
    the main screen, a little down from the top (x=1420, y=180 on a 1920x1080 screen at 100%)."""
    sx, sy = _cfg("AVATAR_START_X", None), _cfg("AVATAR_START_Y", None)
    if sx is not None and sy is not None:
        try:
            return fit_rect((int(sx), int(sy), int(w), int(h)), areas)
        except (TypeError, ValueError):
            pass
    area = primary_area(areas)
    if area is None:
        return (OLD_SPOT[0], OLD_SPOT[1], int(w), int(h))
    l, t, r, b = area
    w, h = min(int(w), r - l), min(int(h), b - t)
    x = r - w - round(0.04 * (r - l))
    y = t + round(0.17 * (b - t))
    return _clamp_into((x, y, w, h), area)


def scale_rect(rect: Rect, k: float, areas: Sequence[Area], min_side: int = MIN_SIZE) -> Tuple[Rect, str]:
    """Bigger / smaller around her centre, same shape. -> (new rect, "" | "max" | "min")."""
    x, y, w, h = rect
    nw, nh = w * k, h * k
    limit = ""
    if min(nw, nh) < min_side:
        m = min_side / max(1, min(w, h))
        nw, nh = w * m, h * m
        limit = "min"
    home = home_area(rect, areas)
    if home is not None:
        l, t, r, b = home
        m = min(1.0, (r - l) / nw, (b - t) / nh)
        if m < 1.0:
            nw, nh = nw * m, nh * m
            limit = "max"
    nw, nh = int(round(nw)), int(round(nh))
    if (nw, nh) == (w, h) and k != 1.0:
        limit = limit or ("max" if k > 1 else "min")
    cx, cy = x + w / 2, y + h / 2
    new = (int(round(cx - nw / 2)), int(round(cy - nh / 2)), nw, nh)
    if home is not None:
        new = _clamp_into(new, home)
    return new, limit


def place_rect(rect: Rect, where: str, areas: Sequence[Area]) -> Rect:
    """Same size, moved to a side / corner / the middle of the screen she is on."""
    x, y, w, h = rect
    home = home_area(rect, areas) or (0, 0, 1920, 1040)
    l, t, r, b = home
    m = round(0.02 * (r - l))
    horiz = "left" if "left" in where else "right" if "right" in where else None
    vert = "top" if "top" in where else "bottom" if "bottom" in where else None
    if where in ("middle", "center", "centre"):
        horiz = vert = "middle"
    if horiz == "left":
        x = l + m
    elif horiz == "right":
        x = r - w - m
    elif horiz == "middle":
        x = l + (r - l - w) // 2
    if vert == "top":
        y = t + m
    elif vert == "bottom":
        y = b - h - m
    elif vert == "middle":
        y = t + (b - t - h) // 2
    return _clamp_into((x, y, w, h), home)


# ======================================================================== screens (Windows)

def work_areas() -> List[Area]:
    """Every screen's work area (without the taskbar), in the window's coordinates. [] if unknown."""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        found: List[Tuple[bool, Area]] = []
        proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                  ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

        def cb(hmon, _hdc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcWork
                found.append((bool(info.dwFlags & 1), (r.left, r.top, r.right, r.bottom)))
            return True

        user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.c_void_p, proc, wintypes.LPARAM]
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
        user32.EnumDisplayMonitors(None, None, proc(cb), 0)
        found.sort(key=lambda f: not f[0])                  # the main screen first
        return [a for _, a in found]
    except Exception:  # noqa: BLE001
        logger.debug("avatar_place: couldn't list the screens", exc_info=True)
        return []


# ======================================================================== the saved state

def state_path() -> str:
    return str(_cfg("AVATAR_PLACE_FILE", os.path.join(getattr(config, "_SCRIPT_DIR", "."), "avatar_place.json")))


def remember() -> bool:
    return bool(_cfg("AVATAR_REMEMBER_PLACE", True))


_file_lock = threading.Lock()


def _read_all() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load(style: str) -> Optional[Rect]:
    """The saved rectangle for "orb" / "vrm", or None."""
    if not remember():
        return None
    with _file_lock:
        entry = _read_all().get(style)
    try:
        rect = tuple(int(entry[k]) for k in ("x", "y", "w", "h"))
    except (TypeError, KeyError, ValueError):
        return None
    if rect[2] < 50 or rect[3] < 50 or abs(rect[0]) > 100000 or abs(rect[1]) > 100000:
        return None
    return rect


def _write(style: str, rect: Optional[Rect]):
    path = state_path()
    with _file_lock:
        data = _read_all()
        if rect is None:
            data.pop(style, None)
        else:
            x, y, w, h = rect
            data[style] = {"x": int(x), "y": int(y), "w": int(w), "h": int(h), "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
        data["version"] = 1
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, path)
        except OSError:
            logger.warning("avatar_place: couldn't save %s", path, exc_info=True)


def forget(style: str):
    _write(style, None)


# saving a moment after the last move (a drag sends hundreds of moves)
_pending: Optional[Tuple[str, Rect]] = None
_pending_at = 0.0
_cv = threading.Condition()
_saver: Optional[threading.Thread] = None


_suspended = False


def suspend_saving(flag: bool):
    """While her window is shown big for a moment ("show me the universe"), that size is not her size."""
    global _suspended
    _suspended = bool(flag)


def note(style: str, rect: Rect):
    """The window moved / was resized: save it once things are still for SAVE_DELAY_SECONDS."""
    global _pending, _pending_at, _saver
    if not remember() or _suspended:
        return
    with _cv:
        _pending, _pending_at = (style, tuple(int(v) for v in rect)), time.monotonic()
        if _saver is None or not _saver.is_alive():
            _saver = threading.Thread(target=_save_loop, name="avatar-place", daemon=True)
            _saver.start()
        _cv.notify()


def _save_loop():
    global _pending
    while True:
        with _cv:
            while _pending is None:
                if not _cv.wait(60):
                    return                                  # idle for a minute: the thread ends
            wait = _pending_at + SAVE_DELAY_SECONDS - time.monotonic()
            if wait > 0:
                _cv.wait(wait)
                continue
            item, _pending = _pending, None
        _write(*item)
        logger.debug("avatar_place: saved %s %s", *item)


def flush():
    """Save now (at shutdown)."""
    global _pending
    with _cv:
        item, _pending = _pending, None
    if item is not None:
        _write(*item)


# ======================================================================== the window (set by avatar_window.py)

class Controller:
    def __init__(self, style: str, get_rect: Callable[[], Optional[Rect]], set_rect: Callable[[Rect], None],
                 scale: Callable[[], float], default_size: Tuple[int, int],
                 areas: Callable[[], List[Area]] = work_areas):
        self.style = style
        self.get_rect = get_rect
        self.set_rect = set_rect
        self.scale = scale                  # window pixels per CSS pixel (1.25 at 125 % scaling)
        self.default_size = default_size    # the configured size, in CSS pixels
        self.areas = areas

    def _apply(self, rect: Rect):
        self.set_rect(rect)
        note(self.style, rect)

    def scale_by(self, k: float) -> Tuple[Optional[Rect], str]:
        cur = self.get_rect()
        if cur is None:
            return None, "no_window"
        new, limit = scale_rect(cur, k, self.areas(), min_side=int(MIN_SIZE * self.scale()))
        if new != cur:
            self._apply(new)
        return new, limit

    def move_to(self, where: str) -> Optional[Rect]:
        cur = self.get_rect()
        if cur is None:
            return None
        new = place_rect(cur, where, self.areas())
        if new != cur:
            self._apply(new)
        return new

    def reset(self, what: str = "both") -> Optional[Rect]:
        """what: size | place | both - back to the configured size / the default spot."""
        cur = self.get_rect()
        if cur is None:
            return None
        s = self.scale()
        w, h = (int(round(self.default_size[0] * s)), int(round(self.default_size[1] * s))) \
            if what in ("size", "both") else cur[2:]
        areas = self.areas()
        if what == "size":                               # same centre, usual size
            cx, cy = cur[0] + cur[2] / 2, cur[1] + cur[3] / 2
            new = fit_rect((int(cx - w / 2), int(cy - h / 2), w, h), areas)
        else:
            new = default_rect(w, h, areas)
        self._apply(new)
        return new


_controller: Optional[Controller] = None


def set_controller(c: Optional[Controller]):
    global _controller
    _controller = c


def controller() -> Optional[Controller]:
    return _controller


# ======================================================================== voice commands

_POLITE = r"(?:(?:please|raziel|hey|ok|okay|can you|could you|would you)\s+)*"
_YOU = r"(?:yourself|you|the avatar|the orb|your window|the window|raziel)"
_AMOUNT = r"(?P<amt>much|a lot|way|a (?:little )?bit|a little|slightly|a tiny bit)"
_SIZE_RE = re.compile(
    rf"^{_POLITE}(?:(?:make|get|grow|go|become|be|turn)\s+(?:{_YOU}\s+)?)?(?:{_AMOUNT}\s+)?"
    rf"(?P<dir>bigger|larger|smaller|tinier)(?:\s+(?:please|now|again))*$")
_SIZE2_RE = re.compile(rf"^{_POLITE}(?P<verb>shrink|grow)(?:\s+{_YOU})?(?:\s+{_AMOUNT})?(?:\s+please)?$")
_SIZE3_RE = re.compile(rf"^{_POLITE}(?P<verb>increase|decrease|reduce)\s+(?:your|the avatar's|the orb's|the window)\s+size"
                       rf"(?:\s+{_AMOUNT})?(?:\s+please)?$")
_RESET_RE = re.compile(
    rf"^{_POLITE}(?:reset|restore)\s+(?:your|the avatar'?s?|the orb'?s?|the window'?s?)\s+"
    rf"(?P<a>size|position|place|spot|window|location)(?:\s+and\s+(?P<b>size|position|place|spot|location))?(?:\s+please)?$"
    rf"|^{_POLITE}(?:go|come|move)\s+back\s+to\s+(?:your|the)\s+(?:normal|usual|default|old|original)\s+"
    rf"(?P<c>size|place|spot|position|corner|location)(?:\s+and\s+(?P<d>size|place|spot|position))?(?:\s+please)?$"
    rf"|^{_POLITE}(?:back\s+to\s+)?(?:your\s+)?(?:normal|usual|default)\s+(?P<e>size)(?:\s+please)?$")
_WHERE = (r"(?P<where>top left|top right|bottom left|bottom right|upper left|upper right|lower left|lower right|"
          r"left|right|top|bottom|middle|centre|center)")
_MOVE_RE = re.compile(
    rf"^{_POLITE}(?:move|go|slide|float|shift)(?:\s+(?:yourself|over))?\s+(?:to\s+)?(?:the\s+)?{_WHERE}"
    rf"(?:\s+(?:corner|side|edge|hand side|of the screen|of my screen|of the display))*(?:\s+please)?$")
_HI_SIZE_RE = re.compile(r"^(?:(?P<amt>थोड़ा|थोड़ी|और|बहुत|thoda|thodi|aur|bahut)\s+)?"
                         r"(?P<dir>बड़ी|बड़ा|बड़े|बडी|बडा|छोटी|छोटा|छोटे|badi|bada|chhoti|choti|chhota|chota)\s+"
                         r"(?:हो\s+जाओ|हो\s+जाइए|बनो|ho\s+jao|ho\s+jaao|bano)$")


def _normalise(transcript: str) -> str:
    t = (transcript or "").lower().replace("’", "'")
    t = re.sub(r"[^\w\s'ऀ-ॿ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _step(amt: Optional[str], bigger: bool) -> float:
    k = STEP
    if amt:
        if amt in ("much", "a lot", "way", "बहुत", "bahut"):
            k = BIG_STEP
        elif amt not in ("और", "aur"):
            k = SMALL_STEP
    return k if bigger else 1 / k


def parse(transcript: str) -> Optional[Tuple[str, object]]:
    """-> ("scale", factor) | ("move", where) | ("reset", size|place|both) | None"""
    t = _normalise(transcript)
    if not t:
        return None
    m = _SIZE_RE.match(t)
    if m:
        return "scale", _step(m.group("amt"), m.group("dir") in ("bigger", "larger"))
    m = _SIZE2_RE.match(t)
    if m:
        return "scale", _step(m.group("amt"), m.group("verb") == "grow")
    m = _SIZE3_RE.match(t)
    if m:
        return "scale", _step(m.group("amt"), m.group("verb") == "increase")
    m = _HI_SIZE_RE.match(t)
    if m:
        return "scale", _step(m.group("amt"), m.group("dir") in ("बड़ी", "बड़ा", "बड़े", "बडी", "बडा", "badi", "bada"))
    m = _RESET_RE.match(t)
    if m:
        words = {w for w in m.group("a", "b", "c", "d", "e") if w}
        size = "size" in words
        place = bool(words - {"size"})
        return "reset", "both" if size and place else "size" if size else "place"
    m = _MOVE_RE.match(t)
    if m:
        where = m.group("where").replace("upper", "top").replace("lower", "bottom").replace("centre", "center")
        return "move", where
    return None


def try_handle(transcript: str) -> Optional[str]:
    """main.py matcher."""
    parsed = parse(transcript)
    if parsed is None:
        return None
    if not _cfg("AVATAR_ENABLED", True):
        return None
    c = _controller
    if c is None:
        return _tr("no_window")
    kind, arg = parsed
    if kind == "scale":
        rect, limit = c.scale_by(float(arg))
        if rect is None:
            return _tr("no_window")
        logger.info("Avatar window: %s x%.2f -> %s", "bigger" if arg > 1 else "smaller", arg, rect)
        if limit == "max" and arg > 1:
            return _tr("max")
        if limit == "min" and arg < 1:
            return _tr("min")
        return _tr("bigger" if arg > 1 else "smaller")
    if kind == "move":
        rect = c.move_to(str(arg))
        if rect is None:
            return _tr("no_window")
        logger.info("Avatar window: moved to the %s -> %s", arg, rect)
        return _tr("moved")
    rect = c.reset(str(arg))
    if rect is None:
        return _tr("no_window")
    logger.info("Avatar window: reset %s -> %s", arg, rect)
    return _tr({"size": "reset_size", "place": "reset_place"}.get(str(arg), "reset_both"))
