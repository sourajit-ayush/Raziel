@echo off
REM Installs PyAudioWPatch: lets the orb avatar hear what this PC is playing, so it can dance to music.
REM Safe to run again; it only installs into this project's venv.
cd /d "%~dp0"
if not exist "venv\Scripts\python.exe" (
  echo Could not find venv\Scripts\python.exe next to this file.
  pause
  exit /b 1
)
echo Installing PyAudioWPatch into the Raziel venv...
"venv\Scripts\python.exe" -m pip install PyAudioWPatch > install_music_support.log 2>&1
set RC=%ERRORLEVEL%
"venv\Scripts\python.exe" -c "import pyaudiowpatch; print('pyaudiowpatch OK', pyaudiowpatch.__file__)" >> install_music_support.log 2>&1
if not %RC%==0 set RC=1
if errorlevel 1 set RC=1
type install_music_support.log
echo.
if %RC%==0 (
  echo Done. Now restart Raziel: use "Stop Raziel", then "Start Raziel".
) else (
  echo Install FAILED - see the messages above.
)
echo.
pause
