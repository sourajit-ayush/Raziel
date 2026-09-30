"""
Starts Raziel in the background - no console window, nothing to type. Then just say "Raziel".

Run by the "Start Raziel" Desktop shortcut, and (with --delay 15 --quiet) by the Startup-folder
shortcut, both made by install_autostart.bat. Double-clicking it while she's already running just
tells you so. All the logic is in launcher.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import launcher
except Exception as e:  # noqa: BLE001 - pythonw has no console, so say it in a message box
    import ctypes
    ctypes.WinDLL("user32").MessageBoxW(None, f"Raziel's launcher couldn't load:\n\n{e}", "Raziel", 0x10)
    sys.exit(1)

sys.exit(launcher.start(sys.argv[1:]))
