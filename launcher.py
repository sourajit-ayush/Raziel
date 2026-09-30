"""
launcher.py - start, stop and auto-start Raziel without typing anything into a command prompt.

You don't run this file yourself. Use:
  install_autostart.bat    double-click once: Raziel then starts by herself every time you log in
                           to Windows, and "Start Raziel" / "Stop Raziel" shortcuts appear on your
                           Desktop
  uninstall_autostart.bat  undo that (removes those three shortcuts, nothing else)
  start_raziel.pyw         what the Startup and "Start Raziel" shortcuts run
  stop_raziel.pyw          what the "Stop Raziel" shortcut runs

Once she's running, just say "Raziel" - she listens in the background all the time, exactly as
she does when started from a command prompt.

How she runs in the background: venv\\Scripts\\python.exe main.py, started with CREATE_NO_WINDOW -
the ordinary console Python, with a console that simply has no window. Deliberately NOT
pythonw.exe: under pythonw there is no console at all, so every console program she starts
herself (PowerShell for the app search, and others) would pop up a window of its own, and
libraries that write progress bars to stderr would crash. The working folder is this folder, so
every relative path in config.py (assistant.log, Raziel.onnx, ...) means the same thing it always
did. Her log is still assistant.log; anything printed outside it (a crash before logging starts,
pywebview's own messages) goes to raziel_console.log, and the previous run's copy is kept as
raziel_console.prev.log.

Command line (what the .bat / .pyw files call):
    python launcher.py install | uninstall | status | start [--delay N] [--quiet] | stop
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import instance_guard  # noqa: E402 - needs HERE on sys.path when run from a shortcut

IS_WINDOWS = sys.platform == "win32"
CREATE_NO_WINDOW = 0x08000000

STARTUP_DELAY_SECONDS = 15    # at login: give Windows, the microphone and Ollama a moment to come up first
STARTUP_GRACE_SECONDS = 25    # if she exits sooner than this after starting, she failed to start
STOP_TIMEOUT_SECONDS = 15     # shutdown.py force-exits after ~4 s, so this is generous

MAIN_SCRIPT = os.path.join(HERE, "main.py")
START_SCRIPT = os.path.join(HERE, "start_raziel.pyw")
STOP_SCRIPT = os.path.join(HERE, "stop_raziel.pyw")
CONSOLE_LOG = os.path.join(HERE, "raziel_console.log")
PREV_CONSOLE_LOG = os.path.join(HERE, "raziel_console.prev.log")
VENV_SCRIPTS = os.path.join(HERE, "venv", "Scripts")

# (Windows special folder, shortcut file name, script it runs, extra arguments, tooltip)
SHORTCUTS = (
    ("Startup", "Raziel.lnk", START_SCRIPT, f"--delay {STARTUP_DELAY_SECONDS} --quiet",
     "Starts Raziel in the background when you log in"),
    ("Desktop", "Start Raziel.lnk", START_SCRIPT, "",
     "Start Raziel in the background - then just say Raziel"),
    ("Desktop", "Stop Raziel.lnk", STOP_SCRIPT, "",
     "Stop Raziel"),
)

ALREADY_RUNNING_TEXT = "Raziel is already running in the background.\n\nJust say \"Raziel\" to wake her."


# --- Talking to the user -----------------------------------------------------

def _message_box(text: str, error: bool = False) -> None:
    """The .pyw scripts have no console, so they answer with a small message box."""
    try:
        import ctypes
        icon = 0x10 if error else 0x40                      # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.WinDLL("user32").MessageBoxW(None, text, "Raziel", icon | 0x10000 | 0x40000)  # SETFOREGROUND|TOPMOST
    except Exception:                                       # noqa: BLE001 - not Windows: a console is all there is
        print(text)


notify = _message_box   # replaced in tests


# --- Small helpers -----------------------------------------------------------

def find_python(windowed: bool = False) -> Optional[str]:
    """The venv's python.exe (pythonw.exe with windowed=True), else the one next to whatever is
    running this script. None if neither exists."""
    name = "pythonw.exe" if windowed else "python.exe"
    for folder in (VENV_SCRIPTS, os.path.dirname(sys.executable)):
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def parse_args(argv):
    """--delay N and --quiet, in any order. Anything unrecognised is ignored."""
    delay, quiet = 0.0, False
    argv = list(argv or ())
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--quiet":
            quiet = True
        elif arg == "--delay" and i + 1 < len(argv):
            try:
                delay = max(0.0, float(argv[i + 1]))
            except ValueError:
                pass
            i += 1
        i += 1
    return delay, quiet


def _rotate_console_log():
    try:
        if os.path.isfile(CONSOLE_LOG):
            os.replace(CONSOLE_LOG, PREV_CONSOLE_LOG)
    except OSError:
        pass                                                 # e.g. still open somewhere: just overwrite it


# --- Start / stop --------------------------------------------------------------

def start(argv=()) -> int:
    """Starts Raziel in the background unless she's already running. Returns an exit code."""
    delay, quiet = parse_args(argv)
    if delay:
        time.sleep(delay)

    if instance_guard.is_running():
        if not quiet:
            notify(ALREADY_RUNNING_TEXT)
        return 0

    python = find_python()
    if not python:
        notify("I couldn't find Python to run Raziel.\n\nExpected it at:\n"
               + os.path.join(VENV_SCRIPTS, "python.exe"), error=True)
        return 1

    _rotate_console_log()
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", RAZIEL_BACKGROUND="1")
    try:
        with open(CONSOLE_LOG, "w", encoding="utf-8") as log:
            log.write(f"[launcher] {time.strftime('%Y-%m-%d %H:%M:%S')} starting: {python} main.py\n")
            log.flush()
            proc = subprocess.Popen(
                [python, MAIN_SCRIPT],
                cwd=HERE,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
    except OSError as e:
        notify(f"Raziel couldn't be started:\n\n{e}", error=True)
        return 1

    try:
        code = proc.wait(timeout=STARTUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return 0     # she's up; she keeps running on her own after this launcher exits

    if code == instance_guard.ALREADY_RUNNING_EXIT_CODE:   # another start won the race by a moment
        if not quiet:
            notify(ALREADY_RUNNING_TEXT)
        return 0

    notify("Raziel stopped right after starting.\n\n"
           "If you didn't stop her yourself, the reason is at the end of assistant.log or "
           "raziel_console.log in this folder:\n" + HERE, error=True)
    return 1


def stop(argv=()) -> int:
    """Asks a background Raziel to shut down cleanly and waits for her to go. Returns an exit code."""
    running = instance_guard.is_running()
    if running is None:
        notify("I couldn't check whether Raziel is running (this only works on Windows).", error=True)
        return 1
    if not running:
        notify("Raziel isn't running.")
        return 0
    if not instance_guard.request_stop():
        notify("Raziel is running, but she didn't respond to the stop request.\n\n"
               "To stop her anyway: Task Manager > Details > end python.exe.", error=True)
        return 1
    if instance_guard.wait_until_stopped(STOP_TIMEOUT_SECONDS):
        notify("Raziel has stopped.\n\nUse \"Start Raziel\" on your Desktop to start her again.")
        return 0
    notify(f"Raziel was asked to stop but is still running after {STOP_TIMEOUT_SECONDS} seconds.\n\n"
           "To stop her anyway: Task Manager > Details > end python.exe.", error=True)
    return 1


# --- Shortcuts (install / uninstall / status) ------------------------------------
# PowerShell does the Windows-specific parts (where your Startup/Desktop folders really are - the
# Desktop is often moved into OneDrive - and writing .lnk files). Every value goes in through an
# environment variable, so folder names with spaces or quotes ("Voice Agent") can't break the script.

_PS_FOLDER = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
              "[Environment]::GetFolderPath($env:RAZIEL_FOLDER)")
_PS_CREATE = ("$ws = New-Object -ComObject WScript.Shell; "
              "$lnk = $ws.CreateShortcut($env:RAZIEL_LNK); "
              "$lnk.TargetPath = $env:RAZIEL_TARGET; "
              "$lnk.Arguments = $env:RAZIEL_ARGS; "
              "$lnk.WorkingDirectory = $env:RAZIEL_DIR; "
              "$lnk.Description = $env:RAZIEL_DESC; "
              "$lnk.Save()")


def _powershell(script: str, values: dict) -> str:
    """Runs a PowerShell one-liner with `values` as environment variables. Its output, or OSError."""
    env = dict(os.environ, **values)
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=60,
            creationflags=CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(f"PowerShell couldn't run: {e}") from e
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout or f"PowerShell exit code {result.returncode}").strip())
    return (result.stdout or "").strip()


