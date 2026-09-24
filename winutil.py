"""
winutil.py - the Windows-only plumbing shared by the system-control, clipboard and dictation
features: clipboard (Unicode / Hindi safe), keyboard input (SendInput), window listing and focus.

* Everything is imported and set up LAZILY on first use, so this module imports fine on Linux
  (tests) and on Windows without side effects. On a non-Windows system every function that
  needs the OS raises OSError("This works on Windows only.").
* Private ctypes.WinDLL handles with explicit argtypes/restype (64-bit safe). The shared
  ctypes.windll objects are NOT used: other libraries (pyautogui) set their own argtypes on those.
* Written against the Microsoft docs and pyperclip's clipboard code; not run on Windows by the
  author, so each function reports failure by raising OSError (callers turn that into speech).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Dict, List, Optional

logger = logging.getLogger("voice_assistant")

IS_WINDOWS = sys.platform == "win32"

# virtual-key codes
VK_BACK, VK_TAB, VK_RETURN, VK_SHIFT, VK_CONTROL, VK_MENU = 0x08, 0x09, 0x0D, 0x10, 0x11, 0x12
VK_ESCAPE, VK_SPACE, VK_LWIN, VK_DELETE = 0x1B, 0x20, 0x5B, 0x2E
VK_VOLUME_MUTE, VK_VOLUME_DOWN, VK_VOLUME_UP = 0xAD, 0xAE, 0xAF
VK_A, VK_C, VK_D, VK_V, VK_X, VK_Z, VK_M = 0x41, 0x43, 0x44, 0x56, 0x58, 0x5A, 0x4D
VK_F4 = 0x73

_ctx: Optional[dict] = None


def _need_windows():
    if not IS_WINDOWS:
        raise OSError("This works on Windows only.")


def _init() -> dict:
    """Builds the ctypes handles once."""
    global _ctx
    if _ctx is not None:
        return _ctx
    _need_windows()
    import ctypes
    from ctypes import wintypes as wt

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wt.HGLOBAL
    kernel32.GlobalLock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalLock.restype = wt.LPVOID
    kernel32.GlobalUnlock.argtypes = [wt.HGLOBAL]
    kernel32.GlobalUnlock.restype = wt.BOOL
    kernel32.GlobalFree.argtypes = [wt.HGLOBAL]
    kernel32.GlobalFree.restype = wt.HGLOBAL
    kernel32.GetCurrentThreadId.restype = wt.DWORD
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]
    kernel32.CloseHandle.restype = wt.BOOL

    user32.OpenClipboard.argtypes = [wt.HWND]
    user32.OpenClipboard.restype = wt.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wt.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wt.BOOL
    user32.GetClipboardData.argtypes = [wt.UINT]
    user32.GetClipboardData.restype = wt.HANDLE
    user32.SetClipboardData.argtypes = [wt.UINT, wt.HANDLE]
    user32.SetClipboardData.restype = wt.HANDLE

    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _U)]

    user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wt.UINT

    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
    user32.EnumWindows.restype = wt.BOOL
    user32.IsWindowVisible.argtypes = [wt.HWND]
    user32.IsWindowVisible.restype = wt.BOOL
    user32.IsIconic.argtypes = [wt.HWND]
    user32.IsIconic.restype = wt.BOOL
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = [wt.HWND]
    user32.SetForegroundWindow.restype = wt.BOOL
    user32.BringWindowToTop.argtypes = [wt.HWND]
    user32.BringWindowToTop.restype = wt.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
    user32.AttachThreadInput.restype = wt.BOOL
    user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    user32.PostMessageW.restype = wt.BOOL
    user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
    user32.GetWindow.restype = wt.HWND
    user32.LockWorkStation.argtypes = []
    user32.LockWorkStation.restype = wt.BOOL
    user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
    user32.CreateWindowExW.restype = wt.HWND
    user32.DestroyWindow.argtypes = [wt.HWND]
    user32.DestroyWindow.restype = wt.BOOL

    _ctx = dict(ctypes=ctypes, wt=wt, user32=user32, kernel32=kernel32, INPUT=INPUT,
                WNDENUMPROC=WNDENUMPROC)
    return _ctx


# ------------------------------------------------------------------ clipboard

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _open_clipboard(timeout: float = 0.6, hwnd=None) -> bool:
    c = _init()
    end = time.time() + timeout
    while time.time() < end:
        if c["user32"].OpenClipboard(hwnd):
            return True
        time.sleep(0.01)
    return False


def _make_owner_window():
    """A tiny invisible window to own the clipboard while WRITING to it: after OpenClipboard(NULL) +
    EmptyClipboard the clipboard has no owner and SetClipboardData fails (Microsoft's documentation;
    pyperclip does the same thing). Returns the HWND, or None if one can't be created."""
    c = _init()
    try:
        hwnd = c["user32"].CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, None, None, None, None)
        return hwnd or None
    except Exception:                                    # noqa: BLE001
        return None


