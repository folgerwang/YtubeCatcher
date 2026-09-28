@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [YtubeCatcher] Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Python 3.9+ is required. Install it from https://www.python.org/downloads/
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
) else (
    rem keep yt-dlp current - YouTube changes often
    ".venv\Scripts\python.exe" -m pip install -q --upgrade "yt-dlp[default]"
)

if "%~1"=="" (
    start "" ".venv\Scripts\pythonw.exe" ytubecatcher.py
) else (
    ".venv\Scripts\python.exe" ytubecatcher.py %*
)
