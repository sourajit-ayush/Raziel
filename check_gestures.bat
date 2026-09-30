@echo off
REM Shows the camera with the hands and gestures Raziel sees (nothing is done). Esc closes it.
REM Stop Raziel first (or say "stop watching"), because only one program can use the camera.
cd /d "%~dp0"
venv\Scripts\python.exe gestures.py --check
pause
