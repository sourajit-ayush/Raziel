"""
The assistant's toolset for Phase 2.

Each tool is a plain Python function, plus a matching JSON schema (the
format Ollama/OpenAI-style function calling expects) describing what it
does and what arguments it takes. The LLM picks which tool to call and
with what arguments; we just execute it and hand back the result.

Add new tools by:
  1. Writing the function below
  2. Adding its schema to TOOL_SCHEMAS
  3. Adding it to TOOL_FUNCTIONS
"""

import difflib
import importlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser

import config
import confirmation
import instant_replies
import lang
import netutil
import spotify_queue

logger = logging.getLogger("voice_assistant")


# --- Sentences that are asked as yes/no questions (English + Hindi) ------------
# The English wording is relied on by tests and by the confirmation step; Hindi summaries are
# infinitives ("...भेजना") because confirmation.py builds sentences around them.

_TT = {
    "wa_need_pkg": {"en": "Sending WhatsApp messages needs the pyautogui package. Run: pip install pyautogui",
                    "hi": "WhatsApp संदेश भेजने के लिए pyautogui पैकेज चाहिए। यह चलाइए: pip install pyautogui"},
    "wa_no_number": {"en": "I don't have a phone number saved for {name}. Add them to CONTACTS in config.py first, e.g. "
                           "\"{ckey}\": \"+91XXXXXXXXXX\".",
                     "hi": "{name} का फ़ोन नंबर मेरे पास सेव नहीं है। पहले config.py के CONTACTS में उनका नाम जोड़िए।"},
    "wa_ambiguous": {"en": "I have more than one contact that could be {name}: {names}. Which one do you mean?",
                     "hi": "{name} नाम से कई संपर्क हो सकते हैं: {names}। आपका मतलब किससे है?"},
    "wa_what": {"en": "What should the message to {who} say?", "hi": "{who} को संदेश में क्या लिखूँ?"},
    "wa_intro": {"en": "I think you mean {who}. ", "hi": "मुझे लगता है आपका मतलब {who} है। "},
    "wa_question": {"en": "{intro}Send {who} this WhatsApp message: {msg} Say yes to send, or no to cancel.",
                    "hi": "{intro}{who} को यह WhatsApp संदेश भेजूँ: {msg} भेजने के लिए हाँ कहिए, रद्द करने के लिए नहीं।"},
    "wa_summary": {"en": "send {who} that WhatsApp message", "hi": "{who} को वह WhatsApp संदेश भेजना"},
    "wa_ack": {"en": "Sending it now. Please don't touch the keyboard or mouse for a few seconds.",
               "hi": "अभी भेज रही हूँ। कृपया कुछ सेकंड कीबोर्ड या माउस को न छुएँ।"},
    "wa_sent": {"en": "Sent your message to {who} on WhatsApp.", "hi": "{who} को आपका संदेश WhatsApp पर भेज दिया।"},
    "wa_app_failed": {"en": "I couldn't open the WhatsApp app, so I did not send anything to {who}. "
                            "Is the WhatsApp app installed and signed in on this PC?",
                      "hi": "मैं WhatsApp ऐप नहीं खोल पाई, इसलिए {who} को कुछ नहीं भेजा। क्या इस PC पर WhatsApp ऐप "
                            "इंस्टॉल है और साइन-इन है?"},
    "ow_same": {"en": "{name} already contains exactly that, so I left it as it is.",
                "hi": "{name} में पहले से यही लिखा है, इसलिए मैंने उसे वैसा ही रहने दिया।"},
    "ow_summary": {"en": "overwrite {name}", "hi": "{name} फ़ाइल को बदलना"},
    "ow_question": {"en": "{name} already exists in the {dir} folder. Overwrite it with the new content? "
                          "Say yes to overwrite, or no to cancel.",
                    "hi": "{name} पहले से {dir} फ़ोल्डर में मौजूद है। नई सामग्री से बदल दूँ? "
                          "बदलने के लिए हाँ कहिए, रद्द करने के लिए नहीं।"},
    "ow_saved": {"en": "Saved the file as {name} in the {dir} folder.",
                 "hi": "फ़ाइल {name} नाम से {dir} फ़ोल्डर में सेव कर दी।"},
    "feature_missing": {"en": "That feature isn't available right now.", "hi": "यह सुविधा अभी उपलब्ध नहीं है।"},
}


def _t(key: str, **kw) -> str:
    return lang.tr(_TT, key, **kw)


# --- Tool implementations ----------------------------------------------------

# Friendly names -> how to actually launch them on Windows.
# Add more entries here as you need them.
APP_COMMANDS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "file manager": "explorer.exe",
    "files": "explorer.exe",
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "edge": "msedge.exe",
    "word": "winword.exe",
    "excel": "excel.exe",
    "spotify": "spotify.exe",
    "steam": "steam.exe",
    "vscode": "code.exe",
    "vs code": "code.exe",
    "visual studio code": "code.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "terminal": "wt.exe",
}

# Things that are actually websites, not installed applications - "open X"
# for these should open the default browser to the right URL instead of
# trying (and failing) to find a desktop app called "youtube.exe".
WEBSITE_URLS = {
    "youtube": "https://youtube.com",
    "google": "https://google.com",
    "gmail": "https://mail.google.com",
    "github": "https://github.com",
    "facebook": "https://facebook.com",
    "instagram": "https://instagram.com",
    "twitter": "https://twitter.com",
    "x": "https://twitter.com",
    "whatsapp web": "https://web.whatsapp.com",
    "chatgpt": "https://chat.openai.com",
    "netflix": "https://netflix.com",
}

# A few of the sites above commonly exist as a real installed app too - a PWA someone
# installed via the browser's "Install as app" option, or a Microsoft Store app (Netflix
# ships one by default on a lot of PCs). For these, open_app tries to find and launch the
# real app FIRST and only falls back to the plain website if nothing is installed, so
# "open YouTube" opens an actual YouTube window instead of always forcing a browser tab.
# Deliberately NOT applied to every entry above - a short key like "x" would too easily
# loose-match some unrelated installed app (Xbox, Excel, ...) that has nothing to do with it.
APP_PREFERRED_WEBSITES = {"youtube", "netflix", "chatgpt"}

# URL templates for sites that support searching directly via the URL, so
# "open X and search for Y" can actually perform the search instead of
# just opening the homepage.
SEARCH_URL_TEMPLATES = {
    "google": "https://www.google.com/search?q={query}",
    "youtube": "https://www.youtube.com/results?search_query={query}",
    "bing": "https://www.bing.com/search?q={query}",
    "wikipedia": "https://en.wikipedia.org/wiki/Special:Search?search={query}",
    "amazon": "https://www.amazon.com/s?k={query}",
}


def _youtube_first_video_id(query: str):
    """Best-effort lookup of the first result's video ID for `query`, so 'play X on YouTube'
    can jump straight to playing it instead of just showing search results. YouTube's real
    search API needs a key, so this scrapes the same results page a browser would load; it
    returns None on any failure (network, blocked, nothing found) so the caller can fall back
    to just opening the search results page instead of erroring out."""
    try:
        html = netutil.get_text(
            "https://www.youtube.com/results",
            params={"search_query": query, "hl": "en"},
            timeout=6.0,
        )
    except Exception as e:
        logger.info("YouTube first-result lookup failed (%s) - falling back to search results", e)
        return None
    m = re.search(r'"videoId":"([A-Za-z0-9_-]{11})"', html)
    return m.group(1) if m else None


def open_website_search(site: str, query: str, play: bool = False) -> str:
    """Opens a website with a search already performed, e.g. YouTube results for a query.
    When play=True on a site that supports it (currently YouTube), jumps straight to playing
    the first result instead of leaving the user looking at a results list."""
    import urllib.parse

    key = site.strip().lower()

    if play and key == "youtube" and query.strip():
        video_id = _youtube_first_video_id(query)
        if video_id:
            url = f"https://www.youtube.com/watch?v={video_id}"
            logger.info("Opening YouTube and playing: %s -> %s", query, url)
            webbrowser.open(url)
            return f"Playing {query} on YouTube."
        logger.info("No video found to play for '%s' - opening search results instead", query)

    template = SEARCH_URL_TEMPLATES.get(key)

    if not template:
        if key in WEBSITE_URLS:
            webbrowser.open(WEBSITE_URLS[key])
            return f"Opened {site}, but I don't know how to search directly on that site yet."
        return f"I don't know how to search on {site}."

    url = template.format(query=urllib.parse.quote(query))
    logger.info("Opening search: %s -> %s", site, url)
    webbrowser.open(url)
    return f"Searched {site} for {query}."

# A few built-in Windows/UWP apps are most reliably opened via their
# registered URI protocol rather than an exe name.
PROTOCOL_APPS = {
    "microsoft store": "ms-windows-store:",
    "store": "ms-windows-store:",
    "settings": "ms-settings:",
    "windows settings": "ms-settings:",
    "camera": "microsoft.windows.camera:",
    "mail": "outlookmail:",
    "calendar": "outlookcal:",
}

# Some apps (like Steam) install to a custom folder and aren't reliably
# resolvable by just their exe name via PATH or Windows' App Paths registry.
# For these, try known real install locations directly. %ENV_VARS% get
# expanded automatically. Add your own here if your install path differs.
FULL_PATH_CANDIDATES = {
    "steam": [
        r"%ProgramFiles(x86)%\Steam\Steam.exe",
        r"%ProgramFiles%\Steam\Steam.exe",
    ],
}

# Windows creates a Start Menu shortcut for nearly everything you install,
# including UWP/Store apps (Notion, Spotify, the Store itself, etc.) - this
# is what actually runs when you type a name into the Start menu and hit
# Enter. Searching these shortcuts is a far more general way to "open
# whatever's installed" than maintaining a manual list of every app.
START_MENU_DIRS = [
    os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
    os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
]

# Cached once per program run (not per call) - PowerShell has real startup
# overhead (2-3 s measured in assistant.log), which would otherwise land on
# whichever "open X" command happens to come first. Apps installed after the
# assistant started won't show up until it's restarted - a reasonable
# trade-off for not paying that cost on every command.
#
# Two things the original version got wrong, both visible in the log:
#   * It was LAZY, so the first "open X" of every session paid the whole 2-3 s
#     ("Loaded 161 apps from Get-StartApps" appears 2-3 s after the first
#     "Attempting to open app" of each run). warm_up_app_index() below now loads
#     it in the background at startup instead.
#   * A failed/empty result was cached FOREVER, so one bad PowerShell run
#     disabled UWP app launching until restart. An empty result is now retried
#     after _START_APPS_RETRY_S.
_start_apps_cache = None
_start_apps_failed_at = 0.0
_START_APPS_RETRY_S = 60
_start_apps_lock = threading.Lock()

# Start Menu .lnk/.url index. The old code re-walked both Start Menu trees with
# os.walk on EVERY call that reached it; now it is built once.
_shortcut_index = None
_shortcut_lock = threading.Lock()


