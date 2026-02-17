@echo off
REM Build portable Windows executable (no install needed for end users)
python -m pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name gp-p1-mig-ui src\gp_p1_mig\ui.py
echo Output: dist\gp-p1-mig-ui.exe
