"""
Stops a Raziel that's running in the background (there's no console to press Ctrl+C in).

Run by the "Stop Raziel" Desktop shortcut made by install_autostart.bat. She shuts down the same
clean way as with Ctrl+C. All the logic is in launcher.py.
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

sys.exit(launcher.stop(sys.argv[1:]))