def _query_start_apps():
    """One real Get-StartApps call. Returns [(name, app_id), ...], [] on failure."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or not result.stdout.strip():
            logger.error("Get-StartApps returned no output (code %s)", result.returncode)
            return []

        import json
        data = json.loads(result.stdout)
        if isinstance(data, dict):  # PowerShell returns a bare object, not a list, if there's only one match
            data = [data]
        apps = [
            (item.get("Name", ""), item.get("AppID", ""))
            for item in data if item.get("Name") and item.get("AppID")
        ]
        logger.info("Loaded %d apps from Get-StartApps", len(apps))
        return apps
    except Exception as e:
        logger.error("Get-StartApps failed: %s", e)
        return []


def _start_apps_fresh() -> bool:
    if _start_apps_cache is None:
        return False
    if _start_apps_cache:
        return True     # a real list is kept for the whole run
    return time.time() - _start_apps_failed_at < _START_APPS_RETRY_S


def _get_start_apps():
    """
    Returns [(name, app_id), ...] for EVERY app in the Start Menu, including
    UWP/Store apps (WhatsApp, Notion, Microsoft Store itself) that don't
    have a normal .exe or a physical shortcut file the folder-scan below can
    find. Uses PowerShell's Get-StartApps cmdlet - the same source Windows
    itself uses to power Start Menu search, so this is the most complete
    and accurate way to find "everything actually installed".

    Thread-safe: if the startup warm-up is still running when the first
    command arrives, that command waits for the SAME result instead of
    launching a second PowerShell.
    """
    global _start_apps_cache, _start_apps_failed_at
    if _start_apps_fresh():
        return _start_apps_cache
    with _start_apps_lock:
        if _start_apps_fresh():
            return _start_apps_cache
        apps = _query_start_apps()
        _start_apps_cache = apps
        if not apps:
            _start_apps_failed_at = time.time()
        return apps


def _get_shortcut_index():
    """{lowercase name: path} for every Start Menu .lnk/.url. Built once."""
    global _shortcut_index
    if _shortcut_index is not None:
        return _shortcut_index
    with _shortcut_lock:
        if _shortcut_index is not None:
            return _shortcut_index
        candidates = {}
        for base in START_MENU_DIRS:
            if not os.path.isdir(base):
                continue
            for root, _dirs, files in os.walk(base):
                for f in files:
                    if f.lower().endswith((".lnk", ".url")):
                        name = os.path.splitext(f)[0]
                        candidates[name.lower()] = os.path.join(root, f)
        _shortcut_index = candidates
        return _shortcut_index


def warm_up_app_index():
    """
    Load the Start Menu app list and shortcut index in a background thread.
    Call once at startup (main.py does): by the time you say "open Spotify"
    the ~2-3 s PowerShell cost has already been paid. Non-blocking, never raises.
    """
    def _work():
        started = time.time()
        try:
            apps = _get_start_apps()
            shortcuts = _get_shortcut_index()
            logger.info("App index ready in %.1fs (%d apps, %d shortcuts).",
                        time.time() - started, len(apps), len(shortcuts))
        except Exception as e:
            logger.warning("App index warm-up failed: %s", e)

    threading.Thread(target=_work, name="app-index-warmup", daemon=True).start()


def _find_start_app(app_name: str):
    """Fuzzy-matches app_name against everything Get-StartApps returns. Returns an AppID or None."""
    apps = _get_start_apps()
    if not apps:
        return None

    key = app_name.strip().lower()
    name_map = {name.lower(): app_id for name, app_id in apps}

    if key in name_map:
        return name_map[key]

    for name, app_id in apps:
        lname = name.lower()
        if key in lname or lname in key:
            return app_id

    close = difflib.get_close_matches(key, name_map.keys(), n=1, cutoff=0.6)
    if close:
        return name_map[close[0]]

    return None


def _find_start_menu_shortcut(app_name: str):
    """Fuzzy-searches Start Menu shortcuts for one matching app_name."""
    candidates = _get_shortcut_index()

    if not candidates:
        return None

    key = app_name.strip().lower()

    if key in candidates:
        return candidates[key]

    # Substring match - e.g. "chrome" matching "Google Chrome"
    for name, path in candidates.items():
        if key in name or name in key:
            return path

    # Fuzzy match for near-misses/typos in speech-to-text output
    close = difflib.get_close_matches(key, candidates.keys(), n=1, cutoff=0.6)
    if close:
        return candidates[close[0]]

    return None


def open_app(app_name: str) -> str:
    """Opens an application, or a website if the name matches a known site."""
    key = app_name.strip().lower()

    if key in APP_PREFERRED_WEBSITES:
        app_id = _find_start_app(app_name)
        if app_id:
            try:
                os.startfile(f"shell:AppsFolder\\{app_id}")
                logger.info("Opened installed app for '%s' (found via Get-StartApps) instead of the website",
                            app_name)
                return f"Opened {app_name}."
            except OSError as e:
                logger.error("Failed to open via shell:AppsFolder for '%s': %s", app_id, e)
        else:
            shortcut = _find_start_menu_shortcut(app_name)
            if shortcut:
                try:
                    os.startfile(shortcut)
                    logger.info("Opened installed app for '%s' (found via Start Menu shortcut) instead of "
                                "the website", app_name)
                    return f"Opened {app_name}."
                except OSError as e:
                    logger.error("Failed to open shortcut '%s': %s", shortcut, e)
        # nothing installed (or launching it failed) - fall through to the website below

    if key in WEBSITE_URLS:
        logger.info("Opening website: %s -> %s", app_name, WEBSITE_URLS[key])
        webbrowser.open(WEBSITE_URLS[key])
        return f"Opened {app_name} in your browser."

    if key in PROTOCOL_APPS:
        try:
            os.startfile(PROTOCOL_APPS[key])
            return f"Opened {app_name}."
        except OSError as e:
            logger.error("Failed to open protocol app '%s': %s", app_name, e)

    logger.info("Attempting to open app: %s", app_name)

    # Try #1: known exact install paths for apps that don't resolve nicely
    # by name alone (e.g. Steam, which installs to a custom folder).
    for candidate in FULL_PATH_CANDIDATES.get(key, []):
        expanded = os.path.expandvars(candidate)
        if os.path.isfile(expanded):
            try:
                subprocess.Popen(
                    [expanded], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                return f"Opened {app_name}."
            except Exception as e:
                logger.error("Popen failed for '%s': %s", expanded, e)

    # Try #2: search everything Windows itself knows about via Get-StartApps -
    # this is the most complete method and covers UWP/Store apps (WhatsApp,
    # Notion, Microsoft Store) that don't always have a physical shortcut
    # file the folder-scan below can find.
    app_id = _find_start_app(app_name)
    if app_id:
        try:
            os.startfile(f"shell:AppsFolder\\{app_id}")
            return f"Opened {app_name}."
        except OSError as e:
            logger.error("Failed to open via shell:AppsFolder for '%s': %s", app_id, e)

    # Try #3: search Start Menu .lnk shortcuts directly (older fallback method,
    # still useful for anything Get-StartApps somehow misses).
    shortcut = _find_start_menu_shortcut(app_name)
    if shortcut:
        try:
            os.startfile(shortcut)
            return f"Opened {app_name}."
        except OSError as e:
            logger.error("Failed to open shortcut '%s': %s", shortcut, e)

    # Try #4: is a mapped/aliased command actually on PATH?
    command = APP_COMMANDS.get(key, app_name)
    resolved = shutil.which(command)
    if resolved:
        try:
            subprocess.Popen(
                [resolved], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return f"Opened {app_name}."
        except Exception as e:
            logger.error("Popen failed for '%s': %s", resolved, e)

    # Try #5: let Windows itself resolve it - covers built-in tools like
    # notepad.exe/calc.exe that live in System32 but have no Start Menu
    # shortcut of their own.
    try:
        os.startfile(command)
        return f"Opened {app_name}."
    except OSError as e:
        logger.error("Failed to open app '%s': %s", app_name, e)
        return (
            f"I couldn't find {app_name} on this computer. It might not be "
            f"installed, or you can add its exact install path to "
            f"FULL_PATH_CANDIDATES in tools.py."
        )


# Common folder names -> real paths. "~" expands to the user's home folder
# (e.g. C:\Users\Ayush), so "~\Downloads" becomes their actual Downloads folder.
KNOWN_FOLDERS = {
    "downloads": "~\\Downloads",
    "download": "~\\Downloads",
    "documents": "~\\Documents",
    "desktop": "~\\Desktop",
    "pictures": "~\\Pictures",
    "photos": "~\\Pictures",
    "music": "~\\Music",
    "videos": "~\\Videos",
    "home": "~",
}


# --- Searching for a folder anywhere on the PC (fallback for open_folder) ------------------
# Checked, in order, when a name isn't one of KNOWN_FOLDERS and isn't a literal path that
# already exists: a handful of likely install spots first (instant), then a bounded,
# time-limited walk of the user's profile and Program Files, so "find the Steam folder"
# works without needing the exact path. Kept bounded on purpose - an unindexed scan of a
# whole drive can take minutes, so this trades completeness for staying fast.
FOLDER_SEARCH_TIME_BUDGET = 6.0    # seconds, wall clock, across the whole search
FOLDER_SEARCH_MAX_DEPTH = 6        # levels below each root
FOLDER_SEARCH_SKIP_DIRS = {
    "$recycle.bin", "system volume information", "windows", "programdata",
    "node_modules", ".git", "$windows.~ws", "$windows.~bt", "recovery",
    "msocache", "config.msi", "venv", "__pycache__",
}


def _folder_search_roots():
    """Places worth walking, cheapest/most-likely first."""
    roots = []
    home = os.path.expanduser("~")
    for sub in ("Desktop", "Documents", "Downloads", ""):
        p = os.path.join(home, sub) if sub else home
        if os.path.isdir(p) and p not in roots:
            roots.append(p)
    for base in (r"C:\Program Files", r"C:\Program Files (x86)"):
        if os.path.isdir(base):
            roots.append(base)
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":       # C: is already covered above
        root = f"{letter}:\\"
        if os.path.isdir(root):
            roots.append(root)
    return roots


def _quick_folder_guesses(name: str):
    """A handful of common install locations, checked directly (no walking)."""
    home = os.path.expanduser("~")
    for c in (
        rf"C:\Program Files\{name}",
        rf"C:\Program Files (x86)\{name}",
        os.path.join(home, "AppData", "Local", name),
        os.path.join(home, "AppData", "Roaming", name),
        os.path.join(home, name),
    ):
        if os.path.isdir(c):
            return c
    return None


def _search_for_folder(name: str, time_budget: float = FOLDER_SEARCH_TIME_BUDGET):
    """Looks for a directory named (or starting with) `name` anywhere reasonable on the PC.
    Bounded by a wall-clock budget and a depth limit so it can't hang on a huge/slow drive.
    Returns the first good match, or None."""
    key = name.strip().lower()
    if not key:
        return None

    quick = _quick_folder_guesses(name)
    if quick:
        return quick

    prefix_match = None
    deadline = time.monotonic() + time_budget
    for root in _folder_search_roots():
        if time.monotonic() >= deadline:
            break
        try:
            for dirpath, dirnames, _filenames in os.walk(root):
                if time.monotonic() >= deadline:
                    break
                rel = os.path.relpath(dirpath, root)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth >= FOLDER_SEARCH_MAX_DEPTH:
                    dirnames[:] = []
                    continue
                # prune noisy/system directories in place so os.walk never descends into them
                dirnames[:] = [d for d in dirnames if d.lower() not in FOLDER_SEARCH_SKIP_DIRS
                               and not d.lower().startswith("$")]
                for d in dirnames:
                    dl = d.lower()
                    if dl == key:
                        return os.path.join(dirpath, d)
                    if prefix_match is None and dl.startswith(key):
                        prefix_match = os.path.join(dirpath, d)
        except OSError:
            continue
    return prefix_match


def open_folder(folder_name: str) -> str:
    """Opens a specific folder in File Explorer, by name or path. Checks the common folders
    first, then a literal path, then searches Program Files/the user's profile/other drives
    for a folder with that name (bounded search, see _search_for_folder)."""
    key = folder_name.strip().lower()
    raw_path = KNOWN_FOLDERS.get(key, folder_name)
    path = os.path.expanduser(os.path.expandvars(raw_path))

    logger.info("Attempting to open folder: %s -> %s", folder_name, path)

    if not os.path.isdir(path):
        found = None
        try:
            found = _search_for_folder(folder_name)
        except Exception as e:
            logger.warning("Folder search for '%s' failed: %s", folder_name, e)
        if found:
            logger.info("Folder search found: %s -> %s", folder_name, found)
            path = found
        else:
            return (
                f"I couldn't find a folder called {folder_name} anywhere I looked "
                f"(common folders, Program Files, your user folder, and other drives). "
                f"If it's somewhere unusual, try giving the full path."
            )

    try:
        os.startfile(path)
        return f"Opened the {folder_name} folder."
    except OSError as e:
        logger.error("Failed to open folder '%s': %s", path, e)
        return f"I couldn't open that folder: {e}"


# Folders searched when the user asks to open a file/document/PDF by name
# without giving a full path.
FILE_SEARCH_FOLDERS = [
    "~\\Desktop",
    "~\\Downloads",
    "~\\Documents",
    "~\\Pictures",
    "~\\Videos",
    "~\\Music",
]


def find_file(filename: str):
    """Resolves `filename` to a real path: an exact/literal path, else a search of
    FILE_SEARCH_FOLDERS by exact name, then by name-without-extension, then a fuzzy match.
    Returns (path, None) on success or (None, reason) on failure - shared by open_file() (opens
    it locally) and the phone bridge's /file endpoint (sends it to the phone), so both use
    exactly the same "what counts as finding the file" logic."""
    filename = (filename or "").strip()
    if not filename:
        return None, "no filename given"

    direct = os.path.expanduser(os.path.expandvars(filename))
    if os.path.isfile(direct):
        return direct, None

    key = filename.lower()
    candidates = {}
    for folder in FILE_SEARCH_FOLDERS:
        folder_path = os.path.expanduser(os.path.expandvars(folder))
        if not os.path.isdir(folder_path):
            continue
        try:
            for entry in os.listdir(folder_path):
                full = os.path.join(folder_path, entry)
                if os.path.isfile(full):
                    candidates[entry.lower()] = full
        except OSError:
            continue

    if not candidates:
        return None, f"couldn't find a file called {filename}"

    if key in candidates:
        return candidates[key], None

    # Match without needing the exact extension, e.g. "resume" -> "resume.pdf"
    stem_matches = [full for name, full in candidates.items() if os.path.splitext(name)[0] == key]
    if stem_matches:
        return stem_matches[0], None

    close = difflib.get_close_matches(key, candidates.keys(), n=1, cutoff=0.5)
    if close:
        return candidates[close[0]], None

    return None, (
        f"couldn't find a file called {filename} in your common folders "
        f"(Desktop, Downloads, Documents, Pictures, Videos, Music)"
    )


def open_file(filename: str) -> str:
    """Opens a file (document, PDF, image, etc.) by exact path or by searching common folders."""
    match_path, error = find_file(filename)
    if not match_path:
        return f"I {error}." if error else f"I couldn't find a file called {filename}."
    try:
        os.startfile(match_path)
        return f"Opened {os.path.basename(match_path)}."
    except OSError as e:
        logger.error("Failed to open file '%s': %s", match_path, e)
        return f"I couldn't open that file: {e}"


def _write_file_now(safe_name: str, full_path: str, content: str, keep_backup: bool = False) -> str:
    """The actual write. Split out so an overwrite can be confirmed first."""
    logger.info("Writing file: %s", full_path)
    try:
        if keep_backup and os.path.isfile(full_path):
            # One-deep safety net for a confirmed overwrite: notes.txt -> notes.txt.bak
            shutil.copy2(full_path, full_path + ".bak")
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)
        return _t("ow_saved", name=safe_name, dir=config.GENERATED_FILES_DIR)
    except Exception as e:
        logger.error("Failed to write file '%s': %s", full_path, e)
        return f"I couldn't save that file: {e}"


def write_file(filename: str, content: str) -> str:
    """
    Writes text content to a file in the project's generated_files folder.
    Creating a NEW file just happens; REPLACING an existing file with different
    content asks for a spoken yes/no first (Phase 3 confirmation step).
    """
    safe_name = os.path.basename(filename)  # strip any path traversal attempts
    if not safe_name:
        return "What should I call the file?"
    os.makedirs(config.GENERATED_FILES_DIR, exist_ok=True)
    full_path = os.path.join(config.GENERATED_FILES_DIR, safe_name)

    if os.path.isfile(full_path) and confirmation.needs_confirmation("overwrite_file"):
        try:
            with open(full_path, "r", encoding="utf-8") as f:
                unchanged = f.read() == content
        except Exception:
            unchanged = False          # unreadable / not text: treat as different
        if unchanged:
            return _t("ow_same", name=safe_name)
        keep_backup = bool(getattr(config, "CONFIRM_KEEP_BACKUP_ON_OVERWRITE", True))
        return confirmation.request(
            kind="overwrite_file",
            summary=_t("ow_summary", name=safe_name),
            question=_t("ow_question", name=safe_name, dir=config.GENERATED_FILES_DIR),
            execute=lambda: _write_file_now(safe_name, full_path, content, keep_backup),
            yes_phrases=("overwrite", "overwrite it", "replace it", "save it", "ओवरराइट", "बदल दो", "बदल दीजिए"),
        )

    return _write_file_now(safe_name, full_path, content)


def web_search(query: str = "", **_ignored) -> str:
    """
    Searches Google. With a Gemini key (config.GEMINI_API_KEY) the answer comes back finished (a
    webinfo.Answer: spoken as it is); without one, DuckDuckGo results come back as text for the model to
    summarise. See webinfo.py.
    """
    logger.info("Web search: %s", query)
    try:
        import webinfo
        return webinfo.web_search(query)
    except Exception as e:                       # noqa: BLE001 - a search must never crash a turn
        logger.exception("Web search failed")
        return f"I couldn't complete that search: {e}"


def get_current_datetime(**_ignored) -> str:
    """Returns the current real-world date and time."""
    from datetime import datetime

    now = datetime.now()
    result = now.strftime("Today is %A, %B %d, %Y. The current time is %I:%M %p.")
    logger.info("get_current_datetime -> %s", result)
    return result


def remember(fact: str) -> str:
    """Saves a fact so the assistant remembers it in future sessions, even after a restart."""
    import memory

    memory.add_fact(fact)
    logger.info("Remembered: %s", fact)
    return f"I'll remember that: {fact}"


def recall(query: str) -> str:
    """Searches remembered facts and past conversation transcripts for something relevant."""
    import memory

    facts = memory.get_recent_facts(limit=config.MAX_REMEMBERED_FACTS_IN_PROMPT)
    matching_facts = [f for f in facts if query.lower() in f.lower()]

    sessions = memory.search_sessions(query, limit=2)

    parts = []
    if matching_facts:
        parts.append("Remembered facts: " + "; ".join(matching_facts))
    for s in sessions:
        date = s["started_at"][:10]
        snippet = s["transcript"][:400].strip()
        parts.append(f"From a past conversation on {date}: {snippet}")

    if not parts:
        return f"I don't have anything remembered about '{query}'."
    return " | ".join(parts)


def forget(keyword: str) -> str:
    """Removes remembered facts matching a keyword."""
    import memory

    removed = memory.forget_facts_matching(keyword)
    if not removed:
        return f"I didn't find anything remembered matching '{keyword}'."
    logger.info("Forgot: %s", removed)
    return f"Forgot: {'; '.join(removed)}"


_LAST_SCREENSHOT_PATH = None


def take_screenshot(filename: str = "") -> str:
    """Captures the screen and saves it as a PNG."""
    global _LAST_SCREENSHOT_PATH
    from datetime import datetime

    try:
        from PIL import ImageGrab
    except ImportError:
        return "Screenshots aren't available - Pillow isn't installed. Run: pip install Pillow"

    os.makedirs(config.SCREENSHOTS_DIR, exist_ok=True)

    if filename:
        safe_name = os.path.basename(filename)
        if not safe_name.lower().endswith(".png"):
            safe_name += ".png"
    else:
        safe_name = datetime.now().strftime("screenshot_%Y-%m-%d_%H-%M-%S.png")

    full_path = os.path.join(config.SCREENSHOTS_DIR, safe_name)

    logger.info("Taking screenshot: %s", full_path)
    try:
        image = ImageGrab.grab()
        image.save(full_path)
        _LAST_SCREENSHOT_PATH = full_path
        return f"Saved a screenshot as {safe_name} in the {config.SCREENSHOTS_DIR} folder."
    except Exception as e:
        logger.error("Failed to take screenshot: %s", e)
        return f"I couldn't take the screenshot: {e}"


def show_last_screenshot(**_ignored) -> str:
    """Opens the most recently taken screenshot in the default image viewer."""
    path = _LAST_SCREENSHOT_PATH

    if not path or not os.path.isfile(path):
        # Fall back to the newest file on disk, in case the assistant was
        # restarted since the screenshot was taken.
        if os.path.isdir(config.SCREENSHOTS_DIR):
            files = [
                os.path.join(config.SCREENSHOTS_DIR, f)
                for f in os.listdir(config.SCREENSHOTS_DIR)
                if f.lower().endswith(".png")
            ]
            if files:
                path = max(files, key=os.path.getmtime)

    if not path or not os.path.isfile(path):
        return "I don't have a screenshot to show yet - take one first."

    logger.info("Opening screenshot: %s", path)
    try:
        os.startfile(path)
        return "Here's the screenshot."
    except OSError as e:
        logger.error("Failed to open screenshot: %s", e)
        return f"I couldn't open the screenshot: {e}"


# --- Sending: the WhatsApp desktop app first, WhatsApp Web as the fallback ---------
#
# The desktop app registers the whatsapp:// link scheme with Windows, so
# whatsapp://send?phone=<digits>&text=<message> opens the chat with the message
# typed in; pressing Enter then sends it (the same trick the Web version uses).
# Unlike the Web flow she checks WHICH window has the keyboard focus before
# pressing Enter, and does not press it if that isn't WhatsApp.

_WA_MAX_LINK_CHARS = 2000        # Windows' ShellExecute refuses longer links
_BROWSER_EXES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
                 "vivaldi.exe", "iexplore.exe", "arc.exe"}


def _whatsapp_digits(phone: str) -> str:
    """WhatsApp wants the number in international format: digits only, no '+'."""
    return re.sub(r"\D", "", phone or "")


def _is_whatsapp_window(title: str, exe: str) -> bool:
    """A real WhatsApp window: 'WhatsApp' in the title and not a browser tab showing WhatsApp Web.
    (Store apps show up as ApplicationFrameHost.exe, so the exe name can't be required.)"""
    return "whatsapp" in (title or "").lower() and (exe or "").lower() not in _BROWSER_EXES


def _open_whatsapp_app(uri: str) -> bool:
    """Hands the whatsapp:// link to Windows. False when nothing on this PC handles it."""
    if not hasattr(os, "startfile"):          # not Windows
        return False
    try:
        os.startfile(uri)
        return True
    except OSError as e:
        logger.warning("Windows has no app for whatsapp:// links (%s)", e)
        return False


def _win_dll(name: str):
    """A private handle to a Windows DLL. (Not ctypes.windll.user32: that object is shared with
    other libraries such as pyautogui, and we set argtypes/restype on the functions we call.)"""
    import ctypes
    return ctypes.WinDLL(name)


def _window_info(hwnd):
    """(title, exe name) of a window handle (Windows only)."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = _win_dll("user32"), _win_dll("kernel32")
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)      # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(1024)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                exe = os.path.basename(path.value).lower()
        finally:
            kernel32.CloseHandle(handle)
    return buf.value, exe


def _whatsapp_in_front():
    """True if a WhatsApp app window has the keyboard focus, False if something else
    does, None if this can't be checked (not Windows, or the Windows call failed)."""
    try:
        from ctypes import wintypes
        user32 = _win_dll("user32")
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        return _is_whatsapp_window(*_window_info(hwnd))
    except Exception as e:  # noqa: BLE001 - never let a focus check crash the send
        logger.warning("Couldn't check which window has focus: %s", e)
        return None


def _focus_whatsapp():
    """Best effort: bring the WhatsApp window to the front (Windows often only flashes
    the taskbar when another program asks for focus)."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = _win_dll("user32")
        found = []
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
        for fn in (user32.IsWindowVisible, user32.IsIconic, user32.SetForegroundWindow):
            fn.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

        def visit(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd) and _is_whatsapp_window(*_window_info(hwnd)):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(enum_proc(visit), 0)
        if not found:
            return
        hwnd = found[0]
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)                     # SW_RESTORE
        user32.keybd_event(0x12, 0, 0, 0)                  # a tap of Alt lets SetForegroundWindow through
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
    except Exception as e:  # noqa: BLE001
        logger.warning("Couldn't bring WhatsApp to the front: %s", e)


def _wait_for_whatsapp_front(timeout: float):
    """Waits until WhatsApp has the keyboard focus. True = it does, False = it never did
    within `timeout` seconds, None = can't tell on this system."""
    deadline = time.monotonic() + timeout
    next_focus = time.monotonic() + 2.0
    while True:
        state = _whatsapp_in_front()
        if state is None or state:
            return state
        now = time.monotonic()
        if now >= deadline:
            return False
        if now >= next_focus:
            _focus_whatsapp()
            next_focus = now + 2.0
        time.sleep(0.4)


def _send_via_whatsapp_app(contact_name: str, phone: str, message: str):
    """
    Sends through the WhatsApp desktop app. Returns the sentence to speak, or None if
    the app could not be opened at all (so the caller may fall back to WhatsApp Web).
    """
    import urllib.parse

    try:
        import pyautogui
    except ImportError:
        return "Sending WhatsApp messages needs the pyautogui package. Run: pip install pyautogui"

    uri = f"whatsapp://send?phone={_whatsapp_digits(phone)}&text={urllib.parse.quote(message, safe='')}"
    if len(uri) > _WA_MAX_LINK_CHARS:
        return (f"That message is too long for me to hand to WhatsApp "
                f"(about {_WA_MAX_LINK_CHARS} characters at most), so I did not send it to {contact_name}.")
    logger.info("Opening the WhatsApp app to message %s", contact_name)
    if not _open_whatsapp_app(uri):
        return None

    # The chat needs a moment to load with the message typed in. Enter goes to whatever
    # window has focus, so first make sure that is WhatsApp.
    front = _wait_for_whatsapp_front(float(getattr(config, "WHATSAPP_APP_TIMEOUT", 20)))
    if front is False:
        logger.error("WhatsApp never came to the front - not pressing Enter")
        return (f"I opened WhatsApp with your message to {contact_name}, but it didn't come to the front, "
                "so I did not press send. Press Enter in WhatsApp to send it.")
    time.sleep(float(getattr(config, "WHATSAPP_APP_DELAY", 6)))
    if front is not None and _whatsapp_in_front() is False:
        logger.error("Focus left WhatsApp while its chat was loading - not pressing Enter")
        return (f"I opened WhatsApp with your message to {contact_name}, but another window took the focus, "
                "so I did not press send. Press Enter in WhatsApp to send it.")
    pyautogui.press("enter")
    return _t("wa_sent", who=contact_name)


def _send_whatsapp_web(contact_name: str, phone: str, message: str) -> str:
    """Sends through WhatsApp Web in the default browser (opens it, waits, presses Enter)."""
    import urllib.parse

    try:
        import pyautogui
    except ImportError:
        return "Sending WhatsApp messages needs the pyautogui package. Run: pip install pyautogui"

    encoded_message = urllib.parse.quote(message)
    url = f"https://web.whatsapp.com/send?phone={_whatsapp_digits(phone)}&text={encoded_message}"
    logger.info("Opening WhatsApp Web to message %s", contact_name)
    if not webbrowser.open(url):
        # On Windows webbrowser.open() returns False (it does not raise) when it
        # can't launch the browser. Pressing Enter anyway would send that keypress
        # to whatever window happens to have focus.
        logger.error("webbrowser.open() failed for the WhatsApp URL - nothing sent")
        return f"I couldn't open WhatsApp Web, so I did not send anything to {contact_name}."

    # WhatsApp Web needs time to load the chat before we can send - don't
    # touch the keyboard/mouse during this window, since pyautogui sends a
    # real OS-level keypress to whatever window has focus.
    time.sleep(config.WHATSAPP_SEND_DELAY)
    pyautogui.press("enter")

    return _t("wa_sent", who=contact_name)


def _send_whatsapp_now(contact_name: str, phone: str, message: str) -> str:
    """The actual send. Split out so it only ever runs after the user has said yes.
    config.WHATSAPP_MODE: "app" (desktop app only), "web" (WhatsApp Web only), or
    "auto" (the app, falling back to Web when nothing on this PC opens whatsapp:// links)."""
    mode = str(getattr(config, "WHATSAPP_MODE", "auto")).strip().lower()
    if mode != "web":
        result = _send_via_whatsapp_app(contact_name, phone, message)
        if result is not None:
            return result
        if mode == "app":
            return _t("wa_app_failed", who=contact_name)
        logger.info("No WhatsApp app found - falling back to WhatsApp Web")
    return _send_whatsapp_web(contact_name, phone, message)


def _readback(text: str) -> str:
    """The message exactly as she reads it back before a confirmation. Never cut
    short: "yes" sends the WHOLE text, so the whole text must be heard."""
    return " ".join((text or "").split())


def _sentence(text: str) -> str:
    """Ends `text` with sentence punctuation, so what follows it is spoken as a new sentence."""
    if text[-1:] in ".!?।":
        return text
    return text + ("।" if lang.has_devanagari(text) else ".")


# Words people put in front of a name ("my friend Fazal", "to Mom") that are not part of it.
_NAME_LEAD_WORDS = {"to", "my", "the", "our", "mr", "mrs", "ms", "miss", "dr", "friend", "bro", "brother",
                    "sister", "uncle", "aunt", "aunty", "contact"}


def _name_key(name: str) -> str:
    """Lowercase letters/digits/spaces only ("Fazal's" -> "fazals"), for comparing names."""
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("'", ""))
    return " ".join(cleaned.split())


