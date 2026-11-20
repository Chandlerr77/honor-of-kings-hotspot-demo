@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONDONTWRITEBYTECODE=1"
set "REVIEW_PORT=%HOTSPOT_PORT%"
if not defined REVIEW_PORT set "REVIEW_PORT=8765"
set "BUNDLED_PY=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if exist "%BUNDLED_PY%" (
  "%BUNDLED_PY%" serve.py
) else (
  where py >nul 2>nul
  if errorlevel 1 (
    python serve.py
  ) else (
    py -3 serve.py
  )
)
pause
