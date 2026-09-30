"""
quiet_mode.py - game and call mode: Raziel goes quiet by herself.

  * GAME  - something is full-screen (a game, or a film / YouTube in full screen). She never speaks
            on her own: timers, reminders and her small talk become pop-ups. She still answers when
            you say "Raziel".
  * CALL  - another app is using the microphone (Zoom, Discord, Teams, Google Meet in Chrome, ...).
            Same, and her wake word is off too, so saying "Raziel" to a friend on Discord doesn't
            make her talk into the call.
  * DND   - "do not disturb" / "quiet mode on", until "quiet mode off" / "you can talk again".

How it knows, every few seconds and without any extra package:
  full screen - Windows' own SHQueryUserNotificationState (what apps use to decide whether to show a
                notification), plus "does the window in front cover the whole monitor" for borderless games
  microphone  - Windows' privacy records (the same ones behind the microphone icon in the taskbar):
                HKCU\\...\\CapabilityAccessManager\\ConsentStore\\microphone lists every app that uses the mic
                and whether it is using it right now. Raziel's own Python is left out, of course.

Pop-ups: a Windows notification (via PowerShell, nothing to install) and a card under the orb.
Settings (config.py): QUIET_MODE_ENABLED, QUIET_GAME_MODE, QUIET_CALL_MODE, QUIET_CALL_BLOCKS_WAKE_WORD,
QUIET_MIC_IGNORE (extra .exe names that never count as a call), QUIET_POPUP_TOAST.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional

import config
import lang

logger = logging.getLogger("voice_assistant")

POLL_SECONDS = 3.0
EXIT_AFTER_SECONDS = 8.0          # a mode ends only after this long without its reason (no flapping)
_MIC_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
_SELF_EXES = {"python.exe", "pythonw.exe", "py.exe"}

_T = {
    "game_on": {"en": "Game mode: I'll stay quiet and show timers as pop-ups.",
                "hi": "गेम मोड: मैं चुप रहूँगी, टाइमर पॉप-अप में दिखेंगे।"},
    "call_on": {"en": "{app} is using the mic, so I'm staying quiet.",
                "hi": "{app} माइक इस्तेमाल कर रहा है, इसलिए मैं चुप हूँ।"},
    "dnd_on": {"en": "Okay, I'll stay quiet. Timers and reminders will pop up instead. Say 'quiet mode off' when you want me back.",
               "hi": "ठीक है, मैं चुप रहूँगी। टाइमर और रिमाइंडर पॉप-अप में दिखेंगे। वापस बुलाना हो तो कहिए 'क्वाइट मोड बंद करो'।"},
    "dnd_off": {"en": "Okay, I'm back to talking.", "hi": "ठीक है, मैं फिर से बोलूँगी।"},
    "dnd_was_off": {"en": "I'm not in quiet mode.", "hi": "मैं क्वाइट मोड में नहीं हूँ।"},
    "status_game": {"en": "Something is full screen, so I'm in game mode: quiet unless you call me.",
                    "hi": "कुछ फुल स्क्रीन पर है, इसलिए मैं गेम मोड में हूँ: बुलाने पर ही बोलूँगी।"},
    "status_call": {"en": "{app} is using the microphone, so I'm in call mode.",
                    "hi": "{app} माइक इस्तेमाल कर रहा है, इसलिए मैं कॉल मोड में हूँ।"},
    "status_dnd": {"en": "Quiet mode is on.", "hi": "क्वाइट मोड चालू है।"},
    "status_none": {"en": "No, I'm not in quiet mode.", "hi": "नहीं, मैं क्वाइट मोड में नहीं हूँ।"},
    "chip_game": {"en": "Game mode · quiet", "hi": "गेम मोड · शांत"},
    "chip_call": {"en": "In a call ({app}) · quiet", "hi": "कॉल में ({app}) · शांत"},
    "chip_dnd": {"en": "Quiet mode", "hi": "क्वाइट मोड"},
}


def _cfg(name: str, default):
    return getattr(config, name, default)


# ------------------------------------------------------------------ detection (Windows)

def fullscreen_now() -> bool:
    """True when a full-screen app is in front (games, full-screen video, presentations)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in (2, 3, 4):
            return True            # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        cls = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, cls, 64)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return False
        if user32.IsZoomed(hwnd):
            return False           # a maximised window (covers the screen when the taskbar auto-hides)
        style = user32.GetWindowLongW(hwnd, -16)                 # GWL_STYLE
        if style & 0x00C00000 == 0x00C00000:                     # WS_CAPTION: a normal window with a title bar
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == os.getpid():
            return False
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        mon = user32.MonitorFromWindow(hwnd, 2)              # MONITOR_DEFAULTTONEAREST
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return False
        m = info.rcMonitor
        return (rect.left <= m.left and rect.top <= m.top and rect.right >= m.right and rect.bottom >= m.bottom)
    except Exception as e:  # noqa: BLE001
        logger.debug("quiet_mode: full-screen check failed: %s", e)
        return False