def _tokens_prefix(spoken_words, contact_words) -> bool:
    """One is the beginning of the other ("fazal singh" / "fazal"), word by word."""
    n = min(len(spoken_words), len(contact_words))
    return n > 0 and spoken_words[:n] == contact_words[:n]


def _resolve_contact(spoken_name: str):
    """
    Finds who was meant among config.CONTACTS. Speech recognition rarely gives the
    name exactly as it is saved: "Fazal Singh" for the key "fazal", "my friend Fazal",
    "Fazul". Returns (keys, how): `keys` are the matching CONTACTS keys ([] = nobody,
    2+ = ambiguous), `how` is "exact", "partial" or "fuzzy". Never guesses loosely: a
    fuzzy match needs 0.8 similarity, and she says "I think you mean X" out loud.
    """
    contacts = {}
    for key, phone in (getattr(config, "CONTACTS", None) or {}).items():
        norm = _name_key(key)
        if norm and phone:
            contacts[norm] = key
    if not contacts:
        return [], "exact"

    spoken = _name_key(spoken_name)
    if not spoken:
        return [], "exact"
    if spoken in contacts:
        return [contacts[spoken]], "exact"

    words = spoken.split()
    while len(words) > 1 and words[0] in _NAME_LEAD_WORDS:
        words = words[1:]
    core = " ".join(words)
    if core in contacts:
        return [contacts[core]], "exact"

    # "fazal singh" -> contact "fazal"; "fazal" -> contact "fazal singh"; "uncle fazal singh" -> "fazal"
    def contains(big, small):
        return any(big[i:i + len(small)] == small for i in range(len(big) - len(small) + 1))

    only_titles = all(w in _NAME_LEAD_WORDS for w in words)    # "dr", "my friend": not a name
    if not only_titles:
        prefix = [n for n in contacts if _tokens_prefix(words, n.split())]
        if prefix:
            return [contacts[n] for n in prefix], "partial"
        inside = [n for n in contacts if contains(words, n.split())]
        if inside:
            best = max(len(n.split()) for n in inside)             # the most specific match wins
            return [contacts[n] for n in inside if len(n.split()) == best], "partial"
        # "mehta" -> contact "dr mehta" (a surname / nickname said on its own)
        part = [n for n in contacts if contains(n.split(), words)]
        if part:
            return [contacts[n] for n in part], "partial"

    close = difflib.get_close_matches(core, list(contacts), n=2, cutoff=0.8)
    if not close and len(words) > 1:
        close = difflib.get_close_matches(words[0], list(contacts), n=2, cutoff=0.8)
    if close:
        best_score = difflib.SequenceMatcher(None, core, close[0]).ratio()
        if len(close) == 2 and (best_score - difflib.SequenceMatcher(None, core, close[1]).ratio()) < 0.05:
            return [contacts[n] for n in close], "fuzzy"        # too close to call
        return [contacts[close[0]]], "fuzzy"
    return [], "exact"


