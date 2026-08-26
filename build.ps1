# Build Y.A.C.I.N.E into a standalone Windows .exe.
# Run from the project folder:  .\build.ps1
# Output: dist\yacine_server.exe
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[!] .venv not found. Create it first:" -ForegroundColor Yellow
    Write-Host "    python -m venv .venv"
    Write-Host "    .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

Write-Host "[1/4] Installing/updating build dependencies..." -ForegroundColor Cyan
& $py -m pip install -q --upgrade pip pyinstaller
& $py -m pip install -q -r requirements.txt

Write-Host "[2/4] Running the test suite (build stops if anything fails)..." -ForegroundColor Cyan
& $py -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Host "[!] Tests failed - not building." -ForegroundColor Red; exit 1 }

Write-Host "[3/4] Clearing previous build output..." -ForegroundColor Cyan
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host "[4/4] Building the executable..." -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean yacine.spec
if ($LASTEXITCODE -ne 0) { Write-Host "[!] PyInstaller failed." -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "==============================================================" -ForegroundColor Green
Write-Host " Build complete:  dist\yacine_server.exe"
Write-Host ""
Write-Host " Run it:          .\dist\yacine_server.exe serve"
Write-Host " Then open:       http://127.0.0.1:8765/ui"
Write-Host ""
Write-Host " The API key is created on first run and printed in the"
Write-Host " console. The UI authenticates itself automatically."
Write-Host "==============================================================" -ForegroundColor Green