def _pretty(name: str) -> str:
    """'C:#Users#x#AppData#Local#Discord#app-1.0#Discord.exe' -> 'Discord'; 'MSTeams_8wekyb3d8bbwe' -> 'Teams'."""
    base = name.replace("#", "\\").split("\\")[-1]
    base = re.sub(r"\.exe$", "", base, flags=re.I)
    if "_" in base and "\\" not in name.replace("#", "\\"):
        base = base.split("_")[0].split(".")[-1]
    base = re.sub(r"^ms(?=[A-Z])", "", base)
    known = {"zoom": "Zoom", "discord": "Discord", "teams": "Teams", "ms-teams": "Teams", "chrome": "Chrome",
             "msedge": "Edge", "firefox": "Firefox", "skype": "Skype", "skypeapp": "Skype", "slack": "Slack",
             "whatsapp": "WhatsApp", "telegram": "Telegram", "steam": "Steam", "obs64": "OBS", "webex": "Webex",
             "msteams": "Teams", "microsoftteams": "Teams"}
    return known.get(base.lower(), base[:1].upper() + base[1:])


def _ignored_exe(name: str) -> bool:
    path = name.replace("#", "\\").lower()
    exe = path.split("\\")[-1]
    extra = {str(x).lower() for x in _cfg("QUIET_MIC_IGNORE", ())}
    if exe in _SELF_EXES or exe in extra:
        return True
    here = os.path.dirname(os.path.abspath(__file__)).lower()
    return path.startswith(here)


def _running(entry: str) -> bool:
    """Is the app behind a microphone record still running? (A crashed app can leave its record
    saying "in use" for ever.) Unknown -> True."""
    try:
        import psutil
    except ImportError:
        return True
    try:
        exes = []
        for proc in psutil.process_iter(["exe"]):
            exe = (proc.info.get("exe") or "").lower()
            if exe:
                exes.append(exe)
    except Exception:  # noqa: BLE001
        return True
    if "#" in entry:                                   # NonPackaged: the full path of the .exe
        path = entry.replace("#", "\\").lower()
        return any(e == path for e in exes)
    name, _, publisher = entry.lower().partition("_")  # packaged: Name_PublisherId
    return any(f"\\windowsapps\\{name}_" in e and publisher in e for e in exes) if publisher else True


def mic_users() -> List[str]:
    """Apps using the microphone right now (other than Raziel herself), pretty names."""
    return _capability_users(_MIC_KEY)


def camera_users() -> List[str]:
    """Apps using the webcam right now (other than Raziel herself) - gestures.py lets go of it then."""
    return _capability_users(_MIC_KEY.rsplit("\\", 1)[0] + "\\webcam")


def _capability_users(key_path: str) -> List[str]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    found: List[str] = []

    def scan(key, prefix=""):
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(key, i)
            except OSError:
                break
            i += 1
            if sub == "NonPackaged":
                try:
                    with winreg.OpenKey(key, sub) as np_key:
                        scan(np_key, "np:")
                except OSError:
                    pass
                continue
            try:
                with winreg.OpenKey(key, sub) as app:
                    stop = winreg.QueryValueEx(app, "LastUsedTimeStop")[0]
                    start = winreg.QueryValueEx(app, "LastUsedTimeStart")[0]
            except OSError:
                continue
            if int(stop) == 0 and int(start) > 0 and not _ignored_exe(sub) and _running(sub):
                found.append(_pretty(sub))

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as root:
            scan(root)
    except OSError as e:
        logger.debug("quiet_mode: can't read %s: %s", key_path, e)
    return sorted(set(found))