def _contact_display(key: str) -> str:
    """How her voice says a saved contact: "fazal" -> "Fazal", "Mom" stays "Mom"."""
    return key.title() if key == key.lower() else key


def send_whatsapp_message(contact_name: str, message: str) -> str:
    """
    Sends a WhatsApp message through the WhatsApp desktop app (config.WHATSAPP_MODE
    = "app"), or WhatsApp Web. Requires:
      1. Being already signed in: to the WhatsApp app, or to WhatsApp Web in your
         default browser (one-time QR scan - the login session is remembered)
      2. The contact's phone number saved in config.CONTACTS (we can't read
         your phone's contact list directly, so names need to be mapped
         to numbers once, manually)
      3. Not touching your mouse/keyboard for a few seconds while it loads
         and sends, since this simulates a real keypress

    Phase 3: this does not send straight away. She reads the message back and
    waits for a spoken "yes" (see confirmation.py); "no" cancels it. Turn it off
    with CONFIRM_RISKY_ACTIONS = False in config.py.
    """
    try:
        import pyautogui  # noqa: F401 - checked up front so a missing package is
                          # reported BEFORE asking the user to confirm a send
    except ImportError:
        return _t("wa_need_pkg")

    contact_name = (contact_name or "").strip()
    if lang.has_devanagari(contact_name):
        # A name spoken in Hindi ("फ़ज़ल") is matched against the saved contacts in Latin letters.
        contact_name = lang.hint_latin(contact_name)
    matches, how = _resolve_contact(contact_name)
    if not matches:
        key = contact_name.lower()
        return _t("wa_no_number", name=contact_name, ckey=key)
    if len(matches) > 1:
        names = lang.tr({"and": {"en": " and ", "hi": " और "}}, "and").join(_contact_display(m) for m in matches)
        return _t("wa_ambiguous", name=contact_name, names=names)
    saved_key = matches[0]
    phone = config.CONTACTS[saved_key]
    who = _contact_display(saved_key)          # the SAVED name: what she says is who really gets it
    if not (message or "").strip():
        return _t("wa_what", who=who)

    if confirmation.needs_confirmation("send_whatsapp_message"):
        intro = _t("wa_intro", who=who) if how == "fuzzy" else ""
        return confirmation.request(
            kind="send_whatsapp_message",
            summary=_t("wa_summary", who=who),
            question=_t("wa_question", intro=intro, who=who, msg=_sentence(_readback(message))),
            execute=lambda: _send_whatsapp_now(who, phone, message),
            ack=_t("wa_ack"),
            yes_phrases=("send", "send it", "भेज दो", "भेजो"),
        )

    return _send_whatsapp_now(who, phone, message)


