@echo off
cd /d "%~dp0"

rem Find a Python that already has the runtime dependencies.
set "PY="
set "CANDIDATES=pythonw;python;py"
for %%C in (%CANDIDATES%) do (
  if not defined PY (
    "%%~C" -c "import win32gui, win32con, psutil, PIL, flask" >nul 2>&1 && set "PY=%%~C"
  )
)
if not defined PY (
  echo ERROR: Python with dependencies not found.
  echo Install them first:  python -m pip install -r requirements.txt
  pause
  exit /b 1
)

start "" "%PY%" main.py start
echo Started. A tray icon should appear near the clock.
timeout /t 3 >nul
