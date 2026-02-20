@echo off
cd /d "%~dp0"
echo 啟動 gp-p1-mig 網頁介面...
set PYTHONPATH=src
python -m gp_p1_mig.web_ui
pause