# --- Email (Phase 3): opens a pre-filled DRAFT, never sends ------------------------
#
# No password, no SMTP, no API key: she opens a compose window with the
# recipient, subject and body already filled in, and YOU press Send. Because
# nothing leaves the computer until then, this needs no spoken confirmation -
# pressing Send in the draft IS the confirmation.

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+$")


def _spoken_to_email(text: str) -> str:
    """'john dot smith at gmail dot com' -> 'john.smith@gmail.com'. Returns ''
    when the text isn't clearly an address (so it is treated as a name instead)."""
    t = (text or "").strip().lower().strip(".,;:!?")
    t = re.sub(r"(?:\s+(?:please|thanks|thank you|thx))+$", "", t).strip()   # "... dot com please"
    if not t:
        return ""
    if "@" not in t:
        t = re.sub(r"\s+at\s+", "@", t, count=1)
    t = re.sub(r"\s+dot\s+", ".", t)
    t = re.sub(r"\s+underscore\s+", "_", t)
    t = re.sub(r"\s+(?:dash|hyphen)\s+", "-", t)
    if re.search(r"\s", t):
        return ""                 # leftover words: not a clean address, never glue them together
    return t if _EMAIL_RE.match(t) else ""


def _resolve_email_recipients(to: str):
    """
    Turns what she heard into (addresses, labels, unknown_names).
    Accepts a saved EMAIL_CONTACTS name, a spoken or typed address, or several
    of those separated by commas / "and".
    """
    contacts = {k.strip().lower(): v for k, v in getattr(config, "EMAIL_CONTACTS", {}).items()}
    addresses, labels, unknown = [], [], []
    for part in re.split(r"\s*(?:,|;|\band\b)\s*", to or ""):
        part = part.strip()
        if not part:
            continue
        address = _spoken_to_email(part)
        if address:
            addresses.append(address)
            labels.append(address)
            continue
        name = part.lower()
        fuzzy = False
        if name not in contacts:
            # Whisper often mishears a name by a letter ("Sam Smyth"). Accept a close
            # match only if the FIRST word is spelled exactly ("Don Brown" must not
            # become "Dan Brown"), and say it was a close match when reporting it.
            close = [c for c in difflib.get_close_matches(name, list(contacts), n=3, cutoff=0.85)
                     if c.split()[:1] == name.split()[:1]]
            name, fuzzy = (close[0], True) if close else ("", False)
        if name:
            addresses.append(contacts[name])
            labels.append(f"{name.title()} ({contacts[name]}{', closest match' if fuzzy else ''})")
        else:
            unknown.append(part)
    return addresses, labels, unknown


def _build_email_url(mode: str, addresses, subject: str, body: str) -> str:
    """The URL that opens a pre-filled draft. Everything is percent-encoded, so
    nothing said aloud can smuggle in extra headers (bcc=, etc.)."""
    import urllib.parse
    q = lambda v: urllib.parse.quote(v, safe="")  # noqa: E731
    to = ",".join(addresses)
    if mode == "mailto":
        # Mail clients want CRLF line breaks in the body.
        return (f"mailto:{urllib.parse.quote(to, safe='@,')}"
                f"?subject={q(subject)}&body={q(body.replace(chr(10), chr(13) + chr(10)))}")
    if mode == "outlook":
        return ("https://outlook.live.com/mail/0/deeplink/compose"
                f"?to={q(to)}&subject={q(subject)}&body={q(body)}")
    url = f"https://mail.google.com/mail/?view=cm&fs=1&to={q(to)}&su={q(subject)}&body={q(body)}"
    account = getattr(config, "EMAIL_GMAIL_ACCOUNT", "")
    if account:
        url += f"&authuser={q(account)}"      # picks the right Google account
    return url


def _open_email_url(url: str) -> bool:
    """Opens the draft. Returns False if nothing could be launched."""
    if url.startswith("mailto:") and hasattr(os, "startfile"):
        os.startfile(url)                     # the Windows default mail app (raises OSError on failure)
        return True
    return bool(webbrowser.open(url))         # returns False, not an exception, on failure (Windows)


