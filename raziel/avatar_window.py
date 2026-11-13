"""
avatar_window.py — always-on-top pywebview window for the Raziel avatar.

Two avatars (config.AVATAR_STYLE):
  "orb"  avatar_orb.html - a living orb of glowing particles that can turn into a dragon, a sword,
         a knight... when asked, and dances to music playing on the PC (the default)
  "vrm"  avatar.html     - the 3D character, Raziel-1.vrm

WHY THERE'S AN HTTP SERVER IN HERE
----------------------------------
avatar.html loads Raziel-1.vrm with fetch(). WebView2 (like Chrome) blocks
fetch on file:// URLs, so pointing pywebview straight at the .html file means
the model never loads and the window sits on "LOADING" forever.

So we spin up a tiny static file server bound to 127.0.0.1 on a free port,
serving only the project folder, and point the window at that. It starts in
milliseconds, is invisible to the user, and never leaves the machine.

WHERE SHE OPENS
---------------
Where you last dragged her, at the size you last gave her (Ctrl + mouse wheel over her, or "make
yourself bigger"): avatar_place.py saves it in avatar_place.json and brings her back onto a screen
if that place is gone. Nothing saved yet: the right-hand side of the main screen.

Usage in main.py:

    import avatar_window
    window = avatar_window.create()      # must be called from the MAIN thread
    ...
    avatar_window.start()                # blocks; pywebview owns the main thread
"""

from __future__ import annotations

import http.server
import os
import socket
import socketserver
import threading
import time
from functools import partial

import webview

import avatar_place

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Portrait aspect — a full-body figure needs roughly 1:1.8.
WIDTH = 460
HEIGHT = 820
# The orb and its shapes were designed in a 3:4 frame.
ORB_WIDTH = 420
ORB_HEIGHT = 560
ORB_PARTICLES = 24000
PAGES = {"orb": "avatar_orb.html", "vrm": "avatar.html"}

_httpd = None
_http_thread = None
_base_url = None
_window = None


# ---------------------------------------------------------------- file server

class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that doesn't spam the console with GET lines."""

    def log_message(self, *args):
        pass

    def end_headers(self):
        # Never cache, so editing avatar.html and reloading actually shows changes.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        super().end_headers()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_file_server(directory: str = _SCRIPT_DIR) -> str:
    """Serve `directory` on a random localhost port. Returns the base URL."""
    global _httpd, _http_thread, _base_url
    if _base_url:
        return _base_url

    port = _free_port()
    handler = partial(_QuietHandler, directory=directory)

    class _Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    _httpd = _Server(("127.0.0.1", port), handler)
    _http_thread = threading.Thread(target=_httpd.serve_forever,
                                    name="avatar-http", daemon=True)
    _http_thread.start()
    _base_url = f"http://127.0.0.1:{port}"
    print(f"[avatar] serving {directory} at {_base_url}")

    try:
        import shutdown
        shutdown.on_shutdown(stop_file_server)
    except Exception:
        pass

    return _base_url


def stop_file_server():
    global _httpd, _base_url
    if _httpd is not None:
        try:
            _httpd.shutdown()
            _httpd.server_close()
        except Exception:
            pass
        _httpd = None
        _base_url = None


# ---------------------------------------------------------------- window

# How the window background is removed.
#
#   "native"  DEFAULT. pywebview's own transparent=True. It paints the host
#             window pure red, tells Windows "red is see-through"
#             (TransparencyKey), and tells WebView2 to leave the page
#             background transparent. avatar.html draws nothing behind her, so
#             the only thing under the page is the red host surface, which
#             Windows removes - the desktop shows through, and her edges are
#             properly alpha-blended (no halo).
#   "none"    Opaque dark tile (DARK_BG). Always works, not see-through.
#
# Set it in config.py:  AVATAR_TRANSPARENCY = "native"  or  "none".
#
# WHY THE OLD FLAT-COLOUR TRICK IS GONE
# The previous approach painted the page magenta and asked Windows to key
# magenta out. On this machine the screenshot proves it does nothing: WebView2
# presents the page through DirectComposition, a GPU path GDI colour keys never
# touch, so the magenta stayed. "native" works the other way round - the page
# contributes ALPHA rather than a colour, and the key is applied to the host's
# own surface underneath it, which is the layer Windows does honour.
# (The "pixel probe" that reported the key as WORKING was also wrong: it ran
# before the page had painted, and was allowed to declare success. The check
# below can now only ever act on evidence of FAILURE.)
try:
    import config as _config