def get_clipboard_text() -> str:
    """Text on the clipboard ('' if it holds something else). Unicode / Hindi safe."""
    c = _init()
    if not _open_clipboard():
        raise OSError("Couldn't open the clipboard (another program is using it).")
    try:
        handle = c["user32"].GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = c["kernel32"].GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return c["ctypes"].wstring_at(ptr)
        finally:
            c["kernel32"].GlobalUnlock(handle)
    finally:
        c["user32"].CloseClipboard()


def set_clipboard_text(text: str) -> None:
    """Puts text on the clipboard (Unicode / Hindi safe)."""
    c = _init()
    ctypes = c["ctypes"]
    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    handle = c["kernel32"].GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise MemoryError("GlobalAlloc failed")
    ptr = c["kernel32"].GlobalLock(handle)
    if not ptr:
        c["kernel32"].GlobalFree(handle)
        raise MemoryError("GlobalLock failed")
    ctypes.memmove(ptr, data, len(data))
    c["kernel32"].GlobalUnlock(handle)
    owner = _make_owner_window()
    try:
        if not _open_clipboard(hwnd=owner):
            c["kernel32"].GlobalFree(handle)
            raise OSError("Couldn't open the clipboard (another program is using it).")
        try:
            c["user32"].EmptyClipboard()
            if not c["user32"].SetClipboardData(CF_UNICODETEXT, handle):
                c["kernel32"].GlobalFree(handle)
                raise OSError("SetClipboardData failed")
            # success: the clipboard owns the handle now - never free it
        finally:
            c["user32"].CloseClipboard()
    finally:
        if owner:
            try:
                c["user32"].DestroyWindow(owner)
            except Exception:                            # noqa: BLE001
                pass


# ------------------------------------------------------------------ keyboard

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


def _send(events) -> None:
    c = _init()
    arr = (c["INPUT"] * len(events))(*events)
    sent = c["user32"].SendInput(len(arr), arr, c["ctypes"].sizeof(c["INPUT"]))
    if sent != len(arr):
        # Also happens (silently) when the target window is elevated and we are not (UIPI).
        raise OSError("Windows refused the keyboard input (is the target app running as administrator?).")


def _vk_event(vk: int, up: bool = False, extended: bool = False):
    c = _init()
    ev = c["INPUT"](type=INPUT_KEYBOARD)
    ev.ki.wVk = vk
    ev.ki.dwFlags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if extended else 0)
    return ev


def _unicode_event(unit: int, up: bool = False):
    c = _init()
    ev = c["INPUT"](type=INPUT_KEYBOARD)
    ev.ki.wVk = 0
    ev.ki.wScan = unit
    ev.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    return ev


def send_hotkey(*vks: int) -> None:
    """Presses the keys together and releases them in reverse order: send_hotkey(VK_CONTROL, VK_V)."""
    _send([_vk_event(v) for v in vks] + [_vk_event(v, up=True) for v in reversed(vks)])


def tap_key(vk: int, times: int = 1) -> None:
    for _ in range(max(1, times)):
        send_hotkey(vk)


def press_volume_key(vk: int, times: int = 1) -> None:
    """Media keys need the extended flag on some keyboards' drivers; harmless otherwise."""
    for _ in range(max(1, times)):
        _send([_vk_event(vk, extended=True), _vk_event(vk, up=True, extended=True)])


