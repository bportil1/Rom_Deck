@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
    echo Python launcher not found. Install Python 3.10+ and try again.
    pause
    exit /b 1
)
py -3 romdeck_gui.py
if errorlevel 1 pause