except Exception:                       # standalone run, no config on path
    _config = None

_requested = str(getattr(_config, "AVATAR_TRANSPARENCY", "native")).strip().lower()
TRANSPARENCY = _requested if _requested in ("native", "none") else "native"


def _style() -> str:
    """ "orb" or "vrm". Falls back to the 3D avatar if avatar_orb.html is missing."""
    requested = str(getattr(_config, "AVATAR_STYLE", "orb")).strip().lower()
    style = requested if requested in PAGES else "orb"
    if style == "orb" and not os.path.exists(os.path.join(_SCRIPT_DIR, PAGES["orb"])):
        print("[avatar] avatar_orb.html is missing next to avatar_window.py - showing the 3D avatar instead.")
        style = "vrm"
    return style


STYLE = _style()


def _int_setting(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(getattr(_config, name, default))))
    except (TypeError, ValueError):
        return default

# Background of the opaque tile ("none" mode, and the automatic fallback).
DARK_BG = "0d1017"

# Colours that can only be on screen if transparency FAILED: pywebview's key
# colour (pure red) or the old magenta chroma. A real desktop is very unlikely
# to show either at several window edges at once.
_FAILURE_COLOURS = ((255, 0, 0), (255, 0, 255))

_mode = None            # the mode actually in effect: native | none
_watchdog_started = False

# Leave WebView2's own rendering defaults alone. (The earlier
# --disable-gpu-compositing workaround existed only to help the colour key,
# which never worked; it forced slower software compositing for nothing.)
# Set to False only if the window is invisible or flickers on your GPU.
GPU_COMPOSITING = True

if not GPU_COMPOSITING:
    os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
        "--disable-gpu-compositing "
        "--disable-features=CalculateNativeWinOcclusion"
    )
else:
    # A stale value from an earlier run of this app's older builds must not leak in.
    if "--disable-gpu-compositing" in os.environ.get("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", ""):
        os.environ.pop("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", None)


def _window_tree(host_hwnd):
    """
    Every descendant window of host_hwnd, with class names.

    WebView2 does not paint into the window pywebview creates — it creates its
    own child window (class "Chrome_WidgetWin_*") and renders there. Keying
    the host does nothing, because the host never draws anything under a
    fully-covering child. This is what EnumChildWindows reveals: it walks the
    ENTIRE descendant subtree, not just immediate children, in one call.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        length = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        found.append((hwnd, cls.value, title.value,
                      rect.right - rect.left, rect.bottom - rect.top))
        return True

    user32.EnumChildWindows(host_hwnd, WNDENUMPROC(callback), 0)
    return found


def _find_process_windows():
    """
    Every visible top-level window owned by this process.

    Matching by title is unreliable: pywebview's frameless windows often report
    an empty or unexpected caption, which is why the first attempt at this
    silently found nothing. The owning process ID is dependable.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []
    my_pid = os.getpid()

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != my_pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 100 or h < 100:          # skip tooltips and hidden helpers
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        found.append((hwnd, buf.value, w, h))
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return found



def _orb_look() -> str:
    """ "sphere" (she lives in a glass sphere, the default) or "float" (bare particles on the desktop)."""
    look = str(getattr(_config, "AVATAR_ORB_LOOK", "sphere")).strip().lower()
    return look if look in ("sphere", "float") else "sphere"


def _page_url() -> str:
    """The avatar page's URL for the style and background mode currently in effect."""
    page = f"{start_file_server()}/{PAGES[STYLE]}"
    params = []
    if _mode == "none":
        params.append(f"bg={DARK_BG}")
    if STYLE == "orb":
        n = _int_setting("AVATAR_PARTICLES", ORB_PARTICLES, 4000, 60000)
        if n != ORB_PARTICLES:
            params.append(f"n={n}")
        if _mode != "none" and _orb_look() != "sphere":
            params.append(f"look={_orb_look()}")
    return page + ("?" + "&".join(params) if params else "")


