@echo off
rem Undoes install_autostart.bat: Raziel stops starting by herself at login, and the
rem "Start Raziel" / "Stop Raziel" Desktop shortcuts are removed. Nothing else is touched.
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
    echo Couldn't find venv\Scripts\python.exe next to this file.
    echo Keep this file in the Voice Agent folder, the one with Raziel's venv in it.
    echo.
    pause
    exit /b 1
)
"venv\Scripts\python.exe" launcher.py uninstall
echo.
pause
