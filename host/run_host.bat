@echo off
rem Double-click to start the e-paper host. Extra args are passed through, e.g. run_host.bat --port COM5
cd /d "%~dp0"
python epaper_host.py %*
if errorlevel 1 pause
