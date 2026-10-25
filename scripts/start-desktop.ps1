# Launch a desktop window and system tray. The browser dashboard stays available.
param([switch]$Hidden, [string]$Config)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = "$root\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) { throw 'Run scripts/setup.ps1 first.' }
& $python -c 'import webview, pystray, PIL'
if ($LASTEXITCODE -ne 0) { throw "Install desktop support: & '$python' -m pip install -r '$root\requirements-desktop.txt'" }
if (-not $Config) { $Config = Join-Path $root 'config\hub.local.yaml' }
if (-not (Test-Path $Config)) { throw 'Configuration missing. Run scripts/setup.ps1 first, or pass -Config.' }
$arguments = '-X utf8 -m infohub'
if ($Config) { $arguments += ' --config "' + (Resolve-Path $Config).Path + '"' }
$arguments += ' desktop'
if ($Hidden) { $arguments += ' --hidden' }
Start-Process -FilePath "$root\.venv\Scripts\pythonw.exe" -ArgumentList $arguments -WorkingDirectory $root
Write-Host 'Info Hub desktop started. Click its tray icon to open the window.'
