@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 goto no_python
python "gp-p1-mig\scripts\bootstrap.py" %*
if errorlevel 1 goto failed
exit /b 0
:no_python
echo Install Python 3.11 or newer from https://www.python.org/downloads/
echo Enable "Add python.exe to PATH", then run start.bat again.
:failed
echo Startup failed. Your existing data has not been reset.
pause
exit /b 1
