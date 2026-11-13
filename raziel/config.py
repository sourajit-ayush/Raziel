"""
config.py (in raziel/) - hands every module your settings from config.py in the project folder.

Your settings live one folder up, next to README.md: copy config.example.py to config.py there and
edit it. That file is never pushed to GitHub (it holds keys and phone numbers); this one is only the
bridge that lets the code keep writing `import config`.

The settings file is run as if it were this module, with __file__ pointing at the real file, so its
_SCRIPT_DIR is the project folder: the voices, the logs, memory.db, the Obsidian vault and both
venvs stay where they have always been.
"""
import os as _os

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
_PATH = _os.path.join(_ROOT, "config.py")

if not _os.path.isfile(_PATH):
    raise ImportError(
        "Raziel has no settings yet. Copy config.example.py to config.py in\n    " + _ROOT
        + "\nand fill in what you use (the comments in the file say what each line does).")

__file__ = _PATH
with open(_PATH, "rb") as _f:
    exec(compile(_f.read(), _PATH, "exec"), globals())