def special_folder(name: str) -> str:
    """Full path of your Startup or Desktop folder, as Windows itself reports it."""
    path = _powershell(_PS_FOLDER, {"RAZIEL_FOLDER": name})
    if not path:
        raise OSError(f"Windows didn't say where your {name} folder is")
    return path


def create_shortcut(path: str, target: str, arguments: str, working_dir: str, description: str) -> None:
    _powershell(_PS_CREATE, {"RAZIEL_LNK": path, "RAZIEL_TARGET": target, "RAZIEL_ARGS": arguments,
                             "RAZIEL_DIR": working_dir, "RAZIEL_DESC": description})


def _yes(ask, question: str) -> bool:
    try:
        answer = ask(question)
    except (EOFError, KeyboardInterrupt, OSError):
        return False
    return (answer or "").strip().lower() in ("", "y", "yes")


def install(out=print, ask=input) -> int:
    """Creates the Startup shortcut plus Start/Stop shortcuts on the Desktop."""
    pythonw = find_python(windowed=True)
    if not pythonw:
        out("Couldn't find pythonw.exe in " + VENV_SCRIPTS)
        return 1
    out("Setting Raziel up to start by herself...\n")
    for folder, name, script, extra, description in SHORTCUTS:
        try:
            path = os.path.join(special_folder(folder), name)
            arguments = f'"{script}"' + (f" {extra}" if extra else "")
            create_shortcut(path, pythonw, arguments, HERE, description)
        except OSError as e:
            out(f"  FAILED: {name} ({folder}): {e}")
            out("\nNothing else was changed. Run install_autostart.bat again, or tell me the error above.")
            return 1
        out(f"  made  {path}")

    out("\nDone. From now on Raziel starts by herself about "
        f"{STARTUP_DELAY_SECONDS} seconds after you log in to Windows -")
    out("no command prompt needed. Just say \"Raziel\" to wake her.\n")
    out("  Start Raziel (Desktop)   starts her now, if she isn't running")
    out("  Stop Raziel  (Desktop)   stops her until the next login (or until you start her again)")
    out("  uninstall_autostart.bat  undoes all of this\n")

    if instance_guard.is_running():
        out("She's already running right now, so nothing else to do.")
    elif _yes(ask, "Start Raziel now? [Y/n] "):
        subprocess.Popen([pythonw, START_SCRIPT], cwd=HERE)
        out("Starting her in the background - she'll say \"Systems online\" in a few seconds.")
    return 0