def _near(a, b, tol: int = 14) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def _sample_edges(hwnd):
    """
    Colours (r, g, b) currently on screen at points near the left and right
    edges of the window, where she isn't standing, or [] if it can't tell.
    GetPixel on the screen DC returns what DWM composed.
    """
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return []
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 100 or h < 100:
            return []

        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)

        hdc = user32.GetDC(0)
        if not hdc:
            return []
        out = []
        try:
            for fx, fy in ((0.06, 0.10), (0.94, 0.10), (0.06, 0.50),
                           (0.94, 0.50), (0.06, 0.78), (0.94, 0.78)):
                x, y = int(rect.left + fx * w), int(rect.top + fy * h)
                if not (vx <= x < vx + vw and vy <= y < vy + vh):
                    continue
                c = gdi32.GetPixel(hdc, x, y)
                if c == 0xFFFFFFFF:          # CLR_INVALID
                    continue
                out.append((c & 0xFF, (c >> 8) & 0xFF, (c >> 16) & 0xFF))   # COLORREF = 0x00BBGGRR
        finally:
            user32.ReleaseDC(0, hdc)
        return out
    except Exception as exc:
        print(f"[avatar] background check failed: {exc}")
        return []


def _fall_back_to_opaque(reason: str):
    """Transparency visibly failed: stop showing red/magenta, use the dark tile."""
    global _mode
    _mode = "none"
    print(f"[avatar] transparency FAILED on this machine ({reason}).")
    print(f"[avatar] switching to a dark tile (#{DARK_BG}). To make that permanent "
          f"and skip the flash next time, set AVATAR_TRANSPARENCY = \"none\" in config.py.")
    if _window is not None:
        try:
            _window.load_url(_page_url())
        except Exception as exc:
            print(f"[avatar] couldn't reload the page for the fallback: {exc}")


def _judge_samples(samples):
    """
    "failed"  - most edge pixels are the key/chroma colour: transparency isn't working.
    "ok"      - none of them are: nothing suggests a problem.
    "unclear" - too few samples to say.
    """
    if len(samples) < 3:
        return "unclear"
    bad = sum(1 for s in samples if any(_near(s, c) for c in _FAILURE_COLOURS))
    if bad >= max(3, (len(samples) + 1) // 2):
        return "failed"
    return "ok"


def _watch_background(delays=(5, 3, 4, 6, 8, 12)):
    """
    Native mode only. Some seconds AFTER the page has had time to load, look at
    the real pixels at the window's edges. If they are the key colour (red) or
    magenta, transparency did not take effect: fall back to the dark tile.
    It never declares success and never saves anything - a check that could
    say "it works" is exactly what misled the last version.
    """
    last = []
    for d in delays:
        time.sleep(d)
        hosts = _find_process_windows()
        samples = _sample_edges(hosts[0][0]) if hosts else []
        if samples:
            last = samples
        verdict = _judge_samples(samples)
        if verdict == "failed":
            _fall_back_to_opaque("edge pixels are still the key colour: " + str(samples[:3]))
            return
    if last:
        print(f"[avatar] background check: window-edge pixels are {last[:3]}. If you can "
              f"see a solid white/black rectangle behind her, set "
              f"AVATAR_TRANSPARENCY = \"none\" in config.py for a clean dark tile.")
    else:
        print("[avatar] background check: couldn't sample the window (off-screen or "
              "covered?). Nothing changed.")


# ---------------------------------------------------------------- where she is (avatar_place.py)
#
# All of this works in the window's own screen coordinates: what Windows reports for her window
# (GetWindowRect) is exactly what is saved, and exactly what is set again next time, so a place
# round-trips whatever the display scaling. Only the size handed to pywebview at creation is in its
# own units (WinForms scales it by the screen's DPI); a check right after she appears fixes any
# difference.

_target = None          # the rectangle she should open with (window pixels)
_tracking = False       # True once she is on screen: moves / resizes from then on are saved
_default_size = (ORB_WIDTH, ORB_HEIGHT)


_u32 = None


def _user32():
    """A private handle on user32 with proper 64-bit argument types (other modules' calls unaffected)."""
    global _u32
    if _u32 is None:
        import ctypes
        from ctypes import wintypes
        u = ctypes.WinDLL("user32")
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.UINT]
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsZoomed.argtypes = [wintypes.HWND]
        u.GetDpiForWindow.argtypes = [wintypes.HWND]
        u.GetDpiForWindow.restype = wintypes.UINT
        _u32 = u
    return _u32


def _hwnd():
    """Her window's handle, or None."""
    if os.name != "nt" or _window is None:
        return None
    try:
        return int(_window.native.Handle.ToInt64())
    except Exception:
        hosts = _find_process_windows()
        return hosts[0][0] if hosts else None


