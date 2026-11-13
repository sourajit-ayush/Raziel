"""
Opens Raziel Hub (her reports, metrics and memory) in your browser, with its key in the link.

Run by the "Raziel Hub" Desktop shortcut made by install_autostart.bat. All the logic is in launcher.py.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "raziel"))

try:
    import launcher
except Exception as e:  # noqa: BLE001 - pythonw has no console, so say it in a message box
    import ctypes
    ctypes.WinDLL("user32").MessageBoxW(None, f"Raziel's launcher couldn't load:\n\n{e}", "Raziel", 0x10)
    sys.exit(1)

sys.exit(launcher.open_hub(sys.argv[1:]))
