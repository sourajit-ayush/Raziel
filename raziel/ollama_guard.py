"""
ollama_guard.py - makes sure Ollama (her LLM) is running, and starts it when it isn't.

"Failed to connect to Ollama" turned whole turns into "I'm having trouble reaching my local brain"
(assistant.log, 2026-09-25 / 09-29 / 10-29): Ollama hadn't started yet after a reboot, or had been
closed. Now, at start-up and when a request can't connect, Raziel starts Ollama herself, the way it
normally runs (its tray app, which keeps its own settings) or `ollama serve` when there is no tray
app, waits for it to answer, and the request is tried once more.

Only for an Ollama on this computer (config.OLLAMA_HOST on localhost); OLLAMA_AUTOSTART = False turns
it off.
"""

import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.request
from typing import Callable, List, Optional
from urllib.parse import urlparse

import config

logger = logging.getLogger("voice_assistant")

START_WAIT = 25.0          # seconds Ollama gets to start answering
RETRY_AFTER = 60.0         # a failed start isn't tried again for this long

_lock = threading.Lock()
_last_attempt: List[Optional[float]] = [None]   # when a start was last tried


def _host() -> str:
    return str(getattr(config, "OLLAMA_HOST", "http://localhost:11434") or "http://localhost:11434").rstrip("/")


def is_local() -> bool:
    return (urlparse(_host()).hostname or "").lower() in ("localhost", "127.0.0.1", "::1")


def is_up(timeout: float = 1.5) -> bool:
    """True when Ollama answers on config.OLLAMA_HOST."""
    try:
        with urllib.request.urlopen(_host() + "/api/version", timeout=timeout) as resp:
            return resp.status == 200
    except Exception:  # noqa: BLE001 - refused, timed out, not HTTP: all "not up"
        return False


def _commands() -> List[List[str]]:
    """How to start Ollama here, best first: its tray app, then the server on its own."""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama")
    found = []
    tray = os.path.join(base, "ollama app.exe")
    if os.path.isfile(tray):
        found.append([tray])
    cli = shutil.which("ollama") or os.path.join(base, "ollama.exe")
    if cli and os.path.isfile(cli):
        found.append([cli, "serve"])
    return found


def _launch(cmd: List[str]) -> None:
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)


def ensure_running(wait: float = START_WAIT,
                   up: Callable[[], bool] = is_up,
                   launch: Callable[[List[str]], None] = _launch,
                   commands: Optional[Callable[[], List[List[str]]]] = None,
                   clock: Callable[[], float] = time.monotonic,
                   sleep: Callable[[float], None] = time.sleep) -> bool:
    """True when Ollama is running (already, or started now). Never raises."""
    try:
        if up():
            return True
        if not getattr(config, "OLLAMA_AUTOSTART", True) or not is_local():
            return False
        with _lock:
            if up():                                   # another thread started it meanwhile
                return True
            if _last_attempt[0] is not None and clock() - _last_attempt[0] < RETRY_AFTER:
                return False
            _last_attempt[0] = clock()
            cmds = (commands or _commands)()
            if not cmds:
                logger.warning("Ollama isn't running, and I couldn't find it to start it.")
                return False
            for cmd in cmds:
                logger.info("Ollama isn't running - starting it (%s).", os.path.basename(cmd[0]))
                try:
                    launch(cmd)
                except OSError as e:
                    logger.warning("Couldn't start %s: %s", cmd[0], e)
                    continue
                deadline = clock() + wait
                while clock() < deadline:
                    sleep(0.5)
                    if up():
                        logger.info("Ollama is running.")
                        _last_attempt[0] = None
                        try:
                            import deliverables
                            deliverables.publish("ollama-guard", "Ollama started",
                                                 f"Ollama wasn't running, so Raziel started it ({os.path.basename(cmd[0])}).",
                                                 being="nexus", kind="event")
                        except Exception:  # noqa: BLE001
                            pass
                        return True
            logger.warning("Started Ollama, but it didn't answer within %.0f s.", wait)
            return False
    except Exception:  # noqa: BLE001
        logger.exception("Checking / starting Ollama failed")
        return False


def is_connection_error(exc: BaseException) -> bool:
    """The request never reached Ollama (not running), as opposed to Ollama answering with an error."""
    text = str(exc).lower()
    return isinstance(exc, (ConnectionError, OSError)) or "failed to connect" in text or "10061" in text \
        or "connection refused" in text or "connecterror" in type(exc).__name__.lower()
