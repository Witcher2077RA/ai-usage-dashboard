@echo off
rem Starts the local AI usage dashboard and opens it in your browser.
rem Close this window (or press Ctrl+C) to stop it.
cd /d "%~dp0"
python server.py --open
if errorlevel 1 pause
