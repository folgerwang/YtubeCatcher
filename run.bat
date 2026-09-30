@echo off
rem YtubeCatcher: YouTube player + clip extractor (voice only / audio / video).
rem   run.bat              -> the app
rem   run.bat --classic    -> the older batch window (many URLs, playlists, local files)
rem   run.bat URL [opts]   -> command line extractor (ytubecatcher.py --help)
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

if "%~1"=="--classic" (
    start "" ".venv\Scripts\pythonw.exe" ytubecatcher.py
    exit /b 0
)
if not "%~1"=="" (
    ".venv\Scripts\python.exe" ytubecatcher.py %*
    exit /b %errorlevel%
)

".venv\Scripts\python.exe" -c "import mpv" 2>nul
if errorlevel 1 ".venv\Scripts\python.exe" -m pip install mpv
".venv\Scripts\python.exe" -c "import PIL" 2>nul
if errorlevel 1 ".venv\Scripts\python.exe" -m pip install pillow
if not exist "libmpv-2.dll" (
    echo [YtubeCatcher] Downloading the mpv player library libmpv-2.dll - one time...
    ".venv\Scripts\python.exe" ytubeviewer.py --install-libmpv
    if errorlevel 1 (
        echo Could not download libmpv. Get "mpv-dev-x86_64-*.7z" from
        echo https://github.com/shinchiro/mpv-winbuild-cmake/releases and put libmpv-2.dll here.
        pause
        exit /b 1
    )
)
where deno >nul 2>nul
if errorlevel 1 if not exist "deno.exe" (
    echo [YtubeCatcher] Downloading Deno - JavaScript runtime yt-dlp needs for YouTube, one time...
    ".venv\Scripts\python.exe" ytubeviewer.py --install-deno
)
start "" ".venv\Scripts\pythonw.exe" ytubeviewer.py
