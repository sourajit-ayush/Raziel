"""
instance_guard.py - "is Raziel already running?" and "please stop" for when she runs in the background.

Raziel can start by herself when you log in to Windows (start_raziel.pyw, set up once with
install_autostart.bat) and runs with no console window - so two things that used to rely on the
console need another home:

* ONE copy at a time. main.py holds a named Windows mutex (MUTEX_NAME) for as long as it runs.
  A second copy - the "Start Raziel" shortcut double-clicked while she's already running, or
  `python main.py` typed out of habit - sees it and exits instead of starting a second listener
  that would hear "Raziel" and answer every question twice.
* A way to stop her without Ctrl+C. main.py waits on a named event (STOP_EVENT_NAME) in a
  background thread; stop_raziel.pyw (the "Stop Raziel" shortcut) sets it, and main.py then takes
  the normal shutdown.request_shutdown() path - the same clean exit as Ctrl+C, force-exit
  watchdog included.

Both names live in the per-user "Local\\" namespace. Imports cleanly on any OS (the tests run on
Linux); off Windows every call is a harmless no-op: acquire() -> True, is_running() -> None,
request_stop() -> False, start_stop_listener() -> False. Nothing here ever raises: if a check
itself fails, she starts anyway (a rare second copy beats refusing to start at all).
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("voice_assistant")

IS_WINDOWS = sys.platform == "win32"

MUTEX_NAME = "Local\\RazielVoiceAssistant.Running"
STOP_EVENT_NAME = "Local\\RazielVoiceAssistant.Stop"

# main.py exits with this code when another copy is already running, so start_raziel.pyw can tell
# "someone else won the race" apart from "she crashed while starting".
ALREADY_RUNNING_EXIT_CODE = 3

ERROR_ACCESS_DENIED = 5
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000
EVENT_MODIFY_STATE = 0x0002
INFINITE = 0xFFFFFFFF
WAIT_OBJECT_0 = 0

_k32 = None
_mutex_handle = None      # held for the life of the process; Windows releases it when she exits
_event_handle = None


def _kernel32():
    """A private kernel32 handle with explicit argtypes/restype (same approach as winutil.py)."""
    global _k32
    if _k32 is None:
        import ctypes
        from ctypes import wintypes as wt

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [wt.LPVOID, wt.BOOL, wt.LPCWSTR]
        k.CreateMutexW.restype = wt.HANDLE
        k.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
        k.OpenMutexW.restype = wt.HANDLE
        k.CreateEventW.argtypes = [wt.LPVOID, wt.BOOL, wt.BOOL, wt.LPCWSTR]
        k.CreateEventW.restype = wt.HANDLE
        k.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
        k.OpenEventW.restype = wt.HANDLE
        k.SetEvent.argtypes = [wt.HANDLE]
        k.SetEvent.restype = wt.BOOL
        k.ResetEvent.argtypes = [wt.HANDLE]
        k.ResetEvent.restype = wt.BOOL
        k.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        k.WaitForSingleObject.restype = wt.DWORD
        k.CloseHandle.argtypes = [wt.HANDLE]
        k.CloseHandle.restype = wt.BOOL
        _k32 = k
    return _k32


def _clear_last_error():
    import ctypes
    ctypes.set_last_error(0)


def _last_error() -> int:
    import ctypes
    return ctypes.get_last_error()


def acquire() -> bool:
    """Claims "the running Raziel". True = carry on starting; False = another copy is already
    running, so this one should exit."""
    global _mutex_handle
    if not IS_WINDOWS or _mutex_handle:
        return True
    try:
        k = _kernel32()
        _clear_last_error()                   # CreateMutexW only reports "already existed" via GetLastError
        handle = k.CreateMutexW(None, False, MUTEX_NAME)
        err = _last_error()
        if not handle:
            logger.warning("Couldn't create the single-instance lock (error %s) - starting anyway", err)
            return True
        if err == ERROR_ALREADY_EXISTS:
            k.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception as e:                    # noqa: BLE001 - never stop her from starting over this
        logger.warning("Couldn't check for another running Raziel (%s) - starting anyway", e)
        return True


def is_running() -> Optional[bool]:
    """True if a Raziel is running in this Windows session right now, False if not, None if this
    can't be checked (not Windows, or the check failed)."""
    if not IS_WINDOWS:
        return None
    if _mutex_handle:
        return True                           # it's us
    try:
        k = _kernel32()
        _clear_last_error()
        handle = k.OpenMutexW(SYNCHRONIZE, False, MUTEX_NAME)
        if handle:
            k.CloseHandle(handle)
            return True
        # "access denied" still means the lock exists - someone holds it
        return _last_error() == ERROR_ACCESS_DENIED
    except Exception as e:                    # noqa: BLE001
        logger.warning("Couldn't check whether Raziel is running: %s", e)
        return None


def start_stop_listener(on_stop: Callable[[], None]) -> bool:
    """Calls on_stop() (once, on a background thread) when stop_raziel.pyw asks her to stop.
    Returns False if this isn't available here (then only Ctrl+C / closing the console stops her)."""
    global _event_handle
    if not IS_WINDOWS:
        return False
    try:
        k = _kernel32()
        handle = k.CreateEventW(None, True, False, STOP_EVENT_NAME)    # manual-reset, starts unset
        if not handle:
            logger.warning("Couldn't create the stop signal (error %s)", _last_error())
            return False
        # A leftover signalled event can only belong to a copy that is already on its way out
        # (we hold the single-instance lock, so no other Raziel is running) - it isn't meant for us.
        k.ResetEvent(handle)
        _event_handle = handle
    except Exception as e:                    # noqa: BLE001
        logger.warning("Couldn't set up the stop signal: %s", e)
        return False

    def _wait():
        try:
            result = k.WaitForSingleObject(handle, INFINITE)
        except Exception:                     # noqa: BLE001
            logger.exception("Waiting for the stop signal failed")
            return
        if result != WAIT_OBJECT_0:
            logger.warning("Stop signal wait ended unexpectedly (%s)", result)
            return
        logger.info("Stop requested (the Stop Raziel shortcut) - shutting down.")
        try:
            on_stop()
        except Exception:                     # noqa: BLE001
            logger.exception("Stopping after the stop request failed")

    threading.Thread(target=_wait, name="stop-listener", daemon=True).start()
    return True


def request_stop() -> bool:
    """Asks the running Raziel to shut down. True if the request reached her."""
    if not IS_WINDOWS:
        return False
    try:
        k = _kernel32()
        handle = k.OpenEventW(EVENT_MODIFY_STATE, False, STOP_EVENT_NAME)
        if not handle:
            return False
        try:
            return bool(k.SetEvent(handle))
        finally:
            k.CloseHandle(handle)
    except Exception as e:                    # noqa: BLE001
        logger.warning("Couldn't send the stop request: %s", e)
        return False


def wait_until_stopped(timeout: float, poll: float = 0.25) -> bool:
    """After request_stop(): True once she has actually exited, False if still running at `timeout`."""
    deadline = time.monotonic() + timeout
    while True:
        if is_running() is False:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
