@echo off
rem Optional: installs PyTorch (CUDA) + BS-RoFormer (audio-separator) + Demucs for the BGM filter.
rem Installed app: goes into its own Python (runtime\). From source: run run.bat once first so the
rem .venv exists. Download is ~3.5 GB; the ~900 MB voice model itself is fetched on the first
rem filtered file and kept in the models\ folder.
setlocal
cd /d "%~dp0"
if exist "runtime\python.exe" (
    set PY="runtime\python.exe" -m pip --no-warn-script-location
    set PYX="runtime\python.exe"
) else if exist ".venv\Scripts\python.exe" (
    set PY=".venv\Scripts\python.exe" -m pip
    set PYX=".venv\Scripts\python.exe"
) else (
    echo Run run.bat first.
    pause
    exit /b 1
)

where nvidia-smi >nul 2>nul
if %errorlevel%==0 (
    echo NVIDIA GPU detected - installing CUDA build of PyTorch...
    %PY% install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
) else (
    echo No NVIDIA GPU found - installing CPU build of PyTorch (separation will be slower)...
    %PY% install torch torchaudio
)
%PY% install "audio-separator[cpu]" demucs "numpy>=2"
%PYX% -c "import audio_separator, demucs; print('BGM filter engines OK: BS-RoFormer + Demucs')"
%PYX% -c "import torch;print('CUDA available:', torch.cuda.is_available())"
echo Done. Restart YtubeCatcher - the BGM filter is now enabled.
pause
