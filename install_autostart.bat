@echo off
rem Makes Raziel start by herself every time you log in to Windows (no command prompt needed),
rem and puts "Start Raziel" / "Stop Raziel" shortcuts on your Desktop.
rem Double-click it once. To undo: uninstall_autostart.bat. The details are in launcher.py.
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo Couldn't find venv\Scripts\python.exe next to this file.
    echo Keep this file in the Voice Agent folder, the one with Raziel's venv in it.
    echo.
    pause
    exit /b 1
)
"venv\Scripts\python.exe" launcher.py install
echo.
pause
