@echo off
REM Sets up the new features: reading ChatGPT / Gemini answers aloud, Voice ID and asking your own files.
REM   1. installs uiautomation and pypdf into this project's venv
REM   2. downloads the Voice ID model (26 MB) into models\ and checks it
REM   3. learns your voice from my_voice\
REM   4. asks Ollama for the small file-search model (nomic-embed-text, about 270 MB)
REM Safe to run again: finished steps are skipped. Everything is written to install_new_features.log.
setlocal
cd /d "%~dp0"
set LOG=install_new_features.log
set PY=venv\Scripts\python.exe
set MODEL=models\voice_id_resnet34.onnx
set MODEL_URL=https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/wespeaker_en_voxceleb_resnet34.onnx
set MODEL_SHA=5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94
set FAILED=0
if not exist "%PY%" (
  echo Could not find %PY% next to this file.
  pause
  exit /b 1
)
echo Raziel - new features setup > "%LOG%"
echo.

echo [1/4] Installing uiautomation and pypdf...
"%PY%" -m pip install "uiautomation>=2.0.20" "pypdf>=4.0" >> "%LOG%" 2>&1
"%PY%" -c "import uiautomation, pypdf; print('uiautomation and pypdf', pypdf.__version__, 'OK')" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo       FAILED - see %LOG%
  set FAILED=1
) else (
  echo       OK
)

echo [2/4] Voice ID model...
if not exist models mkdir models
if exist "%MODEL%" (
  certutil -hashfile "%MODEL%" SHA256 | findstr /i /c:"%MODEL_SHA%" > nul
  if errorlevel 1 del "%MODEL%"
)
if not exist "%MODEL%" (
  echo       downloading 26 MB...
  curl.exe -L --fail --silent --show-error -o "%MODEL%.part" "%MODEL_URL%" >> "%LOG%" 2>&1
  if errorlevel 1 powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing -Uri '%MODEL_URL%' -OutFile '%MODEL%.part'" >> "%LOG%" 2>&1
  if exist "%MODEL%.part" move /y "%MODEL%.part" "%MODEL%" > nul
)
if not exist "%MODEL%" (
  echo       FAILED to download - see %LOG%
  set FAILED=1
  goto voice_done
)
certutil -hashfile "%MODEL%" SHA256 | findstr /i /c:"%MODEL_SHA%" > nul
if errorlevel 1 (
  echo       FAILED - the downloaded file is damaged; run this again.
  echo Voice ID model checksum mismatch >> "%LOG%"
  del "%MODEL%"
  set FAILED=1
  goto voice_done
)
echo       OK

echo [3/4] Learning your voice from my_voice\ ...
"%PY%" voice_id.py >> "%LOG%" 2>&1
if errorlevel 1 (
  echo       FAILED - see %LOG%
  set FAILED=1
) else (
  echo       OK
)
:voice_done

echo [4/4] File search model for Ollama (nomic-embed-text)...
where ollama > nul 2>&1
if errorlevel 1 (
  echo       Ollama isn't installed or not on PATH - skipped. File search will work by words only.
  echo ollama not found >> "%LOG%"
  set FAILED=1
  goto ollama_done
)
ollama pull nomic-embed-text >> "%LOG%" 2>&1
if errorlevel 1 (
  echo       FAILED - is Ollama running? Start it, then run this file again. See %LOG%
  set FAILED=1
) else (
  echo       OK
)
:ollama_done

echo.
if %FAILED%==0 (
  echo All done. Now restart Raziel: use "Stop Raziel", then "Start Raziel".
) else (
  echo Some steps failed - the messages above and %LOG% say which. The rest still works.
)
echo.
pause
