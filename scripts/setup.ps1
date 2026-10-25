# One-time setup on a Windows machine (this PC or the always-on box). Re-runnable.
param([switch]$Desktop)  # install the optional window/tray dependencies too
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
if (-not (Test-Path .venv\Scripts\python.exe)) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($Desktop) { .\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt }
npm install --no-fund --no-audit
if (-not (Test-Path config\hub.local.yaml)) { Copy-Item config\hub.example.yaml config\hub.local.yaml }
.\.venv\Scripts\python.exe -m unittest discover -s tests
Write-Host ""
Write-Host "Next (interactive, once per machine/account):"
Write-Host "  2. .\node_modules\.bin\lark-cli config init --new"
Write-Host "  3. .\node_modules\.bin\lark-cli auth login --domain im,calendar,task,contact"
Write-Host "  4. .\.venv\Scripts\python.exe -m infohub doctor"
Write-Host "  5. .\scripts\start.ps1   (or install-autostart.ps1)"
