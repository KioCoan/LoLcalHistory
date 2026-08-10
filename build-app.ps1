# Builds "dist\LoLcal History.exe"
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$py = "$PSScriptRoot\.venv\Scripts\python.exe"

Write-Host "Regenerating icon..."
& $py tools\make_icon.py

Write-Host "Building executable..."
& $py -m PyInstaller lolhist.spec --noconfirm --clean

$exe = "$PSScriptRoot\dist\LoLcal History.exe"
if (Test-Path $exe) {
    $mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
    Write-Host ""
    Write-Host "Built $exe ($mb MB)" -ForegroundColor Green
    Write-Host "Your history lives in $env:LOCALAPPDATA\LoLcal History"
} else {
    throw "Build finished but `"$exe`" is missing."
}