def _dpi_scale(hwnd=None) -> float:
    """Window pixels per CSS pixel: 1.25 at 125 % display scaling (1.0 when Windows scales for us)."""
    if os.name != "nt":
        return 1.0
    try:
        u = _user32()
        dpi = u.GetDpiForWindow(hwnd) if hwnd else u.GetDpiForSystem()
        return max(0.5, min(4.0, dpi / 96.0)) if dpi else 1.0
    except Exception:
        return 1.0


def get_rect():
    """(x, y, width, height) of her window, or None (no window, minimised, not on Windows)."""
    hwnd = _hwnd()
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        u = _user32()
        if u.IsIconic(hwnd) or u.IsZoomed(hwnd):
            return None
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        return None


def set_rect(rect):
    hwnd = _hwnd()
    if not hwnd:
        return
    try:
        x, y, w, h = (int(v) for v in rect)
        _user32().SetWindowPos(hwnd, None, x, y, w, h, 0x0004 | 0x0010)      # NOZORDER | NOACTIVATE
    except Exception as exc:
        print(f"[avatar] couldn't move/resize the window: {exc}")


def _on_shown():
    """She is on screen: make sure she has the size / place she should have, then start saving."""
    global _tracking
    time.sleep(0.05)
    try:
        cur = get_rect()
        if _target is not None and cur is not None and any(abs(a - b) > 2 for a, b in zip(cur, _target)):
            set_rect(_target)
    finally:
        _tracking = True


def _on_changed(*_args):
    """Every move / resize (a drag sends many): remember the latest; avatar_place saves it a second later."""
    if not _tracking:
        return
    rect = get_rect()
    if rect is not None and rect[2] >= 50 and rect[3] >= 50 and rect[0] > -20000 and rect[1] > -20000:
        avatar_place.note(STYLE, rect)


class _PageApi:
    """What the avatar page may call (window.pywebview.api.*): nothing but her own size."""

    def scale(self, factor=1.0):
        """Ctrl + mouse wheel over her."""
        try:
            k = min(1.6, max(0.6, float(factor)))
        except (TypeError, ValueError):
            return False
        c = avatar_place.controller()
        if c is None:
            return False
        rect, _limit = c.scale_by(k)
        return rect is not None


def _initial_rect(width: int, height: int, scale: float):
    """Saved place (made visible on today's screens), else the default spot. Window pixels."""
    areas = avatar_place.work_areas()
    saved = avatar_place.load(STYLE)
    if saved is not None:
        rect = avatar_place.fit_rect(saved, areas)
        if rect != saved:
            print(f"[avatar] the saved place {saved} isn't on a screen any more - using {rect}")
        return rect
    return avatar_place.default_rect(int(round(width * scale)), int(round(height * scale)), areas)


def create(width: int = None, height: int = None, transparent: bool = None):
    """
    Create the always-on-top avatar window. Call from the MAIN thread only.
    Returns the pywebview Window object.
    """
    global _window, _mode, _watchdog_started, _target, _tracking, _default_size
    _mode = TRANSPARENCY
    page = _page_url()
    if STYLE == "orb":
        width = width or _int_setting("AVATAR_ORB_WINDOW_WIDTH", ORB_WIDTH, 200, 2000)
        height = height or _int_setting("AVATAR_ORB_WINDOW_HEIGHT", ORB_HEIGHT, 200, 2000)
    else:
        width, height = width or WIDTH, height or HEIGHT
    _default_size = (width, height)
    scale = _dpi_scale()
    _target = _initial_rect(width, height, scale)
    _tracking = False
    x, y = _target[0], _target[1]
    # pywebview wants the size in its own units (it scales by the DPI); the position as is.
    width, height = max(100, int(round(_target[2] / scale))), max(100, int(round(_target[3] / scale)))

    if transparent is None:
        transparent = (_mode == "native")

    # In native mode pywebview overrides this with its own key colour; in "none"
    # mode it is the tile colour, so no seam shows at the edges while resizing.
    host_bg = f"#{DARK_BG}" if _mode == "none" else "#000000"

    print(f"[avatar] {STYLE} avatar, window mode: {_mode}"
          + (" (transparent background)" if transparent else " (opaque dark tile)"))

    _window = webview.create_window(
        "Raziel",
        page,
        width=width,
        height=height,
        frameless=True,
        easy_drag=True,          # drag her around by the body
        on_top=True,
        resizable=True,
        transparent=transparent,
        # pywebview's default shadow=True calls DwmExtendFrameIntoClientArea on a
        # frameless window, which draws a soft rectangular shadow / 1px glass
        # border around the whole window - exactly what a see-through avatar
        # must not have. Only the opaque tile keeps it.
        shadow=not transparent,
        background_color=host_bg,
        x=x,                     # where you left her last time (avatar_place.py)
        y=y,
        js_api=_PageApi(),       # Ctrl + mouse wheel over her -> bigger / smaller
    )

    events = getattr(_window, "events", None)
    if events is not None:
        try:
            events.shown += _on_shown
            events.moved += _on_changed
            events.resized += _on_changed
        except Exception as exc:
            print(f"[avatar] couldn't watch the window's moves: {exc}")
    avatar_place.set_controller(avatar_place.Controller(
        STYLE, get_rect, set_rect, lambda: _dpi_scale(_hwnd()), _default_size))

    if _mode == "native" and not _watchdog_started:
        _watchdog_started = True
        threading.Thread(target=_watch_background, name="avatar-bg-check", daemon=True).start()

    try:
        import shutdown
        shutdown.on_shutdown(_close_windows)              # (callbacks run last-registered first:)
        shutdown.on_shutdown(avatar_place.flush)          # a drag in the last second is saved, then she closes
    except Exception:
        pass

    return _window


