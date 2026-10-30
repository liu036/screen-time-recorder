@echo off
echo Stopping ScreenTimeRecorder...
taskkill /F /FI "IMAGENAME eq python.exe" /FI "COMMANDLINE eq *main.py*" >nul 2>&1
taskkill /F /FI "IMAGENAME eq pythonw.exe" /FI "COMMANDLINE eq *main.py*" >nul 2>&1
echo Stopped (ignore the error if it was not running).
timeout /t 3 >nul