def _copy_to_clipboard(text: str) -> bool:
    """Best effort: Windows clip.exe reads UTF-16 when it starts with a BOM.
    Returns True if the text is on the clipboard."""
    try:
        data = b"\xff\xfe" + text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-16-le")
        subprocess.run(["clip"], input=data, check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:
        logger.info("Couldn't copy to the clipboard (%s)", e)
        return False


def compose_email(subject: str = "", body: str = "", to: str = "") -> str:
    """Opens a pre-filled email draft. Never sends anything."""
    to, subject = (to or "").strip(), " ".join((subject or "").split())
    body = (body or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        return "What should the email say?"

    mode = str(getattr(config, "EMAIL_DRAFT_MODE", "gmail")).lower()
    if mode not in ("gmail", "mailto", "outlook"):
        mode = "gmail"
    addresses, labels, unknown = _resolve_email_recipients(to)

    url = _build_email_url(mode, addresses, subject, body)
    # Windows hands the link to ShellExecute (os.startfile), which rejects anything
    # much beyond ~2,000 characters - for web links as well as mailto:.
    limit = int(getattr(config, "EMAIL_MAX_LINK_CHARS", 2000))
    saved_note = ""
    if len(url) > limit:
        # Too long for a link: keep the text safe in a file (and on the clipboard)
        # and open a draft with the recipient and subject only.
        os.makedirs(config.GENERATED_FILES_DIR, exist_ok=True)
        name = time.strftime("email_draft_%Y%m%d_%H%M%S.txt")
        path = os.path.join(config.GENERATED_FILES_DIR, name)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as e:
            logger.error("Couldn't save the long email body: %s", e)
            return f"That email is too long for a draft and I couldn't save it either: {e}"
        pasted = _copy_to_clipboard(body)
        saved_note = (f" The message was too long for a draft link, so I saved its text as "
                      f"{name} in the {config.GENERATED_FILES_DIR} folder"
                      + (", and copied it to the clipboard. Press Control V to paste it into the draft."
                         if pasted else "."))
        url = _build_email_url(mode, addresses, subject, "")

    logger.info("Opening %s email draft (to=%s, subject=%r, %d chars)", mode,
                ",".join(addresses) or "(empty)", subject, len(body))
    try:
        opened_ok = _open_email_url(url)
    except Exception as e:
        logger.error("Couldn't open the email draft: %s", e)
        return f"I couldn't open an email draft: {e}"
    if not opened_ok:
        logger.error("The email draft link could not be launched")
        return "I couldn't open an email draft - nothing was launched."

    if labels:
        who = f"to {', '.join(labels)}"
    else:
        who = "with the recipient left empty"
    reply = f"I've opened an email draft {who}"
    reply += f" with the subject {subject}." if subject else "."
    if unknown:
        reply += (f" I don't have an email address for {', '.join(unknown)}"
                  " - add it to EMAIL_CONTACTS in config.py or type it in.")
    reply += saved_note
    reply += " Review it and press Send."
    return reply


def _spotify_precheck() -> str:
    """Returns an error message if Spotify isn't usable yet, else empty string."""
    try:
        import spotipy  # noqa: F401
    except ImportError:
        return "Spotify control needs the spotipy package. Run: pip install spotipy"
    if not config.SPOTIFY_CLIENT_ID or not config.SPOTIFY_CLIENT_SECRET:
        return (
            "Spotify isn't set up yet - add your SPOTIFY_CLIENT_ID and "
            "SPOTIFY_CLIENT_SECRET to config.py first. See the README for "
            "how to get these free from Spotify's developer dashboard."
        )
    return ""


def _get_spotify_client():
    """Creates an authenticated Spotify client, reusing the cached login token."""
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    auth_manager = SpotifyOAuth(
        client_id=config.SPOTIFY_CLIENT_ID,
        client_secret=config.SPOTIFY_CLIENT_SECRET,
        redirect_uri=config.SPOTIFY_REDIRECT_URI,
        scope="user-modify-playback-state user-read-playback-state "
        "playlist-read-private playlist-read-collaborative",
        # Anchored to the project folder like every other path in config.py, so
        # the saved login is found no matter which directory main.py is run from.
        cache_path=os.path.join(config._SCRIPT_DIR, ".spotify_cache"),
        open_browser=True,
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def _find_spotify_device(sp, wait_for_launch: bool = False):
    """
    Finds the active Spotify Connect device (the desktop app). If
    wait_for_launch is True, retries for a few seconds since the app was
    likely just opened and needs a moment to register itself.
    """
    import time as _time

    attempts = 10 if wait_for_launch else 3
    for _ in range(attempts):
        try:
            devices = sp.devices().get("devices", [])
        except Exception as e:
            logger.error("Failed to fetch Spotify devices: %s", e)
            return None
        for d in devices:
            if d.get("type") == "Computer":
                return d["id"]
        _time.sleep(1.5)
    return None


def play_music(query: str = "") -> str:
    """
    Plays a song/artist on Spotify (or a generic popular playlist if no
    query is given), using the Spotify Web API. Requires:
      1. A Spotify Premium account (the API can't start/control playback
         on a free account)
      2. A free Spotify Developer app - see README for setup steps -
         with its Client ID/Secret saved in config.py
      3. The Spotify desktop app installed (this tool opens it for you)
    """
    precheck_error = _spotify_precheck()
    if precheck_error:
        return precheck_error

    # Spotify Connect needs an actual running app instance to send
    # playback commands to - make sure it's open first.
    open_app("spotify")

    try:
        sp = _get_spotify_client()
    except Exception as e:
        logger.error("Spotify auth failed: %s", e)
        return f"I couldn't authenticate with Spotify: {e}"

    device_id = _find_spotify_device(sp, wait_for_launch=True)
    if not device_id:
        return (
            "I opened Spotify but it isn't showing up as an active device "
            "yet. Try asking again in a few seconds."
        )

    try:
        if query:
            results = sp.search(q=query, type="track", limit=5)
            raw_items = results.get("tracks", {}).get("items") or []
            items = [i for i in raw_items if i]  # Spotify's search API sometimes
                                                   # returns null entries in results -
                                                   # a known, documented API quirk
            if not items:
                return f"I couldn't find a song matching '{query}' on Spotify."
            track = items[0]
            sp.start_playback(device_id=device_id, uris=[track["uri"]])
            artist = track["artists"][0]["name"] if track.get("artists") else ""
            return f"Playing {track['name']} by {artist} on Spotify."
        else:
            results = sp.search(q="Today's Top Hits", type="playlist", limit=5)
            raw_items = results.get("playlists", {}).get("items") or []
            items = [i for i in raw_items if i]
            if items:
                playlist = items[0]
                sp.start_playback(device_id=device_id, context_uri=playlist["uri"])
                return f"Playing {playlist['name']} on Spotify."
            sp.start_playback(device_id=device_id)
            return "Playing music on Spotify."
    except Exception as e:
        logger.error("Spotify playback failed: %s", e)
        return f"I couldn't start playback: {e}"


def play_playlist(playlist_name: str) -> str:
    """Plays one of the user's own saved Spotify playlists, matched by name."""
    precheck_error = _spotify_precheck()
    if precheck_error:
        return precheck_error

    open_app("spotify")

    try:
        sp = _get_spotify_client()
    except Exception as e:
        logger.error("Spotify auth failed: %s", e)
        return f"I couldn't authenticate with Spotify: {e}"

    device_id = _find_spotify_device(sp, wait_for_launch=True)
    if not device_id:
        return (
            "I opened Spotify but it isn't showing up as an active device "
            "yet. Try asking again in a few seconds."
        )

    try:
        playlists = []
        results = sp.current_user_playlists(limit=50)
        while results:
            playlists.extend([p for p in results.get("items", []) if p])
            if results.get("next"):
                results = sp.next(results)
            else:
                break
    except Exception as e:
        logger.error("Failed to fetch user's playlists: %s", e)
        return f"I couldn't fetch your playlists: {e}"

    if not playlists:
        return "I couldn't find any playlists in your Spotify library."

    key = playlist_name.strip().lower()
    match = None

    for p in playlists:
        if p.get("name", "").strip().lower() == key:
            match = p
            break

    if not match:
        for p in playlists:
            name = p.get("name", "").strip().lower()
            if key in name or name in key:
                match = p
                break

    if not match:
        names_map = {p["name"].lower(): p for p in playlists if p.get("name")}
        close = difflib.get_close_matches(key, names_map.keys(), n=1, cutoff=0.5)
        if close:
            match = names_map[close[0]]

    if not match:
        available = ", ".join(p["name"] for p in playlists[:8] if p.get("name"))
        return f"I couldn't find a playlist called '{playlist_name}'. You have: {available}."

    try:
        sp.start_playback(device_id=device_id, context_uri=match["uri"])
        return f"Playing your playlist {match['name']} on Spotify."
    except Exception as e:
        logger.error("Failed to start playlist playback: %s", e)
        return f"I couldn't start that playlist: {e}"


def pause_music(**_ignored) -> str:
    """Pauses Spotify playback."""
    precheck_error = _spotify_precheck()
    if precheck_error:
        return precheck_error
    try:
        sp = _get_spotify_client()
        device_id = _find_spotify_device(sp)
        if not device_id:
            return "Spotify doesn't seem to be open right now."
        sp.pause_playback(device_id=device_id)
        return "Paused."
    except Exception as e:
        logger.error("Failed to pause Spotify: %s", e)
        return f"I couldn't pause playback: {e}"


def resume_music(**_ignored) -> str:
    """Resumes Spotify playback from where it was paused."""
    precheck_error = _spotify_precheck()
    if precheck_error:
        return precheck_error
    try:
        sp = _get_spotify_client()
        device_id = _find_spotify_device(sp)
        if not device_id:
            return "Spotify doesn't seem to be open right now."
        sp.start_playback(device_id=device_id)
        return "Resumed."
    except Exception as e:
        logger.error("Failed to resume Spotify: %s", e)
        return f"I couldn't resume playback: {e}"


def next_track(**_ignored) -> str:
    """Skips to the next track on Spotify."""
    precheck_error = _spotify_precheck()
    if precheck_error:
        return precheck_error
    try:
        sp = _get_spotify_client()
        device_id = _find_spotify_device(sp)
        if not device_id:
            return "Spotify doesn't seem to be open right now."
        sp.next_track(device_id=device_id)
        return "Skipped to the next track."
    except Exception as e:
        logger.error("Failed to skip track: %s", e)
        return f"I couldn't skip the track: {e}"


def previous_track(**_ignored) -> str:
    """Goes back to the previous track on Spotify."""
    precheck_error = _spotify_precheck()
    if precheck_error:
        return precheck_error
    try:
        sp = _get_spotify_client()
        device_id = _find_spotify_device(sp)
        if not device_id:
            return "Spotify doesn't seem to be open right now."
        sp.previous_track(device_id=device_id)
        return "Went back to the previous track."
    except Exception as e:
        logger.error("Failed to go to previous track: %s", e)
        return f"I couldn't go back a track: {e}"


def queue_music(query: str = "", **_ignored) -> str:
    """
    Adds a song to the END of the Spotify queue without interrupting what is
    playing. (play_music, by contrast, starts a song immediately.) The search +
    add_to_queue logic lives in spotify_queue.py.

    Does not launch Spotify: a queue only exists while something is playing, so
    if Spotify isn't running there is nothing to add to.
    """
    precheck_error = _spotify_precheck()
    if precheck_error:
        return precheck_error
    try:
        sp = _get_spotify_client()
        device_id = _find_spotify_device(sp)
        if not device_id:
            return "Spotify doesn't seem to be open right now, so there is no queue."
        return spotify_queue.queue_song(sp, query, device_id=device_id)
    except Exception as e:
        logger.error("Failed to queue track: %s", e)
        return f"I couldn't add that to the queue: {e}"


# --- Tools that live in their own modules (v3) ---------------------------------
# Each one is a thin wrapper that imports its module on first use, so a problem in one feature can
# never stop the assistant from starting - it only disables that feature (with a spoken sentence).

def _feature(module_name: str, func_name: str):
    def call(**kwargs):
        try:
            module = importlib.import_module(module_name)
            func = getattr(module, func_name)
        except Exception:                        # noqa: BLE001
            logger.exception("Feature %s.%s is not available", module_name, func_name)
            return _t("feature_missing")
        return func(**kwargs)
    call.__name__ = func_name
    call.__doc__ = f"{module_name}.{func_name}"
    return call


def manage_reminders(action: str = "list", which: str = "", **_ignored) -> str:
    """list / cancel / snooze timers, reminders and alarms."""
    try:
        import reminders
    except Exception:                            # noqa: BLE001
        logger.exception("reminders module not available")
        return _t("feature_missing")
    act = (str(action or "list").strip().lower() or "list")
    which = str(which or "").strip()
    if act in ("cancel", "delete", "remove", "clear", "stop", "dismiss"):
        return reminders.cancel_reminder(which)
    if act == "snooze":
        return reminders.try_handle(f"snooze for {which or '10 minutes'}") or reminders.list_reminders()
    return reminders.list_reminders()


set_reminder = _feature("reminders", "set_reminder")
system_control = _feature("sysctl", "system_control")
calculate = _feature("calc", "calculate")
get_weather = _feature("webinfo", "get_weather")
get_news = _feature("webinfo", "get_news")
manage_notes = _feature("notes", "manage_notes")
clipboard_action = _feature("clipboard_tool", "clipboard_action")
calendar_action = _feature("google_api", "calendar_action")
email_action = _feature("google_api", "email_action")
describe_screen = _feature("vision", "describe_screen")
daily_briefing = _feature("briefing", "daily_briefing")
dictation_control = _feature("dictation", "dictation_control")
control_phone = _feature("phone_control", "control_phone")


def _schema(name: str, description: str, properties=None, required=()):
    """One tool schema, written compactly (every schema is re-read by the model on every request, so
    the wording here is kept short on purpose)."""
    props = {key: {"type": "string", "description": text} for key, text in (properties or {}).items()}
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


_V3_SCHEMAS = [
    _schema("set_reminder",
            "Set a timer, alarm or reminder. 'when' is the time as spoken ('in 10 minutes', 'tomorrow at 7 am', "
            "'every day at 8').",
            {"what": "What to be reminded of (empty for a plain timer or alarm).",
             "when": "When, as the user said it.",
             "kind": "timer, alarm or reminder."}, ["when"]),
    _schema("manage_reminders", "List, cancel or snooze the user's timers, alarms and reminders.",
            {"action": "list, cancel or snooze.", "which": "Which one (a word from it, 'all', or minutes to snooze)."},
            ["action"]),
    _schema("system_control",
            "Control this PC: volume, mute, brightness, battery, lock, sleep, shutdown, restart, close an app, "
            "switch window, show desktop.",
            {"action": "volume_up, volume_down, volume_set, mute, unmute, brightness_up, brightness_down, "
                       "brightness_set, battery, lock, sleep, shutdown, restart, cancel_shutdown, close_app, "
                       "switch_window, show_desktop, minimize_all.",
             "value": "A percent number, or an app name for close_app / switch_window."}, ["action"]),
    _schema("calculate", "Do arithmetic, unit conversion or currency conversion. Always use this for maths.",
            {"expression": "The calculation or conversion as spoken, e.g. '15 percent of 240' or '100 dollars to rupees'."},
            ["expression"]),
    _schema("get_weather", "Current weather and forecast.",
            {"place": "City (empty = the user's own location).", "when": "today, tomorrow or a weekday."}),
    _schema("get_news",
            "Latest news headlines from Google News, fetched live. Call this EVERY time the user asks for news.",
            {"topic": "Optional topic, e.g. 'cricket', 'Tesla', 'India politics'; empty for top stories."}),
    _schema("manage_notes", "Notes and lists (to-do, shopping...).",
            {"action": "add_note, read_notes, add_todo, read_list, done, remove, clear, delete_last_note.",
             "text": "The note or list item.", "list_name": "List name (default to-do)."}, ["action"]),
    _schema("clipboard_action", "Work with what the user copied to the clipboard.",
            {"action": "read, summarize, explain, translate, fix_grammar, copy_last, save_note, save_file, paste, clear.",
             "name": "File name for save_file, or the language for translate."}, ["action"]),
    _schema("calendar_action", "Google Calendar: read events or add one.",
            {"action": "view, next or add.", "when": "A day/range ('today', 'friday') or the time of the new event.",
             "title": "Event title (for add)."}, ["action"]),
    _schema("email_action", "Gmail, read only: unread mail, the latest email, or mail from someone.",
            {"action": "unread, latest or from.", "sender": "Sender name (for from)."}, ["action"]),
    _schema("describe_screen", "Look at the user's screen and describe it or answer a question about it.",
            {"question": "What to find out (empty = describe the screen)."}),
    _schema("daily_briefing", "Give the user's daily briefing: date, weather, reminders, to-dos, calendar, mail, news."),
    _schema("dictation_control", "Type what the user says into the app that has focus.",
            {"action": "start, stop or type.", "text": "Text to type (for type)."}, ["action"]),
    _schema("control_phone",
            "Trigger an action on the user's phone (Android, via the Join app - see config.py). "
            "Only for THIS specific fixed set of actions, nothing else - there is no way to read "
            "anything off the phone this way.",
            {"action": "ring (rings the phone even on silent, to help find it), sms (sends a text "
                       "from the phone - asks yes/no first), open_app (opens an app on the phone), "
                       "open_url (opens a link on the phone), clipboard (sets the phone's clipboard).",
             "contact": "Saved contact name, for sms.",
             "message": "The text to send, for sms.",
             "app_name": "App to open, for open_app.",
             "url": "Link to open, for open_url, or text to copy, for clipboard."},
            ["action"]),
]


# --- Schemas (what the LLM sees) ----------------------------------------------

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open an application on the user's computer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the application to open, e.g. 'notepad', 'chrome', 'spotify'.",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_website_search",
            "description": "Open a website with a search already performed, e.g. 'open Google and search for X' or 'open YouTube and search for Y'. Use this instead of open_app whenever the user wants to search on a specific site, not just open its homepage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {
                        "type": "string",
                        "description": "The website to search on, e.g. 'google', 'youtube', 'wikipedia'.",
                    },
                    "query": {
                        "type": "string",
                        "description": "What to search for.",
                    },
                    "play": {
                        "type": "boolean",
                        "description": "True when the user said 'play X' (start playing the first result immediately), "
                                       "False/omitted when they said 'search for X' or 'look up X' (just show results). "
                                       "Only makes a difference on YouTube.",
                    },
                },
                "required": ["site", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_folder",
            "description": "Open a specific folder in File Explorer, such as Downloads, Documents, Desktop, or any named folder/path. Use this instead of open_app when the user wants to browse to a particular folder, not just open File Explorer in general.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_name": {
                        "type": "string",
                        "description": "Name of the folder to open, e.g. 'downloads', 'desktop', or a full path.",
                    }
                },
                "required": ["folder_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_file",
            "description": "Open a specific file, document, PDF, or image by name or path. Searches common folders (Desktop, Downloads, Documents, Pictures, Videos, Music) if a full path isn't given.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name or path of the file to open, e.g. 'resume.pdf' or 'vacation photo.jpg'.",
                    }
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a text file with the given content and save it locally.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Name of the file to create, e.g. 'notes.txt' or 'todo.md'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The text content to put in the file.",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search Google for current or uncertain facts: scores, prices, who holds an office, recent events.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_datetime",
            "description": "Get the current real-world date and time. Use this whenever the user asks what day, date, or time it is.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a fact to remember permanently, across future sessions and restarts. Use when the user says 'remember that...' or tells you something to keep in mind for later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "The fact to remember, e.g. 'User has an exam on Friday' or 'User's favorite color is blue'.",
                    }
                },
                "required": ["fact"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Search remembered facts and past conversations for something. Use when the user asks what you remember about something, or references a past conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to search for, e.g. 'exam' or 'what we talked about yesterday'.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Remove a remembered fact matching a keyword. Use when the user asks you to forget something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword to match against remembered facts for removal.",
                    }
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "take_screenshot",
            "description": "Capture a screenshot of the user's current screen and save it as an image file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Optional name for the screenshot file, e.g. 'homework.png'. If not given, a timestamped name is used automatically.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_last_screenshot",
            "description": "Open the most recently taken screenshot so the user can see it.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_whatsapp_message",
            "description": "Send a WhatsApp message to a named contact. The contact's phone number must already be saved in CONTACTS. The user is asked to confirm out loud before it is actually sent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_name": {
                        "type": "string",
                        "description": "Name of the person to message, e.g. 'Fazal'.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message text to send.",
                    },
                },
                "required": ["contact_name", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compose_email",
            "description": "Write an email and open it as a pre-filled DRAFT in the user's mail. It never sends anything - the user reviews it and presses Send themselves. Write the full body in the user's voice.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "Recipient: a saved contact name or an email address. Empty if the user didn't say.",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Short subject line.",
                    },
                    "body": {
                        "type": "string",
                        "description": "The complete email text, including a greeting and sign-off.",
                    },
                },
                "required": ["subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": "Play a song or artist on Spotify RIGHT NOW, replacing whatever is playing. If the user just says 'play some music' without specifying what, call this with an empty query to play a popular playlist. Do NOT use this when the user says 'queue' or 'add to the queue' - use queue_music for that.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Song name and/or artist to play, e.g. 'Blinding Lights The Weeknd'. Leave empty for generic/random music.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_playlist",
            "description": "Play one of the user's own saved Spotify playlists by name, e.g. 'Midnight Melodies' or 'Liked Songs'. Use this instead of play_music when the user names a specific playlist rather than a song or artist.",
            "parameters": {
                "type": "object",
                "properties": {
                    "playlist_name": {
                        "type": "string",
                        "description": "Name of the playlist to play, e.g. 'Midnight Melodies'.",
                    }
                },
                "required": ["playlist_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pause_music",
            "description": "Pause Spotify playback.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "resume_music",
            "description": "Resume Spotify playback from where it was paused.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "next_track",
            "description": "Skip to the next track on Spotify.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "previous_track",
            "description": "Go back to the previous track on Spotify.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "queue_music",
            "description": "Add a song to the END of the user's Spotify queue WITHOUT interrupting what is playing now. Use whenever the user says 'queue', 'add X to the queue' or 'add X to my queue'. Never use play_music for those.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Song name and/or artist to queue, e.g. 'Kesariya Arijit Singh'.",
                    }
                },
                "required": ["query"],
            },
        },
    },
]

