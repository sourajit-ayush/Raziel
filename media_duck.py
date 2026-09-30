"""
media_duck.py - other apps' sound: is something playing, and turning it down while Raziel listens.

Why: with music or a video playing, the microphone hears the song. An open mic then takes lyrics for
commands, your real sentence arrives mixed with the music ("crazy playlist" became "graphic
playlist"), and Whisper slows right down on it. So main.py:

  * while other apps are playing, only listens for her name (the wake word), not for any speech;
  * the moment she hears "Raziel", turns the other apps down to MEDIA_DUCK_LEVEL (20 %) until she has
    answered, then brings them back - like a phone assistant does.

How: Windows keeps one volume per app ("audio session", the same sliders as the Volume Mixer). A
worker thread (COM lives on it) looks at every session's peak meter a few times a second; sessions of
this very process (her own voice) are never counted or turned down. Turning down multiplies each
app's own slider, and turning back up restores it - unless you moved that slider in the meantime,
which then wins. Windows remembers an app's volume, so an app that CLOSES while turned down (or a
crash) would come back quiet next time: such apps stay on a to-fix list (also in media_duck.json)
and are put back the moment they reappear at the turned-down level.

Needs pycaw (already in requirements.txt). Without it (or off Windows) playing() falls back to the
music listener's "music playing" and duck() does nothing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("voice_assistant")

_HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_HERE, "media_duck.json")

POLL_SECONDS = 0.2              # how often the peak meters are read
REFRESH_SECONDS = 2.0           # how often the list of apps is re-read
SOUND_PEAK = 0.004              # a peak above this (about -48 dBFS) counts as "making sound"
HISTORY_SECONDS = 4.0


def _cfg(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:
        return default


def enabled() -> bool:
    return bool(_cfg("MEDIA_DUCK_ENABLED", True))


def duck_level() -> float:
    try:
        return max(0.0, min(1.0, float(_cfg("MEDIA_DUCK_LEVEL", 0.2))))
    except (TypeError, ValueError):
        return 0.2


class _Session:
    """One app's audio session, as the worker thread sees it."""
    __slots__ = ("key", "name", "pid", "volume", "meter")

    def __init__(self, key, name, pid, volume, meter):
        self.key, self.name, self.pid, self.volume, self.meter = key, name, pid, volume, meter


class PycawBackend:
    """Talks to Windows. Every method runs on the worker thread only (COM objects are per thread)."""

    def __init__(self):
        import warnings
        # pycaw warns about every device property it can't read, each time with a new pointer in the text:
        # every poll would add another entry to Python's warning registry. Not useful here.
        warnings.filterwarnings("ignore", module=r"pycaw(\.|$)")
        warnings.filterwarnings("ignore", message=r".*COMError attempting to get property")
        import comtypes                                    # noqa: F401  (pycaw needs it)
        from pycaw.pycaw import AudioUtilities
        try:
            from pycaw.pycaw import IAudioMeterInformation
        except ImportError:                                # older / newer layouts
            from pycaw.api.endpointvolume import IAudioMeterInformation
        self._utils = AudioUtilities
        self._meter_iface = IAudioMeterInformation

    @staticmethod
    def thread_init():
        try:
            import comtypes
            comtypes.CoInitialize()
        except Exception:
            pass

    @staticmethod
    def thread_done():
        try:
            import comtypes
            comtypes.CoUninitialize()
        except Exception:
            pass

    def sessions(self) -> List[_Session]:
        me = os.getpid()
        out = []
        for s in self._utils.GetAllSessions():
            try:
                pid = int(getattr(s, "ProcessId", 0) or 0)
                if pid in (0, me):                         # system sounds / her own voice
                    continue
                proc = getattr(s, "Process", None)
                name = (proc.name() if proc is not None else "") or f"pid{pid}"
                try:
                    ident = str(s.InstanceIdentifier)
                except Exception:
                    ident = ""
                key = f"{pid}:{ident}"
                volume = s.SimpleAudioVolume
                try:
                    meter = s._ctl.QueryInterface(self._meter_iface)
                except Exception:
                    meter = None
                out.append(_Session(key, name, pid, volume, meter))
            except Exception:
                continue
        return out

    @staticmethod
    def peak(sess: _Session) -> float:
        if sess.meter is None:
            return 0.0
        try:
            return float(sess.meter.GetPeakValue())
        except Exception:
            return 0.0

    @staticmethod
    def get_volume(sess: _Session) -> Optional[float]:
        try:
            return float(sess.volume.GetMasterVolume())
        except Exception:
            return None

    @staticmethod
    def set_volume(sess: _Session, level: float) -> bool:
        try:
            sess.volume.SetMasterVolume(max(0.0, min(1.0, float(level))), None)
            return True
        except Exception:
            return False


