"""
shutdown.py — cooperative shutdown for Raziel.

WHY Ctrl+C doesn't work today
-----------------------------
Python only runs signal handlers in the MAIN thread, and only between bytecode
instructions. `webview.start()` blocks the main thread inside a native Win32
message loop, so no Python bytecode executes there -> your KeyboardInterrupt is
queued and never delivered until the webview closes. On top of that, any
non-daemon background thread (voice loop, asyncio websocket loop) keeps the
process alive even after the main thread finally returns.

THE FIX
-------
1. Register a native Windows console control handler via ctypes. Windows calls
   it on a fresh OS thread, so it fires even while the main thread is parked in
   the native loop.
2. That handler sets a global Event and runs registered callbacks (destroy the
   webview window, stop the asyncio loop, unblock audio reads).
3. A watchdog force-kills the process if a blocking C call (faster-whisper
   inference, PortAudio read, Ollama HTTP) refuses to unwind in time.
4. A second Ctrl+C kills immediately.

Usage in main.py
----------------
    import shutdown
    shutdown.install()
    shutdown.on_shutdown(lambda: webview.destroy())
    shutdown.on_shutdown(avatar_server.stop)
    ...
    while not shutdown.is_shutting_down():
        ...
"""

from __future__ import annotations

import atexit
import ctypes
import os
import signal
import sys
import threading
import time
from typing import Callable, List

# ---------------------------------------------------------------- MKL / Intel
# numpy, ctranslate2 (faster-whisper) and friends load Intel MKL, whose Fortran
# runtime installs its OWN console Ctrl+C handler that calls abort(). That's
# where `forrtl: error (200): program aborting due to control-C event` and the
# KERNELBASE/ntdll stack dump come from.
#
# This environment variable disables it, but ONLY if it is set before the
# Fortran runtime DLL loads. That is why shutdown.py must be imported BEFORE
# numpy / faster-whisper / torch in main.py — importing it first is the whole
# point of this block.
os.environ.setdefault("FOR_DISABLE_CONSOLE_CTRL_HANDLER", "1")
os.environ.setdefault("FOR_IGNORE_EXCEPTIONS", "1")

# ---------------------------------------------------------------- state

_SHUTDOWN = threading.Event()
_callbacks: List[Callable[[], None]] = []
_lock = threading.Lock()
_installed = False
_hard_exit_seconds = 4.0

# Windows requires us to keep a strong reference to the handler object,
# otherwise it gets garbage collected and the process dies with a nasty
# access violation on the next Ctrl+C.
_win_handler_ref = None


# ---------------------------------------------------------------- public API

def is_shutting_down() -> bool:
    """True once shutdown has been requested. Poll this in every loop."""
    return _SHUTDOWN.is_set()


def wait(timeout: float | None = None) -> bool:
    """Sleep that wakes instantly on shutdown. Use instead of time.sleep()."""
    return _SHUTDOWN.wait(timeout)


def on_shutdown(fn: Callable[[], None]) -> Callable[[], None]:
    """Register a cleanup callback. Runs on a background thread, keep it fast."""
    with _lock:
        _callbacks.append(fn)
    return fn


def request_shutdown(reason: str = "requested") -> None:
    """Begin shutdown. Safe to call from any thread, idempotent."""
    with _lock:
        if _SHUTDOWN.is_set():
            return
        _SHUTDOWN.set()
        callbacks = list(_callbacks)

    print(f"\n[shutdown] {reason} — stopping Raziel...", flush=True)

    # Watchdog: if anything is stuck in native code, kill the process.
    threading.Thread(target=_force_exit_watchdog, daemon=True).start()

    for fn in reversed(callbacks):  # unwind in reverse registration order
        try:
            fn()
        except Exception as exc:  # never let one bad callback block the rest
            print(f"[shutdown] callback {getattr(fn, '__name__', fn)} failed: {exc}",
                  flush=True)


