@echo off
setlocal
cd /d "%~dp0.."
python scripts\bootstrap.py --setup-only
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m pip install pyinstaller
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onedir --name gp-p1-mig-ui --collect-all gp_p1_mig --hidden-import engineio.async_drivers.threading scripts\run_web.py
if errorlevel 1 exit /b 1
echo Copy external tools beside the executable into tools before distribution.