TOOL_SCHEMAS.extend(_V3_SCHEMAS)

TOOL_FUNCTIONS = {
    "open_app": open_app,
    "open_folder": open_folder,
    "open_file": open_file,
    "open_website_search": open_website_search,
    "write_file": write_file,
    "web_search": web_search,
    "get_current_datetime": get_current_datetime,
    "remember": remember,
    "recall": recall,
    "forget": forget,
    "take_screenshot": take_screenshot,
    "show_last_screenshot": show_last_screenshot,
    "send_whatsapp_message": send_whatsapp_message,
    "compose_email": compose_email,
    "play_music": play_music,
    "play_playlist": play_playlist,
    "pause_music": pause_music,
    "resume_music": resume_music,
    "next_track": next_track,
    "previous_track": previous_track,
    "queue_music": queue_music,
    # v3
    "set_reminder": set_reminder,
    "manage_reminders": manage_reminders,
    "system_control": system_control,
    "calculate": calculate,
    "get_weather": get_weather,
    "get_news": get_news,
    "manage_notes": manage_notes,
    "clipboard_action": clipboard_action,
    "calendar_action": calendar_action,
    "email_action": email_action,
    "describe_screen": describe_screen,
    "daily_briefing": daily_briefing,
    "dictation_control": dictation_control,
    "control_phone": control_phone,
}

# Exact-phrase shortcuts for common, unambiguous Spotify controls. Checked
# BEFORE sending anything to the LLM - since the LLM's own thinking time is
# the actual bottleneck for these commands, not the Spotify API calls
# themselves, matching a known phrase here skips an entire model inference
# and responds almost instantly. Anything phrased differently just falls
# through to the normal LLM routing as usual.
QUICK_COMMANDS = {
    "pause_music": {"pause", "pause the music", "pause music", "stop the music", "stop music"},
    "resume_music": {"resume", "resume the music", "resume music", "unpause",
                      "continue", "continue the music", "play the music"},
    "next_track": {"next", "next song", "next track", "skip", "skip song",
                   "skip this song", "play next", "play the next song"},
    "previous_track": {"previous", "previous song", "previous track", "go back",
                        "play before", "play the previous song", "last song"},
}


def match_quick_command(transcript: str):
    """Returns a tool name if transcript exactly matches a known quick command, else None."""
    lowered = transcript.strip().lower().rstrip(".!")
    for tool_name, phrases in QUICK_COMMANDS.items():
        if lowered in phrases:
            return tool_name
    return None


# --- Deterministic "favorite X" memory handling ------------------------------
# Relying on the LLM to reliably call remember/recall for stated preferences
# proved inconsistent with an 8B local model - it would sometimes claim to
# remember something without calling the tool, or fail to notice a fact
# already in its context. These patterns are matched directly, in code,
# before anything reaches the LLM, so "my favorite X is Y" and "what's my
# favorite X" work every single time regardless of model behavior.

_FAVORITE_STATEMENT_RE = re.compile(
    r"^(?:my )?favorite (.+?) is (.+)$", re.IGNORECASE
)
_FAVORITE_QUESTION_RE = re.compile(
    r"^(?:who|what) is my favorite (.+?)\??$", re.IGNORECASE
)

# Filler words people naturally say before the actual sentence ("Okay, my
# favorite musician is...") - stripped before pattern matching, since the
# patterns above are anchored to the start of the sentence and would
# otherwise miss anything with a filler prefix, silently falling through to
# the less reliable LLM-driven path instead.
_FILLER_PREFIX_RE = re.compile(
    r"^(?:okay|ok|so|alright|well|umm?|uhh?|hey|please|now|actually)[,.]?\s+",
    re.IGNORECASE,
)


def _strip_filler_prefix(text: str) -> str:
    for _ in range(3):  # handle stacked fillers like "Okay, so, ..."
        new_text = _FILLER_PREFIX_RE.sub("", text, count=1)
        if new_text == text:
            break
        text = new_text
    return text


def _favorite_key(category: str) -> str:
    return "favorite_" + re.sub(r"\s+", "_", category.strip().lower())


def try_auto_remember_favorite(transcript: str):
    """If transcript states a favorite (e.g. 'my favorite musician is Arijit Singh'),
    saves it deterministically and returns a confirmation, else returns None."""
    text = _strip_filler_prefix(transcript.strip()).rstrip(".!")
    m = _FAVORITE_STATEMENT_RE.match(text)
    if not m:
        return None

    import memory

    category, value = m.group(1).strip(), m.group(2).strip()
    key = _favorite_key(category)
    content = f"User's favorite {category} is {value}"
    memory.add_fact(content, key=key)
    logger.info("Auto-remembered (deterministic): %s", content)
    return f"Got it, I'll remember your favorite {category} is {value}."


def try_auto_recall_favorite(transcript: str):
    """If transcript asks about a favorite (e.g. 'what is my favorite musician'),
    looks it up deterministically and returns an answer, else returns None."""
    # Truncate at the first '?' - if transcription hallucinated repeated or
    # garbled trailing content after the actual question (a known Whisper
    # quirk on longer/noisier recordings), this drops the garbage instead of
    # capturing it into the category and producing a nonsensical reply.
    text = transcript.split("?", 1)[0]
    text = _strip_filler_prefix(text.strip()).rstrip(".!")
    m = _FAVORITE_QUESTION_RE.match(text)
    if not m:
        return None

    import memory

    category = m.group(1).strip()
    key = _favorite_key(category)
    content = memory.get_fact_by_key(key)
    if content:
        return content

    # Fall back to a broader search across ALL remembered facts, including
    # ones saved via the LLM's own remember tool (which doesn't use keys) -
    # so a fact is still findable even if it wasn't saved through this exact
    # deterministic path.
    facts = memory.get_recent_facts(limit=50)
    matches = [
        f for f in facts
        if category.lower() in f.lower() and "favorite" in f.lower()
    ]
    if matches:
        return matches[-1]

    return f"I don't know your favorite {category} yet - tell me and I'll remember it."


# --- Deterministic "open SITE and play/search X" handling -------------------
# Same reasoning as the favorite-fact handling above: relying on the LLM to
# reliably pick the right tool for this specific phrasing pattern proved
# inconsistent (it would sometimes just say "I can't open YouTube" instead
# of calling any tool at all). Matched directly in code before anything
# reaches the LLM, so this works every time regardless of model behavior.

_OPEN_SITE_ACTION_RE = re.compile(
    r"^open (youtube|google|spotify|bing|wikipedia|amazon) and "
    r"(play|search(?: for)?|look up) (.+)$",
    re.IGNORECASE,
)

_GENERIC_QUERY_RE = re.compile(r"^(?:any|some) ", re.IGNORECASE)


