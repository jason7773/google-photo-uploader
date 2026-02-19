@echo off
REM Build portable Windows executable (no install needed for end users)
python -m pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name gp-p1-mig-ui ^
  --add-data "src\gp_p1_mig\web\templates;gp_p1_mig/web/templates" ^
  --add-data "src\gp_p1_mig\web\static;gp_p1_mig/web/static" ^
  src\gp_p1_mig\web_ui.py
echo Output: dist\gp-p1-mig-ui.exe
