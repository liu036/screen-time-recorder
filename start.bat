@echo off
cd /d "%~dp0"

rem Find a Python that has BOTH the collector deps and tkinter.
rem tkinter is required by the todo window (separate process).
rem The managed Python used earlier has no tkinter, so the system install
rem is probed first by absolute path.
set "PY="

if exist "%ProgramFiles%\Python313\pythonw.exe" (
  "%ProgramFiles%\Python313\pythonw.exe" -c "import win32gui, win32con, psutil, PIL, flask, tkinter" >nul 2>&1 && set "PY=%ProgramFiles%\Python313\pythonw.exe"
)
if not defined PY (
  if exist "%LocalAppData%\Programs\Python\Python313\pythonw.exe" (
    "%LocalAppData%\Programs\Python\Python313\pythonw.exe" -c "import win32gui, win32con, psutil, PIL, flask, tkinter" >nul 2>&1 && set "PY=%LocalAppData%\Programs\Python\Python313\pythonw.exe"
  )
)
if not defined PY (
  for %%C in (pythonw python py) do (
    if not defined PY "%%~C" -c "import win32gui, win32con, psutil, PIL, flask, tkinter" >nul 2>&1 && set "PY=%%~C"
  )
)

if not defined PY (
  echo ERROR: Python with dependencies not found.
  echo Install them first:  python -m pip install -r requirements.txt
  pause
  exit /b 1
)

if /i "%~1"=="--check" (
  echo Python: %PY%
  exit /b 0
)

rem pythonw is a GUI-subsystem binary: it detaches immediately, no console window.
start "" "%PY%" main.py start
echo Started. A tray icon should appear near the clock.
timeout /t 3 >nul