def try_auto_open_site_action(transcript: str):
    """If transcript matches 'open SITE and play/search X', handles it
    deterministically and returns the result, else returns None."""
    text = _strip_filler_prefix(transcript.strip()).rstrip(".!?")
    m = _OPEN_SITE_ACTION_RE.match(text)
    if not m:
        return None

    site = m.group(1).strip().lower()
    verb = m.group(2).strip().lower()
    query = m.group(3).strip()
    query = _GENERIC_QUERY_RE.sub("", query)  # "any music" -> "music"
    play = verb == "play"

    logger.info("Deterministic open-site-action matched: site=%s verb=%s query=%s", site, verb, query)

    if site == "spotify":
        return play_music(query)
    return open_website_search(site, query, play=play)


# --- Deterministic "add X to the queue" handling ------------------------------
# There was no queue tool at all, so the LLM routed "add X to the queue" to
# play_music, which plays it immediately. The regex lives in spotify_queue.py.

def try_auto_queue_music(transcript: str):
    """If transcript is 'add X to the queue' / 'queue X', queues it and returns
    the reply, else returns None."""
    query = spotify_queue.parse_queue_request(transcript)
    if query is None:
        return None
    logger.info("Deterministic queue request matched: query=%r", query)
    return queue_music(query)


# --- Deterministic "message X saying Y" handling (WhatsApp) -------------------
# Live log: "Send a message to Fazal saying hello in whatsapp" took 4-5 s to reach
# the model and it once answered "I don't have a phone number saved" from its own
# earlier reply without calling the tool at all. Same remedy as everywhere else:
# match the sentence in code. This never SENDS anything - it only calls
# send_whatsapp_message(), which reads the message back and waits for a "yes".

_WA_WORD = r"what'?s\s?app"
_WA_PREP = r"(?:in|on|via|using|through|over)"
_WA_SAY = (r"(?:\s+(?:(?:saying|says|say|that says|which says|and says?|to say|"
           r"telling (?:him|her|them))(?:\s+that)?|that|with (?:the )?message)\b|\s*:)")
_WA_MSG_NOUN = r"(?:message|text|msg)"
_WA_ON = rf"(?:\s+{_WA_PREP}\s+{_WA_WORD}(?:\s+web)?)?"
_WA_POLITE_RE = re.compile(
    r"^(?:raziel[,]?\s+|(?:can|could|would|will) you\s+(?:please\s+)?|"
    r"i (?:want|need|would like) (?:you )?to\s+|i'd like you to\s+)", re.IGNORECASE)
_WA_TAIL_RE = re.compile(rf"[\s,]*{_WA_PREP}\s+{_WA_WORD}(?:\s+web)?(?:\s+please)?\s*$", re.IGNORECASE)
_WA_LEAD_RE = re.compile(rf"^{_WA_PREP}\s+{_WA_WORD}(?:\s+web)?[,]?\s+", re.IGNORECASE)

# "send a (whatsapp) message to X (on whatsapp) saying Y"   /   "... to X"  (no text yet)
_WA_TO_RE = re.compile(
    rf"^(?:send|write|drop|shoot)\s+(?:a\s+|an\s+|the\s+)?(?:new\s+|quick\s+|short\s+)?(?:whatsapp\s+)?"
    rf"{_WA_MSG_NOUN}\s+(?:to|for)\s+(?P<name>.+?){_WA_ON}(?:{_WA_SAY}\s*(?P<msg>.+))?$",
    re.IGNORECASE)
# "send X a (whatsapp) message saying Y"
_WA_SEND_RE = re.compile(
    rf"^(?:send|write|drop|shoot)\s+(?!(?:a|an|the|new|quick|short)\s)(?P<name>[^,:]+?)\s+"
    rf"(?:a\s+|an\s+)?(?:new\s+|quick\s+|short\s+)?(?:whatsapp\s+)?{_WA_MSG_NOUN}{_WA_ON}"
    rf"{_WA_SAY}\s*(?P<msg>.+)$", re.IGNORECASE)
# "message X saying Y" / "text X that Y" / "whatsapp X saying Y"
_WA_VERB_RE = re.compile(
    rf"^(?:whatsapp|message|text|msg)\s+(?P<name>[^,:]+?){_WA_ON}{_WA_SAY}\s*(?P<msg>.+)$",
    re.IGNORECASE)


def try_auto_whatsapp(transcript: str):
    """
    If the transcript is a "message X saying Y" request, calls send_whatsapp_message()
    (which asks yes/no before sending) and returns her reply; else None.

    Only intercepts when it is sure it is a WhatsApp message: X is a saved contact,
    or WhatsApp was named out loud. Anything else falls through to the LLM.
    """
    text = _strip_filler_prefix(transcript.strip()).rstrip(" .")
    text = _WA_POLITE_RE.sub("", text, count=1)
    text = _strip_filler_prefix(text)
    named_whatsapp = bool(re.search(_WA_WORD, text, re.IGNORECASE))
    text = _WA_LEAD_RE.sub("", text, count=1)
    text = _WA_TAIL_RE.sub("", text, count=1).strip()

    for pattern in (_WA_TO_RE, _WA_SEND_RE, _WA_VERB_RE):
        m = pattern.match(text)
        if m:
            break
    else:
        return None

    name = re.sub(r"\s+(?:please|now)$", "", m.group("name").strip(" ,"), flags=re.IGNORECASE)
    message = (m.groupdict().get("msg") or "").strip()
    message = message.strip('"“” ').strip()
    if not name or len(name.split()) > 4:
        return None
    if not _resolve_contact(name)[0] and not named_whatsapp:
        return None                       # not a known contact and not WhatsApp: the LLM can decide
    logger.info("Deterministic WhatsApp request matched: to=%r message=%r", name, message)
    return send_whatsapp_message(name, message)


# --- Deterministic "open X" handling (app-launch latency, issue #5) ---------
# Measured from assistant.log: open_app() itself takes 5-100 ms. What made
# "open Spotify" feel slow was the ~4 s (3.8-4.5 s, every single time) between
# the transcript and the tool call - qwen3:8b deciding to call open_app - plus
# 2-3 s of PowerShell on the first launch of a session (now prewarmed). Same
# remedy as every other flaky/slow path in this project (handoff section 5):
# match it in code before the LLM ever sees it.
#
# The matcher only INTERCEPTS when it can positively identify the target -
# a known app/site/folder, or something actually in the Start Menu. Anything
# else ("open the file called report", "open a new tab", "open the door")
# returns None and falls through to the LLM exactly as before.

_OPEN_APP_RE = re.compile(
    r"^(?P<verb>open|launch|start|run|fire up)(?:\s+up)?\s+(?P<target>.+)$",
    re.IGNORECASE,
)
_APP_LEAD_RE = re.compile(r"^(?:the|my|an?)\s+", re.IGNORECASE)
_APP_TAIL_RE = re.compile(r"\s+(?:app|application|program|please|for me|now)$", re.IGNORECASE)
_MULTI_STEP_RE = re.compile(r"\s(?:and|then|to|with|in|on|from|for|about|of)\s", re.IGNORECASE)
# First words that mean "this is a file / web page / window, not an app":
# leave to the LLM's open_file / open_website_search / etc.
_NOT_AN_APP_WORDS = {
    "file", "files", "document", "doc", "pdf", "image", "photo", "picture", "folder",
    "directory", "website", "site", "page", "link", "tab", "window", "email",
    "message", "new", "last", "recent", "screenshot",
}


def _known_app_key(key: str):
    """True if key is in one of the hand-maintained app/site tables."""
    return (key in WEBSITE_URLS or key in PROTOCOL_APPS
            or key in APP_COMMANDS or key in FULL_PATH_CANDIDATES)


def _resolve_installed_app(key: str, loose: bool) -> bool:
    """
    Is `key` something actually installed? Exact Start Menu name always counts.
    With loose=True, also: every word of key appears in an app name
    ("code" -> "Visual Studio Code"), or a close fuzzy match (>= 0.8).
    """
    names = [n.lower() for n, _id in _get_start_apps()]
    names += list(_get_shortcut_index().keys())
    if key in names:
        return True
    if not loose or not names:
        return False
    words = key.split()
    for name in names:
        name_words = set(re.findall(r"[a-z0-9]+", name))
        if all(w in name_words for w in words):
            return True
    return bool(difflib.get_close_matches(key, names, n=1, cutoff=0.8))


def try_auto_open_app(transcript: str):
    """If transcript is 'open/launch X' and X is positively identifiable as an
    app, site or common folder, opens it directly and returns the reply.
    Otherwise returns None (falls through to the LLM)."""
    text = _strip_filler_prefix(transcript.strip()).rstrip(".!?").strip()
    m = _OPEN_APP_RE.match(text)
    if not m:
        return None

    verb = m.group("verb").lower()
    target = m.group("target").strip()
    for _ in range(2):
        target = _APP_LEAD_RE.sub("", target, count=1)
    for _ in range(2):
        target = _APP_TAIL_RE.sub("", target, count=1)
    key = re.sub(r"\s+", " ", target).strip().lower()

    if len(key) < 2 or len(key.split()) > 4:
        return None
    if _MULTI_STEP_RE.search(f" {key} "):
        return None                       # "open chrome and search cats" -> LLM
    if re.search(r"\.[a-z0-9]{2,4}$", key):
        return None                       # "open notes.txt" -> open_file via LLM

    # 1. Known app / website / URI-protocol app (includes "file manager").
    if _known_app_key(key):
        logger.info("Deterministic open-app matched (known): %s", key)
        return open_app(key)

    # 2. Common folder ("open downloads", "open my documents folder"). Only for
    # open/launch: "start music" means play music, not open the Music folder.
    folder_key = re.sub(r"\s+(?:folder|directory)$", "", key)
    if verb in ("open", "launch") and folder_key in KNOWN_FOLDERS:
        logger.info("Deterministic open-folder matched: %s", folder_key)
        return open_folder(folder_key)

    if key.split()[0] in _NOT_AN_APP_WORDS:
        return None

    # 3. Installed app. "open"/"launch" allow fuzzy/word matching; the more
    # ambiguous "start"/"run" (start music, run a search...) need an exact name.
    if _resolve_installed_app(key, loose=verb in ("open", "launch")):
        logger.info("Deterministic open-app matched (installed): %s", key)
        return open_app(key)

    return None


# --- Instant answers: date/time, thanks/hello, screenshots, "play X" ----------
# From assistant.log: "what is today's date?" took 17 s (a cold 8B model
# loading, then a tool call) to produce something datetime.now() knows in
# microseconds; a bare "Thank you" cost 4.5 s. The pure matchers live in
# instant_replies.py (strict, whole-utterance, unit-tested); these wrappers do
# the acting. Anything that doesn't match exactly still goes to the LLM.

def try_auto_datetime(transcript: str):
    reply = instant_replies.answer_datetime(transcript)
    if reply:
        logger.info("Instant date/time answer: %s", reply)
    return reply


def try_auto_smalltalk(transcript: str):
    reply = instant_replies.answer_smalltalk(transcript)
    if reply:
        logger.info("Instant small-talk reply: %s", reply)
    return reply


def try_auto_screenshot(transcript: str):
    kind = instant_replies.parse_screenshot_request(transcript)
    if kind == "take":
        logger.info("Deterministic screenshot request")
        return take_screenshot()
    if kind == "show":
        logger.info("Deterministic show-screenshot request")
        return show_last_screenshot()
    return None


def try_auto_play_music(transcript: str):
    """'play X' (Spotify, no other platform named) -> straight to Spotify."""
    parsed = instant_replies.parse_play_request(transcript)
    if parsed is None:
        return None
    kind, arg = parsed
    logger.info("Deterministic play request matched: %s %r", kind, arg)
    if kind == "next":
        return next_track()
    if kind == "previous":
        return previous_track()
    if kind == "playlist":
        return play_playlist(arg)
    return play_music(arg)