def uninstall(out=print, ask=input) -> int:
    """Removes the three shortcuts (and offers to stop her if she's running)."""
    failed = False
    for folder, name, *_ in SHORTCUTS:
        try:
            path = os.path.join(special_folder(folder), name)
        except OSError as e:
            out(f"  couldn't find your {folder} folder: {e}")
            failed = True
            continue
        if os.path.isfile(path):
            try:
                os.remove(path)
                out(f"  removed  {path}")
            except OSError as e:
                out(f"  FAILED to remove {path}: {e}")
                failed = True
        else:
            out(f"  (not there)  {path}")

    out("\nRaziel will no longer start by herself at login. "
        "You can still start her the old way: python main.py")
    if instance_guard.is_running() and _yes(ask, "\nShe's running right now - stop her? [Y/n] "):
        if instance_guard.request_stop() and instance_guard.wait_until_stopped(STOP_TIMEOUT_SECONDS):
            out("Stopped.")
        else:
            out("She didn't stop - end python.exe in Task Manager > Details if you need to.")
            failed = True
    return 1 if failed else 0


def status(out=print) -> int:
    for folder, name, *_ in SHORTCUTS:
        try:
            path = os.path.join(special_folder(folder), name)
            out(f"  {'yes' if os.path.isfile(path) else 'no ':3}  {path}")
        except OSError as e:
            out(f"  ?    {name} ({folder}): {e}")
    running = instance_guard.is_running()
    out("\nRunning right now: " + {True: "yes", False: "no", None: "can't tell"}[running])
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = argv[0].lower() if argv else ""
    rest = argv[1:]
    if command == "start":
        return start(rest)
    if command == "stop":
        return stop(rest)
    if command == "install":
        return install()
    if command == "uninstall":
        return uninstall()
    if command == "status":
        return status()
    print("usage: python launcher.py install | uninstall | status | start [--delay N] [--quiet] | stop")
    return 2


if __name__ == "__main__":
    sys.exit(main())