def _close_windows():
    """Stop Raziel: close her window(s) so pywebview's loop ends. (pywebview has no module-level
    destroy(); each window closes itself.)"""
    for w in list(getattr(webview, "windows", None) or []):
        try:
            w.destroy()
        except Exception:
            pass


def start(debug: bool = False):
    """Blocks. pywebview takes ownership of the main thread from here."""
    global _started_gui
    if _started_gui:
        return
    _started_gui = True
    webview.start(debug=debug, gui="edgechromium")


_started_gui = False


# ---------------------------------------------------------------- back-compat
# The previous avatar_window.py used these names. Aliased so main.py doesn't
# need editing.

def create_window(*args, **kwargs):
    """Old name for create(). Does NOT block - call start() afterwards."""
    return create(*args, **kwargs)


def run(debug: bool = False):
    """Old name for start()."""
    start(debug=debug)


def show(*args, **kwargs):
    """Create the window and block, in one call."""
    win = create(*args, **kwargs)
    start()
    return win


def retry_color_key():
    """Kept so old callers don't break. The colour key is no longer used."""
    print("[avatar] retry_color_key(): the colour-key mode was removed "
          "(it cannot work with WebView2's DirectComposition output).")


def list_windows():
    """Diagnostic: print every window this process owns, host and descendants."""
    for hwnd, caption, w, h in _find_process_windows():
        print(f"host  hwnd={hwnd}  '{caption}'  {w}x{h}")
        for chwnd, cls, ctitle, cw, ch in _window_tree(hwnd):
            tag = " <- render surface" if "chrome_widgetwin" in cls.lower() else ""
            print(f"  child hwnd={chwnd}  class='{cls}'  {cw}x{ch}{tag}")


def reload():
    """Reload the avatar page without restarting Raziel - handy while tuning."""
    if _window is not None and _base_url:
        _window.load_url(_page_url())        # keeps the current mode's URL params


# ---------------------------------------------------------------- standalone
#
# IF THE BACKGROUND IS STILL NOT TRANSPARENT
# ------------------------------------------
# 1. Check the console for "[avatar] window mode: native". If it says "none",
#    config.py has AVATAR_TRANSPARENCY = "none".
# 2. Look at the "[avatar] background check" line a few seconds after start-up.
#    It prints the colours found at the window edges. Red (255, 0, 0) or magenta
#    means the key was not honoured - she is switched to the dark tile
#    automatically. A white or black rectangle means WebView2 ignored the
#    transparent background: set AVATAR_TRANSPARENCY = "none".
# 3. If the window is invisible or flickers, set GPU_COMPOSITING = False above.
# 4. run list_windows() for the window tree.
#
# A guaranteed cut-out on every Windows setup means not using a web view at
# all - a native OpenGL window with its own layered surface. That is a rewrite,
# not a setting.

if __name__ == "__main__":
    # Run this file directly to see the window with no voice loop attached.
    import shutdown
    shutdown.install()
    create()
    start(debug=True)
