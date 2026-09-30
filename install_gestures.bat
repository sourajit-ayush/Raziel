@echo off
REM Sets up hand gestures (gestures.py):
REM   1. installs MediaPipe and OpenCV into this project's venv (numpy stays at 1.26.4)
REM   2. downloads the hand-gesture model (about 8 MB) into models\
REM   3. checks that the model loads and the camera opens
REM Safe to run again. Everything is written to install_gestures.log.
setlocal
cd /d "%~dp0"
set LOG=install_gestures.log
set PY=venv\Scripts\python.exe
set MODEL=models\gesture_recognizer.task
set MODEL_URL=https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/1/gesture_recognizer.task
set FAILED=0
if not exist "%PY%" (
  echo Could not find %PY% next to this file.
  pause
  exit /b 1
)
echo Raziel - hand gestures setup > "%LOG%"
echo.

echo [1/3] Installing MediaPipe and OpenCV (a few minutes the first time)...
"%PY%" -m pip install "mediapipe==1.0.1" "opencv-contrib-python==4.10.0.84" "numpy==1.26.4" >> "%LOG%" 2>&1
"%PY%" -c "import os; os.environ['MPLBACKEND']='Agg'; import mediapipe as mp, cv2, numpy as np; mp.Image(image_format=mp.ImageFormat.SRGB, data=np.zeros((8, 8, 3), np.uint8)); print('mediapipe', mp.__version__, 'opencv', cv2.__version__, 'numpy', np.__version__)" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo       FAILED - see %LOG%
  set FAILED=1
  goto done
)
echo       OK

echo [2/3] Hand-gesture model...
if not exist models mkdir models
if not exist "%MODEL%" (
  curl.exe -L --fail --silent --show-error -o "%MODEL%.part" "%MODEL_URL%" >> "%LOG%" 2>&1
  if errorlevel 1 powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing -Uri '%MODEL_URL%' -OutFile '%MODEL%.part'" >> "%LOG%" 2>&1
  if exist "%MODEL%.part" move /y "%MODEL%.part" "%MODEL%" > nul
)
if not exist "%MODEL%" (
  echo       FAILED to download - see %LOG%
  set FAILED=1
  goto done
)
"%PY%" -c "import os, sys; sys.exit(0 if os.path.getsize(r'%MODEL%') > 3000000 else 1)"
if errorlevel 1 (
  echo       The download is incomplete - deleted. Run this again.
  del "%MODEL%"
  set FAILED=1
  goto done
)
echo       OK

echo [3/3] Checking the model and the camera...
"%PY%" -c "import gestures, sys; gestures.Recognizer(gestures.model_path()).close(); print('model loads OK')" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo       The model didn't load - see the end of %LOG%. If it keeps failing, delete %MODEL% and run this again.
  set FAILED=1
  goto done
)
"%PY%" -c "import gestures; gestures.Camera(int(getattr(gestures.config, 'GESTURES_CAMERA', 0))).close(); print('camera OK')" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo       The model is fine, but the camera didn't open. Is another app using it, or is camera access
  echo       for desktop apps off in Windows Settings, Privacy and security, Camera? Raziel keeps trying.
) else (
  echo       OK
)

:done
echo.
if %FAILED%==0 (
  echo All done. Restart Raziel: "Stop Raziel", then "Start Raziel".
  echo To see what she sees, run check_gestures.bat while Raziel is stopped.
) else (
  echo Something failed - the messages above and %LOG% say what.
)
echo.
pause
