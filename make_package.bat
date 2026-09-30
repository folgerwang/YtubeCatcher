@echo off
rem Pack YtubeCatcher into a Windows installer: dist\YtubeCatcher-Setup-<version>.exe
rem   make_package.bat            -> version = today's date, e.g. 2026.9.30
rem   make_package.bat 1.2.0      -> version 1.2.0
rem Sets up what the build needs on first use: the .venv, libmpv-2.dll and Inno Setup 6 (winget).
setlocal
cd /d "%~dp0"
set "PAUSE_AT_END="
echo %cmdcmdline% | find /i "%~nx0" >nul && set "PAUSE_AT_END=1"

rem ---------------------------------------------------------------- Python environment
if not exist ".venv\Scripts\python.exe" (
    echo [make_package] Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
    if errorlevel 1 (
        echo Python 3.10+ is required. Install it from https://www.python.org/downloads/
        goto :fail
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto :fail
)

rem ---------------------------------------------------------------- mpv player library
if not exist "libmpv-2.dll" (
    echo [make_package] Downloading the mpv player library libmpv-2.dll...
    ".venv\Scripts\python.exe" ytubeviewer.py --install-libmpv
    if errorlevel 1 goto :fail
)

rem ---------------------------------------------------------------- Inno Setup 6
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=1"
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=1"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=1"
if not defined ISCC (
    echo [make_package] Installing Inno Setup 6 - the installer compiler, one time...
    winget install --id JRSoftware.InnoSetup -e --scope user --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo Could not install Inno Setup. Get it from https://jrsoftware.org/isdl.php and run this again.
        goto :fail
    )
)

rem ---------------------------------------------------------------- build
if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "installer\build_installer.ps1"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "installer\build_installer.ps1" -Version "%~1"
)
if errorlevel 1 goto :fail

echo.
echo [make_package] Done - the installer is in the dist folder.
if defined PAUSE_AT_END (
    start "" explorer "dist"
    pause
)
exit /b 0

:fail
echo.
echo [make_package] FAILED - see the messages above.
if defined PAUSE_AT_END pause
exit /b 1