class MediaWatcher:
    """The worker: watches other apps' sound and applies duck / unduck requests."""

    def __init__(self, backend_factory: Callable[[], object] = PycawBackend, clock: Callable[[], float] = time.monotonic,
                 state_file: str = STATE_FILE):
        self._factory = backend_factory
        self._clock = clock
        self._state_file = state_file
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.available: Optional[bool] = None           # None = not known yet
        self._history: deque = deque()                   # (time, any sound?)
        self._last_sound = -1e9
        self._want_duck = False
        self._duck_since = 0.0
        self._ducked: Dict[str, Tuple[float, float, str]] = {}   # key -> (original, ducked, app name)
        # apps that went away (closed / crashed / device changed) before their volume was put back:
        # app name -> (original, ducked, when). Fixed as soon as they reappear at the ducked level.
        self._pending: Dict[str, Tuple[float, float, float]] = {}
        self._sessions: List[_Session] = []
        self._refreshed = -1e9
        self.loud_apps: List[str] = []

    # ------------------------------------------------------------ called from any thread
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="media-duck", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 1.5):
        """Puts every app's volume back and ends the worker."""
        self.unduck("stopping")
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)

    def duck(self, reason: str = ""):
        if not enabled():
            return
        with self._lock:
            if not self._want_duck:
                self._want_duck = True
                if reason:
                    logger.debug("media: turning other apps down (%s)", reason)
            self._duck_since = self._clock()
        self._wake.set()

    def touch(self):
        """Still busy with this turn (a long answer): don't let the safety limit bring the music back."""
        with self._lock:
            if self._want_duck:
                self._duck_since = self._clock()

    def unduck(self, reason: str = ""):
        with self._lock:
            was = self._want_duck
            self._want_duck = False
        if was and reason:
            logger.debug("media: bringing other apps back (%s)", reason)
        self._wake.set()

    def is_ducked(self) -> bool:
        return self._want_duck

    def playing(self, within: float = 1.2, fraction: float = 0.5) -> bool:
        """Did other apps make sound for most of the last `within` seconds? (A 'ding' doesn't count.)"""
        now = self._clock()
        with self._lock:
            pts = [s for t, s in self._history if now - t <= within]
        if len(pts) < 3:
            return False
        return sum(pts) / len(pts) >= fraction

    def silent_for(self) -> float:
        """Seconds since other apps last made any sound."""
        return max(0.0, self._clock() - self._last_sound)

    # ------------------------------------------------------------ the worker thread
    def _run(self):
        backend = None
        try:
            backend = self._factory()
            init = getattr(backend, "thread_init", None)
            if init:
                init()
            self.available = True
        except Exception as e:  # noqa: BLE001
            self.available = False
            logger.info("media: can't see other apps' sound (%s) - music detection falls back to the music "
                        "listener and nothing is turned down.", e)
            return
        try:
            self._load_saved()
        except Exception:  # noqa: BLE001
            logger.debug("media: couldn't read %s", self._state_file, exc_info=True)
        try:
            while not self._stop.is_set():
                try:
                    self.step(backend)
                except Exception:  # noqa: BLE001
                    logger.debug("media: poll failed", exc_info=True)
                self._wake.wait(POLL_SECONDS)
                self._wake.clear()
            try:
                self._apply(backend, False)              # the last thing it does: volumes back
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._sessions = []                          # (COM objects released before COM itself)
            done = getattr(backend, "thread_done", None)
            if done:
                done()

    def step(self, backend):
        """One poll: refresh the app list now and then, read the meters, apply duck / unduck."""
        now = self._clock()
        if now - self._refreshed >= REFRESH_SECONDS or not self._sessions:
            self._sessions = backend.sessions()
            self._refreshed = now
        loud = [s.name for s in self._sessions if backend.peak(s) >= SOUND_PEAK]
        with self._lock:
            self._history.append((now, bool(loud)))
            while self._history and now - self._history[0][0] > HISTORY_SECONDS:
                self._history.popleft()
            if loud:
                self._last_sound = now
            self.loud_apps = sorted(set(loud))
            want = self._want_duck
            if want and now - self._duck_since > float(_cfg("MEDIA_DUCK_MAX_SECONDS", 300)):
                logger.info("media: other apps were turned down for too long - bringing them back")
                self._want_duck = want = False
        self._apply(backend, want)
        if not want and self._pending:
            self._fix_pending(backend)

    def _apply(self, backend, want: bool):
        if want:
            level = duck_level()
            changed = False
            for s in self._sessions:
                if s.key in self._ducked:
                    continue
                orig = backend.get_volume(s)
                if orig is None:
                    continue
                target = orig * level
                if backend.set_volume(s, target):
                    self._ducked[s.key] = (orig, target, s.name)
                    changed = True
            if changed:
                self._save()
            return
        if not self._ducked:
            return
        live = {s.key: s for s in self._sessions}
        for key, (orig, target, name) in list(self._ducked.items()):
            s = live.get(key)
            if s is not None:
                cur = backend.get_volume(s)
                if cur is not None and abs(cur - target) <= 0.02:      # you didn't move its slider meanwhile
                    if not backend.set_volume(s, orig):
                        self._pending[name] = (orig, target, time.time())
            else:
                # the app closed (or its audio restarted) while turned down: Windows will reopen it quiet
                self._pending[name] = (orig, target, time.time())
            del self._ducked[key]
        self._save()

    def _fix_pending(self, backend):
        """Apps that came back after closing while turned down: put their volume back."""
        changed = False
        for s in self._sessions:
            item = self._pending.get(s.name)
            if item is None:
                continue
            orig, target, _when = item
            cur = backend.get_volume(s)
            if cur is None:
                continue
            if abs(cur - target) <= 0.02:
                if backend.set_volume(s, orig):
                    logger.info("media: %s came back turned down - put its volume back", s.name)
            del self._pending[s.name]                  # fixed, or you have set it yourself since
            changed = True
        cutoff = time.time() - 30 * 86400
        for name in [n for n, (_o, _t, when) in self._pending.items() if when < cutoff]:
            del self._pending[name]
            changed = True
        if changed:
            self._save()

    # ------------------------------------------------------------ crash safety
    def _save(self):
        try:
            data = [{"name": n, "original": o, "ducked": t} for (o, t, n) in self._ducked.values()]
            data += [{"name": n, "original": o, "ducked": t, "when": w} for n, (o, t, w) in self._pending.items()]
            if data:
                with open(self._state_file, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            elif os.path.exists(self._state_file):
                os.remove(self._state_file)
        except Exception:  # noqa: BLE001
            logger.debug("media: couldn't write %s", self._state_file, exc_info=True)

    def _load_saved(self):
        """The last run ended (crash, forced exit) while apps were turned down: they go on the to-fix list,
        and are put back as soon as they're seen at the turned-down level (now, or when next opened)."""
        if not os.path.exists(self._state_file):
            return
        with open(self._state_file, encoding="utf-8") as f:
            saved = json.load(f)
        for item in saved if isinstance(saved, list) else []:
            try:
                self._pending[str(item["name"])] = (float(item["original"]), float(item["ducked"]),
                                                    float(item.get("when", time.time())))
            except (KeyError, TypeError, ValueError):
                continue
        if self._pending:
            logger.info("media: %d app volume(s) were left turned down last time - putting them back when seen",
                        len(self._pending))


# ------------------------------------------------------------------ module API (main.py)

_watcher: Optional[MediaWatcher] = None


def start() -> Optional[MediaWatcher]:
    """Starts watching (idempotent, never raises)."""
    global _watcher
    try:
        if _watcher is None:
            _watcher = MediaWatcher()
        _watcher.start()
        try:
            import shutdown
            shutdown.on_shutdown(stop)
        except Exception:
            pass
        import atexit
        atexit.register(stop)
    except Exception:  # noqa: BLE001
        logger.exception("media: couldn't start")
    return _watcher


def stop():
    if _watcher is not None:
        _watcher.stop()


def _music_listener_playing() -> bool:
    try:
        import music_listener
        lst = getattr(music_listener, "_listener", None)
        return bool(lst is not None and lst.playing)
    except Exception:
        return False


def playing(within: float = 1.2) -> bool:
    """Is music / a video / anything else playing in another app right now?"""
    if not enabled():
        return False
    w = _watcher
    if w is not None and w.available:
        return w.playing(within)
    return _music_listener_playing()


def can_tell() -> bool:
    """Can it tell whether something is playing at all? (pycaw, or the orb's music listener running.)"""
    if not enabled():
        return False
    w = _watcher
    if w is not None and w.available:
        return True
    try:
        import music_listener
        lst = getattr(music_listener, "_listener", None)
        return bool(lst is not None and lst.available)
    except Exception:
        return False


def touch():
    if _watcher is not None:
        _watcher.touch()


def silent_for() -> float:
    w = _watcher
    if w is not None and w.available:
        return w.silent_for()
    return 0.0 if _music_listener_playing() else 60.0


def duck(reason: str = ""):
    if _watcher is not None and _watcher.available:
        _watcher.duck(reason)


def unduck(reason: str = ""):
    if _watcher is not None:
        _watcher.unduck(reason)


def is_ducked() -> bool:
    return bool(_watcher is not None and _watcher.available and _watcher.is_ducked())


def loud_apps() -> List[str]:
    return list(_watcher.loud_apps) if _watcher is not None else []
