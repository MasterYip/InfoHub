# Start the hub (poll loop + dashboard) in the background; logs go to logs\hub.log.
$root = Split-Path -Parent $PSScriptRoot
New-Item -ItemType Directory -Force -Path "$root\logs" | Out-Null
Start-Process -WindowStyle Hidden -FilePath "$root\.venv\Scripts\python.exe" `
  -ArgumentList '-X','utf8','-m','infohub','run' -WorkingDirectory $root `
  -RedirectStandardOutput "$root\logs\hub.log" -RedirectStandardError "$root\logs\hub.err.log"
Start-Sleep 3
try { Invoke-RestMethod http://127.0.0.1:8765/api/health | Out-Null; Write-Host "Info Hub running: http://127.0.0.1:8765/" }
catch { Write-Host "Not answering yet; see logs\hub.err.log" }
