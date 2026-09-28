@echo off
rem Optional: installs PyTorch (CUDA) + BS-RoFormer (audio-separator) + Demucs for the BGM filter.
rem Run run.bat once first so the .venv exists. Download is ~3.5 GB; the ~900 MB voice model
rem itself is fetched on the first filtered file and kept in the models\ folder.
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run run.bat first.
    pause
    exit /b 1
)
set PY=.venv\Scripts\python.exe

where nvidia-smi >nul 2>nul
if %errorlevel%==0 (
    echo NVIDIA GPU detected - installing CUDA build of PyTorch...
    %PY% -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
) else (
    echo No NVIDIA GPU found - installing CPU build of PyTorch (separation will be slower)...
    %PY% -m pip install torch torchaudio
)
%PY% -m pip install "audio-separator[cpu]" demucs "numpy>=2"
%PY% -c "import audio_separator, demucs; print('BGM filter engines OK: BS-RoFormer + Demucs')"
%PY% -c "import torch;print('CUDA available:', torch.cuda.is_available())"
echo Done. Restart YtubeCatcher - the BGM filter is now enabled.
pause