# ------------------------------------------------------------------ state

class QuietMode:
    def __init__(self, fullscreen: Callable[[], bool] = fullscreen_now, mics: Callable[[], List[str]] = mic_users,
                 now: Callable[[], float] = time.monotonic, on_change: Optional[Callable[[str, str], None]] = None):
        self._fullscreen, self._mics, self._now = fullscreen, mics, now
        self._on_change = on_change
        self.mode = ""                    # "" | game | call | dnd
        self.app = ""                     # the app using the mic, in a call
        self.manual = False
        self.override_until = -1e9       # "you can talk again" during a game/call: ignore it for a while
        self._last_seen = {"game": -1e9, "call": -1e9}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def poll(self):
        """One check. Call every few seconds (the thread does)."""
        game_on = bool(_cfg("QUIET_GAME_MODE", True))
        call_on = bool(_cfg("QUIET_CALL_MODE", True))
        apps = self._mics() if call_on else []
        full = self._fullscreen() if game_on else False
        t = self._now()
        with self._lock:
            if apps:
                self._last_seen["call"] = t
                self.app = apps[0]
            if full:
                self._last_seen["game"] = t
            in_call = call_on and t - self._last_seen["call"] < (0.1 if apps else EXIT_AFTER_SECONDS)
            in_game = game_on and t - self._last_seen["game"] < (0.1 if full else EXIT_AFTER_SECONDS)
            if apps:
                in_call = True
            if full:
                in_game = True
            if t < self.override_until:
                in_call = in_game = False
            new = "call" if in_call else "game" if in_game else "dnd" if self.manual else ""
            old = self.mode
            self.mode = new
        if new != old:
            logger.info("Quiet mode: %s -> %s%s", old or "off", new or "off", f" ({self.app})" if new == "call" else "")
            if self._on_change is not None:
                try:
                    self._on_change(old, new)
                except Exception:  # noqa: BLE001
                    logger.exception("quiet_mode: on_change failed")

    def set_manual(self, on: bool):
        with self._lock:
            self.manual = bool(on)
            if on:
                self.override_until = -1e9
            elif self.mode in ("game", "call"):
                # "you can talk again" while a game / call is detected: believe you for half an hour
                self.override_until = self._now() + float(_cfg("QUIET_OVERRIDE_SECONDS", 1800))
        self.poll()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.is_set():
                try:
                    self.poll()
                except Exception:  # noqa: BLE001
                    logger.exception("quiet_mode: check failed")
                self._stop.wait(POLL_SECONDS)

        self._thread = threading.Thread(target=loop, name="quiet-mode", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


_state: Optional[QuietMode] = None


_told_apps: set = set()


def _changed(old: str, new: str):
    """Shows / clears the little status chip under the orb (and, the first time an app puts her in call
    mode, a pop-up saying why - an app that keeps the mic open all the time would otherwise be a mystery)."""
    if new == "call" and _state is not None and _state.app and _state.app not in _told_apps:
        _told_apps.add(_state.app)
        logger.info("Quiet mode: %s is using the microphone - the wake word is paused until it stops. If that "
                    "app isn't a call, add its .exe name to QUIET_MIC_IGNORE in config.py.", _state.app)
        _toast("Raziel", lang.tr(_T, "call_on", app=_state.app))
    try:
        import avatar_server
        if not new:
            avatar_server.hide_card("quiet")
            return
        text = lang.tr(_T, f"chip_{new}", app=_state.app if _state else "")
        icon = {"game": "🎮", "call": "📞", "dnd": "🌙"}[new]
        avatar_server.show_card({"id": "quiet", "kind": "status", "icon": icon, "text": text})
    except Exception:  # noqa: BLE001
        logger.debug("quiet_mode: chip update failed", exc_info=True)


def start() -> Optional[QuietMode]:
    global _state
    if not _cfg("QUIET_MODE_ENABLED", True):
        return None
    if _state is None:
        _state = QuietMode(on_change=_changed)
    _state.start()
    try:
        import shutdown
        shutdown.on_shutdown(_state.stop)
    except Exception:  # noqa: BLE001
        pass
    return _state


def mode() -> str:
    return _state.mode if _state is not None else ""


def active() -> bool:
    """True while she should not speak on her own (game, call or do-not-disturb)."""
    return bool(mode())


def wake_word_blocked() -> bool:
    return mode() == "call" and bool(_cfg("QUIET_CALL_BLOCKS_WAKE_WORD", True))


# ------------------------------------------------------------------ pop-ups instead of speech

def _xml(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&apos;"))


def _toast(title: str, text: str) -> bool:
    """A Windows notification, via PowerShell's own app id (no package, no registration)."""
    if os.name != "nt" or not _cfg("QUIET_POPUP_TOAST", True):
        return False
    xml = (f"<toast><visual><binding template='ToastGeneric'><text>{_xml(title)}</text>"
           f"<text>{_xml(text)}</text></binding></visual><audio silent='true'/></toast>")
    script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml(@'\n{xml}\n'@);"
        "$t = [Windows.UI.Notifications.ToastNotification]::new($x);"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
        "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)"
    )
    try:
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-EncodedCommand", encoded],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("quiet_mode: couldn't show a notification: %s", e)
        return False


def notify(text: str, title: str = "Raziel", icon: str = "⏰"):
    """What she would have said out loud, as a pop-up: a Windows notification + a card under the orb."""
    logger.info("Quiet mode (%s): pop-up instead of speech: %s", mode() or "off", text)
    _toast(title, text)
    try:
        import avatar_server
        avatar_server.show_card({"id": f"note:{int(time.time() * 1000)}", "kind": "note", "icon": icon,
                                 "title": title, "text": text, "ttl": 45})
    except Exception:  # noqa: BLE001
        logger.debug("quiet_mode: card failed", exc_info=True)


# ------------------------------------------------------------------ voice commands

_ON_RE = re.compile(
    r"^(?:(?:please|raziel|okay|ok)\s+)*(?:do not disturb|don't disturb me|dnd|(?:turn on |switch on |enable )?(?:quiet|silent) mode(?: on)?|"
    r"be quiet for a while|stay quiet|go quiet|(?:turn on |switch on )?do not disturb(?: mode)?(?: on)?)(?:\s+please)?$")
_OFF_RE = re.compile(
    r"^(?:(?:please|raziel|okay|ok)\s+)*(?:(?:turn off |switch off |disable )?(?:quiet|silent) mode(?: off)?|quiet mode off|"
    r"you can (?:talk|speak) (?:again|now)|(?:turn off |switch off )?do not disturb(?: off)?|stop being quiet|"
    r"(?:turn off |switch off )?(?:game|call) mode(?: off)?)(?:\s+please)?$")
_STATUS_RE = re.compile(r"^(?:are you (?:in )?(?:quiet|game|call|silent) mode|why are you (?:quiet|silent)|"
                        r"is (?:quiet|game|call) mode on)$")
_HI_ON_RE = re.compile(r"^(?:अब\s+)?(?:चुप रहो|चुप रहना|शांत रहो|डिस्टर्ब मत करो|क्वाइट मोड (?:चालू|ऑन) करो)$")
_HI_OFF_RE = re.compile(r"^(?:अब\s+)?(?:बोल सकती हो|अब बोलो|क्वाइट मोड (?:बंद|ऑफ) करो|चुप रहना बंद करो)$")


def try_handle(transcript: str) -> Optional[str]:
    t = re.sub(r"[^\w\s'ऀ-ॿ]+", " ", (transcript or "").lower().replace("’", "'"))
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    th = lang.fold_hindi(t)
    if _state is None and not (_ON_RE.match(t) or _OFF_RE.match(t) or _STATUS_RE.match(t)):
        return None
    if _OFF_RE.match(t) and not _ON_RE.match(t) or _HI_OFF_RE.match(th) or _HI_OFF_RE.match(t):
        st = _state or start()
        if st is None:
            return lang.tr(_T, "dnd_was_off")
        was = st.manual or bool(st.mode)
        st.set_manual(False)
        return lang.tr(_T, "dnd_off" if was else "dnd_was_off")
    if _ON_RE.match(t) or _HI_ON_RE.match(th) or _HI_ON_RE.match(t):
        st = _state or start()
        if st is None:
            return None
        st.set_manual(True)
        return lang.tr(_T, "dnd_on")
    if _STATUS_RE.match(t):
        m = mode()
        if not m:
            return lang.tr(_T, "status_none")
        return lang.tr(_T, f"status_{m}", app=_state.app if _state else "")
    return None
