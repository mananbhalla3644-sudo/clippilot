<#
.SYNOPSIS
    Sets up ClipPilot on Windows: python deps, ffmpeg, tesseract, an icon, and a
    config file. Safe to re-run.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File setup_windows.ps1
    powershell -ExecutionPolicy Bypass -File setup_windows.ps1 -SkipDownloads
    powershell -ExecutionPolicy Bypass -File setup_windows.ps1 -Exe        # also build the .exe
#>
[CmdletBinding()]
param(
    [switch]$SkipDownloads,   # do not fetch ffmpeg/tesseract
    [switch]$Exe,             # also run PyInstaller and leave dist\ClipPilot.exe
    [switch]$Force            # re-download even if already present
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bin = Join-Path $root 'bin'

function Write-Step($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "   OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "   !!  $msg" -ForegroundColor Yellow }

New-Item -ItemType Directory -Force -Path $bin | Out-Null

# ------------------------------------------------------------------ python
Write-Step "Python environment"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { throw "python not found on PATH. Install Python 3.10+ from python.org and tick 'Add to PATH'." }
$ver = & python -c "import sys;print('%d.%d'%sys.version_info[:2])"
Write-Ok "python $ver at $($py.Source)"

$req = Join-Path $root 'requirements.txt'
Write-Step "Python packages"
& python -m pip install --upgrade pip | Out-Null
& python -m pip install -r $req
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
Write-Ok "packages installed"

# ------------------------------------------------------------------ ffmpeg
function Get-Ffmpeg {
    $local = Join-Path $bin 'ffmpeg.exe'
    if ((Test-Path $local) -and -not $Force) { return $local }
    if (-not $SkipDownloads) {
        Write-Step "ffmpeg"
        $url = 'https://github.com/GyanD/codexffmpeg/releases/latest/download/ffmpeg-release-essentials.zip'
        $zip = Join-Path $bin 'ffmpeg.zip'
        try {
            Write-Host "   downloading ffmpeg (about 90 MB)..."
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 900
            $tmp = Join-Path $bin '_ff'
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
            Move-Item (Join-Path $inner.FullName 'bin\ffmpeg.exe')  $bin -Force
            Move-Item (Join-Path $inner.FullName 'bin\ffprobe.exe') $bin -Force
            Remove-Item $tmp -Recurse -Force
            Remove-Item $zip -Force
            Write-Ok "ffmpeg installed to .\bin"
        } catch {
            Write-Warn2 "download failed ($_)"
            Write-Warn2 "install it manually:  winget install --id Gyan.FFmpeg -e"
        }
    }
    if (Test-Path $local) { return $local }
    $onPath = (Get-Command ffmpeg -ErrorAction SilentlyContinue)
    if ($onPath) { return $onPath.Source }
    return $null
}

# --------------------------------------------------------------- tesseract
function Get-Tesseract {
    $local = Join-Path $bin 'tesseract.exe'
    if ((Test-Path $local) -and -not $Force) { return $local }
    if (-not $SkipDownloads) {
        Write-Step "tesseract (screen text recognition)"
        $base = 'https://github.com/UB-Mannheim/tesseract/wiki'
        $ver = '5.5.0.20241111'
        $url = "https://digi.at/UB-Mannheim/tesseract/tesseract-ocr-w64-setup-$ver.exe"
        $installer = Join-Path $bin 'tesseract-setup.exe'
        $target = Join-Path $root 'tools\tesseract'
        try {
            Write-Host "   downloading tesseract installer..."
            Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing -TimeoutSec 900
            Write-Host "   running silent install to .\tools\tesseract ..."
            $p = Start-Process -FilePath $installer -ArgumentList "/S", "/D=$target" -Wait -PassThru
            Remove-Item $installer -Force -ErrorAction SilentlyContinue
            $exe = Get-ChildItem $target -Recurse -Filter 'tesseract.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($exe) {
                Copy-Item $exe.FullName $bin -Force
                Write-Ok "tesseract installed to .\bin"
            } else {
                Write-Warn2 "tesseract installed but tesseract.exe not found; set CLIPPILOT_TESSERACT"
            }
        } catch {
            Write-Warn2 "tesseract download failed ($_)"
            Write-Warn2 "install manually: winget install --id UB-Mannheim.TesseractOCR -e"
        }
    }
    if (Test-Path $local) { return $local }
    $onPath = Get-Command tesseract -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return $null
}

$ff = Get-Ffmpeg
if ($ff) { Write-Ok "ffmpeg: $ff" } else { Write-Warn2 "ffmpeg missing - video editing will not work yet" }

$te = Get-Tesseract
if ($te) { Write-Ok "tesseract: $te" } else { Write-Warn2 "tesseract missing - the agent will use the vision model only" }

# ------------------------------------------------------------------- icon
Write-Step "Application icon"
& python (Join-Path $root 'scripts\make_icon.py') | Out-Null
if (Test-Path (Join-Path $root 'assets\clippilot.ico')) { Write-Ok "assets\clippilot.ico" }

# ------------------------------------------------------------------ config
Write-Step "Config"
$cfg = Join-Path $root 'config.yaml'
if (Test-Path $cfg) {
    Write-Ok "config.yaml already exists (left alone)"
} else {
    Copy-Item (Join-Path $root 'config.example.yaml') $cfg
    Write-Ok "config.yaml created - add your API key to llm.api_key"
}

# -------------------------------------------------------------------- exe
if ($Exe) {
    Write-Step "Building ClipPilot.exe"
    & python -m pip install --upgrade pyinstaller | Out-Null
    Push-Location $root
    try {
        & python -m PyInstaller --noconfirm clippilot.spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    } finally { Pop-Location }
    $exePath = Join-Path $root 'dist\ClipPilot.exe'
    if (Test-Path $exePath) {
        Write-Ok "built $exePath ($([math]::Round((Get-Item $exePath).Length/1MB,1)) MB)"
    }
}

Write-Step "Done"
Write-Host "   1. edit config.yaml  ->  llm.api_key" -ForegroundColor White
Write-Host "   2. python -m clippilot doctor" -ForegroundColor White
Write-Host "   3. python -m clippilot.gui    (or dist\ClipPilot.exe)" -ForegroundColor White
Write-Host ""
