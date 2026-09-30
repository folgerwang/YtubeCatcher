# Build the YtubeCatcher Windows installer (dist\YtubeCatcher-Setup-<version>.exe).
#
# The installer carries a private copy of Python (copied from the Python that .venv was made
# from) with the app's packages installed into it, plus libmpv. It installs per user into
# %LOCALAPPDATA%\Programs\YtubeCatcher - no admin rights, and the folder stays writable, so
# yt-dlp updates and the optional BGM filter (PyTorch) can still be pip-installed later.
#
# Needs: Inno Setup 6 (winget install JRSoftware.InnoSetup), the .venv (run.bat once) and
# libmpv-2.dll next to ytubeviewer.py (run.bat downloads it).
#
#   powershell -ExecutionPolicy Bypass -File installer\build_installer.ps1 [-Version 1.2.0]
param([string]$Version = "")

$ErrorActionPreference = "Stop"
function Native { $ErrorActionPreference = "Continue"; & $args[0] $args[1..($args.Count - 1)] 2>&1 | ForEach-Object { "$_" } }
$Root = Split-Path -Parent $PSScriptRoot
$Build = Join-Path $Root "build\installer"
$Stage = Join-Path $Build "stage"
$Runtime = Join-Path $Stage "runtime"

function Step($m) { Write-Host "==> $m" -ForegroundColor Cyan }

# ---------------------------------------------------------------- tools + inputs
$Iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
          "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) { throw "Inno Setup 6 not found - install it: winget install JRSoftware.InnoSetup" }

$Cfg = Join-Path $Root ".venv\pyvenv.cfg"
if (-not (Test-Path $Cfg)) { throw ".venv not found - run run.bat once first" }
$PyHome = ((Get-Content $Cfg | Where-Object { $_ -match "^home\s*=" }) -replace "^home\s*=\s*", "").Trim()
if (-not (Test-Path (Join-Path $PyHome "pythonw.exe"))) { throw "base Python not found at $PyHome" }
if (-not (Test-Path (Join-Path $PyHome "tcl"))) { throw "$PyHome has no Tcl/Tk (tkinter) - install Python from python.org" }
$Mpv = Join-Path $Root "libmpv-2.dll"
if (-not (Test-Path $Mpv)) { throw "libmpv-2.dll missing - run run.bat once (it downloads it)" }

if (-not $Version) { $Version = (Get-Date -Format "yyyy.M.d") }
Write-Host "Python: $PyHome   version: $Version"

# ---------------------------------------------------------------- stage
Step "Staging files"
if (Test-Path $Build) { Remove-Item $Build -Recurse -Force }
New-Item -ItemType Directory -Force $Stage | Out-Null

& (Join-Path $Root ".venv\Scripts\python.exe") (Join-Path $PSScriptRoot "make_icon.py") | Out-Null
foreach ($f in "ytubeviewer.py", "ytubecatcher.py", "install_vocals.bat", "requirements.txt", "README.md", "libmpv-2.dll") {
    Copy-Item (Join-Path $Root $f) $Stage
}
Copy-Item (Join-Path $PSScriptRoot "ytubecatcher.ico") $Stage

# ---------------------------------------------------------------- private Python
Step "Copying Python runtime from $PyHome"
# robocopy exit codes 0-7 are success; skip docs, headers, tests, IDLE and anything already pip-installed there
robocopy $PyHome $Runtime /E /NFL /NDL /NJH /NJS /NP `
    /XD "$PyHome\Doc" "$PyHome\include" "$PyHome\libs" "$PyHome\Scripts" "$PyHome\Lib\site-packages" `
        "$PyHome\Lib\test" "$PyHome\Lib\idlelib" "$PyHome\Lib\turtledemo" "$PyHome\tcl\tix8.4.3" __pycache__ `
    /XF "*.pyc" "NEWS.txt" | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
New-Item -ItemType Directory -Force (Join-Path $Runtime "Lib\site-packages") | Out-Null
$Py = Join-Path $Runtime "python.exe"

Step "Installing packages into the runtime"
$env:PYTHONNOUSERSITE = "1"
$env:PYTHONDONTWRITEBYTECODE = "1"
Native $Py -m ensurepip --default-pip | Out-Null
if ($LASTEXITCODE -ne 0) { throw "ensurepip failed" }
Native $Py -m pip install --disable-pip-version-check --no-warn-script-location -q --upgrade pip
Native $Py -m pip install --disable-pip-version-check --no-warn-script-location -q -r (Join-Path $Stage "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
# import the staged app the way the shortcut runs it (it puts libmpv-2.dll on the DLL path)
Native $Py -c "import sys; sys.path.insert(0, r'$Stage'); import ytubeviewer as v; m, e = v.import_mpv(); assert m, e; import tkinter, yt_dlp, PIL, imageio_ffmpeg; tkinter.Tcl(); print('runtime OK - libmpv, tkinter, ffmpeg; yt-dlp', yt_dlp.version.__version__)"
if ($LASTEXITCODE -ne 0) { throw "runtime self-test failed" }
Get-ChildItem $Runtime -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force

# ---------------------------------------------------------------- compile
Step "Compiling installer"
New-Item -ItemType Directory -Force (Join-Path $Root "dist") | Out-Null
Native $Iscc /Qp "/DAppVersion=$Version" "/DStageDir=$Stage" "/DOutDir=$(Join-Path $Root 'dist')" (Join-Path $PSScriptRoot "YtubeCatcher.iss")
if ($LASTEXITCODE -ne 0) { throw "ISCC failed ($LASTEXITCODE)" }
Get-ChildItem (Join-Path $Root "dist") -Filter "YtubeCatcher-Setup-*.exe" | Sort-Object LastWriteTime | Select-Object -Last 1 |
    ForEach-Object { Write-Host ("Built {0}  ({1:N0} MB)" -f $_.FullName, ($_.Length / 1MB)) -ForegroundColor Green }
