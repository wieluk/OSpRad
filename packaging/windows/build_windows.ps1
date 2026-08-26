# Build Windows OSpRad.exe via PyInstaller. Must run on Windows. PyInstaller doesn't cross compile.
#
# Usage (PowerShell, from repo root):
#   pip install -r app\requirements.txt pyinstaller
#   packaging\windows\build_windows.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path "$PSScriptRoot\..\.."
$PkgDir = "$RepoRoot\packaging\windows"

Remove-Item -Recurse -Force "$PkgDir\dist", "$PkgDir\build", "$PkgDir\OSpRad.spec" -ErrorAction SilentlyContinue

pyinstaller --name OSpRad --onefile --windowed `
    --add-data "$RepoRoot\app\calibration_data.csv;." `
    --icon "$RepoRoot\packaging\assets\ospradicon.ico" `
    --paths "$RepoRoot\app" `
    --distpath "$PkgDir\dist" `
    --workpath "$PkgDir\build" `
    --specpath "$PkgDir" `
    "$RepoRoot\app\OSpRad.py"

Write-Output "Built: $PkgDir\dist\OSpRad.exe"
