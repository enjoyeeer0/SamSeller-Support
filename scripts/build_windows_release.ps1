$ErrorActionPreference = "Stop"

Write-Host "[1/4] Installing dependencies"
python -m pip install --upgrade pip
pip install -r requirements.txt
pip install pyinstaller

Write-Host "[2/4] Building exe"
pyinstaller --noconfirm --clean --onefile --console --name "SamSeller-Support" Main.py

Write-Host "[3/4] Preparing release bundle"
if (Test-Path "release") { Remove-Item -Recurse -Force "release" }
New-Item -ItemType Directory -Force -Path release | Out-Null

Copy-Item "dist/SamSeller-Support.exe" "release/SamSeller-Support.exe"

Write-Host "[4/4] Archiving"
Compress-Archive -Path release/* -DestinationPath "SamSeller-Support-windows-x64.zip" -Force
Write-Host "Done: SamSeller-Support-windows-x64.zip"