def paste() -> None:
    send_hotkey(VK_CONTROL, VK_V)


def type_text(text: str, delay: float = 0.004) -> int:
    """
    Types text into whatever window has the keyboard focus, one character at a time as real
    Unicode key events (Hindi, emoji and accents all work; the clipboard is not touched).
    Newlines become Enter, tabs become Tab. Returns the number of characters typed.
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    typed = 0
    for ch in text:
        if ch == "\n":
            send_hotkey(VK_RETURN)
        elif ch == "\t":
            send_hotkey(VK_TAB)
        else:
            raw = ch.encode("utf-16-le")
            units = [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]
            _send([_unicode_event(u) for u in units] + [_unicode_event(u, up=True) for u in units])
        typed += 1
        if delay:
            time.sleep(delay)
    return typed


# ------------------------------------------------------------------ windows

SW_RESTORE = 9
SW_MINIMIZE = 6
WM_CLOSE = 0x0010
GW_OWNER = 4


def _exe_of(hwnd) -> str:
    c = _init()
    wt, ctypes = c["wt"], c["ctypes"]
    pid = wt.DWORD()
    c["user32"].GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = c["kernel32"].OpenProcess(0x1000, False, pid.value)      # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wt.DWORD(1024)
        path = ctypes.create_unicode_buffer(1024)
        if c["kernel32"].QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return os.path.basename(path.value).lower()
    finally:
        c["kernel32"].CloseHandle(handle)
    return ""


def _title_of(hwnd) -> str:
    c = _init()
    n = c["user32"].GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = c["ctypes"].create_unicode_buffer(n + 1)
    c["user32"].GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def list_windows() -> List[Dict[str, object]]:
    """Visible, titled top-level windows in Z-order (front first): [{hwnd, title, exe, minimized}]."""
    c = _init()
    found: List[Dict[str, object]] = []

    @c["WNDENUMPROC"]
    def _cb(hwnd, _lparam):
        try:
            if c["user32"].IsWindowVisible(hwnd) and not c["user32"].GetWindow(hwnd, GW_OWNER):
                title = _title_of(hwnd)
                if title and title not in ("Program Manager",):
                    found.append({"hwnd": int(hwnd), "title": title, "exe": _exe_of(hwnd),
                                  "minimized": bool(c["user32"].IsIconic(hwnd))})
        except Exception:                       # a window vanishing mid-enumeration is normal
            pass
        return True

    c["user32"].EnumWindows(_cb, 0)
    return found


def foreground_window() -> Dict[str, object]:
    c = _init()
    hwnd = c["user32"].GetForegroundWindow()
    if not hwnd:
        return {"hwnd": 0, "title": "", "exe": ""}
    return {"hwnd": int(hwnd), "title": _title_of(hwnd), "exe": _exe_of(hwnd)}


def focus_window(hwnd: int) -> bool:
    """Brings a window to the front. Windows restricts SetForegroundWindow, so a tap of Alt and a
    temporary AttachThreadInput are used to be allowed to. Returns True if it worked."""
    c = _init()
    user32, kernel32, wt = c["user32"], c["kernel32"], c["wt"]
    hwnd = wt.HWND(hwnd)
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    try:
        _send([_vk_event(VK_MENU), _vk_event(VK_MENU, up=True)])
    except OSError:
        pass
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_tid) and fg_tid != me and user32.AttachThreadInput(me, fg_tid, True)
    try:
        user32.BringWindowToTop(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_tid, False)


def close_window(hwnd: int) -> bool:
    """Asks a window to close, exactly like clicking its X (the app may still ask to save)."""
    c = _init()
    return bool(c["user32"].PostMessageW(c["wt"].HWND(hwnd), WM_CLOSE, 0, 0))


def minimize_window(hwnd: int) -> bool:
    c = _init()
    return bool(c["user32"].ShowWindow(c["wt"].HWND(hwnd), SW_MINIMIZE))


def lock_workstation() -> bool:
    c = _init()
    return bool(c["user32"].LockWorkStation())