def install(hard_exit_seconds: float = 4.0) -> None:
    """
    Install signal + console handlers.

    WHERE TO CALL THIS: after your heavy imports (numpy, faster-whisper,
    pywebview) but before the main loop starts. Windows invokes console
    handlers in REVERSE registration order, so registering ours last means
    ours runs first — and because it returns True, the handlers installed by
    MKL and pywebview never run at all. That's what keeps the console clean.

    Note that `import shutdown` itself must still come FIRST in main.py, ahead
    of numpy, so the FOR_DISABLE_CONSOLE_CTRL_HANDLER block above takes effect.
    """
    global _installed, _hard_exit_seconds, _win_handler_ref
    if _installed:
        return
    _installed = True
    _hard_exit_seconds = hard_exit_seconds

    # Standard POSIX-style handler. Works when the main thread is actually
    # running Python (e.g. before webview.start(), or if you run headless).
    def _sig(signum, _frame):
        request_shutdown(f"signal {signum}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _sig)
        except (ValueError, OSError, AttributeError):
            pass  # not on main thread / unsupported platform

    if sys.platform == "win32":
        _win_handler_ref = _install_windows_console_handler()

    atexit.register(lambda: _SHUTDOWN.set())


# ---------------------------------------------------------------- internals

def _force_exit_watchdog() -> None:
    deadline = time.monotonic() + _hard_exit_seconds
    while time.monotonic() < deadline:
        time.sleep(0.1)
        if threading.active_count() <= 2:  # main + this watchdog
            break
    print("[shutdown] forcing exit", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def _install_windows_console_handler():
    """
    SetConsoleCtrlHandler fires on an OS-created thread, so it works even while
    the main thread is blocked inside pywebview's native message pump.
    """
    CTRL_C_EVENT = 0
    CTRL_BREAK_EVENT = 1
    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6

    HANDLER = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    seen = {"count": 0}

    def _handler(ctrl_type: int) -> bool:
        if ctrl_type in (CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT,
                         CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            seen["count"] += 1
            if seen["count"] >= 2:
                # Impatient user, or something is genuinely wedged.
                print("\n[shutdown] second interrupt — exiting now", flush=True)
                os._exit(1)
            names = {CTRL_C_EVENT: "Ctrl+C", CTRL_BREAK_EVENT: "Ctrl+Break",
                     CTRL_CLOSE_EVENT: "window closed"}
            request_shutdown(names.get(ctrl_type, "console event"))

            # CTRL_CLOSE/LOGOFF/SHUTDOWN: Windows gives us ~5s then kills us.
            if ctrl_type != CTRL_C_EVENT and ctrl_type != CTRL_BREAK_EVENT:
                time.sleep(min(_hard_exit_seconds, 4.5))
            return True  # handled; don't run the default terminate handler
        return False

    cb = HANDLER(_handler)
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(cb, True):
        print("[shutdown] warning: SetConsoleCtrlHandler failed", flush=True)
    return cb


# ---------------------------------------------------------------- helpers
# Small utilities for making your existing blocking loops interruptible.

def interruptible_stream_read(stream, frames: int, chunk_frames: int = 512):
    """
    Read `frames` from a sounddevice InputStream in small chunks, bailing out
    the moment shutdown is requested. Replaces a single large blocking read.
    Returns None if interrupted.
    """
    import numpy as np
    out = []
    remaining = frames
    while remaining > 0:
        if is_shutting_down():
            return None
        n = min(chunk_frames, remaining)
        data, _overflow = stream.read(n)
        out.append(data)
        remaining -= n
    return np.concatenate(out, axis=0)


class ShutdownAware:
    """
    Context manager that guarantees a resource is released on shutdown.

        with ShutdownAware(stream.close):
            ...
    """

    def __init__(self, closer: Callable[[], None]):
        self._closer = closer
        self._token = on_shutdown(closer)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        with _lock:
            if self._token in _callbacks:
                _callbacks.remove(self._token)
        try:
            self._closer()
        except Exception:
            pass
        return False
